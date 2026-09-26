"""Real controller → effect judge → motor corpus, using isolated fake environments.

These fixtures explicitly emulate real-data provenance, so the corpus treats their rows as
it treats a session's. They are never connected to a game or written outside pytest's
temporary directory.
"""

from dataclasses import replace

from test_play_controller import Environment, Tutor, arm

from jev.learn.episode import SkillOutcome
from jev.play.controller import PlayConfig, PlayController
from jev.play.journal import PlayJournal
from jev.play.learning import MotorLearner, _qualified


class ProvenanceFixture(Environment):
    def observe(self, arm, *, retain=True):
        observed = super().observe(arm, retain=retain)
        return replace(observed, data={**observed.data, "synthetic": False})


def run_episode(tmp_path, learner, run_id, *, max_actions=4):
    env = ProvenanceFixture()
    env.values["target.in_melee"] = True
    teacher = Tutor([({"kind": "key", "control": "attack_target"}, "target_dead")])
    journal = PlayJournal(tmp_path / run_id, run_id=run_id)
    controller = PlayController(
        observer=env, executor=env, teacher=teacher, learner=learner, journal=journal,
        controls={}, controls_fingerprint="c" * 64, knowledge_fingerprint="d" * 64,
        config=PlayConfig(outcome_wait_s=0.001, poll_s=0.001,
                          max_actions=max_actions, max_no_effect=1), say=lambda _: None)
    try:
        outcome = controller.run(arm(), lambda: None)
        assert controller.learning_error is None
    finally:
        journal.close()
    return outcome, env, teacher


def test_the_controllers_rows_are_qualified_corpus_records(tmp_path):
    learner = MotorLearner(tmp_path / "models")
    for run in ("fit-a", "fit-b", "fit-c"):
        result, _, teacher = run_episode(tmp_path, learner, run)
        assert result.outcome is SkillOutcome.SUCCEEDED
        assert len(teacher.requests) == 1
    rows = learner.records()
    assert len(rows) == 3 and all(_qualified(row) for row in rows)
    for row in rows:
        assert row["author"] == "teacher"
        assert row["before"]["id"] == row["request_observation_id"]
        assert row["before"]["id"] != row["execution_before"]["id"]
        assert row["episode_outcome"]["success"]


def test_controller_journal_recovery_reconstructs_the_same_corpus(tmp_path):
    original = MotorLearner(tmp_path / "original")
    for run in ("fit-a", "fit-b", "fit-c"):
        run_episode(tmp_path, original, run)
    recovered = MotorLearner(tmp_path / "recovered")
    for run in ("fit-a", "fit-b", "fit-c"):
        assert recovered.ingest_run(tmp_path / run) == {"records": 1, "episodes": 1, "errors": []}
    assert original.records() == recovered.records()


def test_successful_final_budgeted_action_still_teaches_completed_episode(tmp_path):
    learner = MotorLearner(tmp_path / "models")
    result, _, _ = run_episode(tmp_path, learner, "last-action", max_actions=1)
    assert result.outcome is SkillOutcome.SUCCEEDED
    assert learner.records()[0]["episode_outcome"]["success"]
