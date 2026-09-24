"""Real grading/training/persistence with fixture observations; no client is opened."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jev.coach.schema import Intent
from jev.coach.situation import situation_key
from jev.guide.graph import Node
from jev.learn.dataset import build
from jev.learn.distill import Prediction, cold_start
from jev.learn.episode import DecisionRow, Recorder, SkillOutcome, SkillResultRow, TickRow
from jev.learn.evidence import (
    EvidenceGates,
    Observation,
    compare,
    independent,
    metrics,
    observations,
    shadow_evidence,
    split_runs,
)
from jev.learn.parquet import convert_run, read_parquet
from jev.learn.registry import ModelRegistry, PolicyManager, file_lock, grounded_decision
from jev.learn.worker import CycleReport, LearningWorker, WorkerConfig
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    GuidePos,
    Pos,
    Sense,
    Source,
    State,
    StepKind,
    Vitals,
)

DIGEST = "a" * 64


def state(t=0, *, travel=False, source=Source.RADIO):
    s = State(t=t, client_id="c", char=Char(level=2, xp_pct=0.1),
              vitals=Vitals(hp=1, dead=False, ghost=False, combat=False),
              bags=Bags(free=10, durability_min=1),
              pos=Pos(mx=0.1 if travel else 0.5, my=0.5),
              guide=GuidePos(graph_id="g", step_id="a", kind=StepKind.QUEST_ACCEPT,
                             on_route=not travel, age_s=0, progress=0),
              sense=Sense(addon_ok=True, source=source))
    return s.model_copy(update={"situation_key": situation_key(s)})


NODE = Node(id="a", kind=StepKind.QUEST_ACCEPT, zone="Elwynn", zone_id=12,
            pos=(0.5, 0.5), quest_id=33, target_name="Willem", target_kind="creature",
            skills=("TRAVEL_TO", "ACCEPT_QUEST"))


def write_run(root, name, *, source=Source.RADIO, episodes=12, duration=4,
              model="scripted:guide.step", xp=0.2, die=False):
    with Recorder(root, run_id=name) as rec:
        for t in range(episodes * duration + 1):
            i = min(t // duration, episodes - 1)
            travel = i % 2 == 0
            s = state(t, travel=travel, source=source)
            s = s.model_copy(update={
                "char": s.char.model_copy(update={"xp_pct": xp * t / (episodes * duration)}),
                "vitals": s.vitals.model_copy(update={"dead": die and t % duration == 2}),
                "guide": s.guide.model_copy(update={"step_id": f"s{t // duration}"}),
            })
            did = f"d{i}"
            skill, intent = ("TRAVEL_TO", "rejoin") if travel else ("ACCEPT_QUEST", "advance")
            if t < episodes * duration and t % duration == 0:
                rec.decision(DecisionRow(name, did, t + 1, t, "c", s.situation_key,
                                         ArmedBy.POLICY, model, intent, skill))
            rec.tick(TickRow(name, t + 1, t, "c", s.model_dump(mode="json"), s.situation_key,
                             skill, intent, ArmedBy.POLICY, decision_id=did,
                             tracker_event="advance" if t % duration == 1 else "none",
                             shadow_model="policy:v0"))
    (root / name / "route.json").write_text(json.dumps({"graph": "g", "graph_digest": DIGEST}))
    return root / name


def small_config(**kwargs):
    return WorkerConfig(min_training_rows=20, min_training_runs=2, min_new_rows=1,
                        retrain_s=0, window_s=4,
                        gates=EvidenceGates(min_windows=3, min_runs=1, min_observed_s=12,
                                            confidence=0.01), **kwargs)


def metadata(**kwargs):
    return dict({"graphs": ["g"], "graph_digests": {"g": DIGEST},
                 "brackets": ["1-6"], "training_runs": ["train"],
                 "heldout_runs": ["held"], "synthetic": False,
                 "shadow_evidence": {"1-6": supported()}}, **kwargs)


def observation(i, *, run="r", model="scripted:guide.step", good=True, xp=0.1,
                died=False, stuck=0, graph="g", t=None):
    return Observation(run, "c", f"d{i}", model, i * 60 if t is None else t, 60,
                       graph, "1-6", "advance", "ACCEPT_QUEST", state(), True,
                       xp, died, stuck, 1 if good else -1, good, DIGEST)


def supported():
    row = observation(0)
    report = compare([row], [row], EvidenceGates(
        min_windows=1, min_runs=1, min_observed_s=60), improvement=False)
    report["checks"].update(coverage=True, supported_actions=True, graph_revision=True)
    return report


def test_worker_grades_trains_and_restarts_without_duplicate_candidates(tmp_path):
    runs, store = tmp_path / "runs", tmp_path / "models"
    for i in range(6):
        write_run(runs, f"run{i}")
    worker = LearningWorker(runs, store, config=small_config())
    report = worker.cycle()
    assert report.errors == {}
    assert report.examples == 72 and report.candidate == "policy:v1"
    snapshot = worker.registry.read()
    meta = snapshot["models"][report.candidate]
    assert set(meta["training_runs"]).isdisjoint(meta["heldout_runs"])
    assert meta["evaluation"]["held_out"]
    assert all(b["active"] is None for b in snapshot["bands"].values())
    assert worker.registry.load("policy:v1").n_rows == meta["training_rows"]
    again = LearningWorker(runs, store, config=small_config()).cycle()
    assert again.errors == {} and again.graded_runs == 0 and again.candidate is None
    assert len(worker.registry.read()["models"]) == 1
    data = build(list(runs.iterdir()))
    training, testing = split_runs(data)
    assert {e.run_id for e in training.examples}.isdisjoint(e.run_id for e in testing.examples)
    # A different outcome in the original corpus invalidates the derived labels.
    write_run(runs, "new_run")
    assert worker.cycle().candidate == "policy:v2"


def test_full_model_cycle_from_recorded_outcomes_through_promotion_and_rollback(tmp_path):
    runs, store = tmp_path / "runs", tmp_path / "models"
    for i in range(6):
        write_run(runs, f"baseline{i}")
    # Small gates make the fixture quick; production defaults remain 30 windows/3 runs.
    worker = LearningWorker(runs, store,
                            config=small_config(allow_canary=True, canary_fraction=1))
    first = worker.cycle()
    assert first.errors == {}
    assert first.candidate == "policy:v1"
    assert worker.registry.read()["bands"]["1-6"]["status"] == "canary"
    manager = PolicyManager(store, graph_digests={"g": DIGEST}, confidence=0.01)
    assert manager.decide(state(), NODE, frozenset(NODE.skills)).skill == "ACCEPT_QUEST"
    assert manager.decide(state(travel=True), NODE, frozenset(NODE.skills)).skill == "TRAVEL_TO"
    write_run(runs, "fresh_candidate", model=manager.model_name, xp=0.4)
    second = worker.cycle()
    assert second.errors == {}
    band = worker.registry.read()["bands"]["1-6"]
    assert band["active"] == "policy:v1" and band["status"] == "active"
    assert "1-6: promoted policy:v1" in second.transitions
    # The model's later deaths invalidate continuation even when its XP remains high.
    write_run(runs, "later_regression", model="policy:v1", xp=0.5, die=True)
    third = worker.cycle()
    assert third.errors == {}
    assert any("rollback policy:v1" in t for t in third.transitions)
    assert "policy:v1" in worker.registry.read()["bands"]["1-6"]["blocked"]


def test_synthetic_and_short_windows_cannot_activate_models(tmp_path):
    runs = tmp_path / "runs"
    for i in range(6):
        write_run(runs, f"run{i}", source=Source.SYNTHETIC)
    live = LearningWorker(runs, tmp_path / "live", config=small_config(allow_canary=True)).cycle()
    assert live.examples == 0 and live.candidate is None
    offline = LearningWorker(runs, tmp_path / "test",
                              config=small_config(include_synthetic=True)).cycle()
    assert offline.candidate == "policy:v1"
    registry = ModelRegistry(tmp_path / "test")
    assert not registry.canary("1-6", "policy:v1", evidence=supported())
    with pytest.raises(ValueError, match="synthetic"):
        small_config(include_synthetic=True, allow_canary=True)


def test_legacy_corpus_trains_in_shadow_but_cannot_activate_without_graph_revision(tmp_path):
    runs = tmp_path / "runs"
    for i in range(6):
        directory = write_run(runs, f"r{i}")
        (directory / "route.json").unlink()
    worker = LearningWorker(runs, tmp_path / "store", config=small_config(allow_canary=True))
    result = worker.cycle()
    assert not result.errors and result.candidate == "policy:v1"
    band = worker.registry.read()["bands"]["1-6"]
    assert band["active"] is None
    evidence = worker.registry.read()["models"]["policy:v1"]["shadow_evidence"]["1-6"]
    assert not evidence["checks"]["graph_revision"]


def test_graph_content_change_revokes_model_scope_even_with_identical_graph_id(tmp_path):
    registry = ModelRegistry(tmp_path)
    name = registry.publish(cold_start(1), metadata())
    registry.canary("1-6", name, evidence=supported(), fraction=1)
    manager = PolicyManager(tmp_path, graph_digests={"g": "b" * 64})
    manager.policies[name] = SimpleNamespace(
        predict_full=lambda _: Prediction("advance", "ACCEPT_QUEST", 1, 1))
    assert manager.decide(state(), NODE, frozenset(NODE.skills)) is None
    # Omitting graph revision is also an abstention, never permission for every revision.
    manager.graph_digests = {}
    assert manager.decide(state(), NODE, frozenset(NODE.skills)) is None


def test_unobserved_life_flags_cannot_become_training_or_safety_evidence(tmp_path):
    runs = tmp_path / "runs"
    directory = write_run(runs, "unknown", episodes=1)
    path = directory / "ticks.jsonl"
    ticks = [json.loads(line) for line in path.read_text().splitlines()]
    ticks[2]["state"]["vitals"]["dead"] = None
    path.write_text("".join(json.dumps(row) + "\n" for row in ticks))
    worker = LearningWorker(runs, tmp_path / "store", config=small_config())
    result = worker.cycle()
    assert result.errors == {}
    assert result.examples == 0 and result.unobserved_outcomes == 1


def test_synthetic_future_ticks_cannot_supply_a_live_decisions_outcome(tmp_path):
    runs = tmp_path / "runs"
    directory = write_run(runs, "mixed", episodes=1)
    path = directory / "ticks.jsonl"
    ticks = [json.loads(line) for line in path.read_text().splitlines()]
    ticks[2]["state"]["sense"]["source"] = "synthetic"
    path.write_text("".join(json.dumps(row) + "\n" for row in ticks))
    report = LearningWorker(runs, tmp_path / "store", config=small_config()).cycle()
    assert report.errors == {}
    assert report.examples == 0 and report.unobserved_outcomes == 1


def test_identical_actions_from_two_models_cannot_share_positive_promotion_credit(tmp_path):
    directory = write_run(tmp_path, "r", episodes=2)
    worker = LearningWorker(tmp_path, tmp_path / "store", config=small_config())
    worker.cycle()
    ticks = [json.loads(line) for line in (directory / "ticks.jsonl").read_text().splitlines()]
    decisions = [json.loads(line) for line in (directory / "decisions.jsonl").read_text().splitlines()]
    grades = [json.loads(line) for line in (directory / "grades.jsonl").read_text().splitlines()]
    decisions[0]["model"] = "policy:v1"
    decisions[1].update(model="policy:v2", intent=decisions[0]["intent"], skill=decisions[0]["skill"])
    rows = observations(ticks, decisions, grades)
    first = next(r for r in rows if r.decision_id == "d0")
    assert not first.exclusive_model
    assert metrics([first]).steps_per_h == 0
    assert metrics([first]).xp_per_h == 0
    # A later intervention cannot hide observed adverse outcomes behind missing credit.
    assert metrics([replace(first, died=True, deaths=1)]).deaths_per_h > 0


def test_one_sustained_death_is_not_counted_again_in_each_outcome_window(tmp_path):
    from jev.learn.grade import grade_run
    directory = write_run(tmp_path, "dead", episodes=3, duration=4)
    path = directory / "ticks.jsonl"
    ticks = [json.loads(line) for line in path.read_text().splitlines()]
    for tick in ticks:
        tick["state"]["vitals"]["dead"] = tick["t"] >= 2
    path.write_text("".join(json.dumps(row) + "\n" for row in ticks))
    grade_run(directory, window_s=4)
    decisions = [json.loads(line) for line in (directory / "decisions.jsonl").read_text().splitlines()]
    grades = [json.loads(line) for line in (directory / "grades.jsonl").read_text().splitlines()]
    rows = observations(ticks, decisions, grades)
    assert [row.deaths for row in rows] == [1, 0, 0]
    assert metrics(rows).deaths_per_h == 3600 / 12
    assert sum(row.advances for row in rows) == 3


def test_worker_lock_and_missing_store_do_not_touch_models(tmp_path):
    worker = LearningWorker(tmp_path / "none", tmp_path / "models")
    assert "does not exist" in worker.cycle().reason
    with file_lock(worker.registry.directory / ".worker.lock"):
        assert "another learning worker" in worker.cycle().reason
    assert not worker.registry.path.exists()


def test_unchanged_legacy_run_without_decisions_is_not_regraded_every_cycle(tmp_path):
    runs = tmp_path / "runs"
    directory = write_run(runs, "legacy", episodes=1)
    (directory / "decisions.jsonl").unlink()
    worker = LearningWorker(runs, tmp_path / "models", config=small_config())
    first = worker.cycle()
    assert first.errors == {} and first.graded_runs == 1 and first.examples == 0
    assert not (directory / "grades.jsonl").exists()
    again = LearningWorker(runs, tmp_path / "models", config=small_config()).cycle()
    assert again.errors == {} and again.graded_runs == 0 and again.examples == 0
    # A later recorder decision stream changes the source fingerprint and is observed.
    (directory / "decisions.jsonl").write_text("")
    assert worker.cycle().graded_runs == 1
    assert (directory / "grades.jsonl").exists()


def test_model_publication_integrity_and_per_band_known_good_rollback(tmp_path):
    registry = ModelRegistry(tmp_path)
    first = registry.publish(cold_start(1), metadata())
    second = registry.publish(cold_start(2), metadata(brackets=["1-6", "12-18"]))
    # Evidence is mandatory even if a caller asks to activate.
    assert not registry.canary("1-6", second, evidence={"eligible": False})
    assert not registry.canary("1-6", second, evidence={"eligible": True, "agreement": 1})
    with registry.edit() as snap:
        snap["bands"]["1-6"].update(active=first, status="active")
    assert registry.canary("1-6", second, evidence=supported(), now=0)
    assert registry.rollback("1-6", second, "executor refused unsupported plan")
    band = registry.read()["bands"]["1-6"]
    assert band["active"] == first and second in band["blocked"]
    assert registry.read()["bands"]["12-18"]["active"] is None
    assert not registry.canary("1-6", second, evidence=supported())
    assert not registry.rollback("1-6", second, "stale report")
    path = tmp_path / registry.read()["models"][first]["path"]
    path.write_bytes(b"truncated model")
    with pytest.raises(ValueError, match="checksum"):
        registry.load(first)
    manager = PolicyManager(tmp_path, graph_digests={"g": DIGEST})
    assert manager.last_error and manager.shadow(state()).is_abstention


def test_only_observed_current_guide_params_can_be_emitted():
    prediction = Prediction("rejoin", "TRAVEL_TO", 0.9, 0.9)
    decision = grounded_decision(prediction, state(travel=True), NODE,
                                 frozenset({"TRAVEL_TO", "ACCEPT_QUEST"}))
    assert decision.intent is Intent.REJOIN
    assert decision.params == {"zone": "Elwynn", "x": 0.5, "y": 0.5, "r": 0.03}
    assert grounded_decision(prediction, state(), NODE, frozenset({"TRAVEL_TO"})) is None
    assert grounded_decision(replace(prediction, skill_confidence=0.2), state(travel=True),
                             NODE, frozenset({"TRAVEL_TO"})) is None
    assert grounded_decision(replace(prediction, skill="ACCEPT_QUEST"), state(travel=True),
                             NODE, frozenset({"ACCEPT_QUEST"})) is None
    low = state(travel=True).model_copy(update={"bags": Bags(free=0)})
    assert grounded_decision(prediction, low, NODE, frozenset({"TRAVEL_TO"})) is None


def test_manager_canary_expiry_graph_scope_refresh_and_failure(tmp_path):
    registry = ModelRegistry(tmp_path)
    name = registry.publish(cold_start(1), metadata())
    assert registry.canary("1-6", name, evidence=supported(),
                           fraction=1, duration_s=10, now=0)
    now = [0]
    manager = PolicyManager(tmp_path, graph_digests={"g": DIGEST}, clock=lambda: now[0])
    pred = Prediction("advance", "ACCEPT_QUEST", 0.9, 0.9)
    fake = SimpleNamespace(predict_full=lambda _: pred, shadow=lambda _: pred)
    manager.policies[name] = fake
    assert manager.decide(state(), NODE, frozenset({"ACCEPT_QUEST"})) is not None
    assert manager.model_name == name
    assert manager.shadow(state()) == pred and manager.shadow_model == name
    unknown = state().model_copy(update={"guide": GuidePos(graph_id="other")})
    assert manager.decide(unknown, NODE, frozenset({"ACCEPT_QUEST"})) is None
    now[0] = 11
    assert manager.decide(state(), NODE, frozenset({"ACCEPT_QUEST"})) is None
    now[0] = 0
    manager.decide(state(), NODE, frozenset({"ACCEPT_QUEST"}))
    manager.report_failure(state(), "verifier rejected")
    assert registry.read()["bands"]["1-6"]["active"] is None
    assert manager.decide(state(), NODE, frozenset({"ACCEPT_QUEST"})) is None


def test_refresh_reuses_verified_models_and_rolls_back_corrupt_candidate(tmp_path, monkeypatch):
    registry = ModelRegistry(tmp_path)
    first = registry.publish(cold_start(1), metadata())
    with registry.edit() as data:
        data["bands"]["1-6"].update(active=first, status="active", graphs=["g"])
    second = registry.publish(cold_start(2), metadata())
    registry.canary("1-6", second, evidence=supported())
    manager = PolicyManager(tmp_path, graph_digests={"g": DIGEST})
    loads = []
    original = manager.registry.load
    monkeypatch.setattr(manager.registry, "load", lambda *a: (loads.append(a), original(*a))[1])
    manager.refresh()
    assert loads == []
    bad = tmp_path / registry.read()["models"][second]["path"]
    bad.write_bytes(b"broken")
    manager.refresh()
    assert registry.read()["bands"]["1-6"]["active"] == first
    assert first in manager.policies and second not in manager.policies
    assert manager.last_error


def test_failing_previous_model_does_not_disable_an_independent_canary(tmp_path):
    registry = ModelRegistry(tmp_path)
    first = registry.publish(cold_start(1), metadata())
    with registry.edit() as data:
        data["bands"]["1-6"].update(active=first, status="active", graphs=["g"])
    second = registry.publish(cold_start(2), metadata())
    registry.canary("1-6", second, evidence=supported())
    assert registry.rollback("1-6", first, "previous model prediction failed")
    band = registry.read()["bands"]["1-6"]
    assert band["active"] == second and band["status"] == "canary"
    assert band["previous"] is None and first in band["blocked"]
    assert registry.rollback("1-6", second, "canary failed")
    assert registry.read()["bands"]["1-6"]["active"] is None


def test_periodic_worker_survives_broken_reporter_and_stops(tmp_path):
    import threading
    stop = threading.Event()
    worker = LearningWorker(tmp_path / "none", tmp_path / "store",
                            config=WorkerConfig(interval_s=0.01))
    calls = []
    def reporter(report):
        calls.append(report)
        if len(calls) == 2:
            stop.set()
        raise RuntimeError("display disconnected")
    worker.run(stop, report_to=reporter)
    assert len(calls) == 2


def test_overlapping_windows_do_not_multiply_observation_evidence():
    rows = [observation(i, t=i) for i in range(61)]
    assert len(independent(rows)) == 2
    assert metrics(rows).observed_s == 120
    assert not compare(rows, rows, EvidenceGates(min_windows=10), improvement=False)["eligible"]


def test_agreement_is_not_improvement_and_route_safety_cannot_be_hidden():
    gates = EvidenceGates(min_windows=2, min_runs=1, min_observed_s=120)
    baseline = [observation(i) for i in range(2)]
    fake = SimpleNamespace(predict_full=lambda _: Prediction("advance", "ACCEPT_QUEST", 1, 1))
    support = shadow_evidence(fake, baseline, "1-6", {"r"}, gates)
    assert support["eligible"] and support["match_rate"] == 1
    assert not compare(baseline, baseline, gates, improvement=True)["eligible"]
    dying = [replace(o, xp=0.9, died=True) for o in baseline]
    assert not compare(dying, baseline, gates, improvement=True)["eligible"]
    stalled = [replace(o, xp=0.9, stuck_s=20) for o in baseline]
    assert not compare(stalled, baseline, gates, improvement=True)["eligible"]
    alien = [replace(o, graph="other", xp=0.9) for o in baseline]
    assert not compare(alien, baseline, gates, improvement=True)["eligible"]


def test_measured_candidate_promotes_then_later_regression_rolls_back(tmp_path):
    gates = EvidenceGates(min_windows=3, min_runs=1, min_observed_s=180)
    worker = LearningWorker(tmp_path / "runs", tmp_path / "models",
                            config=WorkerConfig(gates=gates))
    name = worker.registry.publish(cold_start(1), metadata())
    assert worker.registry.canary("1-6", name, evidence=supported())
    baseline = [observation(i, run="baseline") for i in range(3)]
    candidate = [observation(i, run="fresh", model=name, xp=0.2) for i in range(3)]
    report = CycleReport()
    worker._lifecycle([*baseline, *candidate], report)
    assert worker.registry.read()["bands"]["1-6"]["status"] == "active"
    assert report.transitions == ["1-6: promoted policy:v1"]
    bad = [replace(row, died=True, good=False) for row in candidate]
    worker._lifecycle([*baseline, *bad], CycleReport())
    assert worker.registry.read()["bands"]["1-6"]["active"] is None


def test_training_or_holdout_rows_never_become_live_promotion_measurements(tmp_path):
    worker = LearningWorker(tmp_path / "runs", tmp_path / "models",
                            config=WorkerConfig(gates=EvidenceGates(
                                min_windows=1, min_runs=1, min_observed_s=1)))
    name = worker.registry.publish(cold_start(1), metadata())
    worker.registry.canary("1-6", name, evidence=supported())
    rows = [observation(0, run="baseline"), observation(0, run="train", model=name, xp=1)]
    worker._lifecycle(rows, CycleReport())
    assert worker.registry.read()["bands"]["1-6"]["status"] == "canary"


def test_parquet_preserves_shadow_identity_and_skill_result_stream(tmp_path):
    path = write_run(tmp_path, "r", episodes=1)
    with Recorder(tmp_path, run_id="r") as rec:
        rec.skill_result(SkillResultRow("r", "c", 4, 5, "ACCEPT_QUEST", ArmedBy.POLICY,
                                       SkillOutcome.SUCCEEDED, 4, "k", decision_id="d0"))
    converted = convert_run(path)
    assert read_parquet(converted["ticks"])[0]["shadow_model"] == "policy:v0"
    assert read_parquet(converted["skills"])[0]["decision_id"] == "d0"


def test_publishing_flushes_through_a_handle_windows_can_flush(tmp_path, monkeypatch):
    """Windows fsyncs only a handle that may write: through a read-only one it is EBADF,
    and every live learning cycle failed at publish from 08:11 on 24 September."""
    import errno
    import fcntl
    import os

    real = os.fsync

    def windows_fsync(fd):
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real(fd)

    monkeypatch.setattr("jev.learn.registry.os.fsync", windows_fsync)
    monkeypatch.setattr("jev.persist.os.fsync", windows_fsync)
    registry = ModelRegistry(tmp_path)
    assert registry.publish(cold_start(1), metadata()) == "policy:v1"
