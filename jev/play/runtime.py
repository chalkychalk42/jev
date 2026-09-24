"""Production composition of the visual tutor, existing body and continuous learner."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from jev.coach.policy import reflex
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


class MotorLearningService:
    """Training never owns HID and cannot block the supervisor's stop clock."""

    def __init__(self, learner, runs: Path, *, interval_s: float = 30, live: Path | None = None):
        self.learner, self.runs, self.interval_s = learner, Path(runs), interval_s
        # The run in progress: its controller records every row itself, and re-reading it
        # each cycle held the store's lock in bursts the controller had to wait through.
        self.live = Path(live) if live is not None else None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="jev-motor-learner", daemon=True)
        self.error = None

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        # A run is re-read only when its play files changed. Re-reading all of them every
        # cycle took and released the store's lock once per recorded row, forty runs over,
        # and a blocking lock on Windows gives up after ten seconds: a cycle failed with
        # `PermissionError: [Errno 13]` (run 20260923T232300-e3b21c), and the teaching
        # controller writes through the same lock.
        seen: dict[Path, tuple] = {}
        while not self.stop.is_set():
            try:
                recovered = []
                for directory in sorted(self.runs.iterdir()) if self.runs.exists() else ():
                    if self.stop.is_set():
                        break
                    if directory == self.live:
                        continue
                    if directory.is_dir() and (directory / "play-actions.jsonl").exists():
                        stamp = tuple((p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
                                      for p in (directory / "play-actions.jsonl",
                                                directory / "play-episodes.jsonl"))
                        if seen.get(directory) == stamp:
                            continue
                        report = self.learner.ingest_run(directory)
                        if report.get("errors"):
                            recovered.append({"run": directory.name, "errors": report["errors"]})
                        else:
                            seen[directory] = stamp
                report = self.learner.update(cancelled=self.stop.is_set)
                atomic_json(self.learner.directory / "latest-cycle.json",
                            {"t": time.time(), "cycle": report, "ingest_errors": recovered})
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self.stop.wait(self.interval_s)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.error = "motor learner still finishing optional offline work"


class PlayingBody:
    """The same supervisor/guide, with teacher/student ownership inside an armed skill."""

    handles_modal = True
    executes_wait = True

    def __init__(self, spine, *, recorder, store: Path, screenshots, mode: str = "teach",
                 teacher_model: str | None = None, teacher_binary: str | None = None,
                 teacher_provider: str = "claude", teacher_base_url: str | None = None,
                 teacher_env_file: Path | None = None, teacher_key_env: str = "GLM_API_KEY",
                 teacher_effort: str | None = None,
                 teacher_calls_per_hour: int = 240, binding_paths=(),
                 config: PlayConfig | None = None, teacher=None, learner=None,
                 start_learning: bool = True, world_db: Path | None = DEFAULT_WORLD_DB):
        if config is not None and mode != config.mode:
            raise ValueError("playing mode and configuration disagree")
        teacher_model = model_for(teacher_provider, teacher_model)
        self.spine, self.client, self.graph = spine, spine.client, spine.graph
        self.available = spine.available
        self._arm = None
        self.routine_clock: float | None = None
        self._checkpoint = lambda: None
        self._closed = False
        self.journal = PlayJournal(recorder.dir, run_id=recorder.run_id)
        self.learning_service = None
        values = self.client.read()
        if values is None:
            raise ValueError("playing setup requires observed character controls")
        self.manifest = build_manifest(values, binding_paths=binding_paths,
                                       skills=self.available)
        self.controls_fingerprint = fingerprint(self.manifest.to_dict())
        self.knowledge = LocalKnowledge(self.graph, world_db=world_db)
        self.learner = learner or MotorLearner(Path(store) / "motor")
        self.observer = LiveObserver(self.client, self.graph, screenshots=screenshots)
        self.executor = Executor(self.client.hid, self.observer.guard, manifest=self.manifest,
                                 checkpoint=lambda: self._checkpoint(),
                                 execute_skill=self._delegate,
                                 invalidate_camera=self.spine.camera.invalidate)
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
            config=config or PlayConfig(mode=mode), say=spine.say, skills_for=self.delegable)
        atomic_json(recorder.dir / "play-config.json", {
            "mode": mode, "config": asdict(self.controller.config),
            "controls": self.manifest.to_dict(), "controls_fingerprint": self.controls_fingerprint,
            "knowledge_fingerprint": self.knowledge.fingerprint,
            "teacher_requested": teacher_model, "teacher_calls_per_hour": teacher_calls_per_hour,
            "teacher_provider": teacher_provider,
            "learning_store": str(self.learner.directory), "live_validated": False,
        })
        if start_learning:
            self.learning_service = MotorLearningService(self.learner, recorder.dir.parent,
                                                         live=recorder.dir).start()

    @property
    def travelling(self):
        return self.spine.travelling

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
            return self._execute(arm, state, checkpoint)
        finally:
            self.routine_clock = None

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
        if reflex(arm.rule):
            self.journal.append("actions", {"event": "reflex", "t": time.time(),
                                            "arm_id": arm.arm_id, "skill": arm.decision.skill,
                                            "rule": arm.rule})
            if state is None:
                state = State.model_validate(self.observer.observe(arm).data["state"])
            self.routine_clock = time.monotonic()
            return self.spine.execute(arm, state, focused_checkpoint)
        result = self.controller.run(arm, focused_checkpoint)
        if (result.code not in {"teacher_unavailable", "teaching_stalled"}
                or arm.decision.skill in {"ABORT_WAIT", "IDLE"}):
            return result
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
        try:
            return self.spine.execute(delegated, state, self._checkpoint)
        finally:
            self.spine.arm = arm

    def poll(self, state):
        if self.learning_service and self.learning_service.error:
            self.spine.say(f"motor learner: {self.learning_service.error}")
            self.learning_service.error = None

    def release(self):
        self.spine.release()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.learning_service:
                self.learning_service.close()
        finally:
            self.journal.close()
