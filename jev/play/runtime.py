"""Production composition of the visual tutor, the existing body and the motor corpus."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, replace
from pathlib import Path

from jev.coach.policy import reflex, routine_only
from jev.coach.schema import Intent
from jev.learn.episode import SkillOutcome
from jev.persist import atomic_json
from jev.play.controller import PlayConfig, PlayController
from jev.play.controls import build_manifest
from jev.play.executor import Executor
from jev.play.journal import PlayJournal
from jev.play.knowledge import LocalKnowledge
from jev.play.learning import MotorLearner
from jev.play.observation import LiveObserver, fingerprint
from jev.play.providers import make_vision_client, model_for
from jev.play.teacher import VisionTeacher
from jev.play.world_knowledge import DEFAULT_WORLD_DB
from jev.run.supervisor import FocusLost, Result
from jev.teacher.bridge import BudgetClient
from jev.world.state_v1 import State

# Routines that are a whole objective loop rather than a step within one. Delegating the
# hunt from inside the hunt objective handed every decision - which unit, when to loot,
# when to eat, when to move on - back to the scripted loop for up to ten minutes, which is
# the decision-making Jev is there to do and the student is there to learn. It remains
# the scripted fallback when the tutor cannot answer or stalls.
OBJECTIVE_LOOPS = frozenset({"GRIND_UNTIL"})
# Routine results that are not the routine failing its objective: nothing sellable, too
# poor, or the objective already done.
ROUTINE_NOT_FAILED = frozenset({"no_junk", "too_poor", "nothing", "done"})


def delegable_skills(current: str, available) -> tuple[str, ...]:
    """The routines Jev may run inside a guide objective armed with `current`.

    Recovery arms delegate only themselves. Waits are not routines, and an objective loop
    is not a step of itself. Everything offered must exist in the body's executor
    catalog. The tutor's menu and the delegation check both use this one rule.
    """
    allowed = {current, "EAT_DRINK", "LOOT", "COMBAT_PROFILE", "FACE_TARGET"}
    if current in {"GRIND_UNTIL", "ACCEPT_QUEST", "TURNIN_QUEST", "TRAVEL_TO"}:
        allowed.add("TRAVEL_TO")
    if current in {"RELEASE_SPIRIT", "CORPSE_RUN"}:
        allowed = {current}
    allowed -= {"ABORT_WAIT", "IDLE"} | OBJECTIVE_LOOPS
    return tuple(sorted(allowed & set(available)))


# After the tutor stalls on a step, its re-arms go to the routine for this long. A stalled
# episode earns no labels, and every fight re-arms the objective: at Eastvale the tutor
# stalled on the wood bundles after every fight, two minutes each, and the routine then
# found them (session 118).
STALL_REST_S = 600.0


class PlayingBody:
    """The same supervisor/guide, with the tutor's ownership inside an armed skill."""

    handles_modal = True
    executes_wait = True

    def __init__(self, spine, *, recorder, store: Path, screenshots,
                 teacher_model: str | None = None, teacher_binary: str | None = None,
                 teacher_provider: str = "claude", teacher_base_url: str | None = None,
                 teacher_env_file: Path | None = None, teacher_key_env: str = "GLM_API_KEY",
                 teacher_effort: str | None = None,
                 teacher_calls_per_hour: int = 240, binding_paths=(),
                 config: PlayConfig | None = None, teacher=None, learner=None,
                 world_db: Path | None = DEFAULT_WORLD_DB):
        # The tutor plays; no student acts or trains in the session (V174, V225), and the
        # motor corpus is still recorded. An ordinary objective goes to the guide's own
        # routine first, and to the tutor only after its routine failed (`_ask_tutor`,
        # hybrid dispatch). The tutor-first dispatch and the loop's A/B went with V223.
        # The objectives whose routine just failed, and how it failed (`_note_routine`).
        self._routine_failed: dict[tuple[str | None, str | None], str] = {}
        # Whether such an objective goes to the tutor or back to its routine, learned from
        # how each did (`jev.learn.choices.Choice`, "recover.after_failure"); `None` always
        # asks the tutor. And the attempt being judged: (objective, option, since).
        self.recovery = None
        self._recovering: tuple[str, str, float] | None = None
        self._stalled: dict[tuple[str | None, str], float] = {}
        teacher_model = model_for(teacher_provider, teacher_model)
        self.spine, self.client, self.graph = spine, spine.client, spine.graph
        self.available = spine.available
        self._arm = None
        self.routine_clock: float | None = None
        # The skill the tutor has handed to a routine right now, if any.
        self._delegating: str | None = None
        self._checkpoint = lambda: None
        self._closed = False
        self.journal = PlayJournal(recorder.dir, run_id=recorder.run_id)
        values = self.client.read()
        if values is None:
            raise ValueError("playing setup requires observed character controls")
        self.manifest = build_manifest(values, binding_paths=binding_paths,
                                       skills=self.available)
        self.controls_fingerprint = fingerprint(self.manifest.to_dict())
        self.knowledge = LocalKnowledge(self.graph, world_db=world_db)
        self.learner = learner or MotorLearner(Path(store) / "motor")
        # What these controls mean, for the learner to pool earlier generations' records.
        self.learner.remember_controls(self.controls_fingerprint, self.manifest.to_dict())
        self.observer = LiveObserver(self.client, self.graph, screenshots=screenshots)
        self.executor = Executor(self.client.hid, self.observer.guard, manifest=self.manifest,
                                 checkpoint=lambda: self._checkpoint(),
                                 execute_skill=self._delegate,
                                 invalidate_camera=self.spine.camera.invalidate,
                                 plates=self.observer.plates)
        if teacher is None:
            transport = make_vision_client(
                provider=teacher_provider, binary=teacher_binary, model=teacher_model,
                base_url=teacher_base_url, env_file=teacher_env_file, key_env=teacher_key_env,
                effort=teacher_effort)
            budget = BudgetClient(transport, Path(store) / "motor-teacher-budget.sqlite",
                                  calls_per_hour=teacher_calls_per_hour, interval_s=0)
            teacher = VisionTeacher(transport, knowledge=self.knowledge,
                                     reserve_call=budget.reserve,
                                     record_call=lambda result: self.journal.append(
                                         "teacher", {"event": "transport", "t": time.time(), **asdict(result)}))
        self.controller = PlayController(
            observer=self.observer, executor=self.executor, teacher=teacher,
            learner=self.learner, journal=self.journal, controls=self.manifest.to_dict(),
            controls_fingerprint=self.controls_fingerprint,
            knowledge_fingerprint=self.knowledge.fingerprint,
            config=config or PlayConfig(), say=spine.say, skills_for=self.delegable)
        atomic_json(recorder.dir / "play-config.json", {
            "mode": self.controller.config.mode, "config": asdict(self.controller.config),
            "controls": self.manifest.to_dict(), "controls_fingerprint": self.controls_fingerprint,
            "knowledge_fingerprint": self.knowledge.fingerprint,
            # The reports tell runs apart by it: the runs before V223 may say "tutor".
            "dispatch": "hybrid",
            "teacher_requested": teacher_model, "teacher_calls_per_hour": teacher_calls_per_hour,
            "teacher_provider": teacher_provider,
            "learning_store": str(self.learner.directory), "live_validated": False,
        })

    @property
    def travelling(self):
        return self.spine.travelling

    @property
    def tutor_exposed(self) -> bool:
        """The tutor holds the objective and nothing is fighting back: no delegated fight
        is running. Combat must not wait on a tutor's decision (V35): a level 7 paladin
        lost 51% to 0 in 30 s to a Defias Thug it had not selected, while the tutor played
        the hunt it was on (run 20260924T055951-0c4439)."""
        return self.routine_clock == math.inf and self._delegating != "COMBAT_PROFILE"

    @property
    def policy_context(self):
        return self.spine.policy_context

    @policy_context.setter
    def policy_context(self, value):
        self.spine.policy_context = value

    def has_focus(self):
        return self.spine.has_focus()

    def reconnect(self, checkpoint, *, env_file=None):
        return self.spine.reconnect(checkpoint, env_file=env_file)

    def validate(self, decision, step_id):
        return self.spine.validate(decision, step_id)

    def execute(self, arm, state, checkpoint):
        self._arm = arm
        # The supervisor's skill budget: bounded by the teaching episode while the tutor
        # plays, and the catalog's from the moment a scripted routine takes over.
        self.routine_clock = math.inf
        try:
            result = self._execute(arm, state, checkpoint)
            self._recovered(result)
            return result
        finally:
            self.routine_clock = None
            self._recovering = None          # cut short by an exception: nothing learned

    def _execute(self, arm, state, checkpoint):

        def focused_checkpoint():
            checkpoint()
            if not self.has_focus():
                raise FocusLost("client lost focus during teaching")

        self._checkpoint = focused_checkpoint
        self.client.hid.checkpoint = focused_checkpoint
        self.spine.checkpoint = focused_checkpoint
        self.spine.arm = arm
        error = self.validate(arm.decision, arm.step_id)
        if error:
            return Result(SkillOutcome.ABORTED, error, "unsupported")
        focused_checkpoint()
        self.spine.ready_camera(state)
        # A fight the character is already in, and death, are never put to the tutor
        # (`policy.reflex`). In run 20260923T172056-54f4d5 one unanswered tutor call in a
        # fight took the character from full health to dead without a swing, and the dead
        # character's one routine then sat behind a menu offering only Esc, which opened
        # the game menu five times. DECISIONS V35: combat and recovery keep the floor.
        if reflex(arm.rule) or routine_only(arm.rule):
            self.journal.append("actions", {"event": "reflex" if reflex(arm.rule) else "routine",
                                            "t": time.time(), "arm_id": arm.arm_id,
                                            "skill": arm.decision.skill, "rule": arm.rule})
            if state is None:
                state = State.model_validate(self.observer.observe(arm).data["state"])
            self.routine_clock = time.monotonic()
            return self.spine.execute(arm, state, focused_checkpoint)
        if not self._ask_tutor(arm):
            self.journal.append("actions", {"event": "hybrid_routine", "t": time.time(),
                                            "arm_id": arm.arm_id, "skill": arm.decision.skill,
                                            "step_id": arm.step_id})
            if state is None:
                state = State.model_validate(self.observer.observe(arm).data["state"])
            self.routine_clock = time.monotonic()
            result = self.spine.execute(arm, state, focused_checkpoint)
            self._note_routine(arm, result)
            return result
        stalled = self._stalled.get((arm.step_id, arm.decision.skill))
        if (stalled is not None and time.monotonic() - stalled < STALL_REST_S
                and arm.decision.skill not in {"ABORT_WAIT", "IDLE"}):
            self.journal.append("actions", {"event": "stalled_routine", "t": time.time(),
                                            "arm_id": arm.arm_id, "skill": arm.decision.skill,
                                            "step_id": arm.step_id})
            self._recovering = None          # given to the tutor, played by the routine
            if state is None:
                state = State.model_validate(self.observer.observe(arm).data["state"])
            self.routine_clock = time.monotonic()
            return self.spine.execute(arm, state, focused_checkpoint)
        result = self.controller.run(arm, focused_checkpoint)
        if (result.code not in {"teacher_unavailable", "teaching_stalled"}
                or arm.decision.skill in {"ABORT_WAIT", "IDLE"}):
            return result
        if result.code == "teaching_stalled":
            self._stalled[(arm.step_id, arm.decision.skill)] = time.monotonic()
        # The system makes progress with zero teacher calls (ARCHITECTURE.md section 0).
        # A tutor that cannot answer - provider down, overloaded past the deadline, or an
        # invalid reply after its re-ask - or that made no verified progress within its
        # bounded episode hands this objective to the guide's own routine rather than
        # stopping the run; the failed episode stays recorded and earns no labels. The
        # next objective asks Jev again. Waits are not routines: a dialog nobody can
        # dismiss stays a stop for a human.
        self.spine.say(f"tutor unavailable ({result.detail}); "
                       f"the scripted {arm.decision.skill} routine plays this objective")
        self.journal.append("actions", {"event": "scripted_fallback", "t": time.time(),
                                        "arm_id": arm.arm_id, "skill": arm.decision.skill,
                                        "reason": result.detail})
        current = self.observer.observe(arm)
        self.journal.observation(current.data)
        state = State.model_validate(current.data["state"])
        if state.ui.modal is True:
            # The tutor's last Escape can open the game menu on its way out, and every
            # routine refuses to act behind a modal: the scripted sale after it failed and
            # the run stopped (run 20260924T012032-0c0c24). Handed back, the policy's own
            # ABORT_WAIT clears it first, as the next session's did.
            return Result(SkillOutcome.PREEMPTED,
                          "a blocking dialog is up; the policy clears it first", "interrupted")
        self.routine_clock = time.monotonic()
        return self.spine.execute(arm, state, focused_checkpoint)

    def _ask_tutor(self, arm) -> bool:
        """Hybrid dispatch: does the tutor take this objective, or the guide's routine?

        The routine first, measured: teach mode cost about a fifth of play time on tutor
        decisions, and runs without the tutor levelled 1.6 to 5.5 times faster at levels 2
        and 3 (docs/plans/nine-hour-session.md). The tutor is asked where it earns its
        time: an objective whose routine has just failed, and then only if the learned
        recovery choice gives it to the tutor (V160).
        """
        key = (arm.step_id, arm.decision.skill)
        if key in self._routine_failed:
            code = self._routine_failed.pop(key)
            if self.recovery is None:
                return True
            objective = f"{arm.decision.skill}:{code}"
            option = self.recovery.pick(objective, ("tutor", "routine"))
            self._recovering = (objective, option, time.monotonic())
            return option == "tutor"
        return False

    def _note_routine(self, arm, result) -> None:
        """A routine that could not do its objective hands the next attempt to the tutor.
        Being interrupted (a fight, a stop) is not failing."""
        if result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT) \
                and result.code not in ROUTINE_NOT_FAILED:
            self._routine_failed[(arm.step_id, arm.decision.skill)] = result.code or "failed"

    def _recovered(self, result) -> None:
        """How the attempt after a routine's failure went, for whichever took it: done, or
        failed again. One interrupted - a fight, a stop - says neither."""
        if self._recovering is None or self.recovery is None:
            return
        objective, option, since = self._recovering
        self._recovering = None
        if result.outcome is SkillOutcome.SUCCEEDED:
            won = True
        elif (result.outcome in (SkillOutcome.ABORTED, SkillOutcome.TIMED_OUT)
              and result.code not in ROUTINE_NOT_FAILED):
            won = False
        else:
            return
        self.recovery.outcome(objective, option, won, time.monotonic() - since)

    def delegable(self, arm) -> tuple[str, ...]:
        return () if arm is None else delegable_skills(arm.decision.skill, self.available)

    def _delegate(self, action):
        """Reuse the existing implementations under the same input worker and objective.

        A delegated skill cannot invent coordinates, change the guide, execute code or
        recursively re-enter the playing controller. Existing validation remains binding.
        """
        arm = self._arm
        if arm is None:
            raise ValueError("no guide objective owns this skill request")
        current = arm.decision.skill
        if action.name not in self.delegable(arm):
            return Result(SkillOutcome.ABORTED, "skill does not serve the current guide objective", "unsupported")
        decision = arm.decision.model_copy(update={
            "skill": action.name, "params": action.params,
            "intent": Intent.REJOIN if action.name == "TRAVEL_TO" else arm.decision.intent,
        })
        error = self.validate(decision, arm.step_id)
        if error:
            return Result(SkillOutcome.ABORTED, error, "unsupported")
        reading = self.observer.observe(arm)
        self.journal.observation(reading.data)
        state = State.model_validate(reading.data["state"])
        if action.name == "TRAVEL_TO" and current == "GRIND_UNTIL":
            from jev.guide.objectives import select_objective

            node = self.graph.get(arm.step_id or "")
            destination = node
            if node and node.objective_targets:
                selection = select_objective(node, state.quests)
                destination = selection.target
            if (destination is None or destination.world is None
                    or destination.map_id != self.client.bounds.map_id):
                return Result(SkillOutcome.ABORTED, "current objective has no observed placed destination", "unsupported")
            ok = self.spine._approach(destination.world)
            return Result(SkillOutcome.SUCCEEDED if ok else SkillOutcome.ABORTED,
                          "current objective destination", "arrived" if ok else "unreachable")
        delegated = replace(arm, decision=decision, rule="play:trusted_skill")
        self._delegating = action.name
        try:
            return self.spine.execute(delegated, state, self._checkpoint)
        finally:
            self._delegating = None
            self.spine.arm = arm

    def release(self):
        self.spine.release()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.controller.settle(None)          # nothing left to judge a held episode by
        finally:
            self.journal.close()
