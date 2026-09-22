#!/usr/bin/env python3
"""Measure how this character actually moves, on this ground.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_motion.py

Three numbers, and none of them is guessable:

  * **walk speed**, in map units per second — different in every zone, because a map unit
    is a fraction of that zone's map and zones are not the same size
  * **turn rate**, in radians per second of held A or D
  * the **noise floor** on a heading taken from a position delta, which decides how long
    a sample has to be before its answer means anything

Everything is in *map space* and stays there. `mx` runs left-to-right and `my` top-to-
bottom on the zone map, and a heading here is `atan2(dmy, dmx)` — its own convention, not
world facing, which this client cannot report at all (`DECISIONS.md` V17). Converting to
world angles would add a coordinate flip nobody needs: the target is a map position, the
reading is a map position, so the whole loop closes in map space.

Movements are short and paired — forward then back, right then left — so the character
ends roughly where it started.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jev.clients import win32
from jev.clients.capture import Backend, WindowCapture
from jev.clients.hid import Hid, Humaniser
from jev.perceive import radio_frame

TAU = 2 * math.pi


def pos(cap: WindowCapture) -> tuple[float, float] | None:
    try:
        r = radio_frame.read(cap.grab().rgb)
    except Exception:
        return None
    if not r.ok or r.values.get("pos.mx") is None:
        return None
    return (r.values["pos.mx"], r.values["pos.my"])


def heading(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    """Map-space heading from a to b, or None if they are too close to mean anything."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) < 1e-4:
        return None
    return math.atan2(dy, dx)


def wrap(a: float) -> float:
    """Signed angle difference, wrapped to (-pi, pi]."""
    return (a + math.pi) % TAU - math.pi


def settle(cap: WindowCapture, tries: int = 20) -> tuple[float, float]:
    for _ in range(tries):
        p = pos(cap)
        if p is not None:
            return p
        time.sleep(0.05)
    raise RuntimeError("could not read a position at all")


def walk(hid: Hid, cap: WindowCapture, key: str, seconds: float) -> tuple:
    """Hold a key, sampling position throughout. Returns (start, end, samples)."""
    samples: list[tuple[float, tuple[float, float]]] = []

    def tick() -> None:
        p = pos(cap)
        if p is not None:
            samples.append((time.perf_counter(), p))

    start = settle(cap)
    t0 = time.perf_counter()
    hid.hold(key, seconds, on_tick=tick)
    time.sleep(0.35)                                    # let the client stop
    end = settle(cap)
    return start, end, samples, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--walk-s", type=float, default=1.6)
    ap.add_argument("--turn-s", type=float, default=0.7)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2
    try:
        hwnd = win32.game_window()
    except win32.GameWindowError as exc:
        print(exc)
        return 1
    win32.focus(hwnd)
    time.sleep(0.4)
    if not win32.is_foreground(hwnd):
        print("could not raise the window; click it and re-run")
        return 1

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("probe"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)
    hid.release_all()

    try:
        print(f"start: {settle(cap)}")

        # -- walk speed, and the heading the character currently faces ------------
        speeds, headings = [], []
        for i in range(args.repeats):
            key = "w" if i % 2 == 0 else "s"
            start, end, _samples, elapsed = walk(hid, cap, key, args.walk_s)
            dist = math.hypot(end[0] - start[0], end[1] - start[1])
            h = heading(start, end)
            # `s` walks backwards, so its heading is the reverse of where we face.
            if h is not None and key == "s":
                h = wrap(h + math.pi)
            speeds.append(dist / elapsed)
            if h is not None:
                headings.append(h)
            print(f"  {key} {args.walk_s}s: moved {dist:.5f} map units "
                  f"({dist / elapsed:.5f}/s), heading "
                  f"{math.degrees(h):7.1f}deg" if h is not None else "  no heading")

        if not speeds:
            print("nothing moved; is the character able to walk?")
            return 1

        # -- heading noise -------------------------------------------------------
        spread = None
        if len(headings) >= 2:
            diffs = [abs(math.degrees(wrap(h - headings[0]))) for h in headings[1:]]
            spread = max(diffs)

        # -- turn rate -----------------------------------------------------------
        # Heading is only observable while moving, so a turn is measured as
        # walk -> turn -> walk and the difference between the two walk headings.
        turn_rates = []
        for key in ("d", "a"):
            s1, e1, _, _ = walk(hid, cap, "w", args.walk_s)
            h1 = heading(s1, e1)
            hid.hold(key, args.turn_s)
            time.sleep(0.25)
            s2, e2, _, _ = walk(hid, cap, "w", args.walk_s)
            h2 = heading(s2, e2)
            if h1 is None or h2 is None:
                print(f"  {key}: could not take both headings")
                continue
            delta = wrap(h2 - h1)
            rate = abs(delta) / args.turn_s
            turn_rates.append(rate)
            print(f"  {key} {args.turn_s}s: turned {math.degrees(delta):7.1f}deg "
                  f"-> {math.degrees(rate):6.1f} deg/s")

        print("\n--- measured ---")
        print(f"walk speed   {statistics.mean(speeds):.5f} map units/s "
              f"(n={len(speeds)})")
        if spread is not None:
            print(f"heading spread over {args.walk_s}s samples: {spread:.1f}deg")
        if turn_rates:
            print(f"turn rate    {math.degrees(statistics.mean(turn_rates)):.1f} deg/s "
                  f"(n={len(turn_rates)})")
        else:
            print("turn rate    not measured")
    finally:
        hid.release_all()
        cap.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
