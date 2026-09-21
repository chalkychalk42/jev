#!/usr/bin/env python3
"""Is `pos.facing` the character's heading, and in which direction does it grow?

    /mnt/c/forever-win/Scripts/python.exe tools/verify_facing.py

DECISIONS V17 said 2.4.3 has no facing and navigation has been closed-loop ever since.
The minimap arrow is a Model and a Model has `GetFacing`, so the addon now polyfills it -
but a painted number is worth nothing until its zero and its sign are known, and neither
can be reasoned out. Both are *measured*, against the one heading that is not in doubt:
the direction the character actually moved.

The test walks forward a few times from different headings. If the polyfill is sound,
`painted - moved` is the same constant every time. If the character is not turning between
samples, or the minimap is set to rotate, the painted value never changes and that shows
up as a spread instead of a constant.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.run.client import NotRunning, attach  # noqa: E402

WALK_S = 1.6
TURN_S = 0.55
MIN_YARDS = 2.0          # below this the heading is noise, not a direction


def _wrap(radians: float) -> float:
    """Into (-pi, pi]. Angle differences are meaningless without it."""
    return (radians + math.pi) % (2.0 * math.pi) - math.pi


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=5)
    args = ap.parse_args()

    try:
        client = attach("facing")
    except NotRunning as e:
        print(e)
        return 2
    if not client.focused():
        print("could not bring the client to the foreground")
        return 1

    v = client.read()
    if v is None:
        print("cannot read the strip")
        return 1
    bounds = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))[v["pos.zone_id"]]

    def where():
        r = client.read()
        if r is None or r.get("pos.mx") is None:
            return None, None
        return map_to_world(r["pos.mx"], r["pos.my"], bounds), r.get("pos.facing")

    print(f"{'painted':>9} {'moved':>9} {'offset':>9}   yards")
    offsets: list[float] = []
    painted_seen: list[float] = []
    for i in range(args.samples):
        before, facing = where()
        if before is None:
            print("  lost the position; stopping")
            break
        client.hid.hold("w", WALK_S)
        time.sleep(0.3)
        after, _ = where()
        if after is None:
            print("  lost the position; stopping")
            break

        dx, dy = after[0] - before[0], after[1] - before[1]
        yards = math.hypot(dx, dy)
        if yards < MIN_YARDS:
            print(f"  only moved {yards:.1f} yards; blocked? skipping this sample")
        elif facing is None:
            print(f"  {'nil':>9} {math.atan2(dy, dx):9.3f}        -   {yards:5.1f}"
                  "   <- the strip is not painting a facing at all")
        else:
            moved = math.atan2(dy, dx)
            offset = _wrap(facing - moved)
            offsets.append(offset)
            painted_seen.append(facing)
            print(f"  {facing:9.3f} {moved:9.3f} {offset:9.3f}   {yards:5.1f}")

        if i < args.samples - 1:
            client.hid.hold("d", TURN_S)       # a new heading for the next sample
            time.sleep(0.3)

    print()
    if len(offsets) < 2:
        print("  not enough samples to say anything")
        return 1

    spread = max(abs(_wrap(o - offsets[0])) for o in offsets)
    painted_spread = max(abs(_wrap(f - painted_seen[0])) for f in painted_seen)
    mean = math.atan2(sum(math.sin(o) for o in offsets) / len(offsets),
                      sum(math.cos(o) for o in offsets) / len(offsets))
    print(f"  offset    {mean:+.3f} rad ({math.degrees(mean):+.1f} deg)")
    print(f"  spread    {spread:.3f} rad ({math.degrees(spread):.1f} deg)")
    print(f"  painted   moved {math.degrees(painted_spread):.1f} deg across the samples")

    if painted_spread < math.radians(10):
        print("\n  VERDICT: the painted facing barely moved while the character turned.")
        print("  That is the rotating-minimap case: the arrow is pinned pointing up.")
        print("  Try `/console rotateMinimap 0`, reload, and run this again.")
        return 1
    if spread > math.radians(25):
        print("\n  VERDICT: offset is not constant; the painted value is not a heading.")
        return 1
    print("\n  VERDICT: painted facing tracks the direction of travel.")
    print(f"  Subtract {mean:+.3f} rad to convert painted -> world heading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
