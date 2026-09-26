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

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.clients.source import Source
from jev.coach import policy as scripted
from jev.coach.schema import Decision, Intent, TeacherReply, Verdict
from jev.coach.situation import with_key
from jev.coach.verifier import verify
from jev.guide.graph import Graph, Node
from jev.guide.objectives import progress
from jev.guide.tracker import SHORT_RIB_S, Event, Tracker
from jev.guide.tracker import Verdict as TrackVerdict
from jev.learn.episode import (
    DecisionRow,
    Recorder,
    SkillOutcome,
    SkillResultRow,
    TickRow,
)
from jev.skills.catalog import NAMES, judges_itself
from jev.skills.catalog import get as get_skill
from jev.world.state_v1 import ArmedBy, State, StepKind

# Skills that serve the character rather than its step: while one runs, the step's clock
# stands still (`Tracker.serving`).
# A dialog nobody closes is a stall, so ABORT_WAIT is not among them.
#
# A fight that finds the character on its way is one too, from its approach to its loot:
# the clock already stood still in combat, but not for the walk to an attacker or the
# corpse after it, and a dozen kobolds on the way back from Fargodeep Mine ran quest 60's
# hand-in out of its four minutes with the quest complete (run 20260924T132256-fc8503).
SERVICING_SKILLS = frozenset({"EAT_DRINK", "BAG_MAKE_SPACE", "VENDOR_REPAIR",
                              "BUY_AMMO_REAGENT_FOOD", "LOOT", "RELEASE_SPIRIT", "CORPSE_RUN",
                              "TRAIN_CLASS", "BIND_HEARTH", "DISCOVER_FLIGHT", "COMBAT_PROFILE"})
# A hand-in passed over after failing twice leaves its quest complete in the log for good:
# Kobold Candles sat there with William Pestle twenty yards from Marshal Dughan, whom the
# guide visits again and again (25 September). A later step that brings the character
# within the hand-in's own arrival radius hands the quest in on the way, once a level - the
# attempt is kept among the retried steps as `DETOUR` + its id + "@" + the level - and
# comes back. Once a level, not once ever: Kobold Candles, Collecting Kelp and the Grape
# Manifest spent theirs on an inn and an abbey whose stairs the walk could not yet climb,
# and sat complete in the log after the walk was fixed (25 September). An entry without a
# level, from before, counts as spent at the level first read.
DETOUR = "detour:"
# How far an outgrown guide goes for a hand-in, in map fractions: a quarter of the zone's map,
# 580 to 870 yards across Elwynn. A hand-in on the way out, not a trip back (V162).
OUTGROWN_REACH = 0.25


@dataclass
class Counters:
    """What the eval board reads. Cheap enough to keep on every client."""

    ticks: int = 0
    advances: int = 0
    rejoins_or_skips: int = 0
    fails: int = 0
    deaths: int = 0
    off_route: int = 0
    rejected: int = 0
    escalated: int = 0
    teacher_applied: int = 0
    blind_ticks: int = 0

    teacher_stale: int = 0
    skills_closed: int = 0
    skills_succeeded: int = 0

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
    decision_id: str = ""
    step_id: str | None = None
    situation_key: str = ""
    arm_id: str = ""


# A teacher is optional and is only ever asked, never awaited. `ask` enqueues and returns
# immediately; `take` returns an answer if one has arrived since.
AskFn = Callable[[State, str], bool | None]
TakeFn = Callable[[str], Decision | TeacherReply | None]


@dataclass
class ClientRuntime:
    client_id: str
    graph: Graph
    source: Source
    recorder: Recorder

    ask: AskFn | None = None
    take: TakeFn | None = None
    shadow: Callable[[State], tuple[str | None, str | None, float]] | None = None
    shadow_model: Callable[[], str | None] | None = None
    learned: Callable[[State, Node | None, frozenset[str]], Decision | None] | None = None
    policy_model: Callable[[], str | None] | None = None
    policy_failed: Callable[[State, str], None] | None = None
    validate_action: Callable[[Decision, str | None], str | None] | None = None

    available_skills: frozenset[str] = NAMES
    keys_down: Callable[[], list[str]] | None = None
    policy_context: scripted.Context = field(default_factory=scripted.Context)
    completed: set[int] = field(default_factory=set)
    start_step: str | None = None
    start_rejoin: str | None = None
    start_deaths: int = 0
    # Steps already failed into a rib once, in earlier sessions (`playhead.Remembered`).
    start_retried: frozenset[str] = frozenset()
    # When a short rib the run resumes on rejoins, as wall time (`playhead.Remembered`).
    start_rib_until: float | None = None
    start_entry_level: int | None = None
    # (step, completed quests, where the step leads back to when that is not its next,
    # deaths on the step, steps already retried, when a short rib rejoins)
    on_progress: Callable[..., None] | None = None
    # The character whose playhead this run keeps (`char.key`). Another character's state
    # is not tracked, recorded or saved: it sets `foreign`, and the run stops.
    character_key: int | None = None
    # The level this guide is outgrown at (`jev.run.cli.OUTGROWN_AT`): from it, between two
    # quests, the complete quests' hand-ins nearby are made and the guide is done (V162).
    outgrown_at: int | None = None
    # The quest under way when the band first applied (`_outgrown`, V177).
    _band_quest: int | None = field(default=None, init=False)
    _band_started: bool = field(default=False, init=False)
    foreign: int | None = field(default=None, init=False)
    last_state: State | None = field(default=None, init=False)
    _was_dead: bool = field(default=False, init=False)
    _decision_seq: int = field(default=0, init=False)
    _arm_seq: int = field(default=0, init=False)
    _tracker_event: str = field(default="none", init=False)
    _tracker_from: str | None = field(default=None, init=False)
    finished: bool = field(default=False, init=False)
    # Steps that have failed into a rib and been retried once after it.
    _retried: set[str] = field(default_factory=set, init=False)

    counters: Counters = field(default_factory=Counters)
    armed: Armed | None = None
    tracker: Tracker = field(init=False)
    _entered: bool = field(default=False, init=False)
    _escalations: int = field(default=0, init=False)
    # The most recent escalation per situation, so an answer links to the question that
    # actually asked it. A single "last escalation" would attribute an answer about one
    # bucket to an unrelated question about another, which is precisely the mis-join
    # this field exists to remove.
    _asked: dict[str, str] = field(default_factory=dict, init=False)
    # The buckets this client has outstanding questions under, and when it asked.
    #
    # Not just the current key: `situation_key` bins step age, so a step crosses from
    # "fresh" into "slow" at sixty seconds and its key changes underneath us. The measured
    # teacher round trip is ~52 s, which means an answer routinely comes back addressed to
    # a bucket the character has already left — and a lookup on the *current* key would
    # miss it every time, re-ask, and never once hit the cache that `situation_key` exists
    # to make possible.
    _asked_at: dict[str, float] = field(default_factory=dict, init=False)

    # How old a teacher answer may be and still be acted on. Matched to the step-age bin
    # in `jev.coach.situation`: past it, the answer is about a different bucket than the
    # one the character is in now.
    stale_after_s: float = 60.0

    def __post_init__(self) -> None:
        self.tracker = Tracker(self.graph, self.graph.entry)

    # -- the tick ------------------------------------------------------------

    def tick(self, *, choose: bool = True, record: bool = True, state: State | None = None) -> State:
        """Track every observation; choose only when the body can accept a new skill.

        The live supervisor calls this at 4 Hz, chooses at up to 2 Hz, and records at
        2 Hz plus events. A blocking body never blocks these clocks.
        """
        state = self.source.read() if state is None else state
        if state.client_id != self.client_id:
            raise ValueError(f"source client {state.client_id!r} != runtime {self.client_id!r}")
        if (self.character_key is not None and state.char.key is not None
                and state.char.key != self.character_key):
            # Logged out and another character logged in. Its quest log is not this
            # playhead's, and tracking it would write one character's progress into the
            # other's file.
            self.foreign = state.char.key
            return state
        self.counters.ticks += 1
        if not state.sense.addon_ok:
            self.counters.blind_ticks += 1
        if not self._entered:
            start = self.start_step if self.graph.get(self.start_step or "") is not None else None
            self.tracker = Tracker.resume(self.graph, state, start=start,
                                          completed=frozenset(self.completed),
                                          rejoin_to=self.start_rejoin)
            if start is not None and self.tracker.step_id == start:
                self.tracker.memory.deaths = self.start_deaths
                self.tracker.memory.until = self.start_rib_until
                if self.start_entry_level is not None:
                    self.tracker.memory.level_at_entry = self.start_entry_level
            self._retried |= set(self.start_retried)
            self._entered = True

        before = self.tracker.step_id
        completed_before = set(self.completed)
        deaths_before = self.tracker.memory.deaths
        retried_before = set(self._retried)
        until_before = self.tracker.memory.until
        entry_before = self.tracker.memory.level_at_entry
        finished_before = self.finished
        self.tracker.serving = (self.armed is not None
                                and self.armed.decision.skill in SERVICING_SKILLS)
        verdict = TrackVerdict(Event.NONE) if self.finished else self.tracker.tick(state)
        self._tracker_event, self._tracker_from = verdict.event.value, before
        if verdict.event is Event.ADVANCE and not verdict.completed:
            self._tracker_event = "rejoin_or_skip"
        self._apply(verdict, state)
        if state.char.level is not None:
            unlevelled = {r for r in self._retried if r.startswith(DETOUR) and "@" not in r}
            self._retried = ((self._retried - unlevelled)
                             | {f"{r}@{state.char.level}" for r in unlevelled})
        if (not self.finished and self.outgrown_at is not None
                and state.char.level is not None and state.char.level >= self.outgrown_at
                and verdict.event not in (Event.FAIL, Event.DEATH)
                and state.vitals.combat is not True
                and state.vitals.dead is not True and state.vitals.ghost is not True):
            onward = self._outgrown(state)
            if onward is None:
                self.finished = True
            elif onward != self.tracker.step_id:
                self.tracker.enter(onward, state)
                self._tracker_event = "rejoin_or_skip"
        if verdict.event is Event.NONE and not self.finished:
            beyond = self._abandoned_now(state)
            if beyond is not None:
                self.tracker.enter(beyond, state)
                self._tracker_event = "rejoin_or_skip"
        # Every tick that stays on the step, arriving included: standing at the quest giver
        # re-reports ARRIVED, never NONE.
        if (not self.finished and self.tracker.step_id == before
                and verdict.event not in (Event.ADVANCE, Event.FAIL, Event.DEATH)):
            if (lost := (self._prerequisite_lost(state)
                         or self._unfinished_hand_in(state))) is not None:
                self.tracker.enter(lost, state)
                self._tracker_event = "rejoin_or_skip"
            elif (detour := self._handin_detour(state)) is not None:
                self._retried.add(f"{DETOUR}{detour}@{state.char.level}")
                self.tracker.enter(detour, state, rejoin_to=before)
                self._tracker_event = "rejoin_or_skip"
        node = self.graph.get(self.tracker.step_id)
        state = self._with_guide(state, verdict)
        record = record or verdict.event in (Event.ADVANCE, Event.FAIL, Event.DEATH)
        # Persist only tracker-witnessed progress, never a caller's guessed completion.
        if self.on_progress is not None and (before != self.tracker.step_id
                                            or completed_before != self.completed
                                            or deaths_before != self.tracker.memory.deaths
                                            or retried_before != self._retried
                                            or until_before != self.tracker.memory.until
                                            or entry_before != self.tracker.memory.level_at_entry
                                            or finished_before != self.finished
                                            or self.last_state is None):
            self._progress()

        if choose:
            plan, by, rule, decision_id = self._choose(state, node)
            if not self._same_arm(plan, by, state.guide.step_id):
                self._close_armed(state, plan, by)
                if not decision_id:
                    decision_id = self._record_decision(state, plan, by, rule)
                self._arm_seq += 1
                self.armed = Armed(plan, by, state.t, rule, decision_id,
                                   state.guide.step_id, state.situation_key or "",
                                   f"{self.recorder.run_id}:{self.client_id}:arm{self._arm_seq}")
            elif decision_id:
                # A new accepted teacher answer is a new decision even if its action
                # agrees with the previous one; retain the body's original start time.
                self.armed.decision_id = decision_id
            record = True

        self.last_state = state
        if record:
            self._record(state)
        return state

    def _same_arm(self, plan: Decision, by: ArmedBy, step_id: str | None) -> bool:
        prev = self.armed
        return bool(prev and prev.by == by and prev.step_id == step_id
                    and prev.decision.skill == plan.skill
                    and prev.decision.intent == plan.intent
                    and prev.decision.goal == plan.goal
                    and prev.decision.params == plan.params)

    def expire_step(self) -> bool:
        """The watchdog's first answer to a stalled run: fail the step being worked.

        Its own `on_fail` edge decides where to, on the next tick - the grind for the
        character's level, which earns what the step could not.
        """
        return False if self.finished else self.tracker.expire()

    def finish(self, outcome: SkillOutcome, detail: str = "", *, state: State | None = None) -> None:
        """The body reports its measured outcome once, against the original arm."""
        if self.armed is not None and self.last_state is not None:
            self._skill_result(state or self.last_state, outcome, detail)
            # Only the step's own work is an attempt at it. A fight that lost its target on
            # the way made a quest accept's first timeout its second attempt, and "not
            # offered" sent a level 11 on a 1,558-yard walk to a grind and back for a
            # quest that was there all along (session 90).
            if (outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)
                    and self.armed.step_id == self.tracker.step_id
                    and self.armed.rule.startswith("guide.")):
                self.tracker.memory.attempts += 1
        self.armed = None

    # -- internals -----------------------------------------------------------

    def fail_over(self, skill: str, reason: str) -> bool:
        """Take the current step's own fail edge after its skill ran out of attempts.

        With one attempt a session, a hand-in the Abbey's stair defeats stopped every
        session a few minutes in, and each new session tried the same step afresh, its
        timeout never reached: the step's rib, and the pass-over after it, never came (run
        20260924T081531-4249a4). Only for the step's own skills - a merchant that failed
        on the way is not the step failing. `False` when there is no edge to take.
        """
        node = self.graph.get(self.tracker.step_id)
        state = self.last_state
        if (node is None or state is None or not node.on_fail or self.finished
                or skill not in (node.skills or ())):
            return False
        before = self.tracker.step_id
        self._apply(TrackVerdict(Event.FAIL, goto=node.on_fail[0].goto,
                                 reason=f"{skill} out of attempts: {reason}"), state)
        if self.on_progress is not None:
            self._progress()
        return self.tracker.step_id != before

    def _past_abandoned_quest(self, verdict) -> str | None:
        """The first step after the current quest's, when the step failed on its quest's
        absence and the quest's accept was passed over; else `None`."""
        reason = verdict.reason or ""
        if not (reason.startswith("quest_missing") or "quest absent" in reason):
            return None
        return self._beyond_abandoned()

    def _abandoned_now(self, state: State) -> str | None:
        """The same, before the step is even walked to: the log read, the quest not in it,
        its accept passed over. Quest 16's hand-in, entered from a rib at Northshire, was
        a walk to Gerard Tiller in Goldshire only to find the quest missing there."""
        node = self.graph.get(self.tracker.step_id)
        if (node is None or node.quest_id is None or state.quests is None
                or any(q.quest_id == node.quest_id for q in state.quests)):
            return None
        return self._beyond_abandoned()

    def _detour_spent(self, step_id: str, level: int | None) -> bool:
        """The hand-in's detour at this level is spent (`DETOUR`); with no level read, any."""
        if level is not None:
            return f"{DETOUR}{step_id}@{level}" in self._retried
        return any(r == DETOUR + step_id or r.startswith(f"{DETOUR}{step_id}@")
                   for r in self._retried)

    def _prerequisite_lost(self, state: State) -> str | None:
        """An accept whose prerequisite's hand-in is lost - passed over, and its detour for
        the level spent - cannot be offered: the step past this quest instead. The Escape
        waits on Collecting Kelp's hand-in, lost in the Lion's Pride Inn (session 109), and
        trying it anyway costs a rib, a retry and a pass-over."""
        node = self.graph.get(self.tracker.step_id)
        if (node is None or node.kind is not StepKind.QUEST_ACCEPT or node.quest_id is None
                or not node.quest_prerequisites or node.quest_id in self.completed):
            return None
        lost = {n.quest_id for n in self.graph.nodes
                if n.kind is StepKind.QUEST_TURNIN and n.quest_id is not None
                and n.quest_id not in self.completed
                and self._detour_spent(n.id, state.char.level)}
        # Alternatives of quests all required, as the route reads them (`compile_route`):
        # impossible only when every alternative holds a lost one.
        if not all(any(q in lost for q in group) for group in node.quest_prerequisites):
            return None
        step = node
        while step is not None and step.quest_id == node.quest_id and step.next:
            step = self.graph.get(step.next[0])
        return step.id if step is not None and step.quest_id != node.quest_id else None

    def _unfinished_hand_in(self, state: State) -> str | None:
        """A hand-in for a quest the log reads as not complete, whose objective was passed
        over, cannot happen: the step past this quest instead. Goldtooth's objective was
        passed over at the bottom of Fargodeep Mine, and its hand-in came next with the
        necklace never taken (session 116)."""
        node = self.graph.get(self.tracker.step_id)
        if (node is None or node.kind is not StepKind.QUEST_TURNIN or node.quest_id is None
                or state.quests is None):
            return None
        quest = next((q for q in state.quests if q.quest_id == node.quest_id), None)
        if quest is None or quest.complete is not False or not any(
                n.id in self._retried for n in self.graph.nodes
                if n.quest_id == node.quest_id and n.kind is StepKind.QUEST_OBJECTIVE):
            return None
        step = node
        while step is not None and step.quest_id == node.quest_id and step.next:
            step = self.graph.get(step.next[0])
        return step.id if step is not None and step.quest_id != node.quest_id else None

    def _handin_detour(self, state: State) -> str | None:
        """A passed-over hand-in within reach, for a quest complete in the log (`DETOUR`)."""
        node = self.graph.get(self.tracker.step_id)
        if (node is None or node.kind is StepKind.GRIND or self.tracker.memory.rejoin_to
                or state.quests is None or state.vitals.combat is True
                or state.char.level is None
                or state.pos.mx is None or state.pos.my is None):
            return None
        complete = {q.quest_id for q in state.quests if q.complete is True}
        here = (state.pos.mx, state.pos.my)
        for step in self.graph.nodes:
            if (step.kind is StepKind.QUEST_TURNIN and step.id != node.id
                    and step.quest_id in complete and step.id in self._retried
                    and not self._detour_spent(step.id, state.char.level)
                    and step.pos is not None
                    and (step.coord_zone_id is None or state.pos.coord_zone_id is None
                         or step.coord_zone_id == state.pos.coord_zone_id)
                    and math.dist(step.pos, here) <= step.r):
                return step.id
        return None

    def _outgrown(self, state: State) -> str | None:
        """The step to be on once the character has outgrown this guide (`outgrown_at`): the
        current one while its quest's objective is under way or it is one of the hand-ins
        below; else the nearest hand-in within `OUTGROWN_REACH` of a quest complete in the
        log, not passed over; else `None`, and the guide is done. The rest of the 1-12 guide paid
        about 5,150 XP of hand-ins against mobs three to five levels below a level-13
        character, when a kill two levels below pays about twice as much as one four below;
        26% of the nine hours' kills were grey (V162)."""
        if state.quests is None or state.pos.mx is None or state.pos.my is None:
            return self.tracker.step_id                 # the log or the place unread: wait
        in_log = {q.quest_id: q for q in state.quests}
        node = self.graph.get(self.tracker.step_id)
        # The quest under way when the band first applied is finished, its hand-in too,
        # however far (V177): the Riverpaw bounty came to 8 of 8 at the camp and its hand-in,
        # across Elwynn, was left for the next guide, which never makes it.
        if not self._band_started:
            self._band_started = True
            if node is not None and node.quest_id in in_log:
                self._band_quest = node.quest_id
        if (node is not None and node.quest_id == self._band_quest
                and node.quest_id in in_log and node.id not in self._retried
                and node.kind in (StepKind.QUEST_OBJECTIVE, StepKind.QUEST_TURNIN)):
            return node.id
        here = (state.pos.mx, state.pos.my)
        hand_ins = [step for step in self.graph.nodes
                    if step.kind is StepKind.QUEST_TURNIN and step.quest_id in in_log
                    and in_log[step.quest_id].complete is True
                    and step.quest_id not in self.completed
                    and step.id not in self._retried
                    and not self._detour_spent(step.id, state.char.level)
                    and step.pos is not None
                    and (step.coord_zone_id is None
                         or step.coord_zone_id == state.pos.coord_zone_id)
                    and math.dist(step.pos, here) <= OUTGROWN_REACH]
        if not hand_ins:
            return None
        if node is not None and node in hand_ins:
            return node.id
        return min(hand_ins, key=lambda s: math.dist(s.pos, here)).id

    def _beyond_abandoned(self) -> str | None:
        node = self.graph.get(self.tracker.step_id)
        if (node is None or node.quest_id is None or node.quest_id in self.completed
                or node.kind not in (StepKind.QUEST_OBJECTIVE, StepKind.QUEST_TURNIN)):
            return None
        accepts = [n.id for n in self.graph.nodes
                   if n.quest_id == node.quest_id and n.kind is StepKind.QUEST_ACCEPT]
        if not accepts or not set(accepts) & self._retried:
            return None
        step = node
        while step is not None and step.quest_id == node.quest_id and step.next:
            step = self.graph.get(step.next[0])
        return step.id if step is not None and step.quest_id != node.quest_id else None

    def _progress(self) -> None:
        """Hand the playhead to whoever keeps it."""
        self.on_progress(self.tracker.step_id, set(self.completed),
                         self.tracker.memory.rejoin_to, self.tracker.memory.deaths,
                         frozenset(self._retried), self.tracker.memory.until,
                         finished=self.finished, entry_level=self.tracker.memory.level_at_entry)

    def _apply(self, verdict, state: State) -> None:
        match verdict.event:
            case Event.ADVANCE:
                self.counters.advances += int(verdict.completed)
                self.counters.rejoins_or_skips += int(not verdict.completed)
                node = self.graph.get(self.tracker.step_id)
                if (verdict.completed and node is not None and node.kind.value == "quest_turnin"
                        and node.quest_id is not None):
                    self.completed.add(node.quest_id)
                if verdict.goto:
                    self.tracker.enter(verdict.goto, state)
                elif node is not None and node.kind is StepKind.GRIND:
                    # A rib with no way back is a detour, not the end of the guide: find
                    # the first step not yet done, as a fresh run would.
                    self.tracker = Tracker.resume(self.graph, state,
                                                  completed=frozenset(self.completed))
                else:
                    self.finished = True
            case Event.FAIL:
                self.counters.fails += 1
                beyond = self._past_abandoned_quest(verdict)
                back = self.tracker.memory.rejoin_to
                if back is not None and self._detour_spent(self.tracker.step_id, None):
                    # A hand-in on the way that could not be done: straight back, no rib.
                    self.tracker.enter(back, state)
                elif beyond is not None:
                    # The rest of a quest whose accept was passed over: its objective and
                    # hand-in cannot happen, and a rib cannot put the quest in the log.
                    # Quest 16's accept failed twice, and its objective then stopped a
                    # session and would have cost two ribs and a walk to its hand-in.
                    self.tracker.enter(beyond, state)
                elif verdict.goto:
                    # Remember where to come back to. A rib is shared by every step in its
                    # zone, so the graph cannot name the way back — only the caller knows.
                    # From a rib, the failed step itself, once: a turn-in that timed out
                    # walking to Marshal McBride and was then passed over left quest 15
                    # complete in the log for good, and quest 21 behind it. A step that
                    # fails again after its rib is passed over, so a step that cannot
                    # succeed costs two ribs, not the run. An alternative step leads on
                    # past the step it replaces.
                    failed = self.tracker.step_id
                    node = self.graph.get(failed)
                    target = self.graph.get(verdict.goto)
                    rejoin = node.next[0] if node and node.next else None
                    goto = verdict.goto
                    if target is not None and target.kind is StepKind.GRIND:
                        # The rib whose mobs suit the character as it is, not as the guide
                        # expected: a level 3 character failed into level 5-6 boars and
                        # died there three times (run 20260923T174132-d01302).
                        here = ((state.pos.mx, state.pos.my)
                                if state.pos.mx is not None and state.pos.my is not None
                                else None)
                        goto = self.graph.rib_for(
                            state.char.level, preferred=target, near=here,
                            short="deaths" not in (verdict.reason or "")).id
                        if failed not in self._retried:
                            self._retried.add(failed)
                            rejoin = failed
                    self.tracker.enter(goto, state, rejoin_to=rejoin)
                    if (target is not None and target.kind is StepKind.GRIND
                            and "deaths" not in (verdict.reason or "")):
                        # A level cures a step that kills the character, not one that
                        # could not find its NPC or its mob (`SHORT_RIB_S`).
                        self.tracker.memory.until = state.t + SHORT_RIB_S
            case Event.DEATH:
                if not self._was_dead:
                    self.counters.deaths += 1
                    self.tracker.memory.deaths += 1
            case Event.OFF_ROUTE:
                self.counters.off_route += 1
        dead = state.vitals.dead is True or state.vitals.ghost is True
        if dead:
            self._was_dead = True
        elif state.vitals.dead is False and state.vitals.ghost is False:
            self._was_dead = False

    def _with_guide(self, state: State, verdict) -> State:
        """Put the playhead on the state, so the recorded row and the situation key agree
        with what the tracker actually believes."""
        node = self.graph.get(self.tracker.step_id)
        if node is None:
            return state
        from jev.guide.tracker import route_destination

        mem = self.tracker.memory
        destination = route_destination(state, node)
        return with_key(state.model_copy(update={
            "guide": state.guide.model_copy(update={
                "graph_id": self.graph.graph_id,
                "step_id": node.id,
                "kind": node.kind,
                "progress": progress(state.quests, node.quest_id).fraction,
                "age_s": state.t - mem.entered_at,
                "on_route": (None if destination is None or state.pos.mx is None or state.pos.my is None
                             else mem.off_route_since is None),
                "deaths_on_step": mem.deaths,
                "attempts": mem.attempts,
            }),
        }))

    def _choose(self, state: State, node) -> tuple[Decision, ArmedBy, str, str]:
        floor = scripted.decide(state, node, context=self.policy_context)
        if node is not None and node.kind.value == "grind" and floor.decision.skill == "GRIND_UNTIL":
            entered = self.tracker.memory.level_at_entry
            floor = scripted.Plan(floor.decision.model_copy(update={"params": {
                **floor.decision.params, "until_level": entered + 1 if entered is not None else node.level[1],
            }}), floor.confident, floor.rule)
        if not state.sense.addon_ok and (state.sense.vision_conf or 0.0) < 0.5:
            floor = scripted.Plan(Decision(goal="wait:blind", intent=Intent.WAIT, skill=None,
                                           abort_if=["senses_restored"], confidence=0,
                                           why="no trustworthy observation; release inputs"),
                                  False, "sense.blind")
        elif self.finished and not floor.rule.startswith(("preempt.", "fight.")):
            # A finished guide still fights what attacks it, and still answers a death: the
            # run ends between fights (`Supervisor.run`), not in one (session 141).
            floor = scripted.Plan(Decision(goal="wait:finished", intent=Intent.WAIT, skill=None,
                                           abort_if=["new_guide"], confidence=1,
                                           why="guide finished"), True, "guide.finished")
        preempt = floor.rule.startswith("preempt.")
        protected = preempt or floor.rule.startswith(("service.", "recover.", "fight.")) or floor.rule in (
            "sense.blind", "guide.finished")
        if self.take is not None:
            try:
                answer, key = self._collect(state)
            except Exception:
                answer, key = None, ""
            if answer is not None:
                artifacts = []
                if isinstance(answer, TeacherReply):
                    artifacts = [a.model_dump(mode="json") for a in answer.artifacts]
                    answer = answer.decision
                check = (self._verify(answer, state) if answer is not None
                         else Verdict.refuse("no_action", "artifacts only; no immediate action"))
                stale = self._is_stale(state, key)
                if stale:
                    check = Verdict.refuse("stale", "situation changed while the teacher was answering")
                    self.counters.teacher_stale += 1
                elif floor.rule in ("sense.blind", "guide.finished"):
                    check = Verdict.refuse(floor.rule, floor.decision.why)
                elif protected:
                    check = Verdict.refuse("preempt", floor.decision.why)
                decision_id = self._record_applied(state, answer, key, check=check, artifacts=artifacts)
                if check.ok:
                    self.counters.teacher_applied += 1
                    return answer, ArmedBy.TEACHER, "teacher", decision_id
                self.counters.rejected += 1

        if scripted.wants_teacher(floor) or (state.guide.attempts and floor.rule.startswith("guide.")):
            self.counters.unresolved += 1
            self._record_unresolved(state, floor)
            if self.ask is not None and state.situation_key:
                try:
                    queued = self.ask(state, state.situation_key)
                    self.counters.escalated += int(queued is not False)
                except Exception:
                    pass  # optional queue failure never removes the scripted floor

        if self.learned is not None and not protected:
            try:
                candidate = self.learned(state, node, self.available_skills)
                if candidate is not None:
                    check = self._verify(candidate, state)
                    model = self.policy_model() if self.policy_model else None
                    if check.ok and model:
                        return candidate, ArmedBy.POLICY, f"learned:{model}", ""
                    self.counters.rejected += 1
                    if self.policy_failed:
                        self.policy_failed(state, f"verifier: {check.rule}: {check.reason}")
            except Exception:
                # Optional inference and registry faults never remove the scripted floor.
                pass

        plan = floor.decision
        by = ArmedBy.S1_PREEMPT if preempt else ArmedBy.POLICY
        check = self._verify(plan, state)
        if not check.ok:
            self.counters.rejected += 1
            self._record_decision(state, plan, by, floor.rule, check)
            plan = Decision(goal="wait:unavailable", intent=Intent.WAIT, skill=None,
                            abort_if=["state_changed"], confidence=1.0,
                            why=f"{check.rule}: {check.reason}"[:280])
            return plan, by, "unavailable", ""
        return plan, by, floor.rule, ""

    def _verify(self, plan: Decision, state: State) -> Verdict:
        check = verify(plan, state, self.available_skills)
        if check.ok and plan.skill is not None and self.validate_action:
            reason = self.validate_action(plan, state.guide.step_id)
            if reason:
                return Verdict.refuse("body_contract", reason)
        return check

    def _record_decision(self, state: State, plan: Decision, by: ArmedBy, rule: str,
                         check: Verdict | None = None) -> str:
        check = check or Verdict.accept()
        self._decision_seq += 1
        decision_id = f"{self.recorder.run_id}:d{self._decision_seq}"
        self.recorder.decision(DecisionRow(
            run_id=self.recorder.run_id, decision_id=decision_id,
            tick_id=self.recorder._tick_id + 1, t=state.t, client_id=self.client_id,
            situation_key=state.situation_key or "", author=by,
            model=rule.removeprefix("learned:") if rule.startswith("learned:") else f"scripted:{rule}",
            intent=plan.intent.value, skill=plan.skill, params=dict(plan.params),
            confidence=plan.confidence, why=plan.why,
            status="ok" if check.ok else "rejected",
            verifier_verdict="ok" if check.ok else check.rule, verifier_reason=check.reason,
        ))
        return decision_id

    def _close_armed(self, state: State, next_plan: Decision, by: ArmedBy) -> None:
        prev = self.armed
        if prev is None or prev.decision.skill is None:
            return
        skill = get_skill(prev.decision.skill)
        if by is ArmedBy.S1_PREEMPT:
            outcome = SkillOutcome.PREEMPTED
        elif skill is not None and judges_itself(skill) and skill.success(state):
            outcome = SkillOutcome.SUCCEEDED
        elif skill is not None and state.t - prev.at >= skill.timeout_s:
            outcome = SkillOutcome.TIMED_OUT
        elif skill is not None and not judges_itself(skill):
            outcome = SkillOutcome.UNKNOWN
        else:
            outcome = SkillOutcome.ABORTED
        self._skill_result(state, outcome, prev.rule)

    def _skill_result(self, state: State, outcome: SkillOutcome, detail: str) -> None:
        prev = self.armed
        if prev is None or prev.decision.skill is None:
            return
        self.counters.skills_closed += 1
        self.counters.skills_succeeded += int(outcome is SkillOutcome.SUCCEEDED)
        self.recorder.skill_result(SkillResultRow(
            run_id=self.recorder.run_id, client_id=self.client_id, t=state.t,
            tick_id=self.recorder._tick_id, skill=prev.decision.skill, armed_by=prev.by,
            outcome=outcome, duration_s=max(0.0, state.t - prev.at),
            situation_key=prev.situation_key, step_id=prev.step_id, detail=detail,
            decision_id=prev.decision_id,
            arm_id=prev.arm_id or None,
        ))

    def _record_unresolved(self, state: State, plan: scripted.Plan) -> str:
        """One decision row per tick no rule could settle.

        Bounded by construction: in a healthy run this fires on a few percent of ticks,
        and when it fires on most of them that is the signal, not the cost.
        """
        self._escalations += 1
        decision_id = f"{self.recorder.run_id}:u{self._escalations}"
        self.recorder.decision(DecisionRow(
            run_id=self.recorder.run_id,
            decision_id=decision_id,
            tick_id=self.recorder._tick_id + 1,     # the row this tick is about to write
            t=state.t,
            client_id=self.client_id,
            situation_key=state.situation_key or "",
            author=ArmedBy.POLICY,
            model=f"scripted:{plan.rule}",
            intent=Intent.ESCALATE.value,
            skill=plan.decision.skill,
            params=dict(plan.decision.params),
            confidence=plan.decision.confidence,
            why=plan.decision.why,
        ))
        if state.situation_key:
            self._asked[state.situation_key] = decision_id
            # `setdefault`, not assignment: the timestamp is when this bucket's question
            # was *first* posed, because that is when the state it describes was true.
            # Refreshing it on every re-ask resets the staleness clock every tick, so an
            # answer could never be older than one tick and nothing was ever stale.
            self._asked_at.setdefault(state.situation_key, state.t)
        return decision_id

    def _collect(self, state: State) -> tuple[Decision | TeacherReply | None, str]:
        """Look for an answer under the current bucket, then under any we asked about.

        Oldest question first, so a backlog drains in the order it was created rather
        than repeatedly serving the freshest and starving the rest.
        """
        current = state.situation_key or ""
        if current:
            answer = self.take(current)
            if answer is not None:
                return answer, current

        for key in sorted(self._asked_at, key=self._asked_at.get):
            if key == current:
                continue
            answer = self.take(key)
            if answer is not None:
                return answer, key
        return None, current

    def _is_stale(self, state: State, key: str) -> bool:
        """Has the situation moved on since we asked?

        Unasked buckets are never stale: an unsolicited answer has no age of its own, and
        inventing one would silently drop every cached and shared answer in the farm.
        """
        asked_at = self._asked_at.get(key)
        return (key != state.situation_key
                or (asked_at is not None and (state.t - asked_at) > self.stale_after_s))

    def _record_applied(self, state: State, answer: Decision | None, key: str,
                        *, check: Verdict, artifacts: list[dict]) -> str:
        """A teacher answer, linked back to the escalation that asked for it.

        `escalated_from` is what lets the board count questions rather than reconstruct
        them from timestamps and buckets — a reconstruction that over-counts the moment
        one client escalates the same bucket twice on a tick.
        """
        self._escalations += 1
        decision_id = f"{self.recorder.run_id}:a{self._escalations}"
        self.recorder.decision(DecisionRow(
            run_id=self.recorder.run_id,
            decision_id=decision_id,
            tick_id=self.recorder._tick_id + 1,
            t=state.t,
            client_id=self.client_id,
            # The bucket the question was asked under, not the one the character is in
            # now. They differ routinely — `situation_key` re-bins step age at sixty
            # seconds and the round trip is longer than that — and recording the current
            # one would file the answer against a question nobody asked.
            situation_key=key,
            author=ArmedBy.TEACHER,
            model="teacher",
            intent=answer.intent.value if answer else None,
            skill=answer.skill if answer else None,
            params=dict(answer.params) if answer else {},
            confidence=answer.confidence if answer else None,
            artifacts=artifacts,
            # A stale answer is recorded as `rejected`, the same status the verifier uses
            # for a well-formed reply that may not be acted on. Its artifacts are still
            # good — they describe the step, not the tick — and a later pass promotes them
            # from this row.
            status="ok" if check.ok else "rejected",
            verifier_verdict="ok" if check.ok else check.rule,
            verifier_reason=check.reason,
            why=(("artifacts kept, action discarded; " if check.rule == "stale" else "")
                 + (answer.why if answer else "artifacts only"))[:280],
            # `None` is honest here: an answer for a bucket this client never asked
            # about is unsolicited — a cache hit, or another client's question — and
            # claiming a link would invent one.
            escalated_from=self._asked.pop(key, None),
        ))
        # Answered, one way or the other. Leaving it outstanding would have every later
        # tick re-collect the same reply.
        self._asked_at.pop(key, None)
        return decision_id

    def _record(self, state: State) -> None:
        # No model is an explicit abstention, the same contract as cold_start.predict.
        intent = skill = None
        confidence = 0.0
        model = None
        if self.shadow is not None:
            try:
                intent, skill, confidence = self.shadow(state)
                model = self.shadow_model() if self.shadow_model else None
            except Exception:
                intent, skill, confidence = None, None, 0.0
        arm = self.armed
        state = state.model_copy(update={"control": state.control.model_copy(update={
            "armed_skill": arm.decision.skill if arm else None,
            "armed_by": arm.by if arm else ArmedBy.TRACKER,
            "armed_at": arm.at if arm else None,
        })})
        self.recorder.tick(TickRow(
            run_id=self.recorder.run_id, tick_id=self.recorder.next_tick_id(),
            t=state.t, client_id=self.client_id, state=state.model_dump(mode="json"),
            situation_key=state.situation_key or "",
            armed_skill=arm.decision.skill if arm else None,
            armed_intent=arm.decision.intent.value if arm else None,
            armed_by=arm.by if arm else ArmedBy.TRACKER,
            decision_id=arm.decision_id if arm else None,
            arm_id=(arm.arm_id or None) if arm else None,
            keys=self.keys_down() if self.keys_down is not None else [],
            shadow_intent=intent, shadow_skill=skill, shadow_confidence=confidence,
            shadow_model=model,
            tracker_event=self._tracker_event, tracker_from=self._tracker_from,
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
