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
    # Where to go when this step finishes, if that is not simply `next[0]`.
    #
    # Ribs need this and cannot get it from the graph. A rib is shared by every step in
    # its zone, so it has no single correct successor to hard-code — and a rib with no way
    # back is a character that grinds forever, which is exactly what the simulator did on
    # its first run: eight advances, then four hundred ticks on a boar.
    rejoin_to: str | None = None
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
    completed: bool = False  # a predicate succeeded; a timed-out rib rejoin did not


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

    def enter(self, step_id: str, state: State, rejoin_to: str | None = None) -> None:
        self.step_id = step_id
        self.memory = StepMemory(
            step_id=step_id,
            entered_at=state.t,
            rejoin_to=rejoin_to,
            quest_was_in_log=_quest_in_log(state, self._node(step_id)),
            level_at_entry=state.char.level,
            xp_at_entry=state.char.xp_pct,
        )

    @classmethod
    def resume(cls, graph: Graph, state: State, *, start: str | None = None,
               completed: frozenset[int] = frozenset()) -> Tracker:
        """A playhead placed on the first step the world does not already satisfy.

        Cold start: nothing knows how far a character got, so the chain is walked from the
        entry and each node is asked its own predicate. Using `enter`/`tick` rather than a
        fresh comparison is the point — a turn-in is only complete if the quest was seen
        in the log first, and a second rule written at the call site would forget that and
        walk straight past every turn-in in the guide.

        It stops at the first unsatisfied step, so a quest **turned in during an earlier
        session** looks identical to one never accepted: both are simply absent from the
        log. That needs completed-quest state on the strip to fix properly. Until then the
        result is re-offering a finished quest and failing at the NPC, which is loud.
        """
        tracker = cls(graph=graph, step_id=start or graph.entry)
        tracker.enter(tracker.step_id, state)
        if start is not None:
            # `start` means "the playhead had got this far", and a playhead only reaches a
            # turn-in by passing its accept — so the quest *was* held, whatever the log
            # says now. Without this seed a resume lands on a hand-in it completed last
            # run, finds the quest absent, cannot confirm a turn-in it did not witness,
            # and sits there forever.
            #
            # Only the start node is seeded. Nodes the scan advances into are entered
            # against the live log and judged on it, which is what stops this from
            # becoming "assume everything behind us is done".
            tracker.memory.quest_was_in_log = True
        for _ in range(len(graph.nodes) + 1):
            node = tracker._node()
            if node is not None and node.quest_id in completed:
                # 2.4.3 cannot be asked which quests are finished — `GetQuestsCompleted`
                # arrived in 3.0 — and the log cannot say either, because a quest handed
                # in and a quest never taken are both simply absent from it. So the answer
                # is carried rather than derived, and every step of a finished quest is
                # behind us regardless of what its own predicate makes of an empty log.
                goto = tracker._exit_of(node)
                if goto:
                    tracker.enter(goto, state)
                    continue
                return tracker
            verdict = tracker.tick(state)
            if verdict.event is not Event.ADVANCE or not verdict.goto:
                return tracker
            tracker.enter(verdict.goto, state)
        return tracker

    def _node(self, step_id: str | None = None) -> Node | None:
        return self.graph.get(step_id or self.step_id)

    def _exit_of(self, node: Node) -> str | None:
        """Where a satisfied step leads.

        A remembered rejoin point wins over the graph's own edge, because that is the only
        place the rib's caller is recorded.
        """
        return self.memory.rejoin_to or _next_of(node)

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

        self._backfill(state)
        age = state.t - self.memory.entered_at
        off_route_s = self._update_off_route(state, node)

        if _predicate(state, node, self.memory):
            return Verdict(Event.ADVANCE, goto=self._exit_of(node),
                           reason=f"{node.kind} satisfied", completed=node.kind is not StepKind.SKIPPABLE_GATE)

        fail = self._match_fail(state, node, age)
        if fail is not None:
            return Verdict(Event.FAIL, goto=fail.goto,
                           reason=f"{fail.when}={fail.value}", off_route_s=off_route_s)

        # A rib that has run its course rejoins even without having levelled. It is a
        # detour, not a destination, and the step that sent us here may well be passable
        # now that the character is better fed and better geared.
        if node.kind is StepKind.GRIND and age > node.timeout_s and self.memory.rejoin_to:
            return Verdict(Event.ADVANCE, goto=self.memory.rejoin_to,
                           reason="rib timed out; rejoin the spine", off_route_s=off_route_s)

        if off_route_s > self.off_route_grace_s:
            return Verdict(Event.OFF_ROUTE, reason=f"{off_route_s:.0f}s off route",
                           off_route_s=off_route_s)

        if self.memory.arrived:
            return Verdict(Event.ARRIVED, reason="in position, predicate not yet true",
                           off_route_s=off_route_s)

        return Verdict(Event.NONE, off_route_s=off_route_s)

    # -- internals -----------------------------------------------------------

    def _backfill(self, state: State) -> None:
        """Fill in entry facts that were unreadable when the step was entered.

        A step can be entered on a blind tick — a failed radio decode, a loading screen —
        and the entry level is then `None` forever, which silently changes the step's exit
        condition. A grind rib entered blind fell back from "gain one level" to "reach the
        top of the band", and a simulated run spent four hundred ticks on a boar because
        of it.

        First readable value wins. Later readings are not the entry state and must not
        overwrite it.
        """
        if self.memory.level_at_entry is None and state.char.level is not None:
            self.memory.level_at_entry = state.char.level
        if self.memory.xp_at_entry is None and state.char.xp_pct is not None:
            self.memory.xp_at_entry = state.char.xp_pct
        if not self.memory.quest_was_in_log:
            node = self._node()
            if node is not None and _quest_in_log(state, node):
                self.memory.quest_was_in_log = True

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
    """Is this node's quest in the log? `False` also for an unread log — see below."""
    if node is None or node.quest_id is None or state.quests is None:
        return False
    return any(q.quest_id == node.quest_id for q in state.quests)


def _quest_log_readable(state: State) -> bool:
    """Did anything actually read the quest log this tick?

    Predicates may treat an unread log as "not present" — that only delays a step. Fail
    edges may not: `QUEST_MISSING` firing because nobody looked would skip a perfectly
    good quest, permanently, on the strength of no evidence at all.
    """
    return state.quests is not None


def _quest_complete(state: State, node: Node) -> bool:
    for q in state.quests or ():
        if q.quest_id == node.quest_id:
            if q.complete is not None:
                return q.complete
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
            return (_quest_log_readable(state) and mem.quest_was_in_log
                    and not _quest_in_log(state, node))

        case StepKind.TRAVEL | StepKind.HEARTH | StepKind.FLIGHT | StepKind.BOAT:
            return _in_radius(state, node)

        case StepKind.GRIND:
            return _grind_done(state, node, mem)

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


def _grind_done(state: State, node: Node, mem: StepMemory) -> bool:
    """When is a grind rib finished?

    **One level gained**, when we know the level we arrived at. Not `level >= node.level[1]`:
    a 1-12 rib entered at level 3 would then run until level 12, which is not a detour,
    it is the rest of the game. A rib exists to unstick a step, and one level is enough
    to have changed the odds.

    Falling back to the band's top only when the entry level was never read keeps the old
    behaviour for a blind character, where something has to bound it.
    """
    if state.char.level is None:
        return False
    if mem.level_at_entry is not None:
        return state.char.level > mem.level_at_entry
    return state.char.level >= node.level[1]


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
            # Only once we are standing where the quest lives, and only on a log we
            # actually read. Firing from across the zone would skip every quest before
            # the character arrived; firing on an unread log would skip a good quest on
            # no evidence.
            return (mem.arrived and node.quest_id is not None
                    and _quest_log_readable(state) and not _quest_in_log(state, node))
        case FailWhen.QUEST_NOT_OFFERED:
            return (mem.arrived and mem.attempts >= 2
                    and _quest_log_readable(state) and not _quest_in_log(state, node))
        case FailWhen.LEVEL_BELOW:
            return state.char.level is not None and state.char.level < (edge.value or 0)
        case FailWhen.GOLD_BELOW:
            return (state.bags.money_copper is not None
                    and state.bags.money_copper < (edge.value or 0))
    return False


def _next_of(node: Node) -> str | None:
    return node.next[0] if node.next else None
