"""One body worker, independently clocked tracking and recording, cooperative stops.

The worker owns all input. A replacement never starts until the previous worker has
released its inputs and returned. Capture or a slow skill cannot silently kill a detached
heartbeat: the supervisor is the main loop and recorder failures propagate to cleanup.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from jev.coach.policy import Context, service
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed, ClientRuntime
from jev.skills.catalog import get
from jev.world.state_v1 import State


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
                 maintenance: Callable[[Callable[[], None]], Result] | None = None):
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
        finally:
            try:
                self.body.release()
            except Exception as exc:
                self.result = Result(SkillOutcome.ABORTED, f"input cleanup failed: {exc}", "error")
            finally:
                self.done.set()


def interruption(arm: Armed, state: State, *, travelling: bool = False,
                 completion_observed: bool = False) -> str | None:
    skill = arm.decision.skill
    if not state.sense.addon_ok and (state.sense.vision_conf or 0.0) < 0.5:
        return "perception unavailable"
    recovery = skill in {"RELEASE_SPIRIT", "CORPSE_RUN"}
    if (state.vitals.dead is True or state.vitals.ghost is True) and not recovery:
        return "dead or ghost"
    if state.ui.modal is True and not recovery:
        return "blocking modal"
    if state.flags.falling is True and not recovery:
        return "falling"
    fighting = skill in {"COMBAT_PROFILE", "APPROACH_TARGET", "ACQUIRE_TARGET", "GRIND_UNTIL", "LOOT"}
    if state.vitals.combat is True and (travelling or not fighting) and not recovery:
        return "combat interrupted the leg or service"
    if arm.step_id != state.guide.step_id and not recovery and not completion_observed:
        return "playhead changed"
    spec = get(skill or "")
    if spec and state.t - arm.at >= spec.timeout_s:
        return "skill timeout"
    return None


class Supervisor:
    def __init__(self, runtime: ClientRuntime, body: Body,
                 *, say: Callable[[str], None] = print, max_failures: int = 3,
                 has_focus: Callable[[], bool] | None = None,
                 focus: Callable[[Callable[[], None]], bool] | None = None,
                 housekeeping: Callable[[State], None] | None = None,
                 watchdog=None):
        if (has_focus is None) != (focus is None):
            raise ValueError("focus observation and restoration must be supplied together")
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

    def step(self, now: float | None = None) -> State:
        now = time.monotonic() if now is None else now
        state = self.runtime.source.read()
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
                if result.outcome is not SkillOutcome.SUCCEEDED:
                    self.failure = result.detail or "focus restoration interrupted"
                    self.stopped.set()
            else:
                self.runtime.finish(result.outcome, result.detail, state=state)
                self.say(f"{worker.arm.decision.skill}: {result.code or result.outcome.value} {result.detail}")
                if result.code == "too_poor":
                    if worker.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD":
                        self.runtime.policy_context.supplies_failed(state.bags.money_copper)
                    else:
                        self.runtime.policy_context.repair_failed(state.bags.money_copper)
                key = worker.arm.step_id, worker.arm.decision.skill
                if result.outcome is SkillOutcome.SUCCEEDED:
                    self.failures.pop(key, None)
                elif result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT) and result.code != "too_poor":
                    self.failures[key] = self.failures.get(key, 0) + 1
                    if self.failures[key] >= self.max_failures:
                        exhausted = key, result.detail
            if (result.code in {"error", "unsupported", "no_food", "refused"}
                    and not model_fault and worker.maintenance is None):
                self.failure = result.detail or result.code
                self.stopped.set()

        needs_focus = self.has_focus is not None and not self.has_focus()
        choose = (self.worker is None and not needs_focus and now >= self.next_coach
                  and not self.stopped.is_set())
        record = now >= self.next_record
        state = self.runtime.tick(choose=choose, record=record, state=state)
        if self.watchdog:
            self.watchdog.observe(state, now)
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
            reason = interruption(self.worker.arm, state, travelling=self.body.travelling,
                                  completion_observed=self.worker.completion_observed)
            # Hunt yields between pulls, after looting. Interrupting it as combat drops
            # would leave the killed corpse behind. A standalone travel leg can yield now.
            if reason is None and self.worker.arm.decision.skill == "TRAVEL_TO":
                needed = service(state, context=self.runtime.policy_context)
                if needed is not None:
                    reason = f"service needed: {needed.decision.why}"
            if reason:
                self.worker.cancel(reason)
        elif self.worker is None and not self.stopped.is_set() and needs_focus:
            # Raising the already-bound window sends no game keys. Keep recording while
            # the existing focus backoff runs, even if another window hides the radio.
            self.worker = Worker(self.body, None, state, focus=self.focus)
            self.worker.thread.start()
        elif (self.worker is None and not self.stopped.is_set() and self.watchdog
              and (maintenance := self.watchdog.maintenance(now)) is not None):
            self.runtime.finish(SkillOutcome.PREEMPTED, "session reconnect")
            self.worker = Worker(self.body, None, state, maintenance=maintenance)
            self.worker.thread.start()
        elif (self.worker is None and choose and not self.stopped.is_set() and self.runtime.armed
              and self.runtime.armed.decision.skill not in (None, "IDLE", "ABORT_WAIT")):
            arm = self.runtime.armed
            reason = interruption(arm, state)
            if not reason:
                self.worker = Worker(self.body, arm, state)
                self.worker.thread.start()
        return state

    def run(self, seconds: float, *, max_steps: int = 0) -> None:
        deadline = time.monotonic() + seconds
        try:
            while not self.stopped.is_set() and time.monotonic() < deadline:
                started = time.monotonic()
                self.step(started)
                if ((self.runtime.finished and self.worker is None)
                        or (max_steps and self.runtime.counters.advances >= max_steps)):
                    break
                self.stopped.wait(max(0.0, 0.25 - (time.monotonic() - started)))
        finally:
            self.close()

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
