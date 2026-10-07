"""Where the game sets a character down, with the height it stands at there (V406).

The strip paints no height, and a plan needs one to start from: the planner snaps the start to
the navmesh floor nearest the height it is asked at, 200 yards up or down. Tracked along a walk
(`jev.run.client.Client._track_height`, V264-V268), the floor is known; after a jump it is not
- a hearthstone, a release, a flight, a teleport, a new session - and a first plan started at
the destination's height and 60 yards either side. From Darnassus, 1,378 yards up the tree,
every one of those to Rut'theran is under the tree, and the flight check's walk to Vesprystus
was "nopath" twice where a plan from the character's own floor walks through the portal (the
hive, 6 Oct 21:30). But where a jump lands is known, height and all, from the world database:
the graveyards a ghost appears at, the flight nodes a flight lands at and their flight masters,
the inns a hearthstone is bound at, the teleports' exits, where each race and class is created.
The body adds what the character's own memory has: its bind spot, the nodes it has flown from.

A landing point near where the character stands gives the plan its first floor; with none, the
plan tries every floor under the start (`jev.run.client.surfaces_wide`).
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import struct
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"
# A landing point this near the character, in x and y, gives a plan's start its height: a
# flight lands at its node's end within a few yards of the node, and its flight master stands
# as far as 40 from it (`jev.world.taxi.SAME_PLACE_YARDS`); the others set it down on the spot.
LANDING_YARDS = 40.0
# The nearest few, each a height of its own (`jev.run.client.FLOOR_GAP`).
LANDINGS_TRIED = 3

Point = tuple[float, float, float]


def _float(bits) -> float:
    """A DBC float column, kept by the knowledge database as its bits."""
    return struct.unpack("<f", struct.pack("<I", int(bits) & 0xFFFFFFFF))[0]


@lru_cache(maxsize=8)
def world_points(map_id: int, db_path: str = str(WORLD_DB)) -> tuple[Point, ...]:
    """Every landing point on `map_id` the world database knows: graveyards, taxi nodes,
    creation spots and teleport exits, and the flight masters and innkeepers of the vendor
    catalog; none it cannot read. Opened as the live bot opens it from Windows (`readonly_uri`)."""
    from jev.play.world_knowledge import readonly_uri

    points: list[Point] = []
    with contextlib.suppress(sqlite3.Error, OSError):
        con = sqlite3.connect(readonly_uri(Path(db_path)), uri=True, timeout=1)
        try:
            for x, y, z in con.execute(
                    "select x, y, z from world_world_safe_locs where map = ?", (map_id,)):
                points.append((float(x), float(y), float(z)))
            for node_map, x, y, z in con.execute("select c1, c2, c3, c4 from dbc_TaxiNodes"):
                if int(node_map) == map_id:
                    points.append((_float(x), _float(y), _float(z)))
            for x, y, z in con.execute(
                    "select position_x, position_y, position_z from world_playercreateinfo "
                    "where map = ?", (map_id,)):
                points.append((float(x), float(y), float(z)))
        finally:
            con.close()
    with contextlib.suppress(Exception):
        from jev.guide.path import load_teleports

        points.extend(t.exit for t in load_teleports(db_path) if t.map_id == map_id)
    with contextlib.suppress(Exception):
        from jev.world.vendor import flightmasters, innkeepers

        points.extend(m.world for m in flightmasters(map_id, None))
        points.extend(i.world for i in innkeepers(map_id, None))
    return tuple(p for p in points if all(math.isfinite(v) for v in p))


def near(points: Iterable[Point], x: float, y: float, *, yards: float = LANDING_YARDS,
         gap: float = 3.0, most: int = LANDINGS_TRIED) -> list[float]:
    """The heights of the landing points within `yards` of (x, y), nearest first, each `gap`
    from the others, at most `most`."""
    ranked = sorted((math.dist((p[0], p[1]), (x, y)), p[2]) for p in points
                    if len(p) >= 3 and p[2] is not None)
    heights: list[float] = []
    for distance, z in ranked:
        if distance > yards or len(heights) >= most:
            break
        if all(abs(z - h) > gap for h in heights):
            heights.append(float(z))
    return heights
