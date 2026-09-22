"""Real controller → effect judge → learner lifecycle, using isolated fake environments.

These fixtures explicitly emulate real-data provenance to exercise production gates.
They are never connected to a game or written outside pytest's temporary directory;
their successful handover is software verification, not measured live competence.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from test_play_controller import Environment, Tutor, arm

from jev.learn.episode import SkillOutcome
from jev.play.controller import PlayConfig, PlayController
from jev.play.executor import ExecutionResult
from jev.play.journal import PlayJournal
from jev.play.learning import LearningConfig, MotorLearner


def learning_config():
    return LearningConfig(min_train_runs=2, min_train_examples=2, min_holdout_runs=1,
                          min_holdout_examples=1, min_support=2, min_support_runs=2,
                          min_shadow_examples=2, min_shadow_runs=2, min_canary_examples=2,
                          min_canary_runs=2, min_teacher_baseline=2, canary_fraction=1,
                          retrain_new_runs=100)


class ProvenanceFixture(Environment):
    def observe(self, arm, *, retain=True):
        observed = super().observe(arm, retain=retain)
        return replace(observed, data={**observed.data, "synthetic": False})


class MeasuredTutor(Tutor):
    async def decide(self, *args, **kwargs):
        # A measured local delay models teacher latency. Without it this zero-work
        # fake teacher is faster than reading/evaluating a real student, correctly
        # failing the maintained-throughput gate rather than fabricating improvement.
        await asyncio.sleep(0.02)
        return await super().decide(*args, **kwargs)


def run_episode(tmp_path, learner, run_id, *, mode="teach", failure=False, max_actions=4):
    env = ProvenanceFixture()
    env.values["target.in_melee"] = True
    teacher = MeasuredTutor([({"kind": "key", "control": "attack_target"}, "target_dead")])
    journal = PlayJournal(tmp_path / run_id, run_id=run_id)
    if failure:
        def no_damage(action, expected=None):
            return ExecutionResult("delivered", True, "accepted but no damage", {}, 1)
        env.execute = no_damage
    controller = PlayController(
        observer=env, executor=env, teacher=teacher, learner=learner, journal=journal,
        controls={}, controls_fingerprint="c" * 64, knowledge_fingerprint="d" * 64,
        config=PlayConfig(mode=mode, outcome_wait_s=0.001, poll_s=0.001,
                          max_actions=max_actions, max_no_effect=1), say=lambda _: None)
    try:
        outcome = controller.run(arm(), lambda: None)
        assert controller.learning_error is None
    finally:
        journal.close()
    return outcome, env, teacher


def test_actual_controller_corpus_trains_shadows_hands_over_and_rolls_back(tmp_path, monkeypatch):
    learner = MotorLearner(tmp_path / "models", config=learning_config())
    for run in ("fit-a", "fit-b", "fit-c"):
        result, _, teacher = run_episode(tmp_path, learner, run)
        assert result.outcome is SkillOutcome.SUCCEEDED
        assert len(teacher.requests) == 1
    learner.update()
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "shadow"
    assert state["evaluation"]["eligible"]
    for row in learner.records():
        assert row["before"]["id"] == row["request_observation_id"]
        assert row["before"]["id"] != row["execution_before"]["id"]
        assert row["episode_outcome"]["success"]
    for run in ("shadow-a", "shadow-b"):
        run_episode(tmp_path, learner, run)
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "canary"
    for run in ("canary-a", "canary-b"):
        result, _, teacher = run_episode(tmp_path, learner, run, mode="adaptive")
        assert result.outcome is SkillOutcome.SUCCEEDED
        assert teacher.requests == []
    learner.update()
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "active"
    assert state["metrics"]["student_examples"] == 2
    for row in learner.records():
        if row["author"] == "student":
            assert row["before"]["id"] == row["execution_before"]["id"]
            assert row["cost"]["teacher_calls"] == 0
    # Choose a normal non-audit decision for the fault check. No registry state or
    # evidence gate is bypassed; only the random decision identifier is deterministic.
    observation = next(row["before"] for row in learner.records() if row["author"] == "student")
    decision = next(str(i) for i in range(100) if learner.predict(
        observation, "approach", decision_id=str(i), controls_fingerprint="c" * 64,
        knowledge_fingerprint="d" * 64).can_execute)
    monkeypatch.setattr("jev.play.controller.uuid.uuid4", lambda: SimpleNamespace(hex=decision))
    result, _, teacher = run_episode(tmp_path, learner, "observed-failure", mode="adaptive", failure=True)
    assert result.outcome is SkillOutcome.ABORTED
    assert teacher.requests == []
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_controller_journal_recovery_reconstructs_same_training_corpus(tmp_path):
    original = MotorLearner(tmp_path / "original", config=learning_config())
    for run in ("fit-a", "fit-b", "fit-c"):
        run_episode(tmp_path, original, run)
    recovered = MotorLearner(tmp_path / "recovered", config=learning_config())
    for run in ("fit-a", "fit-b", "fit-c"):
        assert recovered.ingest_run(tmp_path / run) == {"records": 1, "episodes": 1, "errors": []}
    assert original.records() == recovered.records()
    original.update()
    recovered.update()
    assert original.status()["capabilities"] == recovered.status()["capabilities"]


def test_successful_final_budgeted_action_still_teaches_completed_episode(tmp_path):
    learner = MotorLearner(tmp_path / "models", config=learning_config())
    result, _, _ = run_episode(tmp_path, learner, "last-action", max_actions=1)
    assert result.outcome is SkillOutcome.SUCCEEDED
    assert learner.records()[0]["episode_outcome"]["success"]
