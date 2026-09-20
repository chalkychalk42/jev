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
import pathlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from jev.world.state_v1 import StepKind

GRAPH_SCHEMA = 1


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
    r: float = 0.03                    # arrival radius, in map fractions

    quest_id: int | None = None
    npc_id: int | None = None
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
