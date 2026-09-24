"""Compile an explicit executable subset without pretending skipped quests were earned.

The generated graph remains the source catalog. A run may select this derivative with
an exclusion manifest, then keep the ordinary tracker/playhead/completion semantics.
No live quest is marked complete merely because its executor has not been built.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from jev.guide.graph import Graph, Node
from jev.world.state_v1 import StepKind


@dataclass(frozen=True)
class Exclusion:
    quest_id: int | None
    title: str
    node_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class RoutePlan:
    graph: Graph
    source_graph_id: str
    excluded: tuple[Exclusion, ...]


def _gap(node: Node, available_skills: frozenset[str],
         supported_objectives: frozenset[str]) -> str | None:
    if node.route_blocked_reason:
        return node.route_blocked_reason
    missing = set(node.skills) - available_skills
    if missing:
        return "missing executors: " + ", ".join(sorted(missing))
    if node.world is None or node.pos is None or node.map_id is None:
        return "destination is outside the placed route"
    if (node.kind in (StepKind.QUEST_ACCEPT, StepKind.QUEST_TURNIN)
            and (node.target_kind != "creature" or not node.target_name)):
        return "quest interaction needs a measured gameobject locator or creature identity"
    if node.kind is not StepKind.QUEST_OBJECTIVE:
        return None
    if not node.objective_targets:
        return "guide has no structured objective targets"
    for target in node.objective_targets:
        if target.blocked_reason:
            return target.blocked_reason
        if target.kind not in supported_objectives:
            return f"objective kind {target.kind} has no verified executor"
        if target.kind == "delivery":
            continue  # The accept frame supplies this item; positive complete flag advances it.
        if target.kind == "explore":
            if target.world is None or target.pos is None or target.map_id != node.map_id:
                return "objective destination is outside the placed route"
            continue  # Walked into; only the positive complete flag advances it.
        if target.counter_index is None or target.counter_index >= 3:
            return "objective cannot be joined to one of the three painted counters"
        if target.required_count is None or target.required_count > 126:
            return "objective count exceeds the radio's verified counter range"
        if target.world is None or target.pos is None or target.map_id != node.map_id:
            return "objective destination is outside the placed route"
        if target.target_kind != "creature" or not target.target_name:
            return "objective needs a measured gameobject locator or creature identity"
    return None


def compile_route(graph: Graph, *, available_skills: frozenset[str],
                  supported_objectives: frozenset[str] = frozenset({"kill", "loot", "delivery",
                                                                    "explore"}),
                  completed_quests: frozenset[int] = frozenset()) -> RoutePlan:
    """Retain executable quest chains in source order and report every exclusion.

    Prerequisites are alternative groups, as in the server: every member of one group
    must be available earlier in this sequential route, or explicitly already rewarded.
    Unsupported descendants are excluded to a fixed point, including dependencies
    outside this guide. Coordinates, objective targets and failure thresholds stay exact.
    """
    by_quest: dict[int, list[Node]] = defaultdict(list)
    for node in graph.nodes:
        if node.quest_id is not None:
            by_quest[node.quest_id].append(node)
    reasons: dict[int, str] = {}
    order = {qid: i for i, qid in enumerate(by_quest)}
    for qid, nodes in by_quest.items():
        gap = next((reason for n in nodes
                    if (reason := _gap(n, available_skills, supported_objectives))), None)
        if gap:
            reasons[qid] = gap

    changed = True
    while changed:
        changed = False
        available = set(by_quest) - reasons.keys()
        for qid, nodes in by_quest.items():
            if qid in reasons:
                continue
            prerequisites = nodes[0].quest_prerequisites
            if prerequisites and not any(
                all(p in completed_quests or (p in available and order[p] < order[qid])
                    for p in group) for group in prerequisites
            ):
                groups = " or ".join("+".join(map(str, group)) for group in prerequisites)
                reasons[qid] = f"prerequisite unavailable earlier in this route: {groups}"
                changed = True

    excluded = [Exclusion(qid, nodes[0].title, tuple(n.id for n in nodes), reasons[qid])
                for qid, nodes in by_quest.items() if qid in reasons]
    retained = []
    for node in graph.nodes:
        if node.quest_id is not None:
            if node.quest_id not in reasons:
                retained.append(node)
        elif (reason := _gap(node, available_skills, supported_objectives)):
            excluded.append(Exclusion(None, node.notes or node.id, (node.id,), reason))
        else:
            retained.append(node)
    retained_ids = {n.id for n in retained}
    # Source generations have a single quest spine; derive its order through next edges
    # so schema order is not accidentally promoted into routing authority.
    spine = []
    source = graph.by_id()
    cursor: str | None = graph.entry
    seen: set[str] = set()
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        node = source[cursor]
        if len(node.next) > 1:
            raise ValueError("supported-route compiler requires a single source spine")
        if node.id in retained_ids:
            spine.append(node.id)
        cursor = node.next[0] if node.next else None
    if cursor in seen:
        raise ValueError("supported-route compiler requires an acyclic source spine")
    if not spine:
        raise ValueError("no executable quest spine remains after capability checks")
    positions = {node_id: i for i, node_id in enumerate(spine)}
    wired = []
    for node in retained:
        if any(ref not in retained_ids for ref in node.requires):
            raise ValueError(f"retained node {node.id} requires an excluded node")
        if node.id in positions:
            i = positions[node.id]
            nxt = tuple(spine[i + 1:i + 2])
        else:
            nxt = tuple(ref for ref in node.next if ref in retained_ids)
        fails = tuple(edge for edge in node.on_fail if edge.goto in retained_ids)
        wired.append(node.model_copy(update={"next": nxt, "on_fail": fails}))
    compiled = Graph(graph_id=f"{graph.graph_id}.supported", schema_version=graph.schema_version,
                     faction=graph.faction, nodes=tuple(wired), entry=spine[0],
                     coord_zone_id=graph.coord_zone_id)
    return RoutePlan(compiled, graph.graph_id, tuple(excluded))
