"""What the quests in the log want killed or looted, and where it spawns (V363).

The server counts a kill for every quest in the log that wants it, and a quest item drops for
every quest that holds it; a hunt that fights only its own step's creature leaves the rest to
accident. In the hive's 4 Oct 20:30-23:00 quest steps earned 2,401 experience an hour against a
grind rib's 1,283, and grinding took 68% of the time; in its routes 41-71% of the kill and loot
quests have another quest's objective within 150 yards and two levels. So a hunt's quarry is the
union of every held quest's open kill and loot counters whose creatures spawn in or near its
disk (`held`), and the step's own creature as ever.

The counters are joined as the objective code joins them (`jev.guide.generate.WorldDB.
requirements`): the creature family and the item family each in their slot order, a quest that
mixes them, or has an event, with no counter identity; a loot counter's creatures are those
whose loot carries the item (`WorldDB._drops`), here the ones dropping it at a quarter of the
best chance or better, so a 2% dropper is not hunted beside an 80% one. Elites are left out, as
the objective code blocks them, and so are interact, spell, delivery and event counters, which
no fight serves. Read from the world snapshot once a quest a process; nothing without it.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jev.world.combat import grey_level

WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"
# How far beyond a hunt's disk another quest's spawns are near enough to be hunted with it: a
# station or two past its edge, three aggro reaches (`jev.run.hunt.PACK_YARDS`). The median hunt
# disk in the hive's routes is about 40 yards; the strategy's overlap was measured at 150.
NEAR_YARDS = 60.0
# The highest another quest's creature is taken at, above the character: a level, as a rib's
# creatures (`graph.RIB_LEVELS_ABOVE`, V323) and a dry rib's wider kinds (V337) are. The step's
# own creature keeps its own rule.
LEVELS_ABOVE = 1
# A loot counter's droppers: those whose chance is at least this share of the best one's.
DROP_SHARE = 0.25
# `world_quest_template.SpecialFlags`: the quest has an event or exploration to finish it.
EVENT_FLAG = 2

Point = tuple[float, float, float]


@dataclass(frozen=True)
class Kind:
    """One creature that serves a counter: its template, its name as the strip hashes it
    (`name_id`), its levels and where it spawns, `(map, x, y, z)`."""

    entry: int
    name: int
    low: int
    high: int
    points: tuple[tuple[int, float, float, float], ...]


@dataclass(frozen=True)
class Want:
    """One kill or loot counter of a quest: its leaderboard slot (`None`, not known: a quest of
    mixed families or with an event), what it needs, and the creatures that serve it."""

    quest_id: int
    kind: str
    counter_index: int | None
    need: int
    kinds: tuple[Kind, ...]


@dataclass(frozen=True)
class Held:
    """A hunt's share of the log (`held`): the name ids of the other creatures it fights, the
    spawn points it stands at for them, and the quests they serve. Empty, nothing more."""

    names: frozenset[int] = frozenset()
    points: tuple[Point, ...] = ()
    quests: tuple[int, ...] = ()
    high: int | None = None

    def __bool__(self) -> bool:
        return bool(self.names)


def _connect(path: Path):
    from jev.play.world_knowledge import readonly_uri

    return contextlib.closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=1))


@lru_cache(maxsize=1024)
def wants(quest_id: int, path: Path = WORLD_DB) -> tuple[Want, ...]:
    """The kill and loot counters of `quest_id` a fight can serve, from the world snapshot;
    none for a quest it does not hold or a snapshot it cannot read."""
    from jev.perceive.radio_frame import name_id

    if not Path(path).is_file():
        return ()
    try:
        with _connect(Path(path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("select * from world_quest_template where entry = ?",
                             (quest_id,)).fetchone()
            if row is None:
                return ()
            creatures = [i for i in range(1, 5)
                         if row[f"ReqCreatureOrGOId{i}"] or row[f"ReqSpellCast{i}"]]
            items = [i for i in range(1, 5) if row[f"ReqItemId{i}"]]
            joined = not (creatures and items) and not (row["SpecialFlags"] or 0) & EVENT_FLAG
            found: list[Want] = []
            for index, slot in enumerate(creatures):
                entry, spell = row[f"ReqCreatureOrGOId{slot}"], row[f"ReqSpellCast{slot}"]
                if spell or entry <= 0:
                    continue                 # a quest spell or a gameobject: no fight's
                kinds = _kinds(db, name_id, _killers(db, entry))
                if kinds:
                    found.append(Want(quest_id, "kill", index if joined else None,
                                      row[f"ReqCreatureOrGOCount{slot}"] or 1, kinds))
            for index, slot in enumerate(items):
                item, count = row[f"ReqItemId{slot}"], row[f"ReqItemCount{slot}"] or 1
                if item == row["SrcItemId"] and (row["SrcItemCount"] or 1) >= count:
                    continue                 # supplied at the accept: a delivery
                kinds = _kinds(db, name_id, _droppers(db, item))
                if kinds:
                    found.append(Want(quest_id, "loot", index if joined else None, count,
                                      kinds))
            return tuple(found)
    except sqlite3.Error:
        return ()


def _killers(db, entry: int) -> list[int]:
    """The templates whose kill counts as `entry`'s: itself, and any crediting it."""
    rows = db.execute("select Entry from world_creature_template where Entry = ? "
                      "or KillCredit1 = ? or KillCredit2 = ?", (entry, entry, entry)).fetchall()
    return sorted({r[0] for r in rows})


def _droppers(db, item: int) -> list[int]:
    """The templates whose own loot carries `item` at `DROP_SHARE` of the best chance."""
    rows = db.execute(
        "select t.Entry, abs(l.ChanceOrQuestChance) from world_creature_loot_template l "
        "join world_creature_template t on t.LootId = l.entry where l.item = ?",
        (item,)).fetchall()
    best = max((r[1] for r in rows), default=0)
    return sorted({r[0] for r in rows if best and r[1] >= DROP_SHARE * best})


def _kinds(db, name_id, entries: list[int]) -> tuple[Kind, ...]:
    """Each template of `entries` of normal rank, with its spawns, fixed and random."""
    kinds = []
    for entry in entries:
        template = db.execute("select Name, MinLevel, MaxLevel, Rank from "
                              "world_creature_template where Entry = ?", (entry,)).fetchone()
        if template is None or template[3] != 0 or not template[0]:
            continue                         # an elite or a rare is no solo fight
        points = db.execute(
            "select map, cast(position_x as real), cast(position_y as real), "
            "cast(position_z as real) from world_creature where id = ? union all "
            "select c.map, cast(c.position_x as real), cast(c.position_y as real), "
            "cast(c.position_z as real) from world_creature_spawn_entry e "
            "join world_creature c on c.guid = e.guid where e.entry = ?",
            (entry, entry)).fetchall()
        if points:
            kinds.append(Kind(entry, name_id(template[0]), int(template[1]), int(template[2]),
                              tuple((int(m), float(x), float(y), float(z))
                                    for m, x, y, z in points)))
    return tuple(kinds)


def open_counter(quest, want: Want) -> bool:
    """Is `want` still short in the log's `quest`: its counter read and not full; one with no
    counter identity only while every counter of the quest read is short."""
    if quest.complete is True:
        return False
    if want.counter_index is None:
        return bool(quest.objectives) and not any(o.done for o in quest.objectives)
    objective = next((o for o in quest.objectives if o.counter_index == want.counter_index),
                     None)
    return objective is not None and objective.need == want.need and not objective.done


def held(log, map_id: int | None, centre, radius_yards: float, *, level: int | None,
         grind: bool, own: int | None = None, wanted=None) -> Held:
    """What a hunt round `centre` on `map_id` fights for the quests in `log` too (V363): the
    creatures of every quest's open kill and loot counters (`open_counter`) with a spawn within
    `radius_yards` and `NEAR_YARDS` of it, at most `LEVELS_ABOVE` above the character and, for a
    grind, not all grey to it (its pulls are for experience, V344), `own` aside; and their spawn
    points there. Empty with the log, the map or the place unknown."""
    if not log or map_id is None or centre is None:
        return Held()
    wanted = wanted or wants
    reach = radius_yards + NEAR_YARDS
    grey = grey_level(level) if isinstance(level, int) else None
    names: set[int] = set()
    points: list[Point] = []
    quests: list[int] = []
    for quest in log:
        if quest.quest_id is None or quest.complete is True:
            continue
        served = False
        for want in wanted(quest.quest_id):
            if not open_counter(quest, want):
                continue
            for kind in want.kinds:
                if kind.name == own:
                    continue
                if isinstance(level, int) and (kind.low > level + LEVELS_ABOVE
                                               or (grind and kind.high <= grey)):
                    continue
                here = [(x, y, z) for m, x, y, z in kind.points
                        if m == map_id and math.dist((x, y), centre[:2]) <= reach]
                if here:
                    names.add(kind.name)
                    points.extend(here)
                    served = True
        if served:
            quests.append(quest.quest_id)
    if not names:
        return Held()
    return Held(frozenset(names), tuple(dict.fromkeys(points)), tuple(quests),
                level + LEVELS_ABOVE if isinstance(level, int) else None)
