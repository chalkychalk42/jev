#!/usr/bin/env python3
"""Get the character back on its feet after dying.

    /mnt/c/forever-win/Scripts/python.exe tools/recover.py

Releases, walks the ghost back to where it fell, and resurrects. The corpse position is
read **before** releasing, because afterwards the ghost is at the graveyard and 2.4.3 has
no way to ask where the body is.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.recover import Recover  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.run.client import NotRunning, attach, with_travel  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    ap.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    ap.add_argument("--arrive", type=float, default=6.0)
    ap.add_argument("--corpse", default=None, metavar="MX,MY",
                    help="where the body is, if the character already auto-released")
    args = ap.parse_args()

    try:
        client = attach("recover")
    except NotRunning as e:
        print(e)
        return 2
    if not client.focused():
        print("could not bring the client to the foreground")
        return 1

    v = client.read()
    if v is None:
        print("cannot read the strip")
        client.close()
        return 1
    print(f"  dead={v.get('vitals.dead')} ghost={v.get('vitals.ghost')} "
          f"hp={v.get('vitals.hp')}")

    bounds = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))[v["pos.zone_id"]]
    launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
    with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher),
                arrival_yards=args.arrive, say=print)

    # A map fraction has no height, and the planner needs one. The guide does know the
    # terrain: every quest node carries the world point its spawn was read from, so the
    # nearest one is a far better guess than zero and costs nothing.
    graph = Graph.load(str(ROOT / "content" / "tbc" / "ally_human_1_12.json"))
    placed = [n for n in graph.nodes if n.world is not None and n.map_id == bounds.map_id]

    def height_near(world_xy) -> float:
        if not placed:
            return 0.0
        nearest = min(placed, key=lambda n: (n.world[0] - world_xy[0]) ** 2
                      + (n.world[1] - world_xy[1]) ** 2)
        return nearest.world[2]

    def walk_to(map_point) -> bool:
        wx, wy = map_to_world(map_point[0], map_point[1], bounds)
        return client.approach((wx, wy, height_near((wx, wy))))

    skill = Recover(hid=client.hid, read=client.read, window_origin=client.origin,
                    window_size=client.size, walk_to=walk_to)
    corpse = None
    if args.corpse:
        mx, my = (float(n) for n in args.corpse.split(","))
        corpse = (mx, my)
    outcome = skill.run(corpse)
    print(f"  corpse at {skill.corpse}")
    print(f"  {outcome.value}" + (f" - {skill.detail}" if skill.detail else ""))
    client.close()
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
