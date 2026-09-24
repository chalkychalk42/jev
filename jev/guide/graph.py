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

import json
import math
import pathlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    def for_level(self, level: int) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if n.level[0] <= level <= n.level[1])

    def ribs(self) -> tuple[Node, ...]:
        """Grind loops hanging off the spine — what `on_fail` falls back to."""
        return tuple(n for n in self.nodes if n.kind is StepKind.GRIND)

    def rib_for(self, level: int | None, preferred: Node | None = None,
                near: tuple[float, float] | None = None) -> Node | None:
        """The rib whose mobs suit a character of `level`. See `rib_for`."""
        return rib_for(self.ribs(), level, preferred, near)

    def unreachable(self) -> tuple[str, ...]:
        """Nodes no edge leads to. Not an error — a rib is reached only on failure — but
        a node unreachable from anywhere is dead content and worth reporting."""
        reached = {self.entry}
        for n in self.nodes:
            reached.update(n.next)
            reached.update(e.goto for e in n.on_fail)
        return tuple(sorted({n.id for n in self.nodes} - reached))

    def save(self, path: str | pathlib.Path) -> None:
        pathlib.Path(path).write_text(
            json.dumps(self.model_dump(mode="json"), indent=2), encoding="utf-8"
        )

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


# How far below the character a rib's window may start and still be worth a detour.
RIB_LEVELS_BELOW = 2


def rib_for(ribs, level: int | None, preferred: Node | None = None,
            near: tuple[float, float] | None = None) -> Node | None:
    """The rib whose mobs suit a character of `level`: of the ribs whose level window
    starts at or below it, the highest; below every window, the lowest. `preferred` wins
    a tie, and is the answer when the level is unknown.

    Given where the character is (`near`, the guide's map fractions), the nearest rib whose
    window starts within `RIB_LEVELS_BELOW` of its level wins instead: a level 5 character
    failed out of Echo Ridge Mine into the level 5-7 wolves 1,200 yards away and was failed
    over again on the way, the level 3-5 kobolds beside the mine passed by (run
    20260924T015701-2417ae)."""
    ribs = tuple(ribs)
    if not ribs:
        return None
    if level is None:
        return preferred or ribs[0]
    if near is not None:
        # Mobs never above the character first: a level 6 paladin failed into the level 5-7
        # wolves and died there ten times in three sessions (runs 20260924T035309 to
        # ...042040); the level 3-5 kobolds were beside the abbey it had left.
        safe = [r for r in ribs if r.pos is not None and r.level[1] <= level
                and r.level[0] >= level - RIB_LEVELS_BELOW - 1]
        close = safe or [r for r in ribs if r.pos is not None
                         and level - RIB_LEVELS_BELOW <= r.level[0] <= level]
        if close:
            return min(close, key=lambda r: (math.dist(r.pos, near), -r.level[0]))
    fitting = [r for r in ribs if r.level[0] <= level]
    if not fitting:
        return min(ribs, key=lambda r: r.level[0])
    top = max(r.level[0] for r in fitting)
    best = [r for r in fitting if r.level[0] == top]
    return preferred if preferred in best else best[0]


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
