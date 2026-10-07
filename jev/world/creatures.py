"""What kind of creature a unit is, by the name and level the strip paints (V395).

A hold that takes only some kinds of creature - Hibernate beasts and dragonkin, Polymorph
beasts, humanoids and critters, Shackle Undead the undead, Scare Beast beasts - is pressed only
on a unit whose name and level are only ever such a creature among the world snapshot's
creature templates that spawn: the strip paints a unit's name and level and not its type, and
two names in 65,536 share a code - 542 of the 10,858 spawned names' codes are shared with a
creature of another type, the Defias Bandit's with a mechanical's. A unit no template matches
is not held so: a press the server refuses costs a look, and a cast one the whole cast. Read
once a process; nothing without the snapshot.
"""

from __future__ import annotations

import contextlib
import sqlite3
from functools import cache
from pathlib import Path

WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"
# How far a unit's level may lie outside its template's own levels and still be one: a level
# the strip reads at the edge of a range.
LEVEL_SLACK = 1


@cache
def _kinds(path: Path = WORLD_DB) -> dict[int, tuple[tuple[int, int, int], ...]]:
    """Every spawned creature name's code (`name_id`) and, for each template of it, its
    levels and its creature type, 1 a beast's: (low, high, type)."""
    from jev.perceive.radio_frame import name_id
    from jev.play.world_knowledge import readonly_uri

    if not Path(path).is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(readonly_uri(Path(path)), uri=True,
                                                timeout=1)) as db:
            # Those that stand somewhere: a code shared with a template nothing spawns (a
            # test dummy, a script's) would bar the hold for nothing.
            spawned = {row[0] for row in db.execute("select distinct id from world_creature")}
            rows = db.execute("select Entry, Name, MinLevel, MaxLevel, CreatureType "
                              "from world_creature_template").fetchall()
    except sqlite3.Error:
        return {}
    found: dict[int, set[tuple[int, int, int]]] = {}
    for entry, name, low, high, kind in rows:
        if entry in spawned and name and kind:
            found.setdefault(name_id(name), set()).add((int(low or 0), int(high or 0),
                                                        int(kind)))
    return {code: tuple(sorted(rows)) for code, rows in found.items()}


def kinds(name: int | None, level: int | None = None, *,
          path: Path = WORLD_DB) -> frozenset[int]:
    """The creature types (`CreatureType`, 1 a beast, 7 a humanoid) a unit of this name - and
    of this level, where it is known - may be."""
    if name is None:
        return frozenset()
    rows = _kinds(path).get(name, ())
    if isinstance(level, int):
        rows = tuple(r for r in rows if r[0] - LEVEL_SLACK <= level <= r[1] + LEVEL_SLACK)
    return frozenset(kind for _, _, kind in rows)


def takes(mask: int, name: int | None, level: int | None = None, *,
          path: Path = WORLD_DB) -> bool:
    """Whether a spell that takes the creature types `mask` (its TargetCreatureType: a type's
    bit is `1 << (type - 1)`) takes a unit of this name and level: any unit for no mask, else
    one whose every creature of that name and level is of a type in it."""
    if not mask:
        return True
    found = kinds(name, level, path=path)
    return bool(found) and all(mask & (1 << (kind - 1)) for kind in found)
