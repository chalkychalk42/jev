"""One client's tick loop — the thing that actually runs.

    perceive -> key -> track -> decide -> verify -> arm -> record

Four properties this loop must have, and each is a line you can point at:

1. **It never awaits the teacher.** Escalation is an enqueue and nothing more. The tick
   that asked continues on the scripted plan, and the answer — if one ever arrives — is
   picked up by a later tick. Awaiting a rate-limited subscription inside a 2 Hz loop
   attached to a live character is how a character stands in a field for ninety seconds.

2. **It records every tick, including the boring ones.** `armed_by` on all of them, and a
   shadow prediction on all of them. Both are unrecoverable after the fact
   (`ARCHITECTURE.md` §4) and both are worthless if they are only written when something
   interesting happened, because "interesting" is exactly the sampling bias that ruins a
   training set.

3. **It cannot stop because something above it failed.** A missing teacher, a refused
   plan, an unreadable frame and an empty queue all continue. The only things that end a
   run are the caller asking and the source being gone.

4. **A dead character does not advance the playhead.** The tracker owns that rule; the
   runtime just never overrides it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.clients.source import Source
from jev.coach import policy as scripted
from jev.coach.schema import Decision, Verdict
from jev.coach.situation import with_key
from jev.coach.verifier import verify
from jev.guide.graph import Graph
from jev.guide.tracker import Event, Tracker
from jev.learn.episode import Recorder, TickRow
from jev.skills.catalog import NAMES
from jev.world.state_v1 import ArmedBy, State


@dataclass
class Counters:
    """What the eval board reads. Cheap enough to keep on every client."""

    ticks: int = 0
    advances: int = 0
    fails: int = 0
    deaths: int = 0
    off_route: int = 0
    rejected: int = 0
    escalated: int = 0
    teacher_applied: int = 0
    blind_ticks: int = 0

    # The headline number (ARCHITECTURE.md §1): ticks no rule could settle. It is the one
    # that must fall, and it distinguishes a perception problem from an intelligence one.
    unresolved: int = 0


@dataclass
class Armed:
    """What System 1 is currently executing, and on whose authority."""

    decision: Decision
    by: ArmedBy
    at: float
    rule: str


# A teacher is optional and is only ever asked, never awaited. `ask` enqueues and returns
# immediately; `take` returns an answer if one has arrived since.
AskFn = Callable[[State, str], None]
TakeFn = Callable[[str], Decision | None]


@dataclass
class ClientRuntime:
    client_id: str
    graph: Graph
    source: Source
    recorder: Recorder

    ask: AskFn | None = None
    take: TakeFn | None = None
    shadow: Callable[[State], tuple[str, str | None, float]] | None = None

    counters: Counters = field(default_factory=Counters)
    armed: Armed | None = None
    tracker: Tracker = field(init=False)
    _entered: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.tracker = Tracker(self.graph, self.graph.entry)

    # -- the tick ------------------------------------------------------------

    def tick(self) -> State:
        state = with_key(self.source.read())
        self.counters.ticks += 1
        if not state.sense.addon_ok:
            self.counters.blind_ticks += 1

        if not self._entered:
            self.tracker.enter(self.graph.entry, state)
            self._entered = True

        node_before = self.graph.get(self.tracker.step_id)
        verdict = self.tracker.tick(state)
        self._apply(verdict, state)

        node = self.graph.get(self.tracker.step_id) or node_before
        state = self._with_guide(state, verdict)

        plan, by, rule = self._choose(state, node)

        check = verify(plan, state, NAMES)
        if not check.ok:
            # A refusal is never a reason to stop. Fall to the floor, which is always
            # valid, and record that the refusal happened.
            self.counters.rejected += 1
            fallback = scripted.decide(state, node)
            plan, by, rule = fallback.decision, ArmedBy.POLICY, f"rejected:{check.rule}"

        self.armed = Armed(plan, by, state.t, rule)
        self._record(state, plan, by)
        return state

    # -- internals -----------------------------------------------------------

    def _apply(self, verdict, state: State) -> None:
        match verdict.event:
            case Event.ADVANCE:
                self.counters.advances += 1
                if verdict.goto:
                    self.tracker.enter(verdict.goto, state)
            case Event.FAIL:
                self.counters.fails += 1
                if verdict.goto:
                    # Remember where to come back to. A rib is shared by every step in its
                    # zone, so the graph cannot name the way back — only the caller knows.
                    node = self.graph.get(self.tracker.step_id)
                    rejoin = node.next[0] if node and node.next else None
                    self.tracker.enter(verdict.goto, state, rejoin_to=rejoin)
            case Event.DEATH:
                # Counted once per death, not once per tick spent dead — otherwise a long
                # corpse run reads as a hundred deaths and the eval board panics.
                if self.armed is None or self.armed.rule != "preempt.dead":
                    self.counters.deaths += 1
                self.tracker.memory.deaths += 1
            case Event.OFF_ROUTE:
                self.counters.off_route += 1

    def _with_guide(self, state: State, verdict) -> State:
        """Put the playhead on the state, so the recorded row and the situation key agree
        with what the tracker actually believes."""
        node = self.graph.get(self.tracker.step_id)
        if node is None:
            return state
        mem = self.tracker.memory
        return with_key(state.model_copy(update={
            "guide": state.guide.model_copy(update={
                "graph_id": self.graph.graph_id,
                "step_id": node.id,
                "kind": node.kind,
                "age_s": state.t - mem.entered_at,
                "on_route": verdict.event is not Event.OFF_ROUTE,
                "deaths_on_step": mem.deaths,
                "attempts": mem.attempts,
            }),
        }))

    def _choose(self, state: State, node) -> tuple[Decision, ArmedBy, str]:
        """The floor first, then an upgrade if one happens to be waiting.

        Order matters: the scripted plan is computed unconditionally so that there is
        always something to arm, and the teacher's answer only ever replaces it. There is
        no path through this function that produces nothing.
        """
        plan = scripted.decide(state, node)

        if self.take is not None and state.situation_key:
            answer = self.take(state.situation_key)
            if answer is not None:
                self.counters.teacher_applied += 1
                return answer, ArmedBy.TEACHER, "teacher"

        if scripted.wants_teacher(plan):
            self.counters.unresolved += 1
            if self.ask is not None and state.situation_key:
                # Enqueue and move on. This is the only contact with the teacher in the
                # hot loop, and it does not block.
                self.ask(state, state.situation_key)
                self.counters.escalated += 1

        by = ArmedBy.S1_PREEMPT if plan.rule.startswith("preempt.") else ArmedBy.POLICY
        return plan.decision, by, plan.rule

    def _record(self, state: State, plan: Decision, by: ArmedBy) -> None:
        intent = skill = None
        confidence = None
        if self.shadow is not None:
            intent, skill, confidence = self.shadow(state)

        self.recorder.tick(TickRow(
            run_id=self.recorder.run_id,
            tick_id=self.recorder.next_tick_id(),
            t=state.t,
            client_id=self.client_id,
            state=state.model_dump(mode="json"),
            situation_key=state.situation_key or "",
            armed_skill=plan.skill,
            armed_by=by,
            shadow_intent=intent,
            shadow_skill=skill,
            shadow_confidence=confidence,
        ))

    # -- driving -------------------------------------------------------------

    def run(self, ticks: int, period_s: float = 0.5,
            sleep: Callable[[float], None] = time.sleep) -> Counters:
        """Run a bounded number of ticks. Bounded on purpose: an unbounded loop in a
        library is a loop somebody has to learn how to stop."""
        for _ in range(ticks):
            self.tick()
            if period_s:
                sleep(period_s)
        return self.counters


def verdict_summary(v: Verdict) -> str:
    return "ok" if v.ok else f"{v.rule}: {v.reason}"
