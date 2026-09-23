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
from jev.run.supervisor import FocusLost, Result, Supervisor, Worker
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
                          # The outcome loop stops as soon as the effect is seen; the ceiling
                          # only matters on a loaded machine, where 0.05 s missed the paint.
                          config=config or PlayConfig(mode=mode, max_actions=2, outcome_wait_s=0.5,
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


class UnavailableTeacher(Teacher):
    async def decide(self, observation, png, **kwargs):
        self.requests.append((observation, png, kwargs.get("skills")))
        return PlayTeacherResult("transport", observation["id"], requested_model="fixture",
                                 detail="GLM API returned HTTP 429 code 1305; transient")


def test_an_unavailable_tutor_hands_the_objective_to_the_scripted_routine(tmp_path):
    """Progress with zero teacher calls: the guide's own routine plays the objective."""
    env = composition(tmp_path)
    env.playing.controller.teacher = UnavailableTeacher()
    scripted = []
    env.spine.execute = lambda arm, state, checkpoint: scripted.append(arm) or Result(
        SkillOutcome.SUCCEEDED, "scripted travel arrived", "arrived")
    try:
        result = env.playing.execute(env.arm, None, lambda: None)
    finally:
        env.screenshots.close()
        env.playing.close()
    assert result.outcome is SkillOutcome.SUCCEEDED and result.code == "arrived"
    assert scripted == [env.arm]
    assert set(env.playing.controller.teacher.requests[0][2]) == {
        "COMBAT_PROFILE", "EAT_DRINK", "FACE_TARGET", "LOOT", "TRAVEL_TO"}
    rows = [json.loads(line) for line in
            (env.recorder.dir / "play-actions.jsonl").read_text().splitlines()]
    assert any(row.get("event") == "scripted_fallback" and "1305" in row["reason"]
               for row in rows)


def test_the_scripted_routine_gets_its_budget_from_the_moment_it_takes_over(tmp_path):
    """Measured 23 September: the tutor spent 25 s retrying an overloaded provider and the
    scripted accept, timed from the arm, got 35 of its 60 s and timed out walking."""
    import math
    import time as clock

    env = composition(tmp_path)
    env.playing.controller.teacher = UnavailableTeacher()
    seen = []
    teaching = env.playing.controller.run

    def run(arm, checkpoint):
        seen.append(("teaching", env.playing.routine_clock))
        return teaching(arm, checkpoint)

    env.playing.controller.run = run
    env.spine.execute = lambda arm, state, checkpoint: seen.append(
        ("scripted", env.playing.routine_clock)) or Result(SkillOutcome.SUCCEEDED, "ok", "arrived")
    before = clock.monotonic()
    try:
        env.playing.execute(env.arm, None, lambda: None)
    finally:
        env.screenshots.close()
        env.playing.close()
    assert seen[0] == ("teaching", math.inf), "the tutor's episode bounds itself"
    assert seen[1][0] == "scripted" and before <= seen[1][1] <= clock.monotonic()
    assert env.playing.routine_clock is None, "the clock outlived the objective"


@pytest.mark.parametrize(("rule", "skill"), [
    ("fight.rotation", "COMBAT_PROFILE"), ("preempt.critical", "COMBAT_PROFILE"),
    ("preempt.dead", "RELEASE_SPIRIT"), ("preempt.ghost", "CORPSE_RUN"),
])
def test_a_fight_or_a_death_runs_its_routine_without_asking_the_tutor(tmp_path, rule, skill):
    """Run 20260923T172056-54f4d5: thirty seconds of an unanswered tutor call in a fight,
    from full health to dead without a swing; then Esc five times at the release popup."""
    env = composition(tmp_path)
    env.playing.controller.run = Mock(side_effect=AssertionError("the tutor was asked"))
    scripted = []
    env.spine.execute = lambda arm, state, checkpoint: scripted.append(
        (arm, env.playing.routine_clock)) or Result(SkillOutcome.SUCCEEDED, "routine ran", "ok")
    reflex = replace(env.arm, rule=rule,
                     decision=env.arm.decision.model_copy(update={"skill": skill}))
    try:
        result = env.playing.execute(reflex, None, lambda: None)
    finally:
        env.screenshots.close()
        env.playing.close()
    assert result.code == "ok" and [arm for arm, _ in scripted] == [reflex]
    assert scripted[0][1] is not None and scripted[0][1] != float("inf"), \
        "the routine's own budget applies from the start"
    rows = [json.loads(line) for line in
            (env.recorder.dir / "play-actions.jsonl").read_text().splitlines()]
    assert [row["rule"] for row in rows if row.get("event") == "reflex"] == [rule]


def test_a_dialog_nobody_can_dismiss_is_not_handed_to_a_routine(tmp_path):
    env = composition(tmp_path)
    env.playing.controller.teacher = UnavailableTeacher()
    env.spine.execute = Mock(side_effect=AssertionError("a wait is not a routine"))
    wait = replace(env.arm, decision=env.arm.decision.model_copy(update={"skill": "ABORT_WAIT"}))
    try:
        result = env.playing.execute(wait, None, lambda: None)
    finally:
        env.screenshots.close()
        env.playing.close()
    assert result.code == "teacher_unavailable"


def test_a_stalled_tutor_also_hands_the_objective_to_the_scripted_routine(tmp_path):
    env = composition(tmp_path)
    env.playing.controller.run = lambda arm, checkpoint: Result(
        SkillOutcome.ABORTED, "bounded teaching episode made no verified useful progress",
        "teaching_stalled")
    scripted = []
    env.spine.execute = lambda arm, state, checkpoint: scripted.append(arm) or Result(
        SkillOutcome.SUCCEEDED, "scripted travel arrived", "arrived")
    try:
        result = env.playing.execute(env.arm, None, lambda: None)
    finally:
        env.screenshots.close()
        env.playing.close()
    assert result.code == "arrived" and scripted == [env.arm]


def test_the_hunt_loop_is_not_a_step_of_itself_but_its_steps_are_offered():
    """Delegating GRIND_UNTIL from inside the hunt handed every decision back to the
    scripted loop; Jev composes the hunt from its steps instead."""
    from jev.play.runtime import delegable_skills

    grind = delegable_skills("GRIND_UNTIL", LiveBody.available)
    assert "GRIND_UNTIL" not in grind
    assert {"COMBAT_PROFILE", "LOOT", "FACE_TARGET", "TRAVEL_TO", "EAT_DRINK"} <= set(grind)
    assert "TURNIN_QUEST" in delegable_skills("TURNIN_QUEST", LiveBody.available), \
        "an interaction routine is still the objective's own step"
    assert delegable_skills("CORPSE_RUN", LiveBody.available) == ("CORPSE_RUN",)


def test_background_rereads_a_run_only_when_its_play_files_change(tmp_path):
    """Re-reading every run each cycle took the store's lock once per recorded row, forty
    runs over, and a cycle failed on Windows' ten-second lock (run 20260923T232300)."""
    runs, store = tmp_path / "runs", tmp_path / "learning"
    store.mkdir()
    (runs / "quiet").mkdir(parents=True)
    (runs / "quiet" / "play-actions.jsonl").write_text("")
    (runs / "live").mkdir()
    actions = runs / "live" / "play-actions.jsonl"
    actions.write_text("")
    cycles = threading.Semaphore(0)
    ingested = []

    class OfflineLearner:
        directory = store
        def ingest_run(self, directory):
            ingested.append(directory.name)
            return {"errors": []}
        def update(self, *, cancelled):
            cycles.release()
            return {"candidates": 0}

    service = MotorLearningService(OfflineLearner(), runs, interval_s=0.05).start()
    try:
        assert cycles.acquire(timeout=3) and cycles.acquire(timeout=3)
        assert sorted(ingested) == ["live", "quiet"], "an unchanged run was read again"
        actions.write_text('{"event": "request"}\n')
        assert cycles.acquire(timeout=3) and cycles.acquire(timeout=3)
    finally:
        service.close()
    assert sorted(ingested) == ["live", "live", "quiet"]
