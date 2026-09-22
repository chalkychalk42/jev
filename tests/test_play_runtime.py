"""Production teaching composition exercised with painted radio and fake physical input."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image
from test_play_executor import Hid
from test_radio_frame import _paint, _values

from jev.clients.capture import Backend, Frame
from jev.coach.schema import Decision, Intent
from jev.guide.coords import ZoneBounds
from jev.guide.graph import Graph, Node, ObjectiveTarget
from jev.learn.episode import Recorder, SkillOutcome, read
from jev.orch.runtime import Armed, ClientRuntime
from jev.play.actions import KeyAction, SkillAction
from jev.play.controller import PlayConfig
from jev.play.learning import MotorPrediction
from jev.play.runtime import MotorLearningService, PlayingBody
from jev.play.teacher import PlayTeacherResult
from jev.run.body import LiveBody
from jev.run.client import Client, ClientSource
from jev.run.screenshots import Screenshots
from jev.run.supervisor import FocusLost, Supervisor, Worker
from jev.world.state_v1 import ArmedBy, StepKind


class PaintedCapture:
    def __init__(self):
        self.values = {**_values(), "seq": 1, "pos.zone_id": 1, "pos.mx": 0.1,
                       "pos.my": 0.5, "vitals.hp": 1.0, "vitals.combat": False,
                       "target.has": False, "target.name_id": None, "target.hp": None,
                       "quests.count": 0, "quests.log_hash": 0, "ui.error_id": 0,
                       "char.class_id": 2, "char.race_id": 1}
        self.frames = {}
        self.grabs = 0

    def grab(self):
        self.grabs += 1
        self.values["seq"] = self.grabs % 254
        pixels = _paint(self.values, gain=(1, 1, 1), lift=(0, 0, 0), noise=0, blur=False)
        self.frames[self.values["seq"]] = pixels.copy()
        return Frame(pixels, (10, 20), Backend.SCREEN)


class Learner:
    def __init__(self, directory, *, mode="shadow"):
        self.directory, self.mode = Path(directory), mode
        self.requests, self.records, self.episodes = [], [], []

    def predict(self, observation, capability, **kwargs):
        self.requests.append(kwargs)
        return MotorPrediction(action={"kind": "key", "control": "move_forward", "duration_s": 0.2},
                               mode=self.mode, expected_effect="arrived", model="fixture-student",
                               confidence=1.0)

    def record(self, row):
        self.records.append(row)

    def finish_episode(self, episode_id, **kwargs):
        self.episodes.append((episode_id, kwargs))


class Teacher:
    def __init__(self):
        self.requests = []

    async def decide(self, observation, png, **kwargs):
        self.requests.append((observation, png, threading.current_thread().name))
        return PlayTeacherResult("ok", observation["id"],
                                 action=KeyAction(control="move_forward", duration_s=0.2),
                                 expected_effect="arrived", capability="travel",
                                 rationale="fixture requests bounded forward movement",
                                 requested_model="fixture", actual_model="fixture")


def composition(tmp_path, *, mode="teach", prediction="shadow", node=None, config=None):
    cap, hid = PaintedCapture(), Hid()
    hid.position = (100, 100)
    hid.keys_down = lambda: sorted(hid.held)
    client = Client(0, hid, cap, origin=(10, 20), size=(420, 240), client_id="fixture")
    client.bounds = ZoneBounds(1, 0, 100, 0, 100, 0)
    client.coordinate_zones = {1: client.bounds}
    client.coordinate_names = {1: "fixture"}
    client.travel = SimpleNamespace(read_pos=None)
    client.approach = Mock(return_value=True)
    node = node or Node(id="step", kind=StepKind.TRAVEL, zone="fixture", zone_id=1,
                        coord_zone_id=1, pos=(0.7, 0.5), world=(50, 30, 0), map_id=0,
                        skills=("TRAVEL_TO",), r=0.02)
    graph = Graph(graph_id="fixture", faction="alliance", entry=node.id, coord_zone_id=1,
                  nodes=(node,))
    spine = LiveBody(client, graph, say=lambda text: None)
    recorder = SimpleNamespace(dir=tmp_path / "runs" / "fixture", run_id="fixture")
    screenshots = Screenshots(client.frame, recorder.dir / "screenshots")
    teacher, learner = Teacher(), Learner(tmp_path / "learning", mode=prediction)
    playing = PlayingBody(spine, recorder=recorder, store=tmp_path / "store",
                          screenshots=screenshots, mode=mode, teacher=teacher, learner=learner,
                          config=config or PlayConfig(mode=mode, max_actions=2, outcome_wait_s=0.05,
                                                      poll_s=0.01), start_learning=False)
    screenshots.start()
    skill = "GRIND_UNTIL" if node.kind is StepKind.QUEST_OBJECTIVE else "TRAVEL_TO"
    arm = Armed(Decision(goal="fixture guide objective", intent=Intent.ADVANCE, skill=skill,
                         abort_if=["dead"], confidence=1.0, why="fixture"),
                ArmedBy.POLICY, 0, "fixture", "decision", node.id, "situation", "arm")
    return SimpleNamespace(playing=playing, spine=spine, client=client, cap=cap, hid=hid,
                           teacher=teacher, learner=learner, arm=arm, screenshots=screenshots,
                           recorder=recorder)


@pytest.mark.parametrize(("mode", "prediction", "author"), [
    ("teach", "active", "teacher"), ("adaptive", "shadow", "teacher"),
    ("adaptive", "canary", "student"), ("adaptive", "active", "student"),
])
def test_real_worker_controller_and_executor_use_one_input_owner(tmp_path, mode, prediction, author):
    f = composition(tmp_path, mode=mode, prediction=prediction)
    owners = []
    def move():
        owners.append(threading.current_thread().name)
        f.cap.values["pos.mx"] = 0.7
    f.hid.on_hold = move
    state = f.client.state()
    worker = Worker(f.playing, f.arm, state)
    try:
        worker.thread.start()
        worker.thread.join(timeout=10)
        assert not worker.thread.is_alive()
        assert worker.result.outcome is SkillOutcome.SUCCEEDED, worker.result
        assert owners == ["jev-body"]
        assert bool(f.teacher.requests) is (author == "teacher")
        assert f.learner.requests[0]["allow_student"] is (mode == "adaptive")
        assert f.learner.records[0]["author"] == author
        assert f.learner.records[0]["outcome"]["verified"]
        assert f.learner.records[0]["outcome"]["success"]
        assert f.learner.records[0]["delivery"]["delivered"]
        assert f.learner.episodes[0][1]["outcome"]["success"]
        assert f.hid.checkpoint is None and not f.hid.held and not f.hid.held_buttons
        assert f.spine.arm.step_id == f.arm.step_id
        f.client.approach.assert_not_called()  # this movement came from Jev's motor action
    finally:
        worker.cancel("test cleanup")
        worker.thread.join(timeout=10)
        f.playing.close()
        f.screenshots.close()


def test_teacher_pixels_radio_and_retained_screenshot_are_one_owned_capture(tmp_path):
    f = composition(tmp_path)
    f.hid.on_hold = lambda: f.cap.values.update({"pos.mx": 0.7})
    try:
        result = f.playing.execute(f.arm, f.client.state(), lambda: None)
        assert result.outcome is SkillOutcome.SUCCEEDED
        observation, png, _ = f.teacher.requests[0]
        assert hashlib.sha256(png).hexdigest() == observation["screen"]["sha256"]
        teacher_pixels = np.array(Image.open(io.BytesIO(png)))
        expected = f.cap.frames[observation["values"]["seq"]]
        assert np.array_equal(teacher_pixels, expected)
        retained_pixels = np.array(Image.open(observation["screen"]["path"]))
        assert np.array_equal(retained_pixels, expected)
        assert observation["origin"] == [10, 20]
        assert observation["size"] == [420, 240]
        config = json.loads((f.recorder.dir / "play-config.json").read_text())
        assert config["live_validated"] is False
        assert config["controls_fingerprint"] == f.playing.controls_fingerprint
    finally:
        f.playing.release()
        f.playing.close()
        f.screenshots.close()


def test_delegated_skill_validates_params_and_preserves_guide_and_worker(tmp_path):
    f = composition(tmp_path)
    f.playing._arm = f.arm
    f.spine.arm = f.arm
    original = f.spine.execute
    calls = []
    def execute(arm, state, checkpoint):
        calls.append((arm, threading.current_thread().name))
        return original(arm, state, checkpoint)
    f.spine.execute = execute
    try:
        refused = f.playing._delegate(SkillAction(name="TRAVEL_TO", params={"x": 0.9}))
        assert refused.code == "unsupported" and not calls
        unrelated = f.playing._delegate(SkillAction(name="ACCEPT_QUEST"))
        assert unrelated.code == "unsupported" and not calls
        result = f.playing._delegate(SkillAction(name="TRAVEL_TO"))
        assert result.code == "arrived"
        assert len(calls) == 1
        delegated, owner = calls[0]
        assert delegated.step_id == f.arm.step_id and delegated.arm_id == f.arm.arm_id
        assert delegated.decision.goal == f.arm.decision.goal
        assert owner == threading.current_thread().name
        assert f.spine.arm is f.arm
        f.client.approach.assert_called_once_with((50, 30, 0), timeout_s=f.spine.travel_timeout)
    finally:
        f.playing.release()
        f.playing.close()
        f.screenshots.close()


def test_travel_delegation_uses_current_incomplete_objective_destination(tmp_path):
    first = ObjectiveTarget(kind="kill", required_id=10, required_count=1, counter_index=0,
                            target_name="First", target_kind="creature", pos=(0.2, 0.2),
                            world=(80, 80, 0), map_id=0, coord_zone_id=1)
    second = first.model_copy(update={"required_id": 11, "counter_index": 1,
                                      "target_name": "Second", "pos": (0.8, 0.8),
                                      "world": (20, 20, 0)})
    node = Node(id="step", kind=StepKind.QUEST_OBJECTIVE, zone="fixture", zone_id=1,
                coord_zone_id=1, quest_id=7, pos=first.pos, world=first.world, map_id=0,
                target_name="First", target_kind="creature", skills=("GRIND_UNTIL",),
                objective_targets=(first, second))
    f = composition(tmp_path, node=node)
    f.cap.values.update({"quests.count": 1, "quests.log_hash": 1, "quests.slot": 1,
                         "quests.slot_id": 7, "quests.slot_complete": False,
                         "quests.o0_have": 1, "quests.o0_need": 1,
                         "quests.o1_have": 0, "quests.o1_need": 1})
    f.playing._arm = f.spine.arm = f.arm
    try:
        result = f.playing._delegate(SkillAction(name="TRAVEL_TO"))
        assert result.code == "arrived"
        f.client.approach.assert_called_once_with(second.world, timeout_s=f.spine.travel_timeout)
        assert f.spine.graph.get("step").world == first.world  # destination choice never mutates guide
        assert f.spine.arm is f.arm
    finally:
        f.playing.release()
        f.playing.close()
        f.screenshots.close()


def test_invalid_arm_and_focus_loss_never_call_teacher(tmp_path):
    f = composition(tmp_path)
    try:
        wrong = replace(f.arm, decision=f.arm.decision.model_copy(update={"params": {"step_id": "other"}}))
        assert f.playing.execute(wrong, f.client.state(), lambda: None).code == "unsupported"
        f.hid.focused = False
        with pytest.raises(FocusLost):
            f.playing.execute(f.arm, f.client.state(), lambda: None)
        assert not f.teacher.requests and not f.hid.calls
    finally:
        f.playing.release()
        f.playing.close()
        f.screenshots.close()


def test_mode_and_config_cannot_disagree_about_student_authority(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        composition(tmp_path, mode="teach", config=PlayConfig(mode="adaptive"))


def test_background_stops_before_journal_and_close_is_idempotent(tmp_path):
    f = composition(tmp_path)
    events = []
    f.playing.learning_service = SimpleNamespace(close=lambda: events.append("trainer stopped"))
    old_close = f.playing.journal.close
    def close_journal():
        events.append("journal closed")
        old_close()
    f.playing.journal.close = close_journal
    f.playing.close()
    f.playing.close()
    f.screenshots.close()
    assert events == ["trainer stopped", "journal closed"]


def test_background_ingests_finished_runs_and_honours_shutdown(tmp_path):
    runs, store = tmp_path / "runs", tmp_path / "learning"
    store.mkdir()
    (runs / "with-motor").mkdir(parents=True)
    (runs / "with-motor" / "play-actions.jsonl").write_text("")
    (runs / "ordinary").mkdir()
    updated = threading.Event()
    ingested, owners = [], []
    class OfflineLearner:
        directory = store
        def ingest_run(self, directory):
            ingested.append(directory.name)
            return {"errors": []}
        def update(self, *, cancelled):
            owners.append(threading.current_thread().name)
            assert callable(cancelled)
            updated.set()
            return {"candidates": 0}
    service = MotorLearningService(OfflineLearner(), runs, interval_s=60).start()
    try:
        assert updated.wait(3)
    finally:
        service.close()
    assert not service.thread.is_alive()
    assert ingested == ["with-motor"] and owners == ["jev-motor-learner"]
    assert json.loads((store / "latest-cycle.json").read_text())["cycle"] == {"candidates": 0}


@pytest.mark.parametrize("combat", [False, True])
def test_real_supervisor_reaches_teacher_escape_and_confirms_modal_closed(tmp_path, combat):
    f = composition(tmp_path)
    f.cap.values.update({"ui.modal": True, "vitals.combat": combat})
    prompts, key_owners = [], []

    class EscapeTeacher:
        async def decide(self, observation, png, **kwargs):
            prompts.append(observation)
            return PlayTeacherResult("ok", observation["id"], action=KeyAction(control="escape"),
                                     capability="observe", expected_effect="ui_closed",
                                     rationale="Observed modal can be dismissed with Escape.")

    f.playing.controller.teacher = EscapeTeacher()
    original_tap = f.hid.tap
    def tap(key):
        key_owners.append((key, threading.current_thread().name))
        accepted = original_tap(key)
        if accepted and key == "esc":
            f.cap.values["ui.modal"] = False
        return accepted
    f.hid.tap = tap
    recorder = Recorder(tmp_path / "supervisor-runs")
    runtime = ClientRuntime(f.client.client_id, f.spine.graph, ClientSource(f.client), recorder,
                            keys_down=f.hid.keys_down, available_skills=f.playing.available,
                            validate_action=f.playing.validate)
    supervisor = Supervisor(runtime, f.playing, say=lambda text: None)
    try:
        supervisor.step(0)
        worker = supervisor.worker
        assert worker is not None, "modal preempt must reach the capable playing body"
        assert worker.arm.decision.skill == "ABORT_WAIT"
        assert worker.done.wait(5)
        assert worker.result.outcome is SkillOutcome.SUCCEEDED, worker.result
        supervisor.stopped.set()
        supervisor.step(0.25)
        assert len(prompts) == 1 and prompts[0]["values"]["ui.modal"] is True
        assert key_owners == [("esc", "jev-body")]
        assert f.cap.values["ui.modal"] is False
        assert f.learner.records[0]["outcome"]["success"]
        assert "ui_closed" in f.learner.records[0]["outcome"]["effects"]
        assert read(recorder.dir / "skills.jsonl")[0]["outcome"] == "succeeded"
        assert f.spine.graph.entry == f.arm.step_id
        f.client.approach.assert_not_called()
    finally:
        supervisor.close()
        f.playing.close()
        f.screenshots.close()
        recorder.close()


def test_default_live_body_keeps_modal_wait_without_input(tmp_path):
    f = composition(tmp_path)
    f.cap.values["ui.modal"] = True
    recorder = Recorder(tmp_path / "supervisor-runs")
    runtime = ClientRuntime(f.client.client_id, f.spine.graph, ClientSource(f.client), recorder,
                            available_skills=f.spine.available, validate_action=f.spine.validate)
    supervisor = Supervisor(runtime, f.spine, say=lambda text: None)
    try:
        supervisor.step(0)
        assert runtime.armed.decision.skill == "ABORT_WAIT"
        assert supervisor.worker is None
        assert not f.hid.calls and not f.teacher.requests
    finally:
        supervisor.close()
        f.playing.close()
        f.screenshots.close()
        recorder.close()
