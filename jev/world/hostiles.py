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


@cache
def _index() -> dict[int, dict[tuple[int, int], list[tuple]]]:
    try:
        raw = json.loads(HOSTILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index: dict[int, dict[tuple[int, int], list[tuple]]] = {}
    for map_id, rows in (raw.get("maps") or {}).items():
        cells = index.setdefault(int(map_id), {})
        for x, y, z, low, high, sides, wander in rows:
            key = (math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS))
            cells.setdefault(key, []).append((x, y, z, low, high, sides, wander))
    return index


def near(map_id: int, x: float, y: float, radius: float, *, side: str | None,
         level: int | None) -> list[tuple[float, float, float, float]]:
    """The spawn points within `radius` yards of (x, y) whose units attack `side` on sight
    and are worth experience at `level` (above its grey level): (x, y, z, extra) each, extra
    the yards its unit strays beyond the ordinary (`ORDINARY_WANDER_YARDS`). None for an
    unknown side; every level's for an unknown level."""
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
            for sx, sy, sz, _low, high, sides, wander in cells.get((i, j), ()):
                if sides & bit and high > grey and math.dist((sx, sy), (x, y)) <= radius:
                    extra = max(0.0, min(wander, MAX_WANDER_YARDS) - ORDINARY_WANDER_YARDS)
                    found.append((sx, sy, sz, extra))
    return found
