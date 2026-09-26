#!/usr/bin/env python3
"""Generate where hostile creatures stand: every spawn point on the world maps whose unit
attacks a side's players on sight (V247).

A walk, a meal and a get-up at the body are safe only out of reach of these. The mage's
34 deaths in sessions 195-219 all began within 18 yards of such a spawn, and 103 of the 110
attacks on it while walking began within 20 yards of one, where it spent 17% of its walking
time; no attack began beyond 30. The guide's own spawns (`*.spawns.json`) hold only the
creatures its steps want, so a body on a travel step lay among wolves no list named.

Hostility is the server's reaction of the unit to a player (CMaNGOS `GetFactionReaction`):
for a faction with reputation, the player's starting standing with it (hostile or at war);
otherwise its template's, a player faction among its enemies, a template that hates all but
its friends, or its hostile mask against the player's own. A side is read by one race of it
(`SIDE_RACES`). Rows are `[x, y, z, min level, max level, sides, wander]`, sides a mask
(1 hostile to the Alliance, 2 to the Horde), wander the yards a random mover strays from
its point (a patrol's route is not known: `PATROL_WANDER`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAPS = (0, 1, 530)
MAX_LEVEL = 40
# Faction template masks, as in tools/gen_vendor_catalog.py.
MASK_PLAYER, MASK_ALLIANCE, MASK_HORDE = 1, 2, 4
# The player races' factions, by side (`ChrRaces.dbc`'s faction ids).
PLAYER_FACTIONS = {1: (1, 3, 4, 115, 1629), 2: (2, 5, 6, 116, 1610)}
SIDE_MASKS = {1: MASK_ALLIANCE, 2: MASK_HORDE}
# One race stands for its side's standing with a reputation faction: human, orc.
SIDE_RACES = {1: 1, 2: 2}
FACTION_TEMPLATE_HATES_ALL = 0x10
REPUTATION_AT_WAR = 0x2
HOSTILE_BELOW = -3000              # a standing under this is hostile or hated
# Units no player can attack or select stand anywhere and fight nothing.
UNIT_FLAG_NON_ATTACKABLE = 0x2
UNIT_FLAG_NOT_SELECTABLE = 0x2000000
MOVE_RANDOM, MOVE_WAYPOINT = 1, 2
PATROL_WANDER = 15.0


def _signed(value: int) -> int:
    return value - 2 ** 32 if value >= 2 ** 31 else value


def reputation_sides(faction: tuple | None) -> int | None:
    """The sides that start hostile to a reputation faction (`dbc_Faction`: its list index,
    then race masks, class masks, base standings and flags, four each); `None` for a
    faction without reputation, whose template decides."""
    if faction is None or _signed(faction[0]) < 0:
        return None
    races, bases, flags = faction[1:5], faction[9:13], faction[13:17]
    sides = 0
    for bit, race in SIDE_RACES.items():
        for mask, base, flag in zip(races, bases, flags, strict=True):
            if mask & (1 << (race - 1)):
                if _signed(base) < HOSTILE_BELOW or flag & REPUTATION_AT_WAR:
                    sides |= bit
                break
    return sides


def hostile_sides(template: tuple | None, factions: dict | None = None) -> int:
    """The sides a unit of this faction template attacks on sight, as a mask of
    `SIDE_MASKS`' keys."""
    if template is None:
        return 0
    faction, flags, _ours, _friendly, hostile_mask, *rest = template
    standing = reputation_sides((factions or {}).get(faction))
    if standing is not None:
        return standing
    enemies, friends = set(rest[:4]) - {0}, set(rest[4:8]) - {0}
    sides = 0
    for bit, player_factions in PLAYER_FACTIONS.items():
        if enemies & set(player_factions):
            sides |= bit
        elif friends & set(player_factions):
            continue
        elif flags & FACTION_TEMPLATE_HATES_ALL or hostile_mask & (MASK_PLAYER | SIDE_MASKS[bit]):
            sides |= bit
    return sides


def generate(db: sqlite3.Connection, maps=MAPS, max_level: int = MAX_LEVEL) -> dict:
    templates = {row[0]: row[1:] for row in db.execute(
        "select id, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13 "
        "from dbc_FactionTemplate")}
    factions = {row[0]: row[1:] for row in db.execute(
        "select id, " + ", ".join(f"c{i}" for i in range(1, 18)) + " from dbc_Faction")}
    out: dict[str, list] = {str(m): [] for m in maps}
    rows = db.execute(
        "select c.map, c.position_x, c.position_y, c.position_z, c.spawndist, c.MovementType, "
        "t.MinLevel, t.MaxLevel, t.Faction, t.UnitFlags from world_creature c "
        "join world_creature_template t on t.Entry = c.id "
        f"where c.map in ({','.join('?' * len(maps))}) and t.MaxLevel <= ? "
        "order by c.map, c.guid", (*maps, max_level))
    for map_id, x, y, z, spawndist, movement, low, high, faction, flags in rows:
        if (flags or 0) & (UNIT_FLAG_NON_ATTACKABLE | UNIT_FLAG_NOT_SELECTABLE):
            continue
        sides = hostile_sides(templates.get(faction), factions)
        if not sides:
            continue
        wander = (float(spawndist or 0.0) if movement == MOVE_RANDOM
                  else PATROL_WANDER if movement == MOVE_WAYPOINT else 0.0)
        out[str(map_id)].append([round(float(x), 1), round(float(y), 1), round(float(z), 1),
                                 int(low), int(high), sides, round(wander, 1)])
    return {"format": 1, "max_level": max_level, "maps": out}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path,
                        default=ROOT / "data/knowledge/tbc-243.sqlite")
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "content/tbc/hostile-spawns.json")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
        index = generate(db)
    args.out.write_text(json.dumps(index, separators=(",", ":")) + "\n")
    print(", ".join(f"map {m}: {len(rows)} hostile spawns" for m, rows in index["maps"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
