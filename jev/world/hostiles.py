"""Where hostile creatures stand, for a character of a side and a level (V247).

Generated from the world database (`tools/gen_hostile_spawns.py`): every spawn point on the
world maps whose unit attacks that side's players on sight. A meal and a get-up at the body
keep clear of the ones worth experience at the character's level: the mage's 34 deaths in
sessions 195-219 all began within 18 yards of such a spawn, and 15 of its 22 get-ups at the
body ended in another death, a median of 39 s later. The step's own spawns (`hunt_spawns`)
named none of them on a travel or quest step.
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path

from jev.world.combat import grey_level

HOSTILES_PATH = Path(__file__).resolve().parents[2] / "content/tbc/hostile-spawns.json"
SIDES = {"alliance": 1, "horde": 2}
CELL_YARDS = 60.0
# A unit that strays further than this from its spawn point carries its reach with it: the
# yards beyond are added to any clearance kept from the point (V255). A Young Forest Bear
# wanders 30 yards, and one attacked the resting level 9 mage 20 yards from a spawn point
# (session 224); most of the low levels' units wander 10 or less.
ORDINARY_WANDER_YARDS = 10.0
MAX_WANDER_YARDS = 30.0
# A unit notices a character from farther the higher it stands above it: about 18 yards at the
# same level and a yard more for each level above, 25 at most (CMaNGOS `Unit::GetAttackDistance`).
# Those yards are carried as a wander's are (V300). Raven Hill's graveyard in Duskwood lies 29
# yards from level 23-25 spawns, outside the ordinary reach and inside a level 24's of a level 7
# (about 35): a level 7 human got up by its Spirit Healer 53 times in two hours and died 66
# (the hive, 28 Sep).
MAX_LEVEL_YARDS = 25


@cache
def _index() -> dict[int, dict[tuple[int, int], list[tuple]]]:
    try:
        raw = json.loads(HOSTILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index: dict[int, dict[tuple[int, int], list[tuple]]] = {}
    for map_id, rows in (raw.get("maps") or {}).items():
        cells = index.setdefault(int(map_id), {})
        for row in rows:
            x, y, z, low, high, sides, wander = row[:7]
            # The kinds the spawn may be (V337), `(name id, min, max, rank)`; none in an
            # index generated before them.
            kinds = tuple(tuple(k) for k in row[7]) if len(row) > 7 else ()
            key = (math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS))
            cells.setdefault(key, []).append((x, y, z, low, high, sides, wander, kinds))
    return index


def near(map_id: int, x: float, y: float, radius: float, *, side: str | None,
         level: int | None) -> list[tuple[float, float, float, float]]:
    """The spawn points within `radius` yards of (x, y) whose units attack `side` on sight
    and are worth experience at `level` (above its grey level): (x, y, z, extra) each, extra
    the yards its unit strays beyond the ordinary (`ORDINARY_WANDER_YARDS`) and the yards it
    notices a character of `level` from beyond the ordinary, a yard a level it stands above
    it (`MAX_LEVEL_YARDS`). None for an unknown side; every level's for an unknown level,
    with no yards for a level."""
    bit = SIDES.get(side or "")
    if bit is None:
        return []
    grey = grey_level(level) if isinstance(level, int) else -1
    cells = _index().get(map_id) or {}
    cx, cy = math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS)
    span = math.ceil(radius / CELL_YARDS)
    found = []
    for i in range(cx - span, cx + span + 1):
        for j in range(cy - span, cy + span + 1):
            for sx, sy, sz, _low, high, sides, wander, _kinds in cells.get((i, j), ()):
                if sides & bit and high > grey and math.dist((sx, sy), (x, y)) <= radius:
                    extra = max(0.0, min(wander, MAX_WANDER_YARDS) - ORDINARY_WANDER_YARDS)
                    if isinstance(level, int):
                        extra += min(max(0, high - level), MAX_LEVEL_YARDS)
                    found.append((sx, sy, sz, extra))
    return found


def kinds(map_id: int, x: float, y: float, radius: float, *, side: str | None,
          low: int, high: int) -> frozenset[int]:
    """The name ids of the kinds of normal rank (no elite, no rare) spawned within `radius`
    yards of (x, y) that attack `side` on sight and whose levels lie within `low`-`high`
    (V337): what a dry grind rib widens to. None for an unknown side or `low` above `high`."""
    bit = SIDES.get(side or "")
    if bit is None or low > high:
        return frozenset()
    cells = _index().get(map_id) or {}
    cx, cy = math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS)
    span = math.ceil(radius / CELL_YARDS)
    found = set()
    for i in range(cx - span, cx + span + 1):
        for j in range(cy - span, cy + span + 1):
            for sx, sy, _sz, _low, _high, sides, _wander, spawn in cells.get((i, j), ()):
                if not sides & bit or math.dist((sx, sy), (x, y)) > radius:
                    continue
                found.update(name for name, least, most, rank in spawn
                             if rank == 0 and low <= least and most <= high)
    return frozenset(found)
