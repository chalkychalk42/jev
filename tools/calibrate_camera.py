#!/usr/bin/env python3
"""How many pixels up from the bottom stop is level? Drag, look, decide.

    /mnt/c/forever-win/Scripts/python.exe tools/calibrate_camera.py --px 400 500 600

`Camera.level` drags into the bottom pitch clamp - an absolute reference, whatever the
camera was doing - then comes back `LEVEL_PX`. That constant is a measurement, and it is
taken against the machine's pointer settings: pin them first with `tools/env_lock.py`,
because a slider notch changes every delta by up to 1.25x.

This deliberately does not decide for itself. A horizon detector would be a second thing
to be wrong, and the question "is that level" is one glance for a person and an open
research problem for a pixel heuristic. It writes one PNG per candidate and says where.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from jev.clients.camera import GRAB_S, LEVEL_PX, SETTLE_S, TO_THE_STOP_PX
from jev.run.client import NotRunning, attach


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--px", type=int, nargs="+", default=[LEVEL_PX],
                    help="candidate pixel counts to come back from the stop")
    ap.add_argument("--out", default="/tmp", help="where to write the frames")
    args = ap.parse_args()

    try:
        client = attach("camera")
    except NotRunning as e:
        print(e)
        return 2
    if not client.focused():
        print("could not bring the client to the foreground")
        return 1

    ox, oy = client.origin
    w, h = client.size
    out = pathlib.Path(args.out)
    for px in args.px:
        client.hid.move_to(ox + w // 2, oy + h // 2)
        time.sleep(GRAB_S)
        client.hid.button(True, right=True)
        try:
            time.sleep(GRAB_S)
            client.hid.move_by(0, TO_THE_STOP_PX)
            client.hid.move_by(0, -px)
            time.sleep(GRAB_S)
        finally:
            client.hid.button(False, right=True)
        time.sleep(SETTLE_S)

        frame = np.ascontiguousarray(client.frame())
        path = out / f"camera-{px}.png"
        Image.fromarray(frame).save(path)
        print(f"  {px:4} px -> {path}")

    print("\n  Look at them. Level is the one where the horizon sits near the middle and")
    print("  a character standing in front of you has a torso, not a hat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
