#!/usr/bin/env python3
"""Pin the machine settings that silently scale what the bot sends.

    /mnt/c/forever-win/Scripts/python.exe tools/env_lock.py [--fix]

`Camera.level` drags a measured 400 pixels to set an absolute pitch. That number is only
absolute if nothing between `SendInput` and the client changes it, and two things do:

  * **Pointer speed.** Windows scales relative mouse deltas by a slider multiplier before
    any application sees them. Only the middle notch is 1:1; this machine sat one notch
    above it, so every drag the bot made was 1.1x the one that was measured.
  * **Enhance pointer precision.** Acceleration: the same delta means different distances
    depending on how fast it was sent. A calibrated constant cannot survive it.

Neither of these is visible from inside the game, and neither announces itself when it
changes. A Windows update, a new mouse driver, or somebody nudging a slider silently
decalibrates the camera and the only symptom is `not_visible` again.

The in-game side is `cameraSmoothStyle`. Smoothing means the pitch the client applies lags
the deltas it was given, so a drag that stops at the right place drifts past it.

This checks and, with `--fix`, sets. **Changing pointer speed invalidates `LEVEL_PX`** -
it is a measurement taken against a specific multiplier - so re-run `Camera.level` and
re-measure after any change here.
"""

from __future__ import annotations

import argparse
import ctypes
import sys

# The neutral notch. 1 through 20, and only 10 applies no multiplier at all, which makes
# it the one value a constant measured in pixels can mean the same thing under.
POINTER_SPEED = 10

# The slider is not linear, and guessing that it was understated this machine's error by
# half: 11 is 1.25x, not 1.1x. Below the midpoint it steps in eighths, above it in
# quarters.
MULTIPLIER = {1: 0.03125, 2: 0.0625, 3: 0.125, 4: 0.25, 5: 0.375, 6: 0.5, 7: 0.625,
              8: 0.75, 9: 0.875, 10: 1.0, 11: 1.25, 12: 1.5, 13: 1.75, 14: 2.0,
              15: 2.25, 16: 2.5, 17: 2.75, 18: 3.0, 19: 3.25, 20: 3.5}

# `SystemParametersInfo` actions.
SPI_GETMOUSE, SPI_SETMOUSE = 0x0003, 0x0004
SPI_GETMOUSESPEED, SPI_SETMOUSESPEED = 0x0070, 0x0071
SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x01, 0x02

# `cvar name -> required value`, with why. Smoothing is the one that matters for pitch;
# the others are recorded so a change shows up as a diff rather than as a mystery.
CVARS = {
    "cameraSmoothStyle": ("0", "smoothing makes the camera lag the deltas it was given, "
                               "so a calibrated drag overshoots"),
}


def _spi():
    return ctypes.windll.user32.SystemParametersInfoW


def read() -> dict:
    speed = ctypes.c_int()
    _spi()(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0)
    accel = (ctypes.c_int * 3)()
    _spi()(SPI_GETMOUSE, 0, ctypes.byref(accel), 0)
    return {"pointer_speed": speed.value,
            "threshold1": accel[0], "threshold2": accel[1], "acceleration": accel[2]}


def apply() -> None:
    flags = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    _spi()(SPI_SETMOUSESPEED, 0, ctypes.c_void_p(POINTER_SPEED), flags)
    off = (ctypes.c_int * 3)(0, 0, 0)            # thresholds and acceleration all off
    _spi()(SPI_SETMOUSE, 0, ctypes.byref(off), flags)


def problems(now: dict) -> list[str]:
    out = []
    if now["pointer_speed"] != POINTER_SPEED:
        scale = MULTIPLIER.get(now["pointer_speed"])
        by = f"{scale:.2f}x" if scale else "an unknown factor"
        out.append(f"pointer speed is {now['pointer_speed']}, not {POINTER_SPEED}: "
                   f"every relative drag is scaled by {by}")
    if now["acceleration"] or now["threshold1"] or now["threshold2"]:
        out.append("enhance pointer precision is on: the same delta travels a different "
                   "distance depending on how fast it was sent")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="set them, do not just report")
    args = ap.parse_args()

    if not sys.platform.startswith("win"):
        print("this reads Windows pointer settings; run it with the Windows interpreter")
        return 2

    now = read()
    print(f"  pointer speed   {now['pointer_speed']}  (want {POINTER_SPEED})")
    print(f"  acceleration    {now['acceleration']}  thresholds "
          f"{now['threshold1']}/{now['threshold2']}  (want 0/0/0)")

    wrong = problems(now)
    if not wrong:
        print("\n  locked: relative drags mean what they say")
        return 0

    for line in wrong:
        print(f"\n  {line}")
    if not args.fix:
        print("\n  re-run with --fix to set them")
        return 1

    apply()
    after = read()
    print(f"\n  set: pointer speed {after['pointer_speed']}, "
          f"acceleration {after['acceleration']}")
    print("  LEVEL_PX was measured against the old setting and is now wrong.")
    print("  Re-measure it before trusting Camera.level.")
    return 0 if not problems(after) else 1


if __name__ == "__main__":
    raise SystemExit(main())
