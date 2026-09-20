#!/usr/bin/env python3
"""First contact: find the game window, capture it, and try to read the strip.

Run with Windows Python, from the repo root:

    /mnt/c/forever-win/Scripts/python.exe tools/probe_window.py

Diagnostic only. It presses nothing, and it says what it found rather than asserting —
this is the step where the assumptions behind every measured constant in `fields.py` and
`radio_frame.py` meet a real client for the first time, and the useful output is a
description, not a pass or a fail.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np

from jev.clients import win32
from jev.clients.capture import Backend, CaptureError, WindowCapture
from jev.perceive import radio_frame
from jev.perceive.fields import GRID_COLS, GRID_ROWS, MARKER_L, MARKER_R


def near(pixel, target, tol: int = 40) -> bool:
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(pixel, target, strict=True))


def main() -> int:
    if not win32.available():
        print("This must run under Windows Python. On WSL, invoke:")
        print("  /mnt/c/forever-win/Scripts/python.exe tools/probe_window.py")
        return 2

    windows = win32.find_windows("World of Warcraft")
    if not windows:
        print("no window whose title contains 'World of Warcraft'")
        return 1
    print(f"windows found: {len(windows)}")
    for h in windows:
        x, y, w, hgt = win32.client_rect(h)
        print(f"  hwnd={h}  {w}x{hgt} at ({x},{y})  fg={win32.is_foreground(h)}  "
              f"{win32.window_title(h)!r}")

    hwnd = windows[0]
    for backend in (Backend.SCREEN, Backend.PRINT_WINDOW):
        print(f"\n--- {backend.value} ---")
        try:
            with WindowCapture(hwnd, backend=backend) as cap:
                frame = cap.grab()
        except (CaptureError, win32.Unavailable) as exc:
            print(f"  {exc}")
            continue

        print(f"  {frame.size[0]}x{frame.size[1]}, mean {frame.mean:.1f}")
        out = pathlib.Path(f"captures/probe-{backend.value}.npy")
        out.parent.mkdir(exist_ok=True)
        np.save(out, frame.rgb)
        print(f"  saved {out}")

        # Where are the markers, if anywhere? Reported before the decoder runs, because
        # "the strip is not on screen" and "the strip is on screen and unreadable" need
        # completely different fixes and the decoder collapses both into NOT_FOUND.
        rgb = frame.rgb
        hits_l = np.argwhere(
            np.abs(rgb.astype(int) - np.array(MARKER_L)).sum(axis=2) < 60)
        hits_r = np.argwhere(
            np.abs(rgb.astype(int) - np.array(MARKER_R)).sum(axis=2) < 60)
        print(f"  marker-left pixels:  {len(hits_l)}"
              + (f"  first at (x={hits_l[0][1]}, y={hits_l[0][0]})" if len(hits_l) else ""))
        print(f"  marker-right pixels: {len(hits_r)}"
              + (f"  first at (x={hits_r[0][1]}, y={hits_r[0][0]})" if len(hits_r) else ""))

        grid = radio_frame.locate(rgb)
        print(f"  locate(): {grid}")

        reading = radio_frame.read(rgb)
        print(f"  read(): ok={reading.ok} fault={reading.fault} detail={reading.detail}")
        if reading.values:
            keep = ("schema", "seq", "char.level", "char.class_id", "vitals.hp",
                    "vitals.hp_max", "pos.mx", "pos.my", "pos.zone_id", "pos.facing",
                    "bags.free", "bags.money_silver", "target.has")
            for k in keep:
                if k in reading.values:
                    print(f"      {k:20} {reading.values[k]}")

    print(f"\nexpected strip geometry: {GRID_COLS} cols x {GRID_ROWS} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
