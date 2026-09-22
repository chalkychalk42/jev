"""Production composition of the visual tutor, existing body and continuous learner."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

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
from jev.play.teacher import ClaudeVisionClient, VisionTeacher
from jev.play.world_knowledge import DEFAULT_WORLD_DB
from jev.run.supervisor import FocusLost, Result
from jev.teacher.bridge import BudgetClient
from jev.world.state_v1 import State


class MotorLearningService:
    """Training never owns HID and cannot block the supervisor's stop clock."""

    def __init__(self, learner, runs: Path, *, interval_s: float = 30):
        self.learner, self.runs, self.interval_s = learner, Path(runs), interval_s
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="jev-motor-learner", daemon=True)
        self.error = None

    def start(self):
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.is_set():
            try:
                recovered = []
                for directory in sorted(self.runs.iterdir()) if self.runs.exists() else ():
                    if self.stop.is_set():
                        break
                    if directory.is_dir() and (directory / "play-actions.jsonl").exists():
                        report = self.learner.ingest_run(directory)
                        if report.get("errors"):
                            recovered.append({"run": directory.name, "errors": report["errors"]})
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
                 teacher_model: str = "sonnet", teacher_binary: str | None = None,
                 teacher_calls_per_hour: int = 240, binding_paths=(),
                 config: PlayConfig | None = None, teacher=None, learner=None,
                 start_learning: bool = True, world_db: Path | None = DEFAULT_WORLD_DB):
        if config is not None and mode != config.mode:
            raise ValueError("playing mode and configuration disagree")
        self.spine, self.client, self.graph = spine, spine.client, spine.graph
        self.available = spine.available
        self._arm = None
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
            transport = ClaudeVisionClient(binary=teacher_binary, model=teacher_model)
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
            config=config or PlayConfig(mode=mode), say=spine.say)
        atomic_json(recorder.dir / "play-config.json", {
            "mode": mode, "config": asdict(self.controller.config),
            "controls": self.manifest.to_dict(), "controls_fingerprint": self.controls_fingerprint,
            "knowledge_fingerprint": self.knowledge.fingerprint,
            "teacher_requested": teacher_model, "teacher_calls_per_hour": teacher_calls_per_hour,
            "learning_store": str(self.learner.directory), "live_validated": False,
        })
        if start_learning:
            self.learning_service = MotorLearningService(self.learner, recorder.dir.parent).start()

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
        return self.controller.run(arm, focused_checkpoint)

    def _delegate(self, action):
        """Reuse the existing implementations under the same input worker and objective.

        A delegated skill cannot invent coordinates, change the guide, execute code or
        recursively re-enter the playing controller. Existing validation remains binding.
        """
        arm = self._arm
        if arm is None:
            raise ValueError("no guide objective owns this skill request")
        current = arm.decision.skill
        allowed = {current, "EAT_DRINK", "LOOT", "COMBAT_PROFILE"}
        if current in {"GRIND_UNTIL", "ACCEPT_QUEST", "TURNIN_QUEST", "TRAVEL_TO"}:
            allowed.add("TRAVEL_TO")
        if current in {"RELEASE_SPIRIT", "CORPSE_RUN"}:
            allowed = {current}
        if action.name not in allowed:
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
