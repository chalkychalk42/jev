"""The GuideGraph — the curriculum the coach services.

The coach does not search Azeroth. It services the next node (PLAN §7.1). That is what
turns 1-70 from a planning problem into a content problem, and it is why the intelligence
budget in `ARCHITECTURE.md` §1 can be small.

Deviation from PLAN §7.3: `on_fail` conditions are **structured, not strings**. The plan
writes them as `{"if": "timeout_s > 240", "goto": ...}`, which needs either `eval` or a
parser. Both are a poor trade here: the condition set is tiny and closed, the tracker has
to dispatch on it anyway, and a typo in a string expression becomes a silent
never-fires rather than a load error. So the condition is an enum and a threshold, and a
bad one fails when the graph loads.

A graph validates on construction. Every `next`, `requires` and `goto` must name a node
that exists, because a dangling edge is a character standing still in a field with nothing
to do and no error to explain it.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from collections import Counter
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jev.world.combat import grey_level, kill_xp
from jev.world.state_v1 import StepKind

GRAPH_SCHEMA = 3


class ObjectiveTarget(BaseModel):
    """One source requirement and its destination, never inferred from display prose.

    ``source_slot`` is one-based in quest_template. ``counter_index`` is zero-based in
    GetQuestLogLeaderBoard; absent means this requirement cannot safely be joined to a
    painted counter. A missing location/action remains an explicit capability gap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["kill", "loot", "interact", "delivery", "explore", "spell", "event"]
    required_id: int | None = None
    required_count: int | None = Field(default=None, ge=1)
    source_slot: int | None = Field(default=None, ge=1, le=4)
    counter_index: int | None = Field(default=None, ge=0)
    target_id: int | None = None
    target_name: str | None = None
    target_kind: Literal["creature", "gameobject"] | None = None
    pos: tuple[float, float] | None = None
    world: tuple[float, float, float] | None = None
    map_id: int | None = None
    coord_zone_id: int | None = None
    hunt_yards: float | None = None
    blocked_reason: str | None = None


class FailWhen(StrEnum):
    """Why a step gives up. Closed set, dispatched on by the tracker."""

    TIMEOUT = "timeout_s"              # value = seconds on the step
    DEATHS = "deaths_on_step"          # value = deaths
    QUEST_MISSING = "quest_missing"    # the quest is not in the log and cannot be taken
    QUEST_NOT_OFFERED = "quest_not_offered"  # the giver has nothing for us
    LEVEL_BELOW = "level_below"        # value = level; we are under-levelled for this
    GOLD_BELOW = "gold_below"          # value = copper; a money gate we cannot pay
    ATTEMPTS = "attempts"              # value = arm-and-fail cycles


class FailEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    when: FailWhen
    goto: str
    value: float | None = None

    @model_validator(mode="after")
    def _threshold_required_where_it_means_something(self) -> FailEdge:
        needs = {FailWhen.TIMEOUT, FailWhen.DEATHS, FailWhen.LEVEL_BELOW,
                 FailWhen.GOLD_BELOW, FailWhen.ATTEMPTS}
        if self.when in needs and self.value is None:
            raise ValueError(f"{self.when} needs a threshold value")
        return self


class Node(BaseModel):
    """One step. PLAN §7.3, with the on_fail change above."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: StepKind
    zone: str
    zone_id: int
    level: tuple[int, int] = (1, 70)

    # Map fraction within `zone_id`, plus the world position it was derived from. Both
    # are kept: the tracker compares against what the addon can see (map fractions), and
    # a navmesh path needs yards. Deriving one from the other at every use is how the two
    # drift apart.
    pos: tuple[float, float] | None = None
    world: tuple[float, float, float] | None = None
    map_id: int | None = None
    coord_zone_id: int | None = None  # Area ID defining pos, independent of the quest's zone
    r: float = 0.03                    # arrival radius, in map fractions

    # How far a kill objective's mobs are spread, in **yards**, from the spawn cluster
    # this node was placed from. `None` on nodes that are a point you stand at.
    #
    # Deliberately not `r`, and deliberately a different unit. A hunt borrowed `r` once,
    # and `r` is the tracker's arrival slop in *map fractions* — 0.06 of a zone the size
    # of Northshire is two hundred and eight yards, which contains Northshire Abbey. The
    # bot walked into the Main Hall, stood facing a wall, and correctly reported that it
    # could not see any kobolds. The mesh had told the truth the whole way.
    hunt_yards: float | None = None
    # A grind rib's creatures' levels, lowest and highest, from the world database; its
    # `level` is the window it was chosen in, and names it. Not the same thing: Westfall's
    # Goretusks are 14-15 and their rib's window 14-16, and read as the creatures' levels a
    # level 15 character was too low for them (V323). `None` where not known (`rib_levels`).
    mob_levels: tuple[int, int] | None = None

    quest_id: int | None = None
    # Alternative prerequisite groups. Every quest in one group must be rewarded;
    # any group suffices (the server's positive prevQuests/exclusive-group semantics).
    quest_prerequisites: tuple[tuple[int, ...], ...] = ()
    route_blocked_reason: str | None = None
    # The quest's name as the client shows it, when this step has a quest.
    #
    # Carried as a fact rather than parsed back out of `objectives[0]`, because a gossip
    # line is matched by a hash of exactly this string and "turn in A Threat Within" is
    # not it. The guide has known the title since it was generated; it was only ever
    # formatted into a sentence and thrown away.
    title: str = ""
    npc_id: int | None = None
    # Explicit DB facts. Display notes are not a targeting contract, and a crate
    # must never be dispatched to the creature nameplate/ring locator.
    target_name: str | None = None
    target_kind: Literal["creature", "gameobject"] | None = None
    objective_targets: tuple[ObjectiveTarget, ...] = ()
    objectives: tuple[str, ...] = ()

    requires: tuple[str, ...] = ()
    next: tuple[str, ...] = ()
    on_fail: tuple[FailEdge, ...] = ()

    skills: tuple[str, ...] = ()
    xp_est: int | None = None
    timeout_s: float = 300.0
    skippable: bool = False
    notes: str = ""

    # Where this node came from, so a generated graph can be diffed against a regenerated
    # one and a hand edit is visible as such.
    source: str = "generated"


class Graph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_id: str
    schema_version: int = GRAPH_SCHEMA
    faction: str
    coord_zone_id: int | None = None  # All generated node fractions use this pinned area map
    nodes: tuple[Node, ...]
    entry: str

    @model_validator(mode="after")
    def _every_edge_points_somewhere(self) -> Graph:
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes):
            dupes = [n.id for n in self.nodes if sum(m.id == n.id for m in self.nodes) > 1]
            raise ValueError(f"duplicate node ids: {sorted(set(dupes))[:5]}")
        if self.entry not in ids:
            raise ValueError(f"entry {self.entry!r} is not a node")
        for node in self.nodes:
            if (self.coord_zone_id is not None and node.coord_zone_id is not None
                    and self.coord_zone_id != node.coord_zone_id):
                raise ValueError(f"node {node.id} uses a different coordinate frame from its graph")
            for target in node.objective_targets:
                if (target.coord_zone_id is not None and node.coord_zone_id is not None
                        and target.coord_zone_id != node.coord_zone_id):
                    raise ValueError(f"objective on {node.id} uses a different coordinate frame")

        dangling: list[str] = []
        for n in self.nodes:
            for ref in (*n.next, *n.requires, *(e.goto for e in n.on_fail)):
                if ref not in ids:
                    dangling.append(f"{n.id} -> {ref}")
        if dangling:
            # A dangling edge is a character standing in a field with nothing to do and
            # no error that explains it. Refuse at load, not at 3am in a live run.
            raise ValueError(f"{len(dangling)} dangling edges, first: {dangling[:3]}")
        return self

    def by_id(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def get(self, node_id: str) -> Node | None:
        return self.by_id().get(node_id)

    def ribs(self) -> tuple[Node, ...]:
        """Grind loops hanging off the spine — what `on_fail` falls back to."""
        return tuple(n for n in self.nodes if n.kind is StepKind.GRIND)

    def rib_for(self, level: int | None, preferred: Node | None = None,
                near: tuple[float, float] | None = None, short: bool = False, *,
                barred: frozenset[str] = frozenset(), key: int | str | None = None) -> Node | None:
        """The rib whose mobs suit a character of `level`. See `rib_for`."""
        return rib_for(self.ribs(), level, preferred, near, short, barred=barred, key=key,
                       scale=frame_yards(self.nodes))

    def unreachable(self) -> tuple[str, ...]:
        """Nodes no edge leads to. Not an error — a rib is reached only on failure — but
        a node unreachable from anywhere is dead content and worth reporting."""
        reached = {self.entry}
        for n in self.nodes:
            reached.update(n.next)
            reached.update(e.goto for e in n.on_fail)
        return tuple(sorted({n.id for n in self.nodes} - reached))

    def save(self, path: str | pathlib.Path) -> None:
        data = self.model_dump(mode="json")
        for node in data["nodes"]:
            # Unknown creature levels are not written: a guide's bytes are the tutor's
            # knowledge fingerprint, and the field is read when a guide is played (V323).
            if node.get("mob_levels") is None:
                node.pop("mob_levels", None)
        pathlib.Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def load(path: str | pathlib.Path) -> Graph:
        return Graph.model_validate_json(pathlib.Path(path).read_text(encoding="utf-8"))


class GraphStats(BaseModel):
    """What a generated graph actually contains. Printed after generation, because a
    graph that silently generated eleven nodes for a whole zone is the failure mode."""

    graph_id: str
    nodes: int
    by_kind: dict[str, int]
    levels: tuple[int, int]
    with_position: int
    unreachable: int
    quests: int


# How far above the character a rib's creatures may be and still be a grind for it (V323). A kill
# pays five in a hundred more a level above (`kill_xp`), and each is harder: the level 12 mage
# died five times in its first 35 minutes among Westfall's 13-15 Riverpaws (V276), and a level 3
# among level 5-6 boars (run 20260923T174132-d01302). One level is the next level's creatures,
# which the character meets at every ding anyway.
RIB_LEVELS_ABOVE = 1
# The ribs paying within this share of the best a kill are as good, and the nearest of them is
# taken: a level 5 character failed out of Echo Ridge Mine into the wolves 1,200 yards away,
# the kobolds beside the mine passed by (run 20260924T015701-2417ae).
RIB_XP_SHARE = 0.9
# Characters spread over the ribs paying within this share of the best a kill and no more than
# `RIB_SPREAD_YARDS` further than the nearest of them, each taking the one its own hash puts
# first (`spread_rank`, V332). Replayed on the hive's rib time of 29 Sep 03:00-09:30 with each
# window's three creatures, 85% put no rib above 14 characters at once (38 on Durotar's Scorpid
# Workers as played) at 94.6% of the best a kill; 90%, V323's band, spread only creatures of the
# same levels, and 30 stood on Durotar's level 6-7 Dire Mottled Boars, the only rib of a level 6
# within it. The distance hardly mattered: 600 and 2,000 yards spread alike.
RIB_SPREAD_SHARE = 0.85
RIB_SPREAD_YARDS = 600.0
# The furthest a short rib is from where its step failed (V330): a minute and a quarter at a run
# each way, most of its five minutes grinding. Its median was 822 yards in the hive's 95 short
# ribs from 11:00 to 15:20 on 29 Sep, and 38% were over 1,000.
SHORT_RIB_YARDS = 500.0


def window_rib(ribs, level: int) -> Node | None:
    """The rib whose window a step's level has reached: of the windows starting at or below it,
    the highest; below every one, the lowest. For wiring a guide's fail edges, which name a rib
    the runtime chooses again by the character's own level (`rib_for`)."""
    ribs = tuple(ribs)
    if not ribs:
        return None
    fitting = [r for r in ribs if r.level[0] <= level]
    if not fitting:
        return min(ribs, key=lambda r: r.level[0])
    top = max(r.level[0] for r in fitting)
    return next(r for r in fitting if r.level[0] == top)


def rib_levels(rib: Node) -> tuple[int, int]:
    """A rib's creatures' levels (`Node.mob_levels`); without them, its window, whose top no
    creature of it is above (`WorldDB.grind_clusters`)."""
    return rib.mob_levels or rib.level


def rib_xp(rib: Node, level: int) -> float:
    """What a kill on this rib pays a character of `level`, on average over its creatures'
    levels (`kill_xp`)."""
    low, high = rib_levels(rib)
    return sum(kill_xp(level, m) for m in range(low, high + 1)) / (high - low + 1)


def rib_fits(rib: Node, level: int) -> bool:
    """Do this rib's creatures suit a character of `level` (V323): none above
    `RIB_LEVELS_ABOVE` over it, and some worth experience to it."""
    top = rib_levels(rib)[1]
    return grey_level(level) < top <= level + RIB_LEVELS_ABOVE


def rib_within(rib: Node, level: int) -> bool:
    """The furthest a grind may be from a character of `level` (V329): its lowest creature at
    most `RIB_LEVELS_ABOVE` above it, and its highest not grey to it."""
    low, top = rib_levels(rib)
    return grey_level(level) < top and low <= level + RIB_LEVELS_ABOVE


def frame_yards(nodes) -> tuple[float, float] | None:
    """Yards a whole map fraction spans along each of a guide's frame's axes
    (`coords.to_yards`), from its nodes: a node's `pos` is its `world` in the guide's frame on
    the frame's map (`generate._generate`'s `place`, `hive.convert`), so the box is read off them
    as the line through them. `None` with too few placed nodes, or when they are not on one line
    (no single frame), when the guide's map fractions stand for themselves."""
    placed = [n for n in nodes if n.pos is not None and n.world is not None and n.map_id is not None]
    if len(placed) < 2:
        return None
    frame_map = Counter(n.map_id for n in placed).most_common(1)[0][0]
    points = [(n.pos, n.world) for n in placed if n.map_id == frame_map]

    def span(fractions, yards) -> float | None:
        mean_f, mean_y = sum(fractions) / len(fractions), sum(yards) / len(yards)
        var = sum((f - mean_f) ** 2 for f in fractions)
        if var < 1e-6:
            return None
        slope = sum((f - mean_f) * (y - mean_y) for f, y in zip(fractions, yards)) / var
        off = max(abs(y - mean_y - slope * (f - mean_f)) for f, y in zip(fractions, yards))
        return abs(slope) if off <= 1.0 else None     # a yard off is another frame

    # The map's horizontal axis is world Y, its vertical world X (`coords.world_to_map`).
    across = span([p[0] for p, _ in points], [w[1] for _, w in points])
    down = span([p[1] for p, _ in points], [w[0] for _, w in points])
    return None if across is None or down is None else (across, down)


def _yards(a: tuple[float, float], b: tuple[float, float],
           scale: tuple[float, float] | None) -> float:
    """Between two of a guide's map positions, in yards; in map fractions without a frame."""
    if scale is None:
        return math.dist(a, b)
    return math.hypot((a[0] - b[0]) * scale[0], (a[1] - b[1]) * scale[1])


def spread_rank(key: int | str, rib_id: str) -> int:
    """Where `rib_id` stands for the character `key` among ribs as good as each other: a hash of
    the two, the same in every process and session, so a character keeps its rib as others come
    and go and characters spread over the ribs (V332)."""
    digest = hashlib.blake2b(f"{key}|{rib_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _best(fit: list[Node], level: int, preferred: Node | None, near, key,
          scale) -> Node:
    """Of ribs that fit: those paying within `RIB_XP_SHARE` of the best a kill, the nearest;
    with `key`, of those within `RIB_SPREAD_SHARE` and no more than `RIB_SPREAD_YARDS` further
    than the nearest of them, the one its `spread_rank` puts first; unplaced, `preferred`, else
    the best."""
    pay = {r.id: rib_xp(r, level) for r in fit}
    best = max(pay.values())
    good = [r for r in fit
            if pay[r.id] >= (RIB_XP_SHARE if key is None else RIB_SPREAD_SHARE) * best]
    placed = [r for r in good if r.pos is not None]
    if near is not None and placed:
        far = {r.id: _yards(r.pos, near, scale) for r in placed}
        nearest = min(placed, key=lambda r: (far[r.id], -pay[r.id]))
        if key is None:
            return nearest
        reach = far[nearest.id] + (RIB_SPREAD_YARDS if scale is not None else math.inf)
        return max((r for r in placed if far[r.id] <= reach),
                   key=lambda r: spread_rank(key, r.id))
    if key is not None:
        return max(good, key=lambda r: spread_rank(key, r.id))
    if preferred is not None and preferred in good:
        return preferred
    return max(good, key=lambda r: pay[r.id])


def _short(fit: list[Node], level: int, near, scale) -> Node | None:
    """Of ribs that fit: the nearest of those paying within `RIB_XP_SHARE` of the best a kill and
    no further than `SHORT_RIB_YARDS` (V330); `None` when none is that near."""
    pay = {r.id: rib_xp(r, level) for r in fit}
    best = max(pay.values())
    close = [r for r in fit if r.pos is not None and pay[r.id] >= RIB_XP_SHARE * best
             and (scale is None or _yards(r.pos, near, scale) <= SHORT_RIB_YARDS)]
    if not close:
        return None
    return min(close, key=lambda r: (_yards(r.pos, near, scale), -pay[r.id]))


def rib_for(ribs, level: int | None, preferred: Node | None = None,
            near: tuple[float, float] | None = None, short: bool = False, *,
            barred: frozenset[str] = frozenset(), key: int | str | None = None,
            scale: tuple[float, float] | None = None) -> Node | None:
    """The grind for a character of `level`: of the ribs not `barred` at the level, those whose
    creatures suit it (`rib_fits`), else of those barred (V329); the one paying the most a kill
    (`rib_xp`), or of those within `RIB_XP_SHARE` of it the nearest to where it is (`near`, the
    guide's map fractions), spread by the character (`key`, `spread_rank`) over those as near
    (V332); unplaced, `preferred`, else the best. With none that suits it, the one of the lowest
    creatures within `rib_within`; with none of those, `None`: the character goes on along the
    spine. A rib the route leaves out (`route_blocked_reason`, V331) is none. `preferred` is the
    answer when the level is unknown. Distances are in yards on the guide's frame (`scale`,
    `frame_yards`, read off `ribs` when not given), or map fractions without one, when neither
    `RIB_SPREAD_YARDS` nor `SHORT_RIB_YARDS` applies.

    By its creatures' levels (`rib_levels`), not its window's top (V323): the window was read
    as the creatures' levels and "never above the character", and Westfall's 14-16 window,
    Goretusks of 14-15 at about 105 experience a kill, was above a level 15 character, which
    was given the 12-14 window's level 12-13 Kobold Diggers at about 54: the live mage ground
    them 197 minutes at 15. Before, the nearest rib starting up to three levels below won.

    Never one above the character or grey to it (V329): with every rib that suited it barred,
    the lowest of the ribs left was taken, and hive-200, a level 8 dwarf hunter, went from Dun
    Morogh's 1-3 rib, grey to it, up the 9-11, 11-13 and 13-15 ribs of Dun Morogh and Loch Modan
    to Loch Modan's 17-19 and Dun Morogh's 19-20, dying on most, 11:42 to 15:02 on 29 Sep; 12%
    of the hive's rib time from 11:00 to 15:20 was on a rib above the character or grey to it,
    each time with a rib that suited it barred.

    A short rib (`short`, `jev.guide.tracker.SHORT_RIB_S`) is a wait for a respawn, and its
    minutes run from the failure: of the ribs that suit the character, the nearest of those
    paying within `RIB_XP_SHARE` of the best, and none further than `SHORT_RIB_YARDS`; `None`,
    the step again at once, when there is none so near (V330). At level 11 the only rib in the
    band above was 1,550 yards from Goldshire, where the inn's steps failed: five minutes was
    four of walking there and four back (sessions 109 to 111)."""
    ribs = tuple(r for r in ribs if not r.route_blocked_reason)
    if not ribs:
        return None
    if level is None:
        return preferred if preferred is not None and not preferred.route_blocked_reason else ribs[0]
    if scale is None:
        scale = frame_yards(ribs)
    free = [r for r in ribs if r.id not in barred]
    held = [r for r in ribs if r.id in barred]
    for tier in (free, held):
        fit = [r for r in tier if rib_fits(r, level)]
        if not fit:
            continue
        if short and near is not None:
            rib = _short(fit, level, near, scale)
            if rib is not None:
                return rib
            continue
        return _best(fit, level, preferred, near, key, scale)
    if short and near is not None:
        return None
    within = [r for r in ribs if rib_within(r, level)]
    if not within:
        return None
    return min(within, key=lambda r: (r.id in barred, rib_levels(r)[1], rib_levels(r)[0]))


def stats(g: Graph) -> GraphStats:
    kinds: dict[str, int] = {}
    for n in g.nodes:
        kinds[n.kind.value] = kinds.get(n.kind.value, 0) + 1
    lows = [n.level[0] for n in g.nodes] or [0]
    highs = [n.level[1] for n in g.nodes] or [0]
    return GraphStats(
        graph_id=g.graph_id,
        nodes=len(g.nodes),
        by_kind=dict(sorted(kinds.items())),
        levels=(min(lows), max(highs)),
        with_position=sum(1 for n in g.nodes if n.pos is not None),
        unreachable=len(g.unreachable()),
        quests=len({n.quest_id for n in g.nodes if n.quest_id}),
    )
