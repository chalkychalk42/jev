"""The real teaching loop with a deterministic environment, never a game client."""

import asyncio
import copy
import json
from dataclasses import replace

import pytest

from jev.coach.schema import Decision, Intent
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed
from jev.play.actions import action_dict, parse_action
from jev.play.controller import PlayConfig, PlayController, _await_checked
from jev.play.executor import ExecutionResult
from jev.play.journal import PlayJournal
from jev.play.learning import MotorLearner, MotorPrediction
from jev.play.observation import Observation
from jev.play.teacher import PlayTeacherResult
from jev.run.supervisor import Cancelled
from jev.teacher.client import TeacherResult
from jev.world.state_v1 import ArmedBy


def arm():
    return Armed(Decision(goal="advance:fixture", intent=Intent.ADVANCE,
                          skill="COMBAT_PROFILE", abort_if=["dead"], confidence=1,
                          why="fixture fight"), ArmedBy.TRACKER, 1, "fixture",
                 step_id="step", arm_id="episode")


class Environment:
    def __init__(self):
        self.seq = 0
        self.values = {"target.has": True, "target.name_id": 42, "target.hp": 1.0,
                       "target.in_melee": False, "vitals.dead": False, "vitals.ghost": False,
                       "vitals.combat": False, "vitals.hp": 1.0, "ui.modal": False,
                       "pos.zone_id": 12, "pos.mx": 0.5, "pos.my": 0.5}
        self.screen = 0
        self.actions = []
        self.views = []
        self.retained = []
        self.refuse = False
        self.on_observe = None

    def observe(self, arm, *, retain=True):
        self.seq += 1
        if self.on_observe:
            self.on_observe(self)
        data = {"id": f"o{self.seq}", "captured_at": float(self.seq),
                "values": {**self.values, "seq": self.seq % 256},
                "freshness": {"paint_generation": self.seq},
                "state": {"quests": [], "vitals": {"power_type": "mana"}},
                "screen": {"sha256": "a" * 64}, "features": {"screen.0": float(self.screen)},
                "origin": [0, 0], "size": [1600, 900], "synthetic": True,
                "context": {"skill": arm.decision.skill, "step_id": arm.step_id,
                            "target_name_id": 42, "goal": arm.decision.goal}}
        self.views.append(copy.deepcopy(data))
        observation = Observation(data, b"fixture" if retain else b"")
        return self.retain(observation) if retain else observation

    def retain(self, observation):
        if observation.id not in self.retained:
            self.retained.append(observation.id)
        return replace(observation, png=b"fixture")

    def note_observation(self, values):
        pass

    def execute(self, action, expected=None):
        doc = action_dict(action)
        self.actions.append(doc)
        if self.refuse:
            return ExecutionResult("refused", False, "fixture focus loss", doc)
        if doc["kind"] == "camera":
            self.screen = 1
        if doc["kind"] == "key" and doc["control"] == "move_forward" and self.screen:
            self.values["target.in_melee"] = True
        if doc["kind"] == "key" and doc["control"] == "attack_target" and self.values["target.in_melee"]:
            self.values["target.hp"] = 0.0
        return ExecutionResult("delivered", True, "accepted", doc, 2)


class Tutor:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.requests = []

    async def decide(self, observation, screenshot, **kwargs):
        self.requests.append((copy.deepcopy(observation), copy.deepcopy(kwargs)))
        value = next(self.actions)
        if isinstance(value, PlayTeacherResult):
            return value
        action, effect = value
        return PlayTeacherResult("ok", observation["id"], parse_action(action), "approach",
                                 "fixture correction", effect, "requested", "served", 10, 5,
                                 calls=(TeacherResult("ok", model="served", tokens_in=10, tokens_out=5),))


def setup(tmp_path, actions, *, learner=None, **config):
    env = Environment()
    teacher = Tutor(actions)
    journal = PlayJournal(tmp_path / "run", run_id="run")
    controller = PlayController(observer=env, executor=env, teacher=teacher,
                                learner=learner, journal=journal, controls={},
                                controls_fingerprint="c" * 64, knowledge_fingerprint="d" * 64,
                                config=PlayConfig(outcome_wait_s=0.001, poll_s=0.001, **config),
                                say=lambda _: None)
    return env, teacher, journal, controller


CORRECTION = [
    ({"kind": "key", "control": "move_forward", "duration_s": 0.3}, "closer"),
    ({"kind": "camera", "axis": "yaw", "pixels": -80}, "scene_changed"),
    ({"kind": "key", "control": "move_forward", "duration_s": 0.3}, "closer"),
    ({"kind": "key", "control": "attack_target"}, "target_dead"),
]


def result_rows(journal):
    return [row for line in (journal.directory / "play-actions.jsonl").read_text().splitlines()
            if (row := json.loads(line))["event"] == "result"]


def test_teacher_corrects_failed_approach_and_real_learner_receives_joined_outcomes(tmp_path):
    learner = MotorLearner(tmp_path / "models")
    env, teacher, journal, controller = setup(tmp_path, CORRECTION, learner=learner)
    assert controller.run(arm(), lambda: None).outcome is SkillOutcome.SUCCEEDED
    rows = result_rows(journal)
    assert len(rows) == 4 and not rows[0]["outcome"]["success"]
    assert rows[-1]["outcome"]["success"] and "target_dead" in rows[-1]["outcome"]["effects"]
    assert teacher.requests[1][1]["recent"][0]["outcome"]["success"] is False
    assert rows[0]["before"]["id"] == teacher.requests[0][0]["id"]
    assert rows[0]["execution_before"]["id"] != rows[0]["before"]["id"]
    assert rows[0]["observation_id"] == rows[0]["before"]["id"]
    assert all(row["cost"]["teacher_calls"] == 1 for row in rows)
    assert controller.learning_error is None
    learned = learner.records()
    assert len(learned) == 4 and all(row["episode_outcome"]["success"] for row in learned)
    assert all(row["shadow"] == {"model": None, "action": None, "confidence": 0.0,
                                 "expected_effect": None} for row in learned), "no student yet"
    assert len(env.retained) < len(env.views)  # polling did not persist every full PNG


def test_teacher_timeout_spends_no_input_and_names_the_stop(tmp_path):
    env, _, journal, controller = setup(tmp_path, [PlayTeacherResult("timeout", "o1", detail="fixture")])
    result = controller.run(arm(), lambda: None)
    assert result.code == "teacher_unavailable" and env.actions == []
    assert result_rows(journal) == []


def test_delegated_closed_shop_purchase_completes_from_measured_supplies(tmp_path):
    env, teacher, journal, controller = setup(tmp_path, [
        ({"kind": "skill", "name": "BUY_AMMO_REAGENT_FOOD"}, "supplies_replenished")])
    env.values.update({"ui.vendor": False, "bags.drink_id": 159,
                       "bags.drink_count": 0, "bags.money_copper": 100})
    service = replace(arm(), decision=arm().decision.model_copy(
        update={"skill": "BUY_AMMO_REAGENT_FOOD"}))

    def buy(action, expected=None):
        # Existing Vendor composes opening, selling, buying and closing. Its final
        # cash can rise after a sale even though it also bought the requested supply.
        env.values.update({"bags.drink_count": 10, "bags.money_copper": 150})
        return ExecutionResult("delegated", False, "trusted purchase returned", action_dict(action))

    env.execute = buy
    assert controller.run(service, lambda: None).outcome is SkillOutcome.SUCCEEDED
    assert len(teacher.requests) == 1
    row = result_rows(journal)[0]
    assert row["outcome"]["success"]
    assert row["expected_effect"] == "supplies_replenished"
    assert "supplies_bought" not in row["outcome"]["effects"]


def test_refused_input_cannot_be_learned_even_if_scene_changes(tmp_path):
    env, _, journal, controller = setup(tmp_path, CORRECTION[:1], max_no_effect=1)
    env.refuse = True
    result = controller.run(arm(), lambda: None)
    assert result.code == "teaching_stalled"
    assert not result_rows(journal)[0]["outcome"]["success"]


def test_observation_expectation_cannot_reward_arbitrary_failed_movement(tmp_path):
    env, _, journal, controller = setup(tmp_path, [
        ({"kind": "key", "control": "move_forward", "duration_s": 0.3}, "observed")], max_no_effect=1)
    assert controller.run(arm(), lambda: None).code == "teaching_stalled"
    row = result_rows(journal)[0]
    assert row["expected_effect"] == "closer" and not row["outcome"]["success"]
    assert len(env.actions) == 1


def test_repeated_camera_motion_has_a_bounded_stop(tmp_path):
    _env, teacher, _journal, controller = setup(tmp_path, [CORRECTION[1]] * 2, max_no_effect=2)
    assert controller.run(arm(), lambda: None).code == "teaching_stalled"
    assert len(teacher.requests) == 2


def test_the_same_action_repeated_without_effect_hands_back_to_jev(tmp_path):
    """Tab showed its effect in none of 15 presses in the first recorded runs: the tutor
    pressed it again and again for minutes. Three in a row without effect end the
    episode, well inside the no-effect budget, and Jev's own routine plays on."""
    tab = ({"kind": "key", "control": "target_next", "duration_s": 0.0}, "selected")
    env, _teacher, _journal, controller = setup(tmp_path, [tab] * 6, max_no_effect=8)
    result = controller.run(arm(), lambda: None)
    assert result.code == "teaching_stalled" and "repeated target_next 3 times" in result.detail
    assert len(env.actions) == 3


def test_different_attempts_without_effect_are_exploration_not_a_loop(tmp_path):
    tab = ({"kind": "key", "control": "target_next", "duration_s": 0.0}, "selected")
    turn = ({"kind": "key", "control": "turn_left", "duration_s": 0.2}, None)
    env, _teacher, _journal, controller = setup(tmp_path, [tab, turn, tab, turn, tab],
                                                max_no_effect=5)
    controller.run(arm(), lambda: None)
    assert len(env.actions) == 5, "alternating attempts ended as a loop"


def test_cancellation_reaps_pending_teacher_before_return():
    count, state = [0], []

    async def hung():
        try:
            state.append("started")
            await asyncio.sleep(100)
        finally:
            state.append("reaped")

    def checkpoint():
        count[0] += 1
        if count[0] > 2:
            raise Cancelled("operator stopped")

    with pytest.raises(Cancelled, match="operator stopped"):
        _await_checked(hung(), checkpoint, poll_s=0.001)
    assert state == ["started", "reaped"]


class Student:
    def __init__(self, predictions):
        self.predictions = iter(predictions)
        self.records = []
        self.episodes = []

    def predict(self, *args, **kwargs):
        return next(self.predictions)

    def record(self, row):
        self.records.append(row)

    def finish_episode(self, *args, **kwargs):
        self.episodes.append((args, kwargs))


def attack_prediction(mode="active"):
    return MotorPrediction(action={"kind": "key", "control": "attack_target", "duration_s": 0.0},
                           model="motor:test", mode=mode, confidence=1, expected_effect="target_dead")


@pytest.mark.parametrize("mode", ["shadow", "active", "canary"])
def test_a_proposal_is_only_the_tutors_shadow_whatever_mode_it_claims(tmp_path, mode):
    """No student acts (V225): even one a registry once promoted is recorded beside the
    tutor's action and never executed."""
    learner = Student([attack_prediction(mode)])
    env, teacher, journal, controller = setup(tmp_path, [CORRECTION[-1]], learner=learner)
    env.values["target.in_melee"] = True
    assert controller.run(arm(), lambda: None).outcome is SkillOutcome.SUCCEEDED
    row = result_rows(journal)[0]
    assert len(teacher.requests) == 1 and row["author"] == "teacher"
    assert row["shadow"]["model"] == "motor:test" and row["shadow"]["action"]["control"] == "attack_target"


def test_the_adaptive_mode_is_refused():
    with pytest.raises(ValueError, match="teach"):
        PlayConfig(mode="adaptive")


def test_no_target_death_from_disappearance_even_after_delivered_attack(tmp_path):
    env, _, journal, controller = setup(tmp_path, [CORRECTION[-1]], max_no_effect=1)

    def vanish(action, expected=None):
        env.values["target.has"] = False
        env.values["target.hp"] = None
        return ExecutionResult("delivered", True, "accepted", action_dict(action), 2)

    env.execute = vanish
    assert controller.run(arm(), lambda: None).code == "teaching_stalled"
    assert not result_rows(journal)[0]["outcome"]["success"]


def test_lightweight_polling_retains_exact_final_observation_not_a_recapture(tmp_path):
    env, _, journal, controller = setup(tmp_path, [CORRECTION[-1]])
    env.values["target.in_melee"] = True
    assert controller.run(arm(), lambda: None).outcome is SkillOutcome.SUCCEEDED
    row = result_rows(journal)[0]
    assert row["after"]["id"] == env.views[-1]["id"]
    assert row["after"]["id"] in env.retained


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0])
def test_nonfinite_or_unbounded_configuration_refused(value):
    with pytest.raises(ValueError):
        PlayConfig(teacher_timeout_s=value)


@pytest.mark.parametrize("code,closed,expected", [
    ("nothing", True, "nothing"), ("nothing", False, "teaching_stalled"),
    ("refused", True, "teaching_stalled"),
])
def test_bounded_loot_no_take_remains_nonfatal_without_learning_reward(tmp_path, code, closed, expected):
    learner = MotorLearner(tmp_path / "models")
    env, _, journal, controller = setup(
        tmp_path, [({"kind": "skill", "name": "LOOT", "params": {}}, "loot_received")],
        learner=learner, max_no_effect=1)
    env.values.update({"target.hp": 0.0, "ui.loot": not closed,
                       "bags.free": 10, "bags.money_copper": 20})

    def execute(action, expected=None):
        return ExecutionResult("delegated", False, "trusted attempt", action_dict(action),
                               metadata={"skill_result": {"outcome": "succeeded", "code": code}})

    env.execute = execute
    loot_arm = replace(arm(), decision=arm().decision.model_copy(update={"skill": "LOOT"}))
    assert controller.run(loot_arm, lambda: None).code == expected
    row = result_rows(journal)[0]
    assert not row["outcome"]["success"] and "loot_received" not in row["outcome"]["effects"]
    episode = json.loads((journal.directory / "play-episodes.jsonl").read_text())
    assert not episode["verified"] and episode["progress"] == 0
    assert learner.records()[0]["episode_outcome"]["verified"] is False
    assert controller.recent[-1]["delivery"]["metadata"]["skill_result"]["code"] == code
    if code == "nothing" and closed:
        assert len(env.views) == 3  # no extra settle delay after the trusted attempt


@pytest.mark.parametrize("power_type,wire_type,power,ready", [
    (None, None, 0, False), ("mana", 0, 0.7, False), ("mana", 0, 0.75, True),
    ("rage", 1, 0, True), ("energy", 3, 0, True),
])
def test_rest_completion_requires_known_resource_type(power_type, wire_type, power, ready):
    from jev.play.controller import finished

    observation = {"context": {"skill": "EAT_DRINK"},
                   "values": {"vitals.hp": 1.0, "vitals.power": power,
                              "vitals.power_type": wire_type},
                   "state": {"vitals": {"power_type": power_type}}}
    assert finished(observation, observation) is ready


def test_each_verified_unit_of_objective_progress_is_its_own_successful_episode(tmp_path):
    """Eight pieces of meat outlast any bounded run of actions; three earned before a
    budget ran out must still teach something. Each gain closes a successful episode, and
    the arm itself ends only when the guide's own predicate says the objective is done."""
    quests = {"count": 0}
    grind = replace(arm(), decision=arm().decision.model_copy(update={"skill": "GRIND_UNTIL"}))
    stride = ({"kind": "key", "control": "move_forward", "duration_s": 0.3}, None)
    env, _, journal, controller = setup(tmp_path, [stride] * 8, max_actions=3, max_no_effect=10)
    original_observe, original_execute = env.observe, env.execute

    def observe(arm_, *, retain=True):
        observation = original_observe(arm_, retain=retain)
        observation.data["state"]["quests"] = [{
            "quest_id": 33, "complete": quests["count"] >= 8,
            "objectives": [{"counter_index": 0, "have": quests["count"], "need": 8}]}]
        observation.data["context"]["quest_id"] = 33
        return observation

    def execute(action, expected=None):
        quests["count"] += 1                     # every stride earns a piece, for the test
        return original_execute(action, expected)

    env.observe, env.execute = observe, execute
    result = controller.run(grind, lambda: None)
    episodes = [json.loads(line) for line in
                (journal.directory / "play-episodes.jsonl").read_text().splitlines()]
    progress = [e for e in episodes if e["code"] == "progress"]
    assert len(progress) == 7 and all(e["success"] and e["verified"] for e in progress)
    assert [e["episode_id"] for e in progress[:3]] == ["episode", "episode:1", "episode:2"]
    assert all(e["progress"] >= 1 and e["actions"] == 1 for e in progress)
    assert episodes[-1]["code"] == "done" and episodes[-1]["episode_id"] == "episode:7"
    assert result.outcome is SkillOutcome.SUCCEEDED and result.code == "done"


def test_an_episode_without_progress_still_ends_on_its_action_budget(tmp_path):
    stride = ({"kind": "key", "control": "move_forward", "duration_s": 0.3}, None)
    env, _, _journal, controller = setup(tmp_path, [stride] * 3, max_actions=3, max_no_effect=10)
    result = controller.run(arm(), lambda: None)
    assert result.code == "teaching_stalled" and len(env.actions) == 3


def test_experience_is_a_level_objectives_progress_whatever_is_selected():
    """Kills inside grind teaching went uncounted when the client cleared the selection at
    the kill: one episode ran 872 s through several and ended "stalled"."""
    from jev.play.controller import progressed

    first = {"context": {"until_level": 8}, "values": {"char.level": 7, "char.xp_pct": 0.10,
                                                      "target.has": False}}
    later = {"context": {"until_level": 8}, "values": {"char.level": 7, "char.xp_pct": 0.12,
                                                      "target.has": False}}
    assert progressed(first, later)
    levelled = {**later, "values": {**later["values"], "char.level": 8, "char.xp_pct": 0.01}}
    assert progressed(first, levelled)
    assert not progressed(first, first)
    unread = {**later, "values": {"char.level": None, "char.xp_pct": 0.5}}
    assert not progressed(first, unread)
    quest = {"context": {"until_level": None}, "values": later["values"]}
    assert not progressed({**first, "context": {"until_level": None}}, quest), \
        "a quest's counter is its own progress, not experience"


def test_arriving_again_where_it_already_arrived_is_not_useful(tmp_path):
    """A tutor on Milly's Harvest walked to the objective's point and "arrived" for ten
    minutes at 6/8, each arrival resetting the no-effect count (run
    20260924T070341-9b2441)."""
    walk = ({"kind": "skill", "name": "TRAVEL_TO", "params": {}}, "arrived")
    grind = replace(arm(), decision=arm().decision.model_copy(update={"skill": "GRIND_UNTIL"}))
    env, _, _journal, controller = setup(tmp_path, [walk] * 10, max_repeats=3, max_no_effect=10)
    original = env.observe

    def observe(arm_, *, retain=True):
        observation = original(arm_, retain=retain)
        observation.data["context"].update(destination=[0.5, 0.5], arrival_radius=0.01,
                                           coord_zone_id=None)
        return observation

    env.observe = observe
    result = controller.run(grind, lambda: None)
    assert result.code == "teaching_stalled"
    assert len(env.actions) == 4, "one arrival, then three that went nowhere"


def _held_setup(tmp_path):
    """A grind the tutor starts by selecting a unit, cut short by combat, with a quest
    counter the fight can move."""
    select = ({"kind": "key", "control": "target_next"}, "selected")
    env, _, journal, controller = setup(tmp_path, [select] * 4, max_no_effect=10)
    env.values.update({"target.has": False, "target.name_id": None})
    quests = {"count": 0}
    original_observe, original_execute = env.observe, env.execute

    def observe(arm_, *, retain=True):
        observation = original_observe(arm_, retain=retain)
        observation.data["state"]["quests"] = [{
            "quest_id": 33, "complete": False,
            "objectives": [{"counter_index": 0, "have": quests["count"], "need": 8}]}]
        observation.data["context"]["quest_id"] = 33
        return observation

    def execute(action, expected=None):
        env.values.update({"target.has": True, "target.name_id": 42})
        return original_execute(action, expected)

    env.observe, env.execute = observe, execute
    grind = replace(arm(), decision=arm().decision.model_copy(update={"skill": "GRIND_UNTIL"}))
    return env, journal, controller, grind, quests


def _combat_after(n):
    from jev.run.supervisor import Cancelled

    calls = {"n": 0}

    def checkpoint():
        calls["n"] += 1
        if calls["n"] > n:
            raise Cancelled("combat interrupted the leg or service")
    return checkpoint


def _episodes(journal):
    path = journal.directory / "play-episodes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_an_episode_combat_cuts_short_is_credited_with_the_kill_it_set_up(tmp_path):
    """Grind teaching succeeded 3 times in 42 once combat took the floor from the tutor:
    the kill its selection set up landed outside its episode."""
    from jev.run.supervisor import Cancelled

    env, journal, controller, grind, quests = _held_setup(tmp_path)
    with pytest.raises(Cancelled):
        controller.run(grind, _combat_after(6))
    assert _episodes(journal) == [], "held for the fight's verdict, not written as lost"
    quests["count"] = 1                                  # the reflex fight's kill counted
    env.values["target.has"] = False
    with pytest.raises(Cancelled):
        controller.run(grind, _combat_after(1))
    first = _episodes(journal)[0]
    assert first["episode_id"] == "episode" and first["success"] and first["verified"]
    assert first["code"] == "progress"


def test_a_held_episode_whose_fight_moved_nothing_is_interrupted(tmp_path):
    from jev.run.supervisor import Cancelled

    _env, journal, controller, grind, _quests = _held_setup(tmp_path)
    with pytest.raises(Cancelled):
        controller.run(grind, _combat_after(6))
    controller.settle(None)
    first = _episodes(journal)[0]
    assert first["code"] == "preempted" and not first["success"]


def test_an_episode_cut_short_by_anything_but_combat_is_closed_at_once(tmp_path):
    from jev.run.supervisor import FocusLost

    _env, journal, controller, grind, _quests = _held_setup(tmp_path)
    calls = {"n": 0}

    def checkpoint():
        calls["n"] += 1
        if calls["n"] > 6:
            raise FocusLost("client lost focus during teaching")
    with pytest.raises(FocusLost):
        controller.run(grind, checkpoint)
    assert [e["code"] for e in _episodes(journal)] == ["preempted"]


def test_a_learner_store_another_writer_holds_is_busy_not_broken(tmp_path):
    """Windows gives up a held file lock after ten seconds with `PermissionError`. The row
    is in the run's journal and the learner reads the run in later; the student is silent
    only until the store takes a row again, not for the rest of the session."""
    learner = MotorLearner(tmp_path / "models")
    real_record, calls = learner.record, []

    def record(row):
        calls.append(row["decision_id"])
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied")
        return real_record(row)

    learner.record = record
    said = []
    _env, _teacher, journal, controller = setup(tmp_path, CORRECTION, learner=learner)
    controller.say = said.append
    assert controller.run(arm(), lambda: None).outcome is SkillOutcome.SUCCEEDED
    assert controller.learning_error is None
    assert controller._learning_busy is False                   # the next rows went in
    assert len(result_rows(journal)) == 4 and len(learner.records()) == 3
    assert sum("busy" in line for line in said) == 1
