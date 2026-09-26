"""One body worker, independently clocked tracking and recording, cooperative stops.

The worker owns all input. A replacement never starts until the previous worker has
released its inputs and returned. Capture or a slow skill cannot silently kill a detached
heartbeat: the supervisor is the main loop and recorder failures propagate to cleanup.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from jev.coach.policy import Context, reflex, service
from jev.learn.episode import Recorder, SkillOutcome
from jev.orch.runtime import Armed, ClientRuntime
from jev.run.evidence import bind, operation
from jev.skills.catalog import get
from jev.world.state_v1 import State

# The body's own jump reads as falling for its whole airtime: measured 0.5-1.06 s per
# jump over 17 unstick jumps on 23 September. Cancelling mid-air released the forward key,
# the character dropped back on the near side of the fence and the leg replanned into the
# same jump for 45 s. Jumps and runs down a slope in the next run measured up to 2.05 s at
# the half-second tick. A fall that outlasts all of those is a real one.
FALL_GRACE_S = 2.5
# A run whose time is up waits out a fight in progress, up to this, as an operator stop does
# (`cli.STOP_COMBAT_GRACE_S`). Session 121 ran out of time at 41% health in a fight, the next
# session's first read was five seconds later at 18%, and the character died before its
# first swing. Under the session loop's hard limit: 900 s of play, this, and the start.
# A run whose guide is finished ends between fights the same way: session 141's guide
# finished as a fight began, the run ended at once, and the character stood in the fight
# through the restart and died (session 142 began by releasing its spirit).
DEADLINE_COMBAT_GRACE_S = 60.0


class Cancelled(Exception):
    pass


class FocusLost(Cancelled):
    """Release the body before the next attempt uses the existing focus backoff."""


class Unsupported(Exception):
    """The requested action needs a capability or fact the body does not have."""


@dataclass(frozen=True)
class Result:
    outcome: SkillOutcome
    detail: str = ""
    code: str = ""


class BodyFailure(Exception):
    """A nested body operation failed with a measured, reportable outcome."""

    def __init__(self, result: Result):
        super().__init__(result.detail or result.code)
        self.result = result


class Body(Protocol):
    available: frozenset[str]
    travelling: bool
    policy_context: Context

    def execute(self, arm: Armed, state: State, checkpoint: Callable[[], None]) -> Result: ...
    def release(self) -> None: ...


class Worker:
    def __init__(self, body: Body, arm: Armed | None, state: State, *,
                 focus: Callable[[Callable[[], None]], bool] | None = None,
                 maintenance: Callable[[Callable[[], None]], Result] | None = None,
                 recorder: Recorder | None = None):
        if arm is None and focus is None and maintenance is None:
            raise ValueError("a game worker needs an armed skill")
        self.body, self.arm = body, arm
        self.state = state
        self.focus = focus
        self.maintenance = maintenance
        self.reason = ""
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.result: Result | None = None
        self.completion_observed = False
        # Seconds the body has spent walking inside this skill (`Skill.walk_free`).
        self.walked_s = 0.0
        self._walk_seen: float | None = None
        self._walk_clock: float | None = None     # the routine clock the walk is counted in
        self._evidence = bind(recorder, arm, client_id=state.client_id)
        self._skill = arm.decision.skill if arm else None
        self._released = False
        self.thread = threading.Thread(target=self._run, args=(state,), name="jev-body")

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled(self.reason)

    def cancel(self, reason: str) -> None:
        if not self.cancelled.is_set():
            self.reason = reason
            self.cancelled.set()

    def _run(self, state: State) -> None:
        try:
            with self._evidence, operation("worker", data={"skill": self._skill}) as span:
                self._perform(state)
                if self.result is None:
                    self.result = Result(SkillOutcome.ABORTED, "worker returned no result", "error")
                span.finish(code=self.result.code, detail=self.result.detail,
                            data={"outcome": self.result.outcome.value})
        except BaseException as exc:
            # A failed evidence write must be visible and must never prevent release
            # or leave the supervisor waiting forever for this worker.
            self.result = Result(SkillOutcome.ABORTED,
                                 f"execution recording failed: {type(exc).__name__}: {exc}",
                                 "error")
        finally:
            try:
                if not self._released:
                    self._release()
            finally:
                self.done.set()

    def _perform(self, state: State) -> None:
        try:
            self.checkpoint()
            if self.maintenance is not None:
                self.result = self.maintenance(self.checkpoint)
            elif self.focus is not None:
                restored = self.focus(self.checkpoint)
                self.result = Result(SkillOutcome.SUCCEEDED if restored else SkillOutcome.ABORTED,
                                     "focus restored" if restored else "client refused focus after backoff",
                                     "focused" if restored else "refused")
            else:
                self.result = self.body.execute(self.arm, state, self.checkpoint)
        except FocusLost as exc:
            self.result = Result(SkillOutcome.PREEMPTED, str(exc), "focus_lost")
        except Cancelled as exc:
            timed_out = self.reason == "skill timeout"
            self.result = Result(SkillOutcome.TIMED_OUT if timed_out else SkillOutcome.PREEMPTED,
                                 str(exc), "timeout" if timed_out else "preempted")
        except Unsupported as exc:
            self.result = Result(SkillOutcome.ABORTED, str(exc), "unsupported")
        except BodyFailure as exc:
            self.result = exc.result
        except Exception as exc:
            self.result = Result(SkillOutcome.ABORTED, f"{type(exc).__name__}: {exc}", "error")
        except BaseException as exc:
            self.result = Result(SkillOutcome.PREEMPTED,
                                 f"{type(exc).__name__}: {exc}", "preempted")
        finally:
            self._release()

    def _release(self) -> None:
        try:
            self.body.release()
        except BaseException as exc:
            self.result = Result(SkillOutcome.ABORTED, f"input cleanup failed: {exc}", "error")
        finally:
            self._released = True


def interruption(arm: Armed, state: State, *, travelling: bool = False,
                 combat_paused: bool = False,
                 completion_observed: bool = False, handles_modal: bool = False,
                 falling_s: float = FALL_GRACE_S,
                 routine_age_s: float | None = None, exposed: bool = False,
                 walked_s: float = 0.0) -> str | None:
    """Why the armed skill must stop now, or `None`.

    `falling_s` is how long falling has been observed continuously; a caller that does
    not track it gets the conservative answer, a fall that has already lasted long enough.
    `routine_age_s` is how long the body's current routine has run when the body keeps
    that clock itself (see `Body.routine_clock`); otherwise the arm's age is used.
    `exposed` is a tutor holding the objective with no fight of its own running: a fighting
    skill it plays is no defence, and combat takes the floor as it does from a walk.
    """
    skill = arm.decision.skill
    if not state.sense.addon_ok and (state.sense.vision_conf or 0.0) < 0.5:
        return "perception unavailable"
    recovery = skill in {"RELEASE_SPIRIT", "CORPSE_RUN"}
    if (state.vitals.dead is True or state.vitals.ghost is True) and not recovery:
        return "dead or ghost"
    if state.ui.modal is True and not recovery and not handles_modal:
        return "blocking modal"
    if state.flags.falling is True and falling_s >= FALL_GRACE_S and not recovery:
        return "falling"
    fighting = skill in {"COMBAT_PROFILE", "APPROACH_TARGET", "ACQUIRE_TARGET", "GRIND_UNTIL", "LOOT"}
    modal_cleanup = (handles_modal and skill == "ABORT_WAIT"
                     and (state.ui.modal is True or completion_observed))
    # Not while combat is paused after fights that never engaged: the walk is the way out
    # of reach (V212).
    if (state.vitals.combat is True and not combat_paused
            and (travelling or exposed or not fighting)
            and not recovery and not modal_cleanup):
        return "combat interrupted the leg or service"
    if arm.step_id != state.guide.step_id and not recovery and not completion_observed:
        return "playhead changed"
    spec = get(skill or "")
    age = state.t - arm.at if routine_age_s is None else routine_age_s
    if spec and spec.walk_free:
        age -= walked_s
    if spec and age >= spec.timeout_s:
        return "skill timeout"
    return None


class Supervisor:
    def __init__(self, runtime: ClientRuntime, body: Body,
                 *, say: Callable[[str], None] = print, max_failures: int = 3,
                 has_focus: Callable[[], bool] | None = None,
                 focus: Callable[[Callable[[], None]], bool] | None = None,
                 housekeeping: Callable[[State], None] | None = None,
                 watchdog=None, operator_active: Callable[[], bool] | None = None,
                 operator_suspected: Callable[[], bool] | None = None):
        if (has_focus is None) != (focus is None):
            raise ValueError("focus observation and restoration must be supplied together")
        # A person at the desk pauses everything (`jev.clients.operator`): no skill, no
        # focus taken, until the desk has been quiet for the operator module's window.
        self.operator_active = operator_active
        # Input that is not the bot's, a person or not yet: the window is not taken back
        # and no session is typed into while there is (`jev.clients.operator.suspected`).
        self.operator_suspected = operator_suspected
        self._operator_paused = False
        self.runtime, self.body, self.say = runtime, body, say
        self.has_focus, self.focus = has_focus, focus
        self.housekeeping, self.watchdog = housekeeping, watchdog
        self._housekeeping_failed = False
        self.runtime.available_skills = body.available
        self.body.policy_context = runtime.policy_context
        self.worker: Worker | None = None
        self.next_record = self.next_coach = 0.0
        self.stopped = threading.Event()
        self.failure: str | None = None
        self.max_failures = max_failures
        self.failures: dict[tuple[str | None, str | None], int] = {}
        self._falling_since: float | None = None

    def step(self, now: float | None = None) -> State:
        now = time.monotonic() if now is None else now
        state = self.runtime.source.read()
        if state.flags.falling is True:
            if self._falling_since is None:
                self._falling_since = now
        else:
            self._falling_since = None
        falling_s = 0.0 if self._falling_since is None else now - self._falling_since
        # A body that plays one objective in stages keeps its routine's clock: while a
        # tutor is deciding, its own episode bounds apply (`inf`); when a scripted
        # routine takes over, the catalog budget starts then. Measured 23 September: a
        # tutor spent 25 s retrying an overloaded provider, and the scripted accept got
        # the remaining 35 s of its 60 for an 82-yard walk and timed out on the way.
        clock = getattr(self.body, "routine_clock", None)
        routine_age = (None if clock is None else 0.0 if clock == math.inf
                       else max(0.0, now - clock))
        exhausted = None
        if self.worker and self.worker.done.is_set():
            worker, self.worker = self.worker, None
            worker.thread.join()
            result = worker.result or Result(SkillOutcome.ABORTED, "worker returned no result", "error")
            model_fault = (worker.arm is not None and worker.arm.rule.startswith("learned:")
                           and result.code in {"error", "unsupported"})
            if model_fault and self.runtime.policy_failed:
                try:
                    self.runtime.policy_failed(worker.state, result.detail or result.code)
                except Exception:
                    self.runtime.learned = None
            if worker.maintenance is not None:
                self.say(f"session: {result.detail}")
                if self.watchdog:
                    self.watchdog.completed(now)
                if result.code in {"credentials", "error"}:
                    self.failure = result.detail or result.code
                    self.stopped.set()
            elif worker.focus is not None:
                self.say(f"focus: {result.detail}")
                if result.outcome is not SkillOutcome.SUCCEEDED and self._person():
                    # Left to whoever is at the desk; the pause resumes play, not a stop.
                    self.say("focus: left to the person at the desk")
                elif result.outcome is not SkillOutcome.SUCCEEDED:
                    self.failure = result.detail or "focus restoration interrupted"
                    self.stopped.set()
            else:
                self.runtime.finish(result.outcome, result.detail, state=state)
                self.say(f"{worker.arm.decision.skill}: {result.code or result.outcome.value} {result.detail}")
                if worker.arm.decision.skill == "COMBAT_PROFILE" and state is not None:
                    # The state's wall clock, as the policy reads it (`_fight`).
                    self.runtime.policy_context.fight_ended(result.code, state.t)
                    if self.runtime.policy_context.fight_paused(state.t):
                        self.say("  fights that never engaged: combat paused, walking on")
                if result.code == "no_junk":
                    self.runtime.policy_context.bags_failed(
                        state.bags.free if state is not None else None)
                if (worker.arm.decision.skill == "BIND_HEARTH"
                        and result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)):
                    # An inn out of reach is not walked to again on this step.
                    self.runtime.policy_context.bind_failed(
                        state.guide.step_id if state is not None else None)
                if (worker.arm.decision.skill == "DISCOVER_FLIGHT"
                        and result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)):
                    self.runtime.policy_context.discover_failed(
                        state.guide.step_id if state is not None else None)
                if (worker.arm.decision.skill == "TRAIN_CLASS"
                        and result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)):
                    # A trainer out of reach is not walked to again this level; a visit a
                    # fight cut short (preempted) is.
                    self.runtime.policy_context.train_failed(state.char.level)
                if (worker.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD"
                        and result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)
                        and result.code not in ("too_poor",)):
                    # A merchant out of reach is not walked to again on this step (V175).
                    self.runtime.policy_context.supplies_unreachable(worker.arm.step_id)
                if (worker.arm.decision.skill == "VENDOR_REPAIR"
                        and result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)
                        and result.code not in ("too_poor",)):
                    # Nor a repairer (V185).
                    self.runtime.policy_context.repair_unreachable(worker.arm.step_id)
                if result.code == "too_poor":
                    if worker.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD":
                        self.runtime.policy_context.supplies_failed(state.bags.money_copper)
                    else:
                        self.runtime.policy_context.repair_failed(state.bags.money_copper)
                key = worker.arm.step_id, worker.arm.decision.skill
                if result.outcome is SkillOutcome.SUCCEEDED:
                    self.failures.pop(key, None)
                elif (result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)
                      and result.code not in ("too_poor", "no_junk")
                      # Training is optional: a trainer out of reach waits for the next
                      # level (above), never stops the run. One nameplate Brother Wilhelm
                      # did not answer from inside Goldshire's smithy stopped session 66.
                      # A meal that runs out of time is armed again while the character is
                      # still low: one drink too many for its budget stopped session 107.
                      and worker.arm.decision.skill not in ("TRAIN_CLASS", "BIND_HEARTH",
                                                            "DISCOVER_FLIGHT", "EAT_DRINK",
                                                            "BUY_AMMO_REAGENT_FOOD",
                                                            "VENDOR_REPAIR")
                      and not reflex(worker.arm.rule)):
                    self.failures[key] = self.failures.get(key, 0) + 1
                    if self.failures[key] >= self.max_failures:
                        exhausted = key, result.detail
            if (result.code in {"error", "unsupported", "no_food", "refused",
                                "teacher_unavailable", "teaching_stalled"}
                    and not model_fault and worker.maintenance is None):
                self.failure = result.detail or result.code
                self.stopped.set()

        needs_focus = self.has_focus is not None and not self.has_focus()
        operator = self.operator_active is not None and self.operator_active()
        if operator != self._operator_paused:
            self._operator_paused = operator
            self.say("operator active: paused until the desk is quiet" if operator
                     else "operator quiet: resuming")
        suspected = operator or self._person()
        # The step's own clock stands still with the pause (`Tracker.paused`).
        tracker = getattr(self.runtime, "tracker", None)
        if tracker is not None:
            tracker.paused = operator
        choose = (self.worker is None and not needs_focus and now >= self.next_coach
                  and not self.stopped.is_set())
        record = now >= self.next_record
        state = self.runtime.tick(choose=choose, record=record, state=state)
        if getattr(self.runtime, "foreign", None) is not None and not self.stopped.is_set():
            self.failure = ("another character is logged in; each keeps its own playhead, "
                            "so this run stops with this one's saved")
            self.stopped.set()
            if self.worker:
                self.worker.cancel(self.failure)
        if self.watchdog:
            memory = getattr(getattr(self.runtime, "tracker", None), "memory", None)
            # A service the step waits on - a meal, a merchant, a trainer - is not a stall:
            # each is bounded by its own timeout, and the step's clock already stands still
            # for it (`Tracker.serving`). Session 101's walk of 389 yards to a merchant was
            # failed over as "no quest or experience progress".
            serving = getattr(getattr(self.runtime, "tracker", None), "serving", False) is True
            self.watchdog.observe(state, now, closest=getattr(memory, "closest", None),
                                  paused=operator or serving)
            if self.watchdog.escalate:
                self.watchdog.escalate = False
                step = self.runtime.tracker.step_id
                if self.runtime.expire_step():
                    self.say(f"watchdog: no progress on {step}; failing it over")
                    if self.worker and self.worker.arm is not None:
                        self.worker.cancel("no quest or experience progress; step failed over")
            if self.watchdog.failure:
                self.failure = self.watchdog.failure
                self.stopped.set()
                if self.worker:
                    self.worker.cancel(self.failure)
        if self.housekeeping is not None:
            try:
                self.housekeeping(state)
            except Exception as exc:
                if not self._housekeeping_failed:
                    self.say(f"background maintenance unavailable: {type(exc).__name__}: {exc}")
                self._housekeeping_failed = True
        # Give the tracker's on_fail edge first refusal; stop only if it cannot rejoin.
        if exhausted and self.runtime.tracker.step_id == exhausted[0][0]:
            fail_over = getattr(self.runtime, "fail_over", None)
            if fail_over is not None and fail_over(exhausted[0][1], exhausted[1] or ""):
                self.say(f"{exhausted[0][1]} out of attempts on {exhausted[0][0]}; "
                         f"failed over to {self.runtime.tracker.step_id}")
                self.failures.pop(exhausted[0], None)
            else:
                self.failure = f"{exhausted[0]}: {self.max_failures} failed attempts; {exhausted[1]}"
                self.stopped.set()
        if choose and self.runtime.armed and self.runtime.armed.rule == "unavailable":
            self.failure = self.runtime.armed.decision.why
            self.stopped.set()
        if record:
            self.next_record = now + 0.5
        if choose:
            self.next_coach = now + 0.5
        if self.worker and self.worker.arm is not None:
            if self.runtime._tracker_event in {"fail", "rejoin_or_skip"}:
                self.worker.completion_observed = False
            if (self.runtime._tracker_event == "advance"
                    and self.runtime._tracker_from == self.worker.arm.step_id
                    and self.worker.arm.decision.skill in {"ACCEPT_QUEST", "TURNIN_QUEST", "GRIND_UNTIL"}):
                # These compositions confirm the same guide predicate themselves, then
                # finish their bounded tail (notably looting a kill). Let them acknowledge
                # completion; a fail edge still cancels, and hard preempts still win.
                self.worker.completion_observed = True
            if (getattr(self.body, "handles_modal", False)
                    and self.worker.arm.decision.skill == "ABORT_WAIT" and state.ui.modal is False
                    and self.runtime._tracker_event not in {"fail", "rejoin_or_skip"}):
                # Let the same input owner acknowledge Escape's observed result even
                # if combat is active; the next arm can then service combat normally.
                self.worker.completion_observed = True
            walking = self.body.travelling is True
            if clock != self.worker._walk_clock:
                # A routine that takes over from the tutor starts its own budget, and only
                # its own walking comes off it (review, 25 September).
                self.worker.walked_s, self.worker._walk_clock = 0.0, clock
            if walking and self.worker._walk_seen is not None:
                self.worker.walked_s += max(0.0, now - self.worker._walk_seen)
            self.worker._walk_seen = now if walking else None
            reason = "operator active" if operator else interruption(
                self.worker.arm, state, travelling=self.body.travelling,
                combat_paused=self.runtime.policy_context.fight_paused(state.t),
                completion_observed=self.worker.completion_observed,
                handles_modal=getattr(self.body, "handles_modal", False),
                falling_s=falling_s, routine_age_s=routine_age,
                exposed=getattr(self.body, "tutor_exposed", False) is True,
                walked_s=self.worker.walked_s)
            # Hunt yields between pulls, after looting. Interrupting it as combat drops
            # would leave the killed corpse behind. A standalone travel leg can yield now.
            if reason is None and self.worker.arm.decision.skill == "TRAVEL_TO":
                needed = service(state, context=self.runtime.policy_context)
                if needed is not None:
                    reason = f"service needed: {needed.decision.why}"
            if reason:
                self.worker.cancel(reason)
        elif self.worker is not None and self.worker.arm is None and operator:
            # Taking the window back or reconnecting stops for a person as a skill does.
            self.worker.cancel("operator active")
        elif self.worker is None and not self.stopped.is_set() and needs_focus and not suspected:
            # Raising the already-bound window sends no game keys. Keep recording while
            # the existing focus backoff runs, even if another window hides the radio.
            self.worker = Worker(self.body, None, state, focus=self.focus)
            self.worker.thread.start()
        elif (self.worker is None and not self.stopped.is_set() and self.watchdog and not suspected
              and (maintenance := self.watchdog.maintenance(now)) is not None):
            self.runtime.finish(SkillOutcome.PREEMPTED, "session reconnect")
            self.worker = Worker(self.body, None, state, maintenance=maintenance)
            self.worker.thread.start()
        elif (self.worker is None and choose and not operator and not self.stopped.is_set()
              and self.runtime.armed
              and self.runtime.armed.decision.skill not in (None, "IDLE")
              and (self.runtime.armed.decision.skill != "ABORT_WAIT"
                   or getattr(self.body, "executes_wait", False))):
            arm = self.runtime.armed
            reason = interruption(arm, state, handles_modal=getattr(self.body, "handles_modal", False),
                                  falling_s=falling_s)
            if not reason:
                self.worker = Worker(self.body, arm, state, recorder=self.runtime.recorder)
                self.worker.thread.start()
        return state

    def _person(self) -> bool:
        """A person at the desk, or input that may be one (`operator_suspected`)."""
        return ((self.operator_active is not None and self.operator_active())
                or (self.operator_suspected is not None and self.operator_suspected()))

    def run(self, seconds: float, *, max_steps: int = 0) -> None:
        deadline = time.monotonic() + seconds
        waited = False
        ended: float | None = None
        try:
            while not self.stopped.is_set():
                started = time.monotonic()
                if started >= deadline:
                    if not self._fighting() or started >= deadline + DEADLINE_COMBAT_GRACE_S:
                        break
                    if not waited:
                        waited = True
                        self.say("run time is up; stopping once this fight is over")
                self.step(started)
                if ((self.runtime.finished and self.worker is None)
                        or (max_steps and self.runtime.counters.advances >= max_steps)):
                    ended = started if ended is None else ended
                    if not self._fighting() or started >= ended + DEADLINE_COMBAT_GRACE_S:
                        break
                    if not waited:
                        waited = True
                        self.say("the guide is finished; stopping once this fight is over")
                self.stopped.wait(max(0.0, 0.25 - (time.monotonic() - started)))
        finally:
            self.close()

    def _fighting(self) -> bool:
        state = self.runtime.last_state
        return (state is not None and state.vitals.combat is True
                and state.vitals.dead is not True and state.vitals.ghost is not True)

    def close(self) -> None:
        self.stopped.set()
        if self.worker:
            self.worker.cancel("operator stop or run deadline")
            # Keep capture and the recorder alive until input ownership is relinquished.
            while not self.worker.done.wait(0.25):
                pass
            self.worker.thread.join()
            result = self.worker.result
            if result and self.worker.arm is not None and result.outcome is not SkillOutcome.PREEMPTED:
                self.runtime.finish(result.outcome, result.detail)
            self.worker = None
        self.body.release()
        self.runtime.finish(SkillOutcome.UNKNOWN, "run ended")
