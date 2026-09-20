"""The tracker — mechanical, free, and the reason the coach is cheap.

Runs at 250 ms on every client (PLAN §7.5). It answers the questions that have answers:
is this step done, are we off route, has it timed out, is the quest even available. Those
are predicates and distances, not judgement, and resolving them here is what keeps
`unresolved/h` — the number that actually matters (`ARCHITECTURE.md` §1) — small.

Anything the tracker can settle must never reach the coach, and anything the coach can
settle must never reach the teacher. Each rung that leaks work downward costs real money.

The tracker **never advances a dead character.** Death is its own event and the recovery
pipeline owns it; advancing the playhead over a corpse loses the step that killed it,
which is the one worth knowing about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from jev.guide.graph import FailEdge, FailWhen, Graph, Node
from jev.world.state_v1 import State, StepKind


class Event(StrEnum):
    NONE = "none"
    ADVANCE = "advance"          # the predicate fired; move to next
    FAIL = "fail"                # an on_fail edge matched; go where it says
    OFF_ROUTE = "off_route"      # too far from the node for too long
    DEATH = "death"              # do not advance, do not plan
    BLOCKED = "blocked"          # no edge applies; this needs the coach
    ARRIVED = "arrived"          # inside the node radius, predicate not yet true


@dataclass
class StepMemory:
    """Per-step memory. The tracker is otherwise pure.

    `quest_was_in_log` is why this exists at all: a turn-in completes when the quest
    *leaves* the log, which is indistinguishable from never having taken it unless
    something remembers. A predicate with no memory would report every turn-in as
    complete before the quest was accepted.
    """

    step_id: str
    entered_at: float
    quest_was_in_log: bool = False
    off_route_since: float | None = None
    deaths: int = 0
    attempts: int = 0
    arrived: bool = False
    level_at_entry: int | None = None
    xp_at_entry: float | None = None


@dataclass(frozen=True)
class Verdict:
    event: Event
    goto: str | None = None
    reason: str = ""
    # Off-route seconds accumulated, so the reward function does not have to
    # re-derive what the tracker already measured.
    off_route_s: float = 0.0


@dataclass
class Tracker:
    """One playhead over one graph, for one client."""

    graph: Graph
    step_id: str
    memory: StepMemory = field(init=False)
    off_route_grace_s: float = 20.0

    def __post_init__(self) -> None:
        self.memory = StepMemory(step_id=self.step_id, entered_at=0.0)

    # -- playhead ------------------------------------------------------------

    def enter(self, step_id: str, state: State) -> None:
        self.step_id = step_id
        self.memory = StepMemory(
            step_id=step_id,
            entered_at=state.t,
            quest_was_in_log=_quest_in_log(state, self._node(step_id)),
            level_at_entry=state.char.level,
            xp_at_entry=state.char.xp_pct,
        )

    def _node(self, step_id: str | None = None) -> Node | None:
        return self.graph.get(step_id or self.step_id)

    # -- the 250 ms tick -----------------------------------------------------

    def tick(self, state: State) -> Verdict:
        node = self._node()
        if node is None:
            return Verdict(Event.BLOCKED, reason=f"step {self.step_id!r} is not in the graph")

        # Death first, unconditionally. Every other answer is wrong while dead: the
        # predicate may even be satisfiable, and advancing would discard the step that
        # did the killing.
        if state.vitals.dead is True or state.vitals.ghost is True:
            return Verdict(Event.DEATH, reason="dead or ghost")

        age = state.t - self.memory.entered_at
        off_route_s = self._update_off_route(state, node)

        if _predicate(state, node, self.memory):
            return Verdict(Event.ADVANCE, goto=_next_of(node), reason=f"{node.kind} satisfied")

        fail = self._match_fail(state, node, age)
        if fail is not None:
            return Verdict(Event.FAIL, goto=fail.goto,
                           reason=f"{fail.when}={fail.value}", off_route_s=off_route_s)

        if off_route_s > self.off_route_grace_s:
            return Verdict(Event.OFF_ROUTE, reason=f"{off_route_s:.0f}s off route",
                           off_route_s=off_route_s)

        if self.memory.arrived:
            return Verdict(Event.ARRIVED, reason="in position, predicate not yet true",
                           off_route_s=off_route_s)

        return Verdict(Event.NONE, off_route_s=off_route_s)

    # -- internals -----------------------------------------------------------

    def _update_off_route(self, state: State, node: Node) -> float:
        """Distance from the node, in map fractions, with a grace period.

        Measured in map fractions rather than yards because that is what the radio can
        see. Unknown position is not off route — a blind character is not a lost one, and
        treating it as lost would fire the moment a loading screen blanked the readout.
        """
        if node.pos is None or state.pos.mx is None or state.pos.my is None:
            self.memory.off_route_since = None
            return 0.0

        dx, dy = state.pos.mx - node.pos[0], state.pos.my - node.pos[1]
        dist = (dx * dx + dy * dy) ** 0.5

        if dist <= node.r:
            self.memory.arrived = True
            self.memory.off_route_since = None
            return 0.0

        # A travel-shaped step is *expected* to be far away; that is the point of it.
        if node.kind is StepKind.TRAVEL or not self.memory.arrived:
            self.memory.off_route_since = None
            return 0.0

        if self.memory.off_route_since is None:
            self.memory.off_route_since = state.t
        return state.t - self.memory.off_route_since

    def _match_fail(self, state: State, node: Node, age: float) -> FailEdge | None:
        for edge in node.on_fail:
            if _fail_matches(edge, state, node, self.memory, age):
                return edge
        # A step past its timeout with no edge to take is not a failure the tracker can
        # resolve. It is exactly the sort of thing the coach exists for.
        return None

    def is_blocked(self, state: State) -> bool:
        node = self._node()
        if node is None:
            return True
        age = state.t - self.memory.entered_at
        return age > node.timeout_s and self._match_fail(state, node, age) is None


# --------------------------------------------------------------------------- predicates


def _quest_in_log(state: State, node: Node | None) -> bool:
    if node is None or node.quest_id is None:
        return False
    return any(q.quest_id == node.quest_id for q in state.quests)


def _quest_complete(state: State, node: Node) -> bool:
    for q in state.quests:
        if q.quest_id == node.quest_id:
            if q.complete is True:
                return True
            return bool(q.objectives) and all(o.done for o in q.objectives)
    return False


def _in_radius(state: State, node: Node) -> bool:
    if node.pos is None or state.pos.mx is None or state.pos.my is None:
        return False
    dx, dy = state.pos.mx - node.pos[0], state.pos.my - node.pos[1]
    return (dx * dx + dy * dy) ** 0.5 <= node.r


def _predicate(state: State, node: Node, mem: StepMemory) -> bool:
    """Is this step done? One answer per node kind, and no kind gets a default `True`."""
    match node.kind:
        case StepKind.QUEST_ACCEPT:
            return _quest_in_log(state, node)

        case StepKind.QUEST_OBJECTIVE:
            return _quest_complete(state, node)

        case StepKind.QUEST_TURNIN:
            # Gone from the log, having once been in it. Without the memory this reads
            # as complete before the quest was ever accepted.
            return mem.quest_was_in_log and not _quest_in_log(state, node)

        case StepKind.TRAVEL | StepKind.HEARTH | StepKind.FLIGHT | StepKind.BOAT:
            return _in_radius(state, node)

        case StepKind.GRIND:
            # A rib ends on the level it was hung off, or when the step that sent us here
            # became satisfiable again — the caller re-checks that, not the rib.
            return state.char.level is not None and state.char.level >= node.level[1]

        case StepKind.VENDOR | StepKind.REPAIR:
            free, dur = state.bags.free, state.bags.durability_min
            # Unknown is not success. A vendor step that "completes" because nothing read
            # the bags leaves a full inventory and an unfixed weapon.
            return free is not None and free >= 6 and dur is not None and dur > 0.6

        case StepKind.TRAIN:
            # Best effort: the client cannot reliably confirm a rank was learned, so this
            # relies on the timeout-and-skip edge rather than claiming success.
            return False

        case StepKind.DING_GATE:
            return state.char.level is not None and state.char.level >= node.level[1]

        case StepKind.MONEY_GATE:
            need = int(node.xp_est or 0)
            return state.bags.money_copper is not None and state.bags.money_copper >= need

        case StepKind.SKIPPABLE_GATE:
            return True

        case _:
            return False


def _fail_matches(edge: FailEdge, state: State, node: Node,
                  mem: StepMemory, age: float) -> bool:
    match edge.when:
        case FailWhen.TIMEOUT:
            return age > (edge.value or node.timeout_s)
        case FailWhen.DEATHS:
            return mem.deaths >= (edge.value or 3)
        case FailWhen.ATTEMPTS:
            return mem.attempts >= (edge.value or 3)
        case FailWhen.QUEST_MISSING:
            # Only once we are standing where the quest lives. Firing this from across
            # the zone would skip every quest before the character arrived.
            return mem.arrived and node.quest_id is not None and not _quest_in_log(state, node)
        case FailWhen.QUEST_NOT_OFFERED:
            return mem.arrived and mem.attempts >= 2 and not _quest_in_log(state, node)
        case FailWhen.LEVEL_BELOW:
            return state.char.level is not None and state.char.level < (edge.value or 0)
        case FailWhen.GOLD_BELOW:
            return (state.bags.money_copper is not None
                    and state.bags.money_copper < (edge.value or 0))
    return False


def _next_of(node: Node) -> str | None:
    return node.next[0] if node.next else None
