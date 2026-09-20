#!/usr/bin/env python3
"""Measure turn rate without ever stopping, and watch for stuck while doing it.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_turn.py

Heading is only observable while moving (`DECISIONS.md` V17), so the first attempt at
this measured walk -> turn -> walk. That needs open ground *twice* and failed on the
second walk: three walks into Northshire and the character was against the Abbey wall,
so the post-turn heading could not be taken at all.

This holds W for the whole run and pulses D in the middle. Heading comes from position
samples either side of the pulse, the character never stops, and one wall only costs the
samples that touch it rather than the measurement. It also reports the stuck signature
directly — position frozen while a movement key is held — which is the next thing that
has to be turned into a threshold.
"""

from __future__ import annotations

import argparse
import itertools
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


def read_pos(cap: WindowCapture) -> tuple[float, float] | None:
    try:
        r = radio_frame.read(cap.grab().rgb)
    except Exception:
        return None
    if not r.ok or r.values.get("pos.mx") is None:
        return None
    return (r.values["pos.mx"], r.values["pos.my"])


def wrap(a: float) -> float:
    return (a + math.pi) % TAU - math.pi


def fit_heading(track: list[tuple[float, tuple[float, float]]]) -> float | None:
    """Heading over a run of samples, from first to last that actually differ."""
    if len(track) < 2:
        return None
    (_, a), (_, b) = track[0], track[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) < 3e-4:
        return None
    return math.atan2(dy, dx)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-s", type=float, default=5.0)
    ap.add_argument("--turn-s", type=float, default=1.0)
    ap.add_argument("--key", default="d", choices=("d", "a"))
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2
    hwnds = win32.find_windows("World of Warcraft")
    if not hwnds:
        print("no game window")
        return 1
    hwnd = hwnds[0]
    win32.focus(hwnd)
    time.sleep(0.4)
    if not win32.is_foreground(hwnd):
        print("could not raise the window; click it and re-run")
        return 1

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("probe"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)
    hid.release_all()

    track: list[tuple[float, tuple[float, float]]] = []
    turn_from = args.run_s / 2 - args.turn_s / 2
    turn_to = turn_from + args.turn_s
    turning = False

    try:
        print(f"holding w for {args.run_s}s, pulsing {args.key} "
              f"from {turn_from:.1f}s to {turn_to:.1f}s")
        hid.key_down("w")
        t0 = time.perf_counter()
        while True:
            now = time.perf_counter() - t0
            if now >= args.run_s:
                break
            if not turning and turn_from <= now < turn_to:
                hid.key_down(args.key)
                turning = True
            elif turning and now >= turn_to:
                hid.key_up(args.key)
                turning = False
            p = read_pos(cap)
            if p is not None:
                track.append((now, p))
            time.sleep(0.02)
    finally:
        hid.release_all()
        cap.close()

    if len(track) < 6:
        print(f"only {len(track)} position samples; nothing to measure")
        return 1

    before = [s for s in track if s[0] < turn_from - 0.1]
    after = [s for s in track if s[0] > turn_to + 0.3]
    h1, h2 = fit_heading(before), fit_heading(after)

    print(f"\nsamples: {len(track)} over {track[-1][0]:.1f}s "
          f"({len(track) / max(track[-1][0], 0.01):.1f}/s)")
    print(f"  before the pulse: {len(before)} samples, heading "
          f"{math.degrees(h1):7.1f}deg" if h1 is not None else "  before: no heading")
    print(f"  after the pulse:  {len(after)} samples, heading "
          f"{math.degrees(h2):7.1f}deg" if h2 is not None else "  after: no heading")

    if h1 is not None and h2 is not None:
        delta = wrap(h2 - h1)
        print(f"\nturned {math.degrees(delta):.1f}deg in {args.turn_s}s "
              f"-> {math.degrees(abs(delta) / args.turn_s):.1f} deg/s")

    # -- the stuck signature -------------------------------------------------
    # Position frozen while W is held. Reported as the longest run of samples whose
    # movement stayed under the radio's own quantisation, because below that the strip
    # cannot tell "not moving" from "moving slowly" and a threshold under it is noise.
    quantum = 1.0 / 16383
    frozen, longest, start_t = 0.0, 0.0, None
    for (ta, pa), (tb, pb) in itertools.pairwise(track):
        if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) < quantum * 2:
            start_t = ta if start_t is None else start_t
            frozen = tb - start_t
            longest = max(longest, frozen)
        else:
            start_t, frozen = None, 0.0
    steps = [math.hypot(b[0] - a[0], b[1] - a[1])
             for (_, a), (_, b) in itertools.pairwise(track)]
    print(f"\nper-sample movement: median {statistics.median(steps):.6f}, "
          f"min {min(steps):.6f}, max {max(steps):.6f} map units")
    print(f"longest frozen run while w held: {longest:.2f}s "
          f"(quantisation floor {quantum * 2:.6f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
