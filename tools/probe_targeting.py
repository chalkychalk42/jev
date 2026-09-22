#!/usr/bin/env python3
"""Measure selected-target hover feedback at explicit window-relative points.

Attaches to an already running client. Each point only moves the cursor and asks the
shared Targeting probe for fresh radio feedback. Optional --select sends one left click;
optional --level uses the existing measured Camera composition. Neither is implicit.
One-second screenshots begin before focus or calibration. Results are diagnostics under
captures/, never learning episodes. Before/after event images are sequential context,
not atomic with radio reads. Exit 0 means the measurements finished; neither fresh paint
after selection nor a hover match establishes an interaction or engagement.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jev.clients.camera import Camera
from jev.persist import atomic_json, file_lock, input_lock_path
from jev.run.cli import _termination_cleanup
from jev.run.client import NotRunning, attach
from jev.run.screenshots import ScreenshotError, Screenshots

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAX_POINTS = 64


@dataclass(frozen=True)
class Point:
    label: str
    xy: tuple[int, int]


def coordinates(value: str) -> tuple[int, int]:
    try:
        x, y = (int(part) for part in value.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("coordinates must be x:y integers") from exc
    if x < 0 or y < 0:
        raise argparse.ArgumentTypeError("window coordinates must be nonnegative")
    return x, y


def named_point(value: str) -> Point:
    try:
        label, x, y = value.split(":")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("point must be label:x:y") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", label):
        raise argparse.ArgumentTypeError(
            "point label must be 1-64 letters, digits, dots, hyphens or underscores, "
            "starting with a letter or digit")
    return Point(label, coordinates(f"{x}:{y}"))


def _targeting(hid, read):
    # Import only on live execution; --check remains independent of client attachment.
    from jev.clients.targeting import Targeting
    return Targeting(hid, read)


def _json_default(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported diagnostic value: {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=named_point, nargs="+", required=True,
                        help="one or more label:x:y points relative to the client area")
    parser.add_argument("--select", type=coordinates,
                        help="optional single left-click x:y before probing")
    parser.add_argument("--level", action="store_true", help="apply measured camera calibration")
    parser.add_argument("--out", type=pathlib.Path,
                        help="new diagnostic directory; default captures/targeting/<unique-id>")
    parser.add_argument("--stop-file", type=pathlib.Path)
    parser.add_argument("--run-for", type=float, default=120.0,
                        help="whole diagnostic deadline in seconds, including focus/calibration")
    parser.add_argument("--client-id", default="targeting-diagnostic")
    parser.add_argument("--check", action="store_true", help="validate arguments without a client")
    args = parser.parse_args(argv)
    if len(args.points) > MAX_POINTS:
        parser.error(f"at most {MAX_POINTS} explicit points per diagnostic")
    if len({point.label for point in args.points}) != len(args.points):
        parser.error("point labels must be unique")
    if not math.isfinite(args.run_for) or args.run_for <= 0:
        parser.error("--run-for must be positive and finite")
    if args.check:
        print(json.dumps({"points": [asdict(point) for point in args.points],
                          "select": args.select, "level": args.level,
                          "screenshot_interval_s": 1.0, "run_for": args.run_for,
                          "live_tested": False}, indent=2))
        return 0
    if args.stop_file is not None and args.stop_file.exists():
        print(f"operator stop file observed: {args.stop_file}")
        return 130
    try:
        with _termination_cleanup(), file_lock(input_lock_path(), blocking=False):
            return _live(args)
    except BlockingIOError:
        print("another process owns this user's input; no client was attached")
        return 2
    except KeyboardInterrupt:
        print("targeting diagnostic stopped; input cleanup completed")
        return 130
    except NotRunning as exc:
        print(exc)
        return 2
    except Exception as exc:
        print(f"targeting diagnostic failed: {type(exc).__name__}: {exc}")
        return 1


def _live(args) -> int:
    deadline = time.monotonic() + args.run_for
    client = attach(args.client_id)
    screenshots = None

    def checkpoint():
        if args.stop_file is not None and args.stop_file.exists():
            raise KeyboardInterrupt
        if time.monotonic() >= deadline:
            raise TimeoutError("targeting diagnostic deadline reached")
        if screenshots is not None and screenshots.error:
            raise ScreenshotError(screenshots.error)

    def observed_read():
        checkpoint()
        values = client.read()
        checkpoint()
        return values

    try:
        client.hid.checkpoint = checkpoint
        checkpoint()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        directory = args.out or ROOT / "captures" / "targeting" / f"{stamp}-{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        print(f"targeting diagnostic: {directory}", flush=True)
        screenshots = Screenshots(client.frame, directory / "screenshots", interval_s=1.0)
        screenshots.start()
        checkpoint()

        width, height = client.size
        for point in [p.xy for p in args.points] + ([args.select] if args.select else []):
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                raise ValueError(f"point {point} is outside the client area {client.size}")
        if not client.focused(checkpoint=checkpoint):
            raise NotRunning("client is not focused")
        values = observed_read()
        if values is None:
            raise NotRunning("radio unavailable; diagnostic requires an existing live session")
        atomic_json(directory / "manifest.json", {
            "kind": "targeting_diagnostic", "t": time.time(), "client_id": args.client_id,
            "window_origin": client.origin, "window_size": client.size,
            "points": [asdict(point) for point in args.points], "select": args.select,
            "level": args.level, "run_for": args.run_for, "initial_radio": values,
            "capture_radio_atomic": False,
        })
        if args.level:
            if not Camera(client.hid, client.origin, client.size).level():
                raise NotRunning("camera calibration input refused")
            checkpoint()

        ox, oy = client.origin
        with (directory / "results.jsonl").open("x", encoding="utf-8") as stream:
            def record(row):
                stream.write(json.dumps(row, default=_json_default, allow_nan=False) + "\n")
                stream.flush()

            targeting = _targeting(client.hid, observed_read)
            if args.select is not None:
                checkpoint()
                sx, sy = args.select
                row = {"event": "select", "started_at": time.time(),
                       "window_point": args.select, "screen_point": (ox + sx, oy + sy),
                       "sent": False}
                try:
                    row["before"] = observed_read()
                    if row["before"] is None:
                        raise NotRunning("radio unavailable before selection")
                    row["sent"] = client.hid.click(ox + sx, oy + sy, right=False)
                    checkpoint()
                    if not row["sent"]:
                        raise NotRunning("selection input refused")
                    paint = targeting.wait_for_paint()
                    row["paint"] = paint
                    if paint.code != "fresh":
                        raise NotRunning(f"selection observation unavailable: {paint.detail}")
                    if paint.after.get("target.has") is not True:
                        raise NotRunning("fresh paint after selection shows no selected unit")
                except BaseException as exc:
                    record({**row, "ended_at": time.time(),
                            "error": {"type": type(exc).__name__, "detail": str(exc)}})
                    raise
                record({**row, "ended_at": time.time()})

            for index, point in enumerate(args.points):
                checkpoint()
                screen_point = ox + point.xy[0], oy + point.xy[1]
                row = {"event": "probe", "index": index, "label": point.label,
                       "window_point": point.xy, "screen_point": screen_point,
                       "started_at": time.time()}
                try:
                    row["capture_before"] = screenshots.capture_event(f"{index}-{point.label}-before")
                    if row["capture_before"]["status"] != "ok":
                        raise ScreenshotError("before-probe visual context unavailable")
                    checkpoint()
                    result = targeting.probe(screen_point)
                    row["result"] = result
                    row["capture_after"] = screenshots.capture_event(f"{index}-{point.label}-after")
                    if row["capture_after"]["status"] != "ok":
                        raise ScreenshotError("after-probe visual context unavailable")
                except BaseException as exc:
                    record({**row, "ended_at": time.time(),
                            "error": {"type": type(exc).__name__, "detail": str(exc)}})
                    raise
                record({**row, "ended_at": time.time()})
                print(f"{point.label}: {result.code} at {point.xy}", flush=True)
                checkpoint()
        return 0
    finally:
        # STOP/deadline must not prevent the final physical release. Join capture before
        # closing shared GDI handles, including interruption immediately after start().
        client.hid.checkpoint = None
        try:
            client.hid.release_all()
        finally:
            try:
                if screenshots is not None:
                    screenshots.close()
            finally:
                client.close()


if __name__ == "__main__":
    raise SystemExit(main())
