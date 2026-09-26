"""Generate a GuideGraph from this server's own world database.

PLAN §7.6 assumes a human walks 1-20 marking nodes with hotkeys. That has the right
instinct — author against *this* server, not a retail guide, because custom servers
delete, rename and relevel quests — with the wrong mechanism. The server's world DB is
already the answer to "what is on this server", exactly and completely, and it is on disk:
6,599 quests and 109,358 creature spawns with coordinates.

So the split is (`DECISIONS.md` V5):

  * **generated here** — node positions, quest chains, objectives, services, graveyards
  * **recorded by walking** — the polylines *between* nodes, water, terrain hazards

The DB knows where everything is. It does not know how to walk there. This file does the
first half and leaves the second half clearly marked, rather than guessing at it.

Nothing here is trusted blindly: `stats()` is printed after every generation, because a
graph that quietly produced eleven nodes for an entire zone is the failure mode, and it
looks exactly like success until a character runs out of things to do.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace

from jev.guide.coords import ZoneBounds, _as_float, load_bounds, on_map, world_to_map
from jev.guide.graph import FailEdge, FailWhen, Graph, Node, ObjectiveTarget, rib_for
from jev.world.state_v1 import StepKind

# Race bitmasks are `1 << (ChrRaces.id - 1)`. RequiredRaces == 0 means every race.
RACE_BIT = {
    "human": 1, "orc": 2, "dwarf": 4, "nightelf": 8, "undead": 16,
    "tauren": 32, "gnome": 64, "troll": 128, "bloodelf": 512, "draenei": 1024,
}
ALLIANCE_MASK = RACE_BIT["human"] | RACE_BIT["dwarf"] | RACE_BIT["nightelf"] | RACE_BIT["gnome"] | RACE_BIT["draenei"]
HORDE_MASK = RACE_BIT["orc"] | RACE_BIT["undead"] | RACE_BIT["tauren"] | RACE_BIT["troll"] | RACE_BIT["bloodelf"]

# From mangos-tbc Unit.h, read rather than remembered.
NPC_GOSSIP = 0x0001
NPC_QUESTGIVER = 0x0002
NPC_TRAINER = 0x0010
NPC_TRAINER_CLASS = 0x0020
NPC_VENDOR = 0x0080
NPC_REPAIR = 0x1000
NPC_FLIGHTMASTER = 0x2000
NPC_INNKEEPER = 0x10000

# Any of these mean "this creature is furniture, not prey".
FRIENDLY_FLAGS = (
    NPC_GOSSIP | NPC_QUESTGIVER | NPC_TRAINER | NPC_TRAINER_CLASS
    | NPC_VENDOR | NPC_REPAIR | NPC_FLIGHTMASTER | NPC_INNKEEPER
)


@dataclass(frozen=True)
class Spawn:
    npc_id: int
    name: str
    map_id: int
    x: float
    y: float
    z: float
    area_id: int | None = None
    # How far the rest of the group sits from this point, in yards. Zero for a lone NPC;
    # for a camp it is how big the camp is, which is the only honest thing to search once
    # the mobs standing on the node have been killed.
    spread: float = 0.0
    kind: str = "creature"
    # Where the group's members actually spawn, nearest the centre first. A hunt stands on
    # these rather than on rings round the centre: Northshire's Young Wolves spawn 24 to
    # 170 yards from their cluster's centre, and rings at 0, 13 and 31 yards looked 38
    # times and found nothing (run 20260923T233909-8b1484).
    points: tuple[tuple[float, float, float], ...] = ()


# Spawn clustering, in yards. A cell wide enough that one camp lands in one or two
# buckets, a reach wide enough to gather a camp and not its neighbour.
CLUSTER_CELL = 60.0
CLUSTER_REACH = 150.0

# An item's droppers are those the quest's level can fight, when it has any creature
# among them: at most this many levels above the quest (one more let a level 1 quest's
# kobolds go for a chest across the zone). With none, every source counts. Every Riverpaw gnoll carries the Gnoll Paws of Patrolling
# Westfall, a level 14 quest, at 80%, and the pool's densest cluster was the Taskmasters'
# camp, level 17 and 18, with the level 11-14 gnolls' 92 spawns elsewhere: a level 13
# paladin was sent there and died twice (session 140).
DROP_LEVELS_ABOVE = 2

# What a hunt is allowed to believe about a camp's size. The floor keeps a lone spawn
# searchable; the ceiling catches a cluster query that has gone wrong.
#
# The ceiling used to be 50, on the grounds that "no camp in the game is a hundred yards
# across". Measured, that is false. The Tough Wolf Meat cluster is 27 spawns of Young Wolf
# and Timber Wolf along the Northshire border with a **median of 100 yards** - it is a
# border strip, not a camp - and clamping it to 50 left the bot searching a disk holding
# three of those 27. It reported an empty camp, correctly, twenty times.
#
# Echo Ridge's kobolds measure 49 on the same code, so both of those are real and the
# range between them is what a "camp" actually spans.
#
# The ceiling still means something, because `CLUSTER_REACH` bounds the spread at 150: a
# `hunt_yards` sitting exactly at the ceiling is worth a look at the query before it is
# worth a bigger disk.
HUNT_MIN_YARDS = 15.0
HUNT_MAX_YARDS = 120.0


@dataclass(frozen=True)
class QuestRow:
    quest_id: int
    title: str
    level: int
    min_level: int
    zone_or_sort: int
    prev_quest: int
    next_quest: int
    objectives: str
    req_counts: tuple[int, ...]
    xp_est: int
    special_flags: int = 0


@dataclass(frozen=True)
class Requirement:
    kind: str
    required_id: int | None = None
    required_count: int | None = None
    source_slot: int | None = None
    counter_index: int | None = None
    spawn: Spawn | None = None
    blocked_reason: str | None = None


# How many of each service NPC to record per zone.
#
# One was a lottery. Elwynn's box contains Northshire, and the single repairer it kept was
# Corina Steele in Goldshire - 556 yards from a character standing in the Abbey with three
# repair merchants 108 yards away. The guide's job is to list the places; deciding which
# one to walk to belongs to whatever is standing somewhere at the time, and it cannot
# decide between alternatives it was never given.
SERVICE_SPAWNS = 6


def _cluster(npc_id: int, name: str, map_id: int, rows) -> Spawn:
    """The densest group of these spawns, and how far across it is.

    Densest rather than the plain centroid, because a creature that appears in three
    zones has a centroid in none of them. Buckets on a coarse grid, takes the fullest
    bucket, then keeps everything within `CLUSTER_REACH` of it.

    The spread is the **median** distance from the centroid, not the maximum and not a
    high percentile. Measured on Kobold Vermin: 31 spawns, one contiguous population
    along Echo Ridge, median 49 yards and 90th percentile 76. The 90th is the outer
    envelope — a circle drawn to contain the stragglers — and a hunt does not want that.
    It wants the scale at which walking somewhere else finds another mob, and half the
    camp is inside the median by construction.
    """
    def weight(row) -> float:
        """How much this spawn should pull the node.

        A pool of droppers is not a pool of equals: an 80% drop and a 2% drop both put a
        creature in the list, and letting them vote alike puts the node with whichever
        happens to be commonest rather than with whatever will actually fill the bag.
        Absent means one, so a plain kill objective clusters exactly as it always did.
        """
        try:
            w = row["weight"]
        except (KeyError, IndexError, TypeError):
            return 1.0
        return 1.0 if w is None else max(0.0, float(w))

    buckets: dict[tuple[int, int], list] = {}
    for row in rows:
        key = (int(row["px"] // CLUSTER_CELL), int(row["py"] // CLUSTER_CELL))
        buckets.setdefault(key, []).append(row)
    seed = max(buckets.values(), key=lambda rs: sum(weight(r) for r in rs))
    seed_w = sum(weight(r) for r in seed) or 1.0
    sx = sum(r["px"] * weight(r) for r in seed) / seed_w
    sy = sum(r["py"] * weight(r) for r in seed) / seed_w

    near = [r for r in rows
            if math.hypot(r["px"] - sx, r["py"] - sy) <= CLUSTER_REACH] or seed
    n = sum(weight(r) for r in near) or 1.0
    x = sum(r["px"] * weight(r) for r in near) / n
    y = sum(r["py"] * weight(r) for r in near) / n
    z = sum(r["pz"] * weight(r) for r in near) / n
    reaches = sorted(math.hypot(r["px"] - x, r["py"] - y) for r in near)
    spread = reaches[len(reaches) // 2] if reaches else 0.0
    members = sorted(near, key=lambda r: math.hypot(r["px"] - x, r["py"] - y))
    points = tuple((round(float(r["px"]), 1), round(float(r["py"]), 1), round(float(r["pz"]), 1))
                   for r in members[:HUNT_SPAWNS])
    return Spawn(npc_id, name, map_id, x, y, z, spread=spread, points=points)


# A grind rib's creature counts as spread out when its spawns sit this far from their nearest
# neighbour, as a median: Northshire's Timber Wolves 31 yards, its Defias Thugs 15.
RIB_SPREAD_YARDS = 20.0
# A rib stands where nothing outclasses its band: a spawn is left out of its creature's
# cluster when anything within `RIB_NEIGHBOUR_YARDS` tops the band by more than
# `RIB_OUTCLASS_LEVELS`. The 1-3 rib's Young Wolves clustered by Goldshire's road among
# level 5-6 Mangy Wolves and Defias Cutpurses; a level 2 mage failed over to it from
# Northshire, 400 yards out, and a Cutpurse killed it twice (the mage's second check, V193).
RIB_NEIGHBOUR_YARDS = 90.0
RIB_OUTCLASS_LEVELS = 2
CREATURE_BEAST = 1                     # creature_template.CreatureType

# Spawn points kept per cluster for a hunt to stand on: the nearest this many to its centre.
HUNT_SPAWNS = 16


def hunt_spawns(spawn: Spawn | None) -> tuple[tuple[float, float, float], ...]:
    return spawn.points if spawn is not None else ()


def hunt_yards(spawn: Spawn | None) -> float:
    """What a hunt should walk for this cluster, clamped to something a camp can be."""
    reach = spawn.spread if spawn is not None else 0.0
    return min(HUNT_MAX_YARDS, max(HUNT_MIN_YARDS, reach))


def _note(spawn: Spawn | None, frac: tuple[float, float] | None, role: str) -> str:
    """Why this node is where it is — or why it is nowhere.

    "No spawn at all" and "a spawn we could not map" need different fixes: the first is a
    quest started by an item or an object, the second is a zone-box problem. A note that
    only carried the NPC's name could not tell them apart.
    """
    if spawn is None:
        return f"no {role} spawn found"
    if frac is None:
        return f"{spawn.name}; {role} spawn at {spawn.x:.0f},{spawn.y:.0f} is off every zone map"
    return spawn.name


def _slug(text: str, limit: int = 28) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in text]
    out = "".join(keep).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out[:limit].strip("_") or "x"


class WorldDB:
    """Read-only access to the mirrored world database.

    Opened read-only on purpose. This is a mirror of the live server's data; anything
    that wants to change it should regenerate the mirror rather than patch it, or the two
    diverge with no way to tell which is right.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.bounds = load_bounds(path)

    def close(self) -> None:
        self.con.close()

    # -- spawns --------------------------------------------------------------

    def first_spawn(self, npc_id: int, map_id: int | None = None) -> Spawn | None:
        """One representative spawn for an NPC.

        Many NPCs have several spawns. The first by guid is chosen deliberately over,
        say, the centroid: a centroid of two guards on opposite sides of a river is a
        point in the river. A real spawn is always somewhere you can stand.
        """
        q = ("select c.id, c.map, cast(c.position_x as real) px, "
             "cast(c.position_y as real) py, cast(c.position_z as real) pz, t.Name "
             "from world_creature c join world_creature_template t on t.Entry = c.id "
             "where c.id = ?")
        args: list[object] = [npc_id]
        if map_id is not None:
            q += " and c.map = ?"
            args.append(map_id)
        r = self.con.execute(q + " order by c.guid limit 1", args).fetchone()
        if not r:
            return None
        return Spawn(npc_id, r["Name"] or f"npc{npc_id}", r["map"],
                     r["px"], r["py"], r["pz"])

    # -- quests --------------------------------------------------------------

    def quests(self, *, faction: str, level_min: int, level_max: int,
               zone_ids: tuple[int, ...]) -> list[QuestRow]:
        """Quests offered on this server, for this faction, in this level band and zone.

        `RequiredRaces == 0` means every race, which is most quests. The mask test is
        `& faction_mask` rather than equality because a quest may list several races.
        """
        mask = ALLIANCE_MASK if faction == "alliance" else HORDE_MASK
        placeholders = ",".join("?" * len(zone_ids))
        rows = self.con.execute(
            f"""
            select entry, Title, QuestLevel, MinLevel, ZoneOrSort, PrevQuestId, NextQuestId,
                   Objectives, RequiredRaces, SpecialFlags,
                   ReqCreatureOrGOCount1, ReqCreatureOrGOCount2,
                   ReqCreatureOrGOCount3, ReqCreatureOrGOCount4,
                   ReqItemCount1, ReqItemCount2, ReqItemCount3, ReqItemCount4
            from world_quest_template
            where QuestLevel between ? and ?
              and ZoneOrSort in ({placeholders})
              and (RequiredRaces = 0 or (RequiredRaces & ?) != 0)
            order by QuestLevel, MinLevel, entry
            """,
            (level_min, level_max, *zone_ids, mask),
        ).fetchall()

        out: list[QuestRow] = []
        for r in rows:
            counts = tuple(
                r[k] or 0 for k in (
                    "ReqCreatureOrGOCount1", "ReqCreatureOrGOCount2",
                    "ReqCreatureOrGOCount3", "ReqCreatureOrGOCount4",
                    "ReqItemCount1", "ReqItemCount2", "ReqItemCount3", "ReqItemCount4",
                )
            )
            out.append(QuestRow(
                quest_id=r["entry"], title=r["Title"] or f"quest{r['entry']}",
                level=r["QuestLevel"], min_level=r["MinLevel"],
                zone_or_sort=r["ZoneOrSort"], prev_quest=r["PrevQuestId"] or 0,
                next_quest=r["NextQuestId"] or 0, objectives=r["Objectives"] or "",
                req_counts=counts, xp_est=max(0, r["QuestLevel"]) * 90,
                special_flags=r["SpecialFlags"] or 0,
            ))
        return out

    def object_spawn(self, object_id: int) -> Spawn | None:
        """One spawn of a gameobject, with its template name."""
        r = self.con.execute(
            "select g.id, g.map, cast(g.position_x as real) px, "
            "cast(g.position_y as real) py, cast(g.position_z as real) pz, t.name "
            "from world_gameobject g "
            "join world_gameobject_template t on t.entry = g.id "
            "where g.id = ? order by g.guid limit 1", (object_id,),
        ).fetchone()
        if not r:
            return None
        return Spawn(object_id, r["name"] or f"object{object_id}", r["map"],
                     r["px"], r["py"], r["pz"], kind="gameobject")

    def giver(self, quest_id: int) -> Spawn | None:
        """Who or what offers this quest.

        Creatures first, then gameobjects. Not every quest giver is a person: "Wanted:
        Hogger" comes off a wanted poster, and a creature-only lookup leaves that whole
        class of quest positionless while the coordinates sit in the next table along.
        """
        r = self.con.execute(
            "select id from world_creature_questrelation where quest = ? limit 1", (quest_id,)
        ).fetchone()
        if r:
            spawn = self.first_spawn(r["id"])
            if spawn:
                return spawn
        r = self.con.execute(
            "select id from world_gameobject_questrelation where quest = ? limit 1", (quest_id,)
        ).fetchone()
        return self.object_spawn(r["id"]) if r else None

    def taker(self, quest_id: int) -> Spawn | None:
        r = self.con.execute(
            "select id from world_creature_involvedrelation where quest = ? limit 1", (quest_id,)
        ).fetchone()
        if r:
            spawn = self.first_spawn(r["id"])
            if spawn:
                return spawn
        r = self.con.execute(
            "select id from world_gameobject_involvedrelation where quest = ? limit 1",
            (quest_id,),
        ).fetchone()
        return self.object_spawn(r["id"]) if r else None

    def objective_spawn(self, quest_id: int, zones: tuple[ZoneBounds, ...] = (),
                        home: ZoneBounds | None = None) -> Spawn | None:
        """Compatibility accessor for the first structured requirement's destination."""
        requirements = self.requirements(quest_id, zones, home)
        return requirements[0].spawn if requirements else None

    def prerequisites(self, quest_id: int) -> tuple[tuple[tuple[int, ...], ...], str | None]:
        """The server's prerequisite alternatives, including reverse NextQuestId edges.

        Grounded in ObjectMgr::LoadQuests and Player::SatisfyQuestPreviousQuest.
        Active-quest (negative) prerequisites need a parallel-quest plan, which this
        sequential spine cannot honour; keep that limitation explicit.
        """
        row = self.con.execute(
            "select PrevQuestId from world_quest_template where entry = ?", (quest_id,),
        ).fetchone()
        previous = [row[0]] if row and row[0] else []
        previous += [r[0] if r[1] > 0 else -r[0] for r in self.con.execute(
            "select entry, NextQuestId from world_quest_template where abs(NextQuestId) = ?",
            (quest_id,),
        )]
        groups = []
        negative = False
        for qid in dict.fromkeys(previous):
            if qid < 0:
                negative = True
                continue
            prior = self.con.execute(
                "select ExclusiveGroup, NextQuestId from world_quest_template where entry = ?",
                (qid,),
            ).fetchone()
            if not prior:
                groups.append((qid,))
                continue
            if prior[0] < 0 and not (row[0] and prior[1] != row[0]):
                groups.append(tuple(r[0] for r in self.con.execute(
                    "select entry from world_quest_template where ExclusiveGroup = ? order by entry",
                    (prior[0],),
                )))
            else:
                groups.append((qid,))
        blocked = "requires an active predecessor quest; sequential route cannot hold it" if negative and not groups else None
        return tuple(dict.fromkeys(groups)), blocked

    def requirements(self, quest_id: int, zones: tuple[ZoneBounds, ...] = (),
                     home: ZoneBounds | None = None) -> tuple[Requirement, ...]:
        """Every requirement with its own DB destination and named unsupported cases.

        A single objective family preserves the server's slot order. Mixed item and
        creature families are deliberately not joined positionally: the radio carries
        client leaderboard indices, not the source family or required item identity.
        """
        row = self.con.execute("select * from world_quest_template where entry = ?",
                               (quest_id,)).fetchone()
        if row is None:
            return ()
        creatures = [i for i in range(1, 5)
                     if row[f"ReqCreatureOrGOId{i}"] or row[f"ReqSpellCast{i}"]]
        items = [i for i in range(1, 5) if row[f"ReqItemId{i}"]]
        mixed = bool(creatures and items)
        extra_event = bool((row["SpecialFlags"] or 0) & 2)
        blocked_counter = ("mixed objective families need painted counter identity" if mixed else
                           "event and counters need painted counter identity" if extra_event else None)
        result: list[Requirement] = []
        for index, slot in enumerate(creatures):
            entry = row[f"ReqCreatureOrGOId{slot}"]
            spell = row[f"ReqSpellCast{slot}"]
            kind = "spell" if spell else "kill" if entry > 0 else "interact"
            spawn = self._creature_cluster(entry) if entry > 0 else self.object_spawn(-entry)
            blocked = blocked_counter
            if spell:
                blocked = f"requires quest spell {spell}; no quest-spell executor"
            elif spawn is None:
                blocked = f"no {'creature' if entry > 0 else 'gameobject'} spawn for requirement {entry}"
            result.append(Requirement(kind=kind, required_id=abs(entry),
                                      required_count=row[f"ReqCreatureOrGOCount{slot}"] or 1,
                                      source_slot=slot, counter_index=None if blocked_counter else index,
                                      spawn=spawn, blocked_reason=blocked))
        for index, slot in enumerate(items):
            item_id, count = row[f"ReqItemId{slot}"], row[f"ReqItemCount{slot}"] or 1
            supplied = (item_id == row["SrcItemId"] and (row["SrcItemCount"] or 1) >= count)
            spawn = (self.taker(quest_id) if supplied
                     else self._drops(item_id, zones, home, level=row["QuestLevel"]))
            kind = "delivery" if supplied else "loot"
            blocked = blocked_counter
            if spawn is None and not supplied:
                vendor = self.con.execute("select 1 from world_npc_vendor where item = ? limit 1",
                                          (item_id,)).fetchone()
                blocked = (f"quest item {item_id} requires purchase; no quest-item purchase executor"
                           if vendor else f"no supported source for quest item {item_id}")
            result.append(Requirement(kind=kind, required_id=item_id, required_count=count,
                                      source_slot=slot, counter_index=None if blocked_counter else index,
                                      spawn=spawn, blocked_reason=blocked))
        # An elite is not a solo character's fight at the quest's level: Hogger, a level 11
        # elite with his gnolls round him, is the claw Wanted: "Hogger" asks for. Only what
        # is fought: a delivery's creature is the quest's taker, and Gryan Stoutmantle, who
        # takes Westfall's hand-ins at Sentinel Hill, is an elite too.
        result = [r if r.blocked_reason or r.spawn is None or r.spawn.kind != "creature"
                  or r.kind not in ("kill", "loot") or not self._elite(r.spawn.npc_id) else
                  replace(r, blocked_reason=f"{r.spawn.name} is an elite; not a solo fight")
                  for r in result]
        if extra_event:
            # These DBC columns are raw float bits, exactly like WorldMapArea. Their
            # layout is AreaTriggerEntry in this server's DBCStructure.h.
            triggers = self.con.execute(
                "select a.* from dbc_AreaTrigger a join world_areatrigger_involvedrelation r "
                "on a.id = r.id where r.quest = ? order by a.id", (quest_id,),
            ).fetchall()
            if len(triggers) == 1 and not (creatures or items):
                trigger = triggers[0]
                spawn = Spawn(trigger["id"], "exploration trigger", trigger["c1"],
                              *(_as_float(trigger[f"c{i}"]) for i in (2, 3, 4)))
                result.append(Requirement("explore", required_id=trigger["id"], spawn=spawn))
            else:
                result.append(Requirement("event", blocked_reason=
                                          "quest event requires a measured interaction or route"))
        return tuple(result)

    def _elite(self, entry: int) -> bool:
        row = self.con.execute("select Rank from world_creature_template where Entry = ?",
                               (entry,)).fetchone()
        return bool(row and row["Rank"] in (1, 2, 3))

    def _creature_cluster(self, entry: int) -> Spawn | None:
        rows = self.con.execute(
            "select c.map, cast(c.position_x as real) px, cast(c.position_y as real) py, "
            "cast(c.position_z as real) pz, t.Name "
            "from world_creature c join world_creature_template t on t.Entry = c.id "
            "where c.id = ? order by c.guid", (entry,),
        ).fetchall()
        if not rows:
            # Randomized creature spawns keep c.id == 0; possible identities live in
            # creature_spawn_entry or spawn_group_entry. These are real source pins,
            # not an excuse to expand an established camp or invent a wander search.
            rows = self.con.execute(
                self._random_creatures_cte() +
                " select c.map, cast(c.position_x as real) px, cast(c.position_y as real) py, "
                "cast(c.position_z as real) pz, t.Name "
                "from random_creatures e join world_creature c on c.guid = e.guid "
                "join world_creature_template t on t.Entry = e.entry "
                "where e.entry = ? order by c.guid", (entry,),
            ).fetchall()
        if not rows:
            return None
        map_id = rows[0]["map"]
        same = [r for r in rows if r["map"] == map_id]
        return _cluster(entry, same[0]["Name"] or "mobs", map_id, same)

    @staticmethod
    def _random_creatures_cte() -> str:
        return (
            "with random_creatures as (select guid, entry from world_creature_spawn_entry "
            "union select s.Guid, e.Entry from world_spawn_group_spawn s "
            "join world_spawn_group_entry e on e.Id = s.Id "
            "join world_spawn_group g on g.Id = s.Id where g.Type = 0)"
        )

    def _drops(self, item_id: int, zones: tuple[ZoneBounds, ...] = (),
               home: ZoneBounds | None = None, level: int | None = None) -> Spawn | None:
        """Where the things that drop this item live.

        Every source, pooled and then clustered, because a wolf camp is a mixed
        population - Ragged Young Wolf, Young Wolf and Timber Wolf all carry Tough Wolf
        Meat - and clustering them separately would put the node on whichever happened to
        have one more spawn than the others.

        **Gameobjects count.** An item objective is "bring me eight of these" and the
        eight come off whatever holds them, which is as often a crate as a corpse.
        Milly's Harvest (quest 3904) has *no* creature dropper at all: the item lives in
        forty chests, a creature-only search returned nothing, and the objective node was
        placed on Milly Osworth with a fifteen-yard disk. The bot would have stood next to
        the woman who wanted the apples.

        Spawns are weighted by drop chance, so a 2% dropper does not pull the node away
        from an 80% one.

        The name that comes back is the commonest in the cluster, because that is what a
        hunt filters plates by.
        """
        rows = [dict(r) for r in self.con.execute(
            """
            select c.map, cast(c.position_x as real) px, cast(c.position_y as real) py,
                   cast(c.position_z as real) pz, t.Name, t.Entry,
                   abs(l.ChanceOrQuestChance) as chance, 'creature' as kind,
                   t.MinLevel as level
            from world_creature_loot_template l
            join world_creature_template t on t.LootId = l.entry
            join world_creature c on c.id = t.Entry
            where l.item = ?
            """,
            (item_id,),
        ).fetchall()]
        rows += [dict(r) for r in self.con.execute(
            """
            select g.map, cast(g.position_x as real) px, cast(g.position_y as real) py,
                   cast(g.position_z as real) pz, t.name as Name, t.entry as Entry,
                   abs(l.ChanceOrQuestChance) as chance, 'gameobject' as kind,
                   null as level
            from world_gameobject_loot_template l
            join world_gameobject_template t on t.data1 = l.entry and t.type = 3
            join world_gameobject g on g.id = t.entry
            where l.item = ?
            """,
            (item_id,),
        ).fetchall()]
        if not rows:
            rows = [dict(r) for r in self.con.execute(
                self._random_creatures_cte() + """
                select c.map, cast(c.position_x as real) px, cast(c.position_y as real) py,
                       cast(c.position_z as real) pz, t.Name, t.Entry,
                       abs(l.ChanceOrQuestChance) as chance, 'creature' as kind,
                       t.MinLevel as level
                from world_creature_loot_template l
                join world_creature_template t on t.LootId = l.entry
                join random_creatures e on e.entry = t.Entry
                join world_creature c on c.guid = e.guid
                where l.item = ? order by c.guid, t.Entry
                """, (item_id,),
            ).fetchall()]
        for r in rows:
            # A zero here means "always" in some rows and "unset" in others; either way a
            # spawn that is in the table drops the thing, so it votes.
            r["weight"] = float(r["chance"]) or 1.0
        if not rows:
            return None

        # Only spawns inside a zone this guide covers, and inside the quest's **own** zone
        # first. A zone box is not a neighbourhood: Elwynn's contains Northshire and a
        # great deal else, so filtering to "any zone in scope" left Young Wolf pooling 122
        # spawns with a median of 2623 yards from the node it produced - and three of them
        # inside that node's fifty-yard disk. Before the zone filter existed at all this
        # put the node at (-6326, 380), off every map in scope.
        for scope in ((home,) if home is not None else (), tuple(zones)):
            inside = [r for r in rows
                      if any(z is not None and r["map"] == z.map_id
                             and (f := world_to_map(r["px"], r["py"], z)) is not None
                             and on_map(*f, slack=0.0)
                             for z in scope)]
            if inside:
                rows = inside
                break
        if level is not None and level > 0 and any(
                r["level"] is not None and r["level"] <= level + DROP_LEVELS_ABOVE for r in rows):
            rows = [r for r in rows if r["level"] is None
                    or r["level"] <= level + DROP_LEVELS_ABOVE]
        m = max({r["map"] for r in rows},
                key=lambda mm: sum(1 for r in rows if r["map"] == mm))
        same = [r for r in rows if r["map"] == m]
        spawn = _cluster(same[0]["Entry"], same[0]["Name"] or "mobs", m, same)

        # Name the cluster after whatever is commonest inside it, not whatever the query
        # happened to return first.
        near = [r for r in same
                if math.hypot(r["px"] - spawn.x, r["py"] - spawn.y) <= CLUSTER_REACH]
        if near:
            names = {}
            for r in near:
                names[r["Name"]] = names.get(r["Name"], 0) + 1
            best = max(names, key=lambda n: names[n])
            chosen = next(r for r in near if r["Name"] == best)
            spawn = Spawn(chosen["Entry"], best or "mobs", m,
                          spawn.x, spawn.y, spawn.z, spread=spawn.spread, kind=chosen["kind"],
                          points=spawn.points)
        return spawn

    # -- services ------------------------------------------------------------

    def services(self, zone_bounds: ZoneBounds, flag: int, limit: int = 3) -> list[Spawn]:
        """NPCs with a given flag whose spawns fall inside this zone's map box.

        The box test is the zone filter: `world_creature` has no area column populated,
        so geometry decides. That is also more honest than an area id would be, since
        what matters is whether the character can walk to it.
        """
        rows = self.con.execute(
            "select c.id, c.map, cast(c.position_x as real) px, "
            "cast(c.position_y as real) py, cast(c.position_z as real) pz, t.Name "
            "from world_creature c join world_creature_template t on t.Entry = c.id "
            "where c.map = ? and (t.NpcFlags & ?) != 0 group by c.id",
            (zone_bounds.map_id, flag),
        ).fetchall()

        out: list[Spawn] = []
        for r in rows:
            frac = world_to_map(r["px"], r["py"], zone_bounds)
            if frac and on_map(*frac, slack=0.0):
                out.append(Spawn(r["id"], r["Name"] or "npc", r["map"],
                                 r["px"], r["py"], r["pz"]))
            if len(out) >= limit:
                break
        return out

    def grind_clusters(self, zone_bounds: ZoneBounds, level_min: int, level_max: int,
                       limit: int = 2) -> list[tuple[Spawn, int]]:
        """Grind ribs: one creature's densest cluster, the safest kinds first.

        "Killable" is a heuristic and named as one: normal rank, in level band, carrying
        loot, and **no NPC flags at all**. A creature that can be talked to, trained from
        or bought from is furniture, not prey. This will occasionally miss a valid grind
        mob and occasionally include a neutral critter; the rib is verified by walking it
        (V5), which is exactly the half the DB cannot answer.

        One creature per cluster. Pooling every creature in the band and naming the densest
        pool after its first row put the "Kobold Worker" rib in the middle of Northshire's
        Defias Thug camp, 230 yards from any kobold: a failed step's detour walked a level 3
        paladin into thugs to hunt kobolds, and it died there (run 20260924T002817-cee9c2).

        A rib is where a step goes when it fails, so the safest grind leads: spawns spread
        out (a median of `RIB_SPREAD_YARDS` to the nearest neighbour, not a camp that pulls
        in pairs), beasts (they neither flee for help nor carry weapons), the lower end of
        the band, then the biggest cluster.
        """
        rows = self.con.execute(
            """
            select cast(c.position_x as real) x, cast(c.position_y as real) y,
                   cast(c.position_z as real) z, c.map, c.id, t.Name, t.CreatureType
            from world_creature c
            join world_creature_template t on t.Entry = c.id
            where c.map = ? and t.Rank = 0 and t.NpcFlags = 0
              and t.LootId > 0 and t.MinLevel >= ? and t.MaxLevel <= ?
            """,
            (zone_bounds.map_id, level_min, level_max),
        ).fetchall()
        levels = dict(self.con.execute(
            "select Entry, MaxLevel from world_creature_template where MinLevel >= ? and MaxLevel <= ?",
            (level_min, level_max)).fetchall())
        stronger = [(r[0], r[1]) for r in self.con.execute(
            """
            select cast(c.position_x as real), cast(c.position_y as real)
            from world_creature c join world_creature_template t on t.Entry = c.id
            where c.map = ? and t.NpcFlags = 0 and t.MaxLevel > ?
            """, (zone_bounds.map_id, level_max + RIB_OUTCLASS_LEVELS)).fetchall()
            if (f := world_to_map(r[0], r[1], zone_bounds)) is not None
            and on_map(*f, slack=0.25)]

        def outclassed(x: float, y: float) -> bool:
            return any(math.hypot(x - sx, y - sy) <= RIB_NEIGHBOUR_YARDS for sx, sy in stronger)

        by_creature: dict[int, list] = defaultdict(list)
        for r in rows:
            frac = world_to_map(r["x"], r["y"], zone_bounds)
            if frac and on_map(*frac, slack=0.0) and not outclassed(r["x"], r["y"]):
                by_creature[r["id"]].append(r)

        ranked = []
        for npc_id, group in by_creature.items():
            if len(group) < 6:      # a "cluster" of four wolves is not a grind rib
                continue
            # Same clustering as a kill objective's, so a rib carries a real reach
            # rather than the floor. `x`/`y`/`z` here, `px`/`py`/`pz` there.
            rekeyed = [{"px": g["x"], "py": g["y"], "pz": g["z"]} for g in group]
            spawn = _cluster(npc_id, group[0]["Name"] or "mobs", group[0]["map"], rekeyed)
            members = [r for r in rekeyed
                       if math.hypot(r["px"] - spawn.x, r["py"] - spawn.y) <= CLUSTER_REACH]
            if len(members) < 6:
                continue
            gaps = sorted(min(math.hypot(a["px"] - b["px"], a["py"] - b["py"])
                              for b in members if b is not a) for a in members)
            spread = gaps[len(gaps) // 2] >= RIB_SPREAD_YARDS
            beast = group[0]["CreatureType"] == CREATURE_BEAST
            key = (spread, beast, -int(levels.get(npc_id) or level_max), len(members))
            ranked.append((key, spawn, len(members)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [(spawn, count) for _key, spawn, count in ranked[:limit]]


# --------------------------------------------------------------------------- generation


def rib_windows(level_min: int, level_max: int, width: int = 2) -> list[tuple[int, int]]:
    """Overlapping level windows for ribs: (1, 3), (3, 5), ... up to `level_max`."""
    return [(lo, min(level_max, lo + width)) for lo in range(level_min, level_max, width)]


def _order_quests(quests: list[QuestRow], zone_order: dict[int, int]) -> list[QuestRow]:
    """Starter zone first, then level order, and never a quest before its prerequisite.

    Three constraints, in that priority:

      * **Zone before level.** Level order alone interleaves the starter zone with the
        zone after it, so a fresh character's first step is a level-1 quest halfway
        across Elwynn rather than the one ten yards from where it logged in. Zones are
        ordered as the spine declares them.
      * **A stable topological pass over `PrevQuestId`**, because level order puts a
        level-3 follow-up before the level-5 quest that unlocks it, and the character then
        stands at an NPC with nothing to offer — which looks exactly like a broken reader.
      * Level within a zone.

    This is still not a route: it does not minimise walking within a zone. That is the
    recorder pass's job (`DECISIONS.md` V5), and it needs the terrain the DB does not have.
    """
    by_id = {q.quest_id: q for q in quests}
    ordered: list[QuestRow] = []
    placed: set[int] = set()

    def place(q: QuestRow, seen: frozenset[int] = frozenset()) -> None:
        if q.quest_id in placed or q.quest_id in seen:
            return                      # cycles exist in the data; break rather than hang
        prev = by_id.get(q.prev_quest)
        if prev is not None:
            place(prev, seen | {q.quest_id})
        placed.add(q.quest_id)
        ordered.append(q)

    def key(q: QuestRow) -> tuple[int, int, int, int]:
        return (zone_order.get(q.zone_or_sort, 99), q.min_level, q.level, q.quest_id)

    for q in sorted(quests, key=key):
        place(q)
    return ordered


def generate(
    db_path: str,
    *,
    graph_id: str,
    faction: str,
    zone_ids: tuple[int, ...],
    zone_names: dict[int, str],
    level_min: int = 1,
    level_max: int = 12,
    max_quests: int | None = None,
    spawns: dict[str, list] | None = None,
) -> Graph:
    """Build a spine plus ribs and services for one faction over one level band.

    `spawns`, when given, is filled with where each hunt's target actually spawns, keyed
    `node_id` and `node_id#target_id` (`hunt_spawns`, `jev.run.hunt.spawn_stations`). It is
    routine data kept out of the guide: the guide's bytes are the tutor's knowledge
    fingerprint, and a changed fingerprint starts the motor learner's corpus again.
    """
    db = WorldDB(db_path)
    spawns = {} if spawns is None else spawns
    try:
        return _generate(db, graph_id=graph_id, faction=faction, zone_ids=zone_ids,
                         zone_names=zone_names, level_min=level_min,
                         level_max=level_max, max_quests=max_quests, spawns=spawns)
    finally:
        db.close()


def _generate(db: WorldDB, *, graph_id: str, faction: str, zone_ids: tuple[int, ...],
              zone_names: dict[int, str], level_min: int, level_max: int,
              max_quests: int | None, spawns: dict[str, list]) -> Graph:
    nodes: list[Node] = []
    prefix = graph_id
    # NPCs whose spawn exists but falls outside every zone box in scope. Recorded so a
    # positionless node says which of the two things happened — no spawn at all, or a
    # spawn we could not map — because they need different fixes.
    unplaced: set[int] = set()
    coordinate_bounds = next((db.bounds[z] for z in zone_ids
                              if z in db.bounds and not db.bounds[z].degenerate), None)

    def place(spawn: Spawn | None, zone_id: int) -> tuple[tuple[float, float] | None,
                                                          tuple[float, float, float] | None,
                                                          int | None]:
        """Map fraction, world position and map id for a spawn — or nothing at all.

        Tries the node's own zone first, then any other zone in scope whose map box
        actually contains the point. That fallback is not a nicety: **not every zone has
        a map of its own.** Northshire Valley is area 9 with thirteen starting quests and
        no `WorldMapArea` row at all, because 2.4.3 draws it on Elwynn's map — so every
        Northshire node would otherwise have come out positionless while its coordinates
        converted perfectly against the map one level up.

        A node with no position is still emitted: it records that the step exists on this
        server, and the recorder pass supplies what the DB could not. Inventing a position
        would be worse than admitting to not having one.
        """
        if spawn is None:
            return None, None, None
        world = (spawn.x, spawn.y, spawn.z)

        candidates = [zone_id, *(z for z in zone_ids if z != zone_id)]
        for zid in candidates:
            b = db.bounds.get(zid)
            if b is None or b.degenerate or b.map_id != spawn.map_id:
                continue
            frac = world_to_map(spawn.x, spawn.y, b)
            if frac is not None and on_map(*frac):
                # Every node in one guide uses one declared frame. Actual client zone
                # changes (e.g. Elwynn -> Stormwind) must not change its coordinate units.
                if coordinate_bounds is not None and coordinate_bounds.map_id == spawn.map_id:
                    frac = world_to_map(spawn.x, spawn.y, coordinate_bounds)
                return frac, world, spawn.map_id
        unplaced.add(spawn.npc_id)
        return None, world, spawn.map_id

    # -- ribs first, so quest nodes have somewhere to fail to -------------------
    # One per level window, each the densest cluster of mobs in it, and the rib's `level`
    # is its window. The densest cluster for the whole band was Stonetusk Boars, level
    # 5-6, and every Northshire step failed into it: a level 3 character went there and
    # died three times running (run 20260923T174132-d01302).
    ribs: list[Node] = []
    for zid in zone_ids:
        b = db.bounds.get(zid)
        if b is None or b.degenerate:
            continue
        taken: set[int] = set()
        for lo, hi in rib_windows(level_min, level_max):
            for spawn, count in db.grind_clusters(b, lo, hi, limit=3):
                if spawn.npc_id in taken:
                    continue
                taken.add(spawn.npc_id)
                frac, world, map_id = place(spawn, zid)
                rid = f"{prefix}_grind_{_slug(zone_names.get(zid, str(zid)), 12)}_{lo}_{hi}"
                if spawn.points:
                    spawns[rid] = [list(p) for p in spawn.points]
                ribs.append(Node(
                    id=rid, kind=StepKind.GRIND, zone=zone_names.get(zid, str(zid)),
                    zone_id=zid, level=(lo, hi), pos=frac, world=world, map_id=map_id,
                    r=0.06,   # a rib is a loop you walk, not a point you stand on
                    hunt_yards=hunt_yards(spawn),
                    target_name=spawn.name, target_kind=spawn.kind,
                    objectives=(f"grind {spawn.name}",),
                    skills=("GRIND_UNTIL",), timeout_s=900.0, skippable=True,
                    notes=f"{count} spawns of {spawn.name} clustered here; route not recorded",
                ))
                break
    nodes.extend(ribs)

    # -- services ---------------------------------------------------------------
    for zid in zone_ids:
        b = db.bounds.get(zid)
        if b is None or b.degenerate:
            continue
        zname = zone_names.get(zid, str(zid))
        for flag, kind, skill in (
            (NPC_VENDOR, StepKind.VENDOR, "VENDOR_REPAIR"),
            (NPC_REPAIR, StepKind.REPAIR, "VENDOR_REPAIR"),
            (NPC_TRAINER_CLASS, StepKind.TRAIN, "TRAIN_CLASS"),
            (NPC_INNKEEPER, StepKind.HEARTH, "HEARTH"),
            (NPC_FLIGHTMASTER, StepKind.FLIGHT, "FLIGHT_PATH"),
        ):
            for i, spawn in enumerate(db.services(b, flag, limit=SERVICE_SPAWNS)):
                frac, world, map_id = place(spawn, zid)
                nodes.append(Node(
                    id=f"{prefix}_{kind.value}_{_slug(zname, 12)}_{i}",
                    kind=kind, zone=zname, zone_id=zid, level=(level_min, level_max),
                    pos=frac, world=world, map_id=map_id, npc_id=spawn.npc_id,
                    target_name=spawn.name, target_kind=spawn.kind,
                    objectives=(f"{kind.value} at {spawn.name}",),
                    skills=("TRAVEL_TO", skill), skippable=True,
                    notes=f"{spawn.name}; route not recorded",
                ))

    # -- the spine --------------------------------------------------------------
    quests = _order_quests(
        db.quests(faction=faction, level_min=level_min, level_max=level_max,
                  zone_ids=zone_ids),
        {zid: i for i, zid in enumerate(zone_ids)},
    )
    if max_quests:
        quests = quests[:max_quests]

    zones_in_scope = tuple(b for b in (db.bounds.get(z) for z in zone_ids) if b)
    chain: list[str] = []
    unplaceable: list[str] = []
    for q in quests:
        zid = q.zone_or_sort if q.zone_or_sort in zone_ids else zone_ids[0]
        zname = zone_names.get(zid, str(zid))
        base = f"{prefix}_{q.quest_id}_{_slug(q.title)}"
        band = (max(1, q.min_level), max(q.level, q.min_level) + 3)

        giver, taker = db.giver(q.quest_id), db.taker(q.quest_id)

        # A quest whose giver does not spawn on this server cannot be taken, and a step
        # that cannot be taken does not fail — it **blocks the chain behind it**, because
        # the playhead stops at the first step the world does not satisfy and this one
        # never will. Seasonal quests are the common case: Waskily Wabbits (7961) is
        # Noblegarden, its giver exists only during the event, and it sat between
        # A Threat Within and the whole rest of Northshire.
        #
        # Dropped at generation rather than skipped at runtime: the guide should describe
        # what this server can actually do, and a runtime skip would have to re-derive
        # that judgement on every pass.
        if giver is None:
            unplaceable.append(f"{q.quest_id} {q.title}")
            continue

        requirements = db.requirements(q.quest_id, zones_in_scope, db.bounds.get(zid))
        prerequisites, route_blocked = db.prerequisites(q.quest_id)
        if taker is None:
            route_blocked = route_blocked or "no spawned quest turn-in target in the source database"
        quest_metadata = {"quest_prerequisites": prerequisites,
                          "route_blocked_reason": route_blocked}
        targets = []
        for requirement in requirements:
            frac, world, map_id = place(requirement.spawn, zid)
            spawn = requirement.spawn
            blocked = requirement.blocked_reason
            if blocked is None and spawn is not None and frac is None:
                blocked = "objective destination is outside this guide's supported maps"
            targets.append(ObjectiveTarget(
                kind=requirement.kind, required_id=requirement.required_id,
                required_count=requirement.required_count,
                source_slot=requirement.source_slot, counter_index=requirement.counter_index,
                target_id=spawn.npc_id if spawn else None,
                target_name=spawn.name if spawn and requirement.kind != "explore" else None,
                target_kind=spawn.kind if spawn and requirement.kind != "explore" else None,
                pos=frac, world=world, map_id=map_id,
                coord_zone_id=coordinate_bounds.area_id if coordinate_bounds else None,
                hunt_yards=hunt_yards(spawn) if requirement.kind in ("kill", "loot") else None,
                blocked_reason=blocked,
            ))

        gfrac, gworld, gmap = place(giver, zid)
        nodes.append(Node(
            id=f"{base}_accept", kind=StepKind.QUEST_ACCEPT, zone=zname, zone_id=zid,
            level=band, pos=gfrac, world=gworld, map_id=gmap, quest_id=q.quest_id,
            npc_id=giver.npc_id if giver else None,
            target_name=giver.name if giver else None, target_kind=giver.kind if giver else None,
            title=q.title,
            **quest_metadata,
            objectives=(f"accept {q.title}",),
            skills=("TRAVEL_TO", "ACCEPT_QUEST"), timeout_s=240.0,
            xp_est=q.xp_est,
            notes=(_note(giver, gfrac, "giver")),
        ))
        chain.append(f"{base}_accept")

        if requirements:
            mobs = requirements[0].spawn
            ofrac, oworld, omap = place(mobs or giver, zid)
            if mobs is not None and mobs.points:
                spawns[f"{base}_do"] = [list(p) for p in mobs.points]
            for requirement in requirements:
                if (requirement.kind in ("kill", "loot") and requirement.spawn is not None
                        and requirement.spawn.points):
                    spawns[f"{base}_do#{requirement.spawn.npc_id}"] = [
                        list(p) for p in requirement.spawn.points]
            nodes.append(Node(
                id=f"{base}_do", kind=StepKind.QUEST_OBJECTIVE, zone=zname, zone_id=zid,
                level=band, pos=ofrac, world=oworld, map_id=omap, quest_id=q.quest_id,
                title=q.title, hunt_yards=hunt_yards(mobs),
                objective_targets=tuple(targets), **quest_metadata,
                target_name=mobs.name if mobs else None, target_kind=mobs.kind if mobs else None,
                objectives=tuple(filter(None, [q.objectives[:120]])) or ("complete objectives",),
                skills=("TRAVEL_TO", "GRIND_UNTIL"), timeout_s=600.0,
                r=0.06, xp_est=q.xp_est,
                notes=_note(mobs or giver, ofrac, "objective"),
                # Escape edges are filled in by the wiring pass below, which is the
                # only place that knows what comes next. A rib is preferred when the
                # zone has one; small zones like Northshire have no cluster that clears
                # the threshold, and a step with nowhere to fail to is a run that ends
                # standing in a field.
                on_fail=(),
            ))
            chain.append(f"{base}_do")

        tfrac, tworld, tmap = place(taker or giver, zid)
        nodes.append(Node(
            id=f"{base}_turnin", kind=StepKind.QUEST_TURNIN, zone=zname, zone_id=zid,
            level=band, pos=tfrac, world=tworld, map_id=tmap, quest_id=q.quest_id,
            npc_id=(taker or giver).npc_id if (taker or giver) else None,
            target_name=(taker or giver).name if (taker or giver) else None,
            target_kind=(taker or giver).kind if (taker or giver) else None,
            title=q.title,
            **quest_metadata,
            objectives=(f"turn in {q.title}",),
            skills=("TRAVEL_TO", "TURNIN_QUEST"), timeout_s=240.0, xp_est=q.xp_est,
            notes=_note(taker or giver, tfrac, "turn-in"),
        ))
        chain.append(f"{base}_turnin")

    # -- wire the spine, and give every step somewhere to fail to ---------------
    by_id = {n.id: n for n in nodes}
    wired: list[Node] = []
    for n in nodes:
        if n.id not in by_id or n.id not in chain:
            wired.append(n)
            continue
        i = chain.index(n.id)
        nxt = (chain[i + 1],) if i + 1 < len(chain) else ()
        fails = n.on_fail
        if not fails:
            # Everything on the spine can stall, so everything on the spine needs an exit.
            # A rib is the better landing - it earns XP while the blocked step waits - the
            # one whose mobs suit the step's level, and the runtime picks again by the
            # character's own. The next node is always available, and skipping forward
            # beats standing still.
            rib = rib_for(ribs, n.level[0])
            escape = rib.id if rib else (nxt[0] if nxt else None)
            if escape:
                # `QUEST_MISSING` means "the quest should be in the log and is not", so it
                # belongs on objectives and turn-ins only. On an *accept* node the quest
                # being absent is the entire reason the character is standing there, and
                # attaching it fires on the first tick after arrival, skipping every
                # quest in the graph before any of them can be taken. The accept-side
                # equivalent is `QUEST_NOT_OFFERED`, which needs attempts, not absence.
                wants_missing = n.quest_id and n.kind in (
                    StepKind.QUEST_OBJECTIVE, StepKind.QUEST_TURNIN)
                wants_not_offered = n.quest_id and n.kind is StepKind.QUEST_ACCEPT
                fails = (
                    FailEdge(when=FailWhen.TIMEOUT, value=n.timeout_s, goto=escape),
                    FailEdge(when=FailWhen.DEATHS, value=3, goto=escape),
                    *((FailEdge(when=FailWhen.QUEST_MISSING, goto=escape),)
                      if wants_missing else ()),
                    *((FailEdge(when=FailWhen.QUEST_NOT_OFFERED, goto=escape),)
                      if wants_not_offered else ()),
                )
        wired.append(n.model_copy(update={"next": nxt, "on_fail": fails}))

    # A node with no position cannot be travelled to, so servicing it means standing
    # still until it times out. Quest 7961 sits at position two of the Human spine with
    # no giver in the database at all: at the default 240 s that is eight minutes of a
    # fresh character doing nothing before the escape edge fires. Mark it skippable and
    # give it seconds rather than minutes — the step is recorded as existing on this
    # server, which is the point of emitting it, and the playhead moves past immediately.
    final = tuple(
        n.model_copy(update={"skippable": True, "timeout_s": min(n.timeout_s, 5.0)})
        if n.pos is None and n.kind is not StepKind.GRIND else n
        for n in wired
    )
    coordinate_id = coordinate_bounds.area_id if coordinate_bounds else None
    final = tuple(n.model_copy(update={"coord_zone_id": coordinate_id}) for n in final)
    entry = chain[0] if chain else (final[0].id if final else "")
    if unplaceable:
        print(f"  dropped {len(unplaceable)} quest(s) with no giver spawn on this server: "
              + ", ".join(unplaceable[:6]) + ("..." if len(unplaceable) > 6 else ""))
    return Graph(graph_id=graph_id, faction=faction, nodes=final, entry=entry,
                 coord_zone_id=coordinate_id)
