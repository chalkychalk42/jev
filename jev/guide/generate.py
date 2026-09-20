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
from dataclasses import dataclass

from jev.guide.coords import ZoneBounds, load_bounds, on_map, world_to_map
from jev.guide.graph import FailEdge, FailWhen, Graph, Node
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


# Spawn clustering, in yards. A cell wide enough that one camp lands in one or two
# buckets, a reach wide enough to gather a camp and not its neighbour.
CLUSTER_CELL = 60.0
CLUSTER_REACH = 150.0

# What a hunt is allowed to believe about a camp's size. The floor keeps a lone spawn
# searchable; the ceiling is the thing that stops this class of bug coming back, because
# no camp in the game is a hundred yards across and a disk that big is a zone.
#
# If a generated `hunt_yards` sits at the ceiling, the cluster query is wrong and the
# ceiling is hiding it. Fix the query.
HUNT_MIN_YARDS = 15.0
HUNT_MAX_YARDS = 50.0


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
    buckets: dict[tuple[int, int], list] = {}
    for row in rows:
        key = (int(row["px"] // CLUSTER_CELL), int(row["py"] // CLUSTER_CELL))
        buckets.setdefault(key, []).append(row)
    seed = max(buckets.values(), key=len)
    sx = sum(r["px"] for r in seed) / len(seed)
    sy = sum(r["py"] for r in seed) / len(seed)

    near = [r for r in rows
            if math.hypot(r["px"] - sx, r["py"] - sy) <= CLUSTER_REACH] or seed
    n = len(near)
    x = sum(r["px"] for r in near) / n
    y = sum(r["py"] for r in near) / n
    z = sum(r["pz"] for r in near) / n
    reaches = sorted(math.hypot(r["px"] - x, r["py"] - y) for r in near)
    spread = reaches[len(reaches) // 2] if reaches else 0.0
    return Spawn(npc_id, name, map_id, x, y, z, spread=spread)


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
                   Objectives, RequiredRaces,
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
                     r["px"], r["py"], r["pz"])

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

    def objective_spawn(self, quest_id: int) -> Spawn | None:
        """Where the objective actually happens.

        A kill objective's node belongs where the mobs are, not where the quest was
        taken. Uses the first required creature's densest spawn area: with ten wolves in
        a field, the centroid of that field is a place you can stand, unlike the centroid
        of an NPC's several spawns.

        The returned spawn carries the group's **spread**, which is what was missing. A
        hunt with no idea how big a camp is borrows the node's arrival radius instead, and
        that is in map fractions.
        """
        r = self.con.execute(
            "select ReqCreatureOrGOId1 as a from world_quest_template where entry = ?",
            (quest_id,),
        ).fetchone()
        if not r or not r["a"] or r["a"] <= 0:
            return None
        rows = self.con.execute(
            "select c.map, cast(c.position_x as real) px, cast(c.position_y as real) py, "
            "cast(c.position_z as real) pz, t.Name "
            "from world_creature c join world_creature_template t on t.Entry = c.id "
            "where c.id = ?", (r["a"],),
        ).fetchall()
        if not rows:
            return None
        m = rows[0]["map"]
        same = [x for x in rows if x["map"] == m]
        return _cluster(r["a"], same[0]["Name"] or "mobs", m, same)

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
        """Dense clusters of killable creatures in band, as grind ribs.

        "Killable" is a heuristic and named as one: normal rank, in level band, carrying
        loot, and **no NPC flags at all**. A creature that can be talked to, trained from
        or bought from is furniture, not prey. This will occasionally miss a valid grind
        mob and occasionally include a neutral critter; the rib is verified by walking it
        (V5), which is exactly the half the DB cannot answer.
        """
        rows = self.con.execute(
            """
            select cast(c.position_x as real) x, cast(c.position_y as real) y,
                   cast(c.position_z as real) z, c.map, c.id, t.Name
            from world_creature c
            join world_creature_template t on t.Entry = c.id
            where c.map = ? and t.Rank = 0 and t.NpcFlags = 0
              and t.LootId > 0 and t.MinLevel >= ? and t.MaxLevel <= ?
            """,
            (zone_bounds.map_id, level_min, level_max),
        ).fetchall()

        # Bucket into a coarse grid over the zone map, then take the fullest buckets.
        # Coarse on purpose: a rib is a loop you walk, not a pin you stand on.
        buckets: dict[tuple[int, int], list] = defaultdict(list)
        for r in rows:
            frac = world_to_map(r["x"], r["y"], zone_bounds)
            if not frac or not on_map(*frac, slack=0.0):
                continue
            buckets[(int(frac[0] * 8), int(frac[1] * 8))].append(r)

        best = sorted(buckets.values(), key=len, reverse=True)[:limit]
        out: list[tuple[Spawn, int]] = []
        for group in best:
            if len(group) < 6:      # a "cluster" of four wolves is not a grind rib
                continue
            # Same clustering as a kill objective's, so a rib carries a real reach
            # rather than the floor. `x`/`y`/`z` here, `px`/`py`/`pz` there.
            rekeyed = [{"px": g["x"], "py": g["y"], "pz": g["z"]} for g in group]
            out.append((
                _cluster(group[0]["id"], group[0]["Name"] or "mobs",
                         group[0]["map"], rekeyed),
                len(group),
            ))
        return out


# --------------------------------------------------------------------------- generation


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
) -> Graph:
    """Build a spine plus ribs and services for one faction over one level band."""
    db = WorldDB(db_path)
    try:
        return _generate(db, graph_id=graph_id, faction=faction, zone_ids=zone_ids,
                         zone_names=zone_names, level_min=level_min,
                         level_max=level_max, max_quests=max_quests)
    finally:
        db.close()


def _generate(db: WorldDB, *, graph_id: str, faction: str, zone_ids: tuple[int, ...],
              zone_names: dict[int, str], level_min: int, level_max: int,
              max_quests: int | None) -> Graph:
    nodes: list[Node] = []
    prefix = graph_id
    # NPCs whose spawn exists but falls outside every zone box in scope. Recorded so a
    # positionless node says which of the two things happened — no spawn at all, or a
    # spawn we could not map — because they need different fixes.
    unplaced: set[int] = set()

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
                return frac, world, spawn.map_id
        unplaced.add(spawn.npc_id)
        return None, world, spawn.map_id

    # -- ribs first, so quest nodes have somewhere to fail to -------------------
    rib_for_zone: dict[int, str] = {}
    for zid in zone_ids:
        b = db.bounds.get(zid)
        if b is None or b.degenerate:
            continue
        for i, (spawn, count) in enumerate(db.grind_clusters(b, level_min, level_max)):
            frac, world, map_id = place(spawn, zid)
            rid = f"{prefix}_grind_{_slug(zone_names.get(zid, str(zid)), 12)}_{i}"
            nodes.append(Node(
                id=rid, kind=StepKind.GRIND, zone=zone_names.get(zid, str(zid)), zone_id=zid,
                level=(level_min, level_max), pos=frac, world=world, map_id=map_id,
                r=0.06,   # a rib is a loop you walk, not a point you stand on
                hunt_yards=hunt_yards(spawn),
                objectives=(f"grind {spawn.name}",),
                skills=("GRIND_UNTIL",), timeout_s=900.0, skippable=True,
                notes=f"{count} spawns of {spawn.name} clustered here; route not recorded",
            ))
            rib_for_zone.setdefault(zid, rid)

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
            for i, spawn in enumerate(db.services(b, flag, limit=1)):
                frac, world, map_id = place(spawn, zid)
                nodes.append(Node(
                    id=f"{prefix}_{kind.value}_{_slug(zname, 12)}_{i}",
                    kind=kind, zone=zname, zone_id=zid, level=(level_min, level_max),
                    pos=frac, world=world, map_id=map_id, npc_id=spawn.npc_id,
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

    chain: list[str] = []
    unplaceable: list[str] = []
    for q in quests:
        zid = q.zone_or_sort if q.zone_or_sort in zone_ids else zone_ids[0]
        zname = zone_names.get(zid, str(zid))
        base = f"{prefix}_{q.quest_id}_{_slug(q.title)}"
        rib = rib_for_zone.get(zid)
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

        needs_objective = any(c > 0 for c in q.req_counts)

        gfrac, gworld, gmap = place(giver, zid)
        nodes.append(Node(
            id=f"{base}_accept", kind=StepKind.QUEST_ACCEPT, zone=zname, zone_id=zid,
            level=band, pos=gfrac, world=gworld, map_id=gmap, quest_id=q.quest_id,
            npc_id=giver.npc_id if giver else None,
            title=q.title,
            objectives=(f"accept {q.title}",),
            skills=("TRAVEL_TO", "ACCEPT_QUEST"), timeout_s=240.0,
            xp_est=q.xp_est,
            notes=(_note(giver, gfrac, "giver")),
        ))
        chain.append(f"{base}_accept")

        if needs_objective:
            mobs = db.objective_spawn(q.quest_id)
            ofrac, oworld, omap = place(mobs or giver, zid)
            nodes.append(Node(
                id=f"{base}_do", kind=StepKind.QUEST_OBJECTIVE, zone=zname, zone_id=zid,
                level=band, pos=ofrac, world=oworld, map_id=omap, quest_id=q.quest_id,
                title=q.title, hunt_yards=hunt_yards(mobs),
                objectives=tuple(filter(None, [q.objectives[:120]])) or ("complete objectives",),
                skills=("TRAVEL_TO", "COMBAT_PROFILE", "LOOT"), timeout_s=600.0,
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
            title=q.title,
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
            # A rib is the better landing when the zone has one — it earns XP while the
            # blocked step waits — but the next node is always available, and skipping
            # forward beats standing still.
            rib = rib_for_zone.get(n.zone_id) or next(iter(rib_for_zone.values()), None)
            escape = rib or (nxt[0] if nxt else None)
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
    entry = chain[0] if chain else (final[0].id if final else "")
    if unplaceable:
        print(f"  dropped {len(unplaceable)} quest(s) with no giver spawn on this server: "
              + ", ".join(unplaceable[:6]) + ("..." if len(unplaceable) > 6 else ""))
    return Graph(graph_id=graph_id, faction=faction, nodes=final, entry=entry)
