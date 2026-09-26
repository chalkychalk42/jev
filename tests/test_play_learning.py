"""The motor corpus, and the shadow proposals of the students training published.

No student trains or acts any more (V174, V225). The corpus keeps every tutor action with
its observed and episode outcomes, and a model already in the store is still asked for a
proposal, recorded as the tutor's shadow. Nothing can train a model now, so `frozen`
writes one here as training wrote them: examples by their features, scales and a radius.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from jev.persist import atomic_json
from jev.play.learning import (
    FORMAT,
    MotorLearner,
    _digest,
    _features,
    _label,
    _parameters,
    _qualified,
    _spatial,
    _template,
)


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


def failure(row):
    """An observed failure, as a model keeps it: its context and its action."""
    action = _label(row["action"])
    return {"features": _features(row["before"], visual=_spatial(action)), "action": action}


def frozen(learner, rows, *, capability="approach", controls="controls-v1", radius=0.05,
           failures=(), mode="shadow", reason="fixture"):
    """A student as training left it in the store, and the registry entry `predict` reads."""
    examples = []
    for row in rows:
        action = _label(row["action"])
        examples.append({"features": _features(row["before"], visual=_spatial(action)),
                         "action": action, "expected_effect": row["expected_effect"],
                         "run_id": row["run_id"], "decision_id": row["decision_id"]})
    scales = {f"{block}.{key}": 1.0 for example in examples
              for block in ("numeric", "visual") for key in example["features"][block]}
    parameter_scales = {f"{_digest(_template(example['action']))}.{key}": 1.0
                        for example in examples for key in _parameters(example["action"])}
    model = {"format": FORMAT, "examples": examples, "scales": scales, "radius": radius,
             "parameter_scales": parameter_scales, "parameter_radii": {}, "failures": [],
             "evaluation": {"eligible": True}}
    model_id = "motor-" + _digest(model)[:24]
    path = learner.directory / "models" / f"{model_id}.json"
    atomic_json(path, model)
    atomic_json(learner.registry_path, {
        "format": FORMAT, "generation": 1,
        "models": {model_id: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "capability": capability}},
        "capabilities": {capability: {"model": model_id, "mode": mode, "reason": reason,
                                      "controls_fingerprint": controls,
                                      "failures": list(failures)}}})
    return model_id


def taught(learner, *, capability="approach", action=None, failures=()):
    """Two rows a side from each of three runs, recorded, and a student frozen from the
    qualified ones, as training took them."""
    for run in ("fit-a", "fit-b", "fit-c"):
        for side in range(2):
            for index in range(2):
                learner.record(record(run, side * 2 + index, side=side, capability=capability,
                                      action=action))
        finish(learner, run)
    qualified = [row for row in learner.records() if _qualified(row) and _label(row["action"])]
    return frozen(learner, qualified, capability=capability, failures=failures)


def predict(learner, *, side=0, observation_value=None, capability="approach",
            controls="controls-v1"):
    return learner.predict(observation_value or observation(side=side), capability,
                           controls_fingerprint=controls)


def test_a_published_student_proposes_each_sides_turn_as_a_shadow_after_a_reload(tmp_path):
    model = taught(MotorLearner(tmp_path))
    other = MotorLearner(tmp_path)
    left, right = predict(other), predict(other, side=1)
    assert left.action == {"kind": "key", "control": "turn_left", "duration_s": 0.2}
    assert right.action == {"kind": "key", "control": "turn_right", "duration_s": 0.4}
    assert left.mode == right.mode == "shadow" and left.model == model
    assert left.expected_effect == "scene_changed"


def test_same_radio_different_screen_is_not_direction_evidence(tmp_path):
    learner = MotorLearner(tmp_path)
    taught(learner)
    assert predict(learner, side=0.5).action is None
    obs = observation()
    obs["features"] = {}
    assert predict(learner, observation_value=obs).action is None
    obs = observation()
    obs["screen"] = {}
    assert predict(learner, observation_value=obs).action is None


def test_unknown_state_missingness_and_changed_bindings_abstain(tmp_path):
    learner = MotorLearner(tmp_path)
    taught(learner)
    obs = observation()
    del obs["values"]["vitals.dead"]
    assert predict(learner, observation_value=obs).action is None
    assert predict(learner, observation_value=observation(hp=0.2)).action is None
    rebound = predict(learner, controls="changed")
    assert rebound.action is None and "controls" in rebound.reason


def test_transfer_ignores_quest_ids_names_goals_and_binds_current_click_identity(tmp_path):
    learner = MotorLearner(tmp_path)
    click = {"kind": "click", "button": "right", "intent": "interact", "x": 0.5, "y": 0.6,
             "expected_target_id": 77}
    taught(learner, action=click)
    obs = observation(target=999)
    obs["context"].update(goal="repair a different quest", step_id="quest-5261")
    proposal = predict(learner, observation_value=obs)
    assert proposal.action["expected_target_id"] == 999
    assert proposal.action["x"] == 0.5
    obs["features"]["screen.0"] = 0.5
    assert predict(learner, observation_value=obs).action is None


def test_an_observed_failure_vetoes_the_same_action_in_its_context(tmp_path):
    learner = MotorLearner(tmp_path)
    taught(learner, failures=[failure(record("veto", 0, success=False))])
    vetoed = predict(learner)
    assert vetoed.action is None and "vetoes" in vetoed.reason
    assert predict(learner, side=1).action is not None, "another context keeps its proposal"


def test_modified_model_fails_closed(tmp_path):
    learner = MotorLearner(tmp_path)
    model = taught(learner)
    path = tmp_path / "models" / f"{model}.json"
    path.write_text(path.read_text() + " ")
    assert predict(learner).action is None
    assert "checksum" in predict(learner).reason


def test_a_blocked_student_or_a_synthetic_view_gets_no_proposal(tmp_path):
    learner = MotorLearner(tmp_path)
    rows = [record(run, index) for run in ("a", "b", "c") for index in range(2)]
    frozen(learner, rows, mode="blocked", reason="student expected effect failed")
    blocked = predict(learner)
    assert blocked.action is None and blocked.reason == "student expected effect failed"
    learner = MotorLearner(tmp_path / "other")
    taught(learner)
    synthetic = observation()
    synthetic["synthetic"] = True
    assert predict(learner, observation_value=synthetic).action is None


def test_a_state_decision_transfers_across_scenery_but_a_turn_does_not(tmp_path):
    """Running COMBAT_PROFILE with a wolf selected is the same decision beside any tree;
    how far to turn depends on the picture."""
    routine = {"kind": "skill", "name": "COMBAT_PROFILE", "params": {}}
    learner = MotorLearner(tmp_path / "routine")
    taught(learner, capability="combat", action=routine)
    elsewhere = observation(side=0)
    elsewhere["features"] = {f"screen.{i}": 0.9 for i in range(12)}      # unfamiliar scene
    elsewhere["screen"] = {"sha256": "screen-elsewhere"}
    assert predict(learner, capability="combat", observation_value=elsewhere).action == routine

    turns = MotorLearner(tmp_path / "turns")
    taught(turns)
    assert predict(turns, observation_value=elsewhere).action is None, \
        "a turn learned in one picture was proposed for a different one"


def test_a_routine_with_graph_parameters_is_not_a_label():
    """Only a routine without parameters of its own is a whole action; its parameters
    would be graph facts the coach owns."""
    assert _label({"kind": "skill", "name": "COMBAT_PROFILE", "params": {}}) == {
        "kind": "skill", "name": "COMBAT_PROFILE", "params": {}}
    assert _label({"kind": "skill", "name": "TRAVEL_TO", "params": {"x": 0.5, "y": 0.4}}) is None


@pytest.mark.parametrize("bad", ["synthetic", "no_episode", "no_progress", "no_effect", "unverified", "wrong_effect"])
def test_every_attempt_is_kept_but_only_qualified_ones_could_teach(tmp_path, bad):
    learner = MotorLearner(tmp_path)
    for run in ("a", "b", "c"):
        for i in range(4):
            row = record(run, i, synthetic=bad == "synthetic", verified=bad != "unverified",
                         success=bad != "no_effect")
            if bad == "wrong_effect":
                row["outcome"]["effects"] = ["observed"]
            learner.record(row)
        if bad != "no_episode":
            finish(learner, run, progress=0 if bad == "no_progress" else 1)
    rows = learner.records()
    assert len(rows) == 12
    assert not any(_qualified(row) for row in rows)


def test_a_qualified_attempt_needs_its_own_verified_effect_and_its_episodes_progress(tmp_path):
    learner = MotorLearner(tmp_path)
    learner.record(record("a", 0))
    assert not _qualified(learner.records()[0]), "no episode yet"
    finish(learner, "a")
    assert _qualified(learner.records()[0])


def test_unknown_final_episode_cannot_be_injected_by_action_author(tmp_path):
    learner = MotorLearner(tmp_path)
    row = record("a", 1)
    row["episode_outcome"] = {"success": True, "verified": True, "progress": 100}
    learner.record(row)
    assert learner.records()[0]["episode_outcome"] is None


def test_duplicate_conflict_and_episode_idempotence(tmp_path):
    learner = MotorLearner(tmp_path)
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
    learner = MotorLearner(tmp_path / "learning")
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


TURNS = {"turn_left": {"command": "TURNLEFT", "keys": ["a"], "mode": "hold", "executable": True},
         "turn_right": {"command": "TURNRIGHT", "keys": ["d"], "mode": "hold", "executable": True}}
MANIFEST = {"bindings": TURNS, "skills": ["HUNT"], "limits": {"max_hold_s": 2.0}}


def test_a_run_brings_the_controls_it_played_under(tmp_path):
    from jev.play.observation import fingerprint

    store = tmp_path / "store"
    learner = MotorLearner(store)
    run = tmp_path / "run"
    run.mkdir()
    (run / "play-config.json").write_text(json.dumps(
        {"controls": MANIFEST, "controls_fingerprint": fingerprint(MANIFEST)}))
    learner.ingest_run(run)
    kept = store / "controls" / f"{fingerprint(MANIFEST)}.json"
    assert json.loads(kept.read_text()) == MANIFEST
    (run / "play-config.json").write_text(json.dumps(
        {"controls": MANIFEST, "controls_fingerprint": "not-its-print"}))
    learner.ingest_run(run)
    assert not (store / "controls" / "not-its-print.json").exists()


def test_an_unfinished_episode_reads_no_records(tmp_path, monkeypatch):
    """Scanning every record under the store's lock, for a student that was not playing,
    made other writers wait past Windows' ten-second lock (PermissionError [Errno 13])."""
    learner = MotorLearner(tmp_path)
    learner.record(record("run-a", 0))
    monkeypatch.setattr(learner, "records", lambda: (_ for _ in ()).throw(AssertionError("scanned")))
    assert learner.finish_episode("episode-run-a", run_id="run-a",
                                  outcome={"verified": True, "success": False, "progress": 0,
                                           "elapsed_s": 5})
