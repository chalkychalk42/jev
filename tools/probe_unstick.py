#!/usr/bin/env python3
"""Which way can this character actually move?

    /mnt/c/forever-win/Scripts/python.exe tools/probe_unstick.py

A stuck recovery that only tries back-and-right is a recovery that fails in a corner. This
tries each direction in turn and reports how far each one got, which is both the diagnosis
and the measurement the real `STUCK_RECOVER` should be built from.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.capture import Backend, WindowCapture  # noqa: E402
from jev.clients.hid import Hid, Humaniser  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, distance_yards  # noqa: E402
from jev.perceive import radio_frame  # noqa: E402


def main() -> int:
    if not win32.available():
        print("run this with Windows Python")
        return 2
    hwnds = win32.find_windows("World of Warcraft")
    if not hwnds:
        return 1
    hwnd = hwnds[0]
    win32.focus(hwnd)
    time.sleep(0.4)

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("unstick"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)

    def read():
        try:
            r = radio_frame.read(cap.grab().rgb)
        except Exception:
            return None
        if not r.ok or r.values.get("pos.mx") is None:
            return None
        return (r.values["pos.mx"], r.values["pos.my"]), r.values

    first = read()
    if first is None:
        print("cannot read the strip")
        return 1
    pos, values = first
    bounds = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))[values["pos.zone_id"]]
    print(f"at {pos}  zone {bounds.area_id}  "
          f"hp {values['vitals.hp']:.2f}  dead={values['vitals.dead']} "
          f"ghost={values['vitals.ghost']} swimming={values['flags.swimming']} "
          f"falling={values['flags.falling']} combat={values['vitals.combat']}")

    try:
        for label, key, secs in (("forward", "w", 1.0), ("back", "s", 1.0),
                                 ("strafe-left", "a", 1.0), ("strafe-right", "d", 1.0),
                                 ("jump+forward", "w", 1.0)):
            before = read()
            if before is None:
                print(f"  {label:14} lost the strip")
                continue
            if label.startswith("jump"):
                hid.tap("space")
                time.sleep(0.15)
            # a/d are strafe only with no modifier in the default binding; if the client
            # is set to turn instead, this still shows as "did not move sideways".
            hid.hold(key, secs)
            time.sleep(0.4)
            after = read()
            if after is None:
                print(f"  {label:14} lost the strip")
                continue
            moved = distance_yards(before[0], after[0], bounds)
            print(f"  {label:14} moved {moved:6.2f} yards")
    finally:
        hid.release_all()
        cap.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
