"""Motor learning tests exercise the full evidence→fit→shadow→canary→handover path."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace

import pytest

from jev.play.learning import LearningConfig, MotorLearner, _grouped_split


def config(**changes):
    return replace(LearningConfig(), min_train_runs=2, min_train_examples=4,
                   min_holdout_runs=1, min_holdout_examples=2,
                   min_support=2, min_support_runs=2,
                   min_shadow_examples=4, min_shadow_runs=2,
                   min_canary_examples=4, min_canary_runs=2,
                   min_teacher_baseline=4, retrain_new_runs=100, **changes)


def observation(identifier="now", *, side=0, target=77, hp=1.0):
    return {"id": identifier, "synthetic": False, "captured_at": 1,
            "values": {"vitals.hp": hp, "vitals.dead": False, "vitals.ghost": False,
                       "vitals.combat": False, "target.has": True, "target.name_id": target,
                       "target.in_melee": False, "ui.modal": False},
            "state": {}, "size": [1600, 900],
            "screen": {"sha256": f"screen-{side}"},
            "features": {f"screen.{i}": float(side) for i in range(12)},
            "context": {"skill": "HUNT", "kind": "quest_objective", "target_name_id": target,
                        "goal": "hunt arbitrary quest", "step_id": "quest-33"}}


def record(run, index, *, side=0, author="teacher", model=None, shadow=None,
           success=True, verified=True, synthetic=False, timestamp=1, action=None,
           capability="approach", controls="controls-v1", knowledge="knowledge-v1"):
    before = observation(f"{run}-{index}-before", side=side)
    after = observation(f"{run}-{index}-after", side=side)
    after["captured_at"] = 2
    return {"run_id": run, "episode_id": f"episode-{run}", "decision_id": f"{run}-{index}",
            "before": before, "after": after,
            "action": action or {"kind": "key", "control": "turn_left" if side == 0 else "turn_right",
                                 "duration_s": 0.2 if side == 0 else 0.4},
            "expected_effect": "scene_changed", "capability": capability,
            "outcome": {"verified": verified, "success": success, "fatal": False,
                        "effects": ["scene_changed"] if success else []},
            "author": author, "model": model, "shadow": shadow,
            "synthetic": synthetic, "controls_fingerprint": controls,
            "knowledge_fingerprint": knowledge, "timestamp": timestamp, "elapsed_s": 5,
            "cost": {"teacher_calls": int(author == "teacher"), "input_tokens": 100 if author == "teacher" else 0,
                     "output_tokens": 20 if author == "teacher" else 0}}


def finish(learner, run, *, success=True, verified=True, progress=1):
    seconds = sum(row.get("elapsed_s", 0) for row in learner.records() if row["run_id"] == run)
    learner.finish_episode(f"episode-{run}", run_id=run,
                           outcome={"verified": verified, "success": success, "progress": progress,
                                    "elapsed_s": max(seconds, 1)})


def teach(learner, *, capability="approach", action=None, synthetic=False):
    for run in ("fit-a", "fit-b", "fit-c"):
        for side in range(2):
            for index in range(2):
                learner.record(record(run, side * 2 + index, side=side, capability=capability,
                                      action=action, synthetic=synthetic))
        finish(learner, run)
    learner.update()
    return learner.status()["capabilities"].get(capability, {}).get("model")


def predict(learner, *, side=0, observation_value=None, capability="approach", decision_id="now", **kw):
    return learner.predict(observation_value or observation(side=side), capability,
                           decision_id=decision_id, controls_fingerprint="controls-v1",
                           knowledge_fingerprint="knowledge-v1", **kw)


def shadow_runs(learner, model):
    for run in ("shadow-a", "shadow-b"):
        for side in range(2):
            action = predict(learner, side=side).action
            learner.record(record(run, side, side=side,
                                  shadow={"model": model, "action": action,
                                          "expected_effect": "scene_changed"}))
        finish(learner, run)
    learner.update()


def canary_runs(learner, model):
    for run in ("canary-a", "canary-b"):
        for side in range(2):
            learner.record(record(run, side, side=side, author="student", model=model))
        finish(learner, run)
    learner.update()


def test_student_learns_direction_and_duration_and_survives_reload(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    assert model
    other = MotorLearner(tmp_path, config=config())
    left, right = predict(other), predict(other, side=1)
    assert left.action == {"kind": "key", "control": "turn_left", "duration_s": 0.2}
    assert right.action == {"kind": "key", "control": "turn_right", "duration_s": 0.4}
    assert left.mode == "shadow" and not left.can_execute
    assert left.expected_effect == "scene_changed"
    info = json.loads((tmp_path / "models" / f"{model}.json").read_text())
    assert not set(info["train_runs"]) & set(info["holdout_runs"])
    assert info["evaluation"]["precision"] == 1


def test_same_radio_different_screen_is_not_direction_evidence(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    teach(learner)
    assert predict(learner, side=0.5).action is None
    obs = observation()
    obs["features"] = {}
    assert predict(learner, observation_value=obs).action is None
    obs = observation()
    obs["screen"] = {}
    assert predict(learner, observation_value=obs).action is None


def test_unknown_state_missingness_and_changed_bindings_abstain(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    teach(learner)
    obs = observation()
    del obs["values"]["vitals.dead"]
    assert predict(learner, observation_value=obs).action is None
    obs = observation(hp=0.2)
    assert predict(learner, observation_value=obs).action is None
    assert learner.predict(observation(), "approach", decision_id="new",
                           controls_fingerprint="changed", knowledge_fingerprint="knowledge-v1").action is None


def test_transfer_ignores_quest_ids_names_goals_and_binds_current_click_identity(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    click = {"kind": "click", "button": "right", "intent": "interact", "x": 0.5, "y": 0.6,
             "expected_target_id": 77}
    teach(learner, action=click)
    obs = observation(target=999)
    obs["context"].update(goal="repair a different quest", step_id="quest-5261")
    proposal = predict(learner, observation_value=obs)
    assert proposal.action["expected_target_id"] == 999
    assert proposal.action["x"] == 0.5
    obs["features"]["screen.0"] = 0.5
    assert predict(learner, observation_value=obs).action is None


@pytest.mark.parametrize("bad", ["synthetic", "no_episode", "no_progress", "no_effect", "unverified", "wrong_effect"])
def test_nontraining_evidence_retained_without_success_labels(tmp_path, bad):
    learner = MotorLearner(tmp_path, config=config())
    for run in ("a", "b", "c"):
        for i in range(4):
            row = record(run, i, synthetic=bad == "synthetic", verified=bad != "unverified",
                         success=bad != "no_effect")
            if bad == "wrong_effect":
                row["outcome"]["effects"] = ["observed"]
            learner.record(row)
        if bad != "no_episode":
            finish(learner, run, progress=0 if bad == "no_progress" else 1)
    learner.update()
    assert len(learner.records()) == 12
    assert predict(learner).action is None


def test_unknown_final_episode_cannot_be_injected_by_action_author(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    row = record("a", 1)
    row["episode_outcome"] = {"success": True, "verified": True, "progress": 100}
    learner.record(row)
    assert learner.records()[0]["episode_outcome"] is None


def test_complete_measured_handover_and_immediate_capability_rollback(tmp_path):
    learner = MotorLearner(tmp_path, config=config(), clock=lambda: 100)
    model = teach(learner)
    shadow_runs(learner, model)
    assert learner.status()["capabilities"]["approach"]["mode"] == "canary"
    draws = [predict(learner, decision_id=str(i)).mode for i in range(1000)]
    assert 50 < draws.count("canary") < 150
    canary_runs(learner, model)
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "active"
    assert state["metrics"]["student_cost"]["teacher_calls"] == 0
    assert state["metrics"]["teacher_baseline"]["teacher_calls"] == 4
    draws = [predict(learner, decision_id=str(i)).mode for i in range(100)]
    assert "active" in draws and "shadow" in draws
    learner.record(record("failure", 1, author="student", model=model, success=False))
    assert predict(learner).action is None
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_student_failed_episode_rejects_motor_effect_success(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    learner.record(record("walked-nowhere", 1, author="student", model=model))
    finish(learner, "walked-nowhere", success=False, progress=0)
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_interruption_quarantines_without_blacklisting_candidate(tmp_path):
    learner = MotorLearner(tmp_path, config=config(), clock=lambda: 100)
    model = teach(learner)
    shadow_runs(learner, model)
    learner.record(record("cancel", 1, author="student", model=model,
                          success=False, verified=False, timestamp=99))
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "shadow"
    assert model not in state["blocked"]
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "shadow"


def test_no_cost_measurement_no_promotion(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    for run in ("canary-a", "canary-b"):
        for side in range(2):
            row = record(run, side, side=side, author="student", model=model)
            del row["cost"]
            learner.record(row)
        finish(learner, run)
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "canary"


def test_training_or_holdout_rows_never_count_as_live_shadow(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    for run in ("fit-a", "fit-b", "fit-c"):
        for side in range(2):
            learner.record(record(run, 100 + side, side=side,
                                  shadow={"model": model, "action": predict(learner, side=side).action,
                                          "expected_effect": "scene_changed"}))
    learner.update()
    assert learner.status()["capabilities"]["approach"]["metrics"]["shadow_examples"] == 0


def test_linked_encounters_cannot_cross_holdout_runs():
    rows = [record("a", 1), record("b", 1), record("c", 1), record("d", 1)]
    rows[0]["encounter_id"] = rows[1]["encounter_id"] = "same-encounter"
    train, test = _grouped_split(rows, 1)
    assert not ({"a", "b"} & {r["run_id"] for r in train}
                and {"a", "b"} & {r["run_id"] for r in test})


def test_duplicate_conflict_and_episode_idempotence(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    row = record("a", 1)
    assert learner.record(row)
    assert not learner.record(row)
    bad = copy.deepcopy(row)
    bad["action"]["duration_s"] = 1.0
    with pytest.raises(ValueError, match="conflicting"):
        learner.record(bad)
    finish(learner, "a")
    finish(learner, "a")
    with pytest.raises(ValueError, match="conflicting"):
        finish(learner, "a", success=False)


def test_journal_recovery_ignores_unfinished_requests_and_partial_line(tmp_path):
    learner = MotorLearner(tmp_path / "learning", config=config())
    run = tmp_path / "run"
    run.mkdir()
    row = record("recovered", 1)
    (run / "play-actions.jsonl").write_text(
        json.dumps({"event": "request", "decision_id": "unexecuted"}) + "\n"
        + json.dumps({"event": "result", **row}) + "\n{\"unfinished\":")
    final = {"schema": 1, "run_id": "recovered", "episode_id": "episode-recovered",
             "verified": True, "success": True, "progress": 1}
    (run / "play-episodes.jsonl").write_text(json.dumps(final) + "\n")
    assert learner.ingest_run(run) == {"records": 1, "episodes": 1, "errors": []}
    assert learner.ingest_run(run) == {"records": 0, "episodes": 0, "errors": []}
    assert learner.records()[0]["episode_outcome"]["success"]


def test_modified_model_fails_closed(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    path = tmp_path / "models" / f"{model}.json"
    path.write_text(path.read_text() + " ")
    assert predict(learner).action is None
    assert "checksum" in predict(learner).reason


def test_cancelled_training_does_not_publish(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    for run in ("a", "b", "c"):
        for i in range(4):
            learner.record(record(run, i))
        finish(learner, run)
    learner.update(cancelled=lambda: True)
    assert not learner.status()["models"]


def test_background_fit_cannot_block_record_or_overwrite_live_rollback(tmp_path, monkeypatch):
    from jev.play import learning

    learner = MotorLearner(tmp_path, config=replace(config(), retrain_new_runs=1))
    model = teach(learner)
    learner.record(record("new-run", 1))
    finish(learner, "new-run")
    fitting, release = threading.Event(), threading.Event()
    original = learning._fit

    def wait_fit(*args, **kwargs):
        fitting.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(learning, "_fit", wait_fit)
    worker = threading.Thread(target=learner.update)
    worker.start()
    assert fitting.wait(5)
    try:
        learner.record(record("bad-live", 1, author="student", model=model, success=False))
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_new_runs_retrain_frozen_candidate_without_promoting_the_new_weights(tmp_path):
    learner = MotorLearner(tmp_path, config=replace(config(), retrain_new_runs=1))
    old = teach(learner)
    for side in range(2):
        learner.record(record("new-fit", side, side=side))
    finish(learner, "new-fit")
    learner.update()
    state = learner.status()["capabilities"]["approach"]
    assert state["model"] != old
    assert state["previous"] == old
    assert state["mode"] == "shadow"


def test_other_capabilities_keep_their_authority_after_failure(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    for run in ("service-a", "service-b", "service-c"):
        for i in range(4):
            learner.record(record(run, i, capability="service"))
        finish(learner, run)
    learner.update()
    before = learner.status()["capabilities"]["service"]
    learner.record(record("bad", 1, author="student", model=model, success=False))
    assert learner.status()["capabilities"]["service"] == before


def test_canary_expires_without_silent_promotion(tmp_path):
    now = [0]
    learner = MotorLearner(tmp_path, config=config(), clock=lambda: now[0])
    model = teach(learner)
    shadow_runs(learner, model)
    now[0] = 10000
    assert predict(learner).mode == "shadow"
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_failed_training_examples_veto_replaying_successful_action(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    for run in ("a", "b", "c"):
        for i in range(4):
            learner.record(record(run, i))
        learner.record(record(run, 100, success=False))
        finish(learner, run)
    learner.update()
    assert predict(learner).action is None


def test_action_authorship_never_proves_success(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    for run in ("teacher-a", "teacher-b", "teacher-c"):
        for i in range(4):
            learner.record(record(run, i, author="teacher", success=False))
        finish(learner, run)
    learner.update()
    assert not learner.status()["models"]


@pytest.mark.parametrize("kind", ["duration", "click", "pointer", "camera"])
def test_continuous_parameters_share_support_without_rounding_or_inventing_outputs(tmp_path, kind):
    learner = MotorLearner(tmp_path, config=config())
    names = ["jitter-a", "jitter-b", "jitter-c", "jitter-d", "jitter-e"]
    _, holdout = _grouped_split([record(run, 0) for run in names], 1)
    heldout_run = holdout[0]["run_id"]
    training_actions = []
    index = 0
    for run in names:
        if run == heldout_run:
            amount = 1.5
        else:
            amount = index
            index += 1
        if kind == "duration":
            action = {"kind": "key", "control": "turn_left", "duration_s": 0.2 + amount * 0.01}
            expected = "scene_changed"
        elif kind in {"click", "pointer"}:
            action = {"kind": kind, "x": 0.45 + amount * 0.005, "y": 0.6 + amount * 0.004}
            expected = "observed"
            if kind == "click":
                action.update(button="right", intent="interact", expected_target_id=77)
                expected = "ui_opened"
        else:
            action = {"kind": "camera", "axis": "yaw", "pixels": -80 - int(amount * 6)}
            expected = "scene_changed"
        if run != heldout_run:
            training_actions.append(action)
        for i in range(4):
            row = record(run, i, action=action)
            row["expected_effect"] = expected
            row["outcome"]["effects"] = [expected]
            learner.record(row)
        finish(learner, run)
    learner.update()
    prediction = predict(learner)
    assert prediction.action is not None
    # Default values may be present in parsed actions; compare every meaningful
    # teacher parameter and require one entire actually observed action, not axis-wise
    # interpolation which could point somewhere no successful click ever occurred.
    assert any(all(prediction.action.get(key) == value for key, value in action.items())
               for action in training_actions)
    evaluation = learner.status()["capabilities"]["approach"]["evaluation"]
    assert evaluation["eligible"]
    assert evaluation["structural_agreement"] == evaluation["parameter_coverage"] == 1
    assert evaluation["exact_agreement"] == 0


def test_unknown_episode_does_not_restore_authority_after_observed_action_failure(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    learner.record(record("failed", 1, author="student", model=model, success=False))
    finish(learner, "failed", success=False, verified=False, progress=0)
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"


def test_verified_wait_is_learnable_without_claiming_hid_input_was_delivered(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    for run in ("a", "b", "c"):
        for index in range(4):
            row = record(run, index, action={"kind": "observe", "wait_s": 0.5})
            row.update(expected_effect="observed", delivery={"code": "observed", "delivered": False})
            row["outcome"]["effects"] = ["observed"]
            learner.record(row)
        finish(learner, run)
    learner.update()
    prediction = predict(learner)
    assert prediction.action == {"kind": "observe", "wait_s": 0.5}
    assert prediction.expected_effect == "observed"


def test_partial_coverage_can_handover_only_supported_contexts(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    for i in range(10):
        learner.record(record("novel", i, side=0.5,
                              shadow={"model": model, "action": None, "expected_effect": None}))
    finish(learner, "novel")
    shadow_runs(learner, model)
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "canary"
    assert state["metrics"]["shadow_examples"] == 4
    assert state["metrics"]["shadow_coverage"] == 4 / 14
    assert predict(learner, side=0.5).action is None


def test_teacher_corrections_in_other_capabilities_are_part_of_student_episode_cost(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    for run in ("canary-a", "canary-b"):
        for side in range(2):
            row = record(run, side, side=side, author="student", model=model)
            row["elapsed_s"] = 1
            learner.record(row)
        for i in range(3):
            correction = record(run, 100 + i, capability="service")
            correction["elapsed_s"] = 0.1
            learner.record(correction)
        finish(learner, run)
    learner.update()
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "canary"
    assert state["metrics"]["student_cost"]["teacher_calls"] == 6
    assert state["metrics"]["student_cost"]["progress_per_hour"] > state["metrics"]["teacher_baseline"]["progress_per_hour"]
    assert state["metrics"]["student_cost"]["teacher_calls_per_progress"] == 3


def test_zero_teacher_calls_cannot_promote_slower_task_progress(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    for run in ("canary-a", "canary-b"):
        for side in range(2):
            row = record(run, side, side=side, author="student", model=model)
            row["elapsed_s"] = 10
            learner.record(row)
        finish(learner, run)
    learner.update()
    state = learner.status()["capabilities"]["approach"]
    assert state["mode"] == "canary"
    assert state["metrics"]["student_cost"]["teacher_calls"] == 0
    assert state["metrics"]["student_cost"]["progress_per_hour"] < state["metrics"]["teacher_baseline"]["progress_per_hour"]


def test_invalid_negative_cost_cannot_be_presented_as_teacher_savings(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    model = teach(learner)
    shadow_runs(learner, model)
    for run in ("canary-a", "canary-b"):
        for side in range(2):
            row = record(run, side, side=side, author="student", model=model)
            row["cost"]["teacher_calls"] = -1
            learner.record(row)
        finish(learner, run)
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "canary"


def test_active_policy_rolls_back_when_successful_work_becomes_slower(tmp_path):
    learner = MotorLearner(tmp_path, config=config(), clock=lambda: 100)
    model = teach(learner)
    shadow_runs(learner, model)
    canary_runs(learner, model)
    assert learner.status()["capabilities"]["approach"]["mode"] == "active"
    for run in ("slow-a", "slow-b"):
        for side in range(2):
            row = record(run, side, side=side, author="student", model=model, timestamp=101)
            row["elapsed_s"] = 10
            learner.record(row)
        finish(learner, run)
    learner.update()
    assert learner.status()["capabilities"]["approach"]["mode"] == "blocked"
    assert "throughput" in learner.status()["capabilities"]["approach"]["reason"]


def test_a_routine_jev_chose_is_learned_as_a_whole_action(tmp_path):
    """Delegating COMBAT_PROFILE in the right situation is a decision the student can take
    over; only routines without parameters of their own become labels."""
    learner = MotorLearner(tmp_path / "motor", config=config())
    routine = {"kind": "skill", "name": "COMBAT_PROFILE", "params": {}}
    model = teach(learner, capability="combat", action=routine)
    assert model is not None
    prediction = predict(learner, capability="combat")
    assert prediction.action == routine


def test_a_routine_with_graph_parameters_is_not_a_label(tmp_path):
    learner = MotorLearner(tmp_path / "motor", config=config())
    routine = {"kind": "skill", "name": "TRAVEL_TO", "params": {"x": 0.5, "y": 0.4}}
    assert teach(learner, capability="travel", action=routine) is None


def test_a_state_decision_transfers_across_scenery_but_a_turn_does_not(tmp_path):
    """Running COMBAT_PROFILE with a wolf selected is the same decision beside any tree;
    how far to turn depends on the picture."""
    routine = {"kind": "skill", "name": "COMBAT_PROFILE", "params": {}}
    learner = MotorLearner(tmp_path / "routine", config=config())
    teach(learner, capability="combat", action=routine)
    elsewhere = observation(side=0)
    elsewhere["features"] = {f"screen.{i}": 0.9 for i in range(12)}      # unfamiliar scene
    elsewhere["screen"] = {"sha256": "screen-elsewhere"}
    assert predict(learner, capability="combat", observation_value=elsewhere).action == routine

    turns = MotorLearner(tmp_path / "turns", config=config())
    teach(turns)
    assert predict(turns, observation_value=elsewhere).action is None, \
        "a turn learned in one picture was proposed for a different one"


def test_a_new_guide_does_not_start_the_corpus_again(tmp_path):
    """Knowledge shaped which actions the tutor chose, but every label is graded by its
    observed outcome and none of it is a model input. Tied to it, the corpus restarted at
    every guide change - a regenerated guide or the next level band (V71)."""
    learner = MotorLearner(tmp_path, config=config())
    for number, run in enumerate(("fit-a", "fit-b", "fit-c")):
        knowledge = "knowledge-v1" if number < 2 else "knowledge-v2"      # the guide moved on
        for side in range(2):
            for index in range(2):
                learner.record(record(run, side * 2 + index, side=side, knowledge=knowledge))
        finish(learner, run)
    learner.update()
    state = learner.status()["capabilities"].get("approach")
    assert state and state["model"], "runs from before the guide changed were thrown away"
    assert state["knowledge_fingerprint"] == "knowledge-v2", "the newest is kept, for audit"
    moved_on = learner.predict(observation(side=1), "approach", decision_id="now",
                               controls_fingerprint="controls-v1",
                               knowledge_fingerprint="knowledge-v3")
    assert moved_on.action == {"kind": "key", "control": "turn_right", "duration_s": 0.4}


def test_new_key_bindings_still_start_a_new_generation(tmp_path):
    learner = MotorLearner(tmp_path, config=config())
    teach(learner)
    assert predict(learner).action is not None
    rebound = learner.predict(observation(side=0), "approach", decision_id="now",
                              controls_fingerprint="controls-v2",
                              knowledge_fingerprint="knowledge-v1")
    assert rebound.action is None and "controls" in rebound.reason
