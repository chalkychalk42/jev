"""Outcome joins exclude unapplied choices, other clients, gaps and incomplete tails."""
import json
from dataclasses import replace

from jev.learn.dataset import build, build_from_rows
from jev.learn.episode import DecisionRow, Recorder, TickRow, read
from jev.learn.grade import grade_run
from jev.world.state_v1 import ArmedBy, Char, GuidePos, Sense, State, Vitals


def corpus(tmp_path, *, duration=65, failure=False, gap=False, death=False):
    rec = Recorder(tmp_path)
    decision = DecisionRow(rec.run_id, "d", 1, 0, "c", "k", ArmedBy.POLICY,
                           "scripted", "advance", "ACCEPT_QUEST", verifier_verdict="ok")
    rec.decision(decision)
    rec.decision(replace(decision, decision_id="rejected", status="rejected"))
    rec.decision(replace(decision, decision_id="unapplied"))
    for t in range(duration + 1):
        if gap and 10 <= t <= 20:
            continue
        state = State(t=t, client_id="c", char=Char(level=1, xp_pct=0),
                      vitals=Vitals(dead=death and t >= 15, ghost=False),
                      guide=GuidePos(graph_id="g", step_id="after" if t >= 10 else "before"),
                      sense=Sense(addon_ok=True))
        rec.tick(TickRow(rec.run_id, t + 1, t, "c", state.model_dump(mode="json"), "k",
                         "ACCEPT_QUEST", "advance", ArmedBy.POLICY, decision_id="d",
                         tracker_event=("fail" if failure else "advance") if t == 10 else "none"))
    rec.close()
    return rec.dir


def test_runtime_decisions_become_graded_examples_without_teacher(tmp_path):
    directory = corpus(tmp_path)
    report = grade_run(directory)
    assert report.graded == report.good == 1
    assert report.unapplied == 2
    dataset = build([directory])
    assert len(dataset) == 1 and dataset.examples[0].decision_id == "d"
    before = (directory / "grades.jsonl").read_bytes()
    grade_run(directory)
    assert (directory / "grades.jsonl").read_bytes() == before


def test_short_live_tail_stays_pending_and_closed_tail_is_abandoned(tmp_path):
    directory = corpus(tmp_path, duration=15)
    assert grade_run(directory).pending == 1
    assert read(directory / "grades.jsonl") == []
    report = grade_run(directory, closed=True)
    assert report.incomplete == 1 and report.good == 0
    assert read(directory / "grades.jsonl")[0]["outcome"] == "abandoned"


def test_fail_edges_do_not_earn_step_completion_reward(tmp_path):
    directory = corpus(tmp_path, failure=True)
    assert grade_run(directory).good == 0
    assert read(directory / "grades.jsonl")[0]["step_advanced"] is False


def test_observation_gap_and_death_do_not_produce_good_labels(tmp_path):
    assert grade_run(corpus(tmp_path / "gap", gap=True)).good == 0
    assert grade_run(corpus(tmp_path / "dead", death=True)).good == 0


def test_progress_does_not_excuse_a_long_stuck_interval(tmp_path):
    directory = corpus(tmp_path)
    ticks = read(directory / "ticks.jsonl")
    for tick in ticks:
        tick["state"]["control"]["s1_mode"] = "stuck" if 15 <= tick["t"] < 40 else "idle"
        # Keep reward positive even after the stuck penalty to test the evidence gate.
        tick["state"]["char"]["xp_pct"] = min(0.9, tick["t"] / 60)
    (directory / "ticks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in ticks))
    assert grade_run(directory).good == 0
    result = read(directory / "grades.jsonl")[0]
    assert result["step_advanced"] and result["reward"] > 0


def test_another_clients_progress_cannot_supply_the_outcome_window(tmp_path):
    directory = corpus(tmp_path, duration=15)
    rows = read(directory / "ticks.jsonl")
    extra = dict(rows[-1], client_id="other", t=100, tick_id=999)
    extra["state"] = dict(extra["state"], client_id="other", t=100)
    with (directory / "ticks.jsonl").open("a") as handle:
        handle.write(json.dumps(extra) + "\n")
    assert grade_run(directory).pending == 1


def test_grades_join_by_run_as_well_as_decision_id(tmp_path):
    first, second = corpus(tmp_path / "a"), corpus(tmp_path / "b", failure=True)
    grade_run(first)
    grade_run(second)
    dataset = build([first, second])
    assert len(dataset) == 1
    assert dataset.examples[0].run_id == first.name


def test_rejected_decision_cannot_train_even_with_an_incorrect_good_grade(tmp_path):
    directory = corpus(tmp_path)
    grade_run(directory)
    ticks = read(directory / "ticks.jsonl")
    decisions = read(directory / "decisions.jsonl")
    grades = read(directory / "grades.jsonl")
    forged = dict(grades[0], decision_id="rejected")
    dataset = build_from_rows(ticks, decisions, [*grades, forged])
    assert len(dataset) == 1


def test_simulated_and_replayed_rows_cannot_enter_live_training_by_default(tmp_path):
    directory = corpus(tmp_path)
    grade_run(directory)
    ticks = read(directory / "ticks.jsonl")
    decisions = read(directory / "decisions.jsonl")
    grades = read(directory / "grades.jsonl")
    for source in ("synthetic", "replay"):
        for tick in ticks:
            tick["state"]["sense"]["source"] = source
        assert len(build_from_rows(ticks, decisions, grades)) == 0
        assert len(build_from_rows(ticks, decisions, grades, include_synthetic=True)) == 1


def test_failed_execution_is_not_credited_for_later_success(tmp_path):
    directory = corpus(tmp_path)
    (directory / "skills.jsonl").write_text(json.dumps({
        "run_id": directory.name, "decision_id": "d", "outcome": "aborted",
    }) + "\n")
    assert grade_run(directory).good == 0


def test_new_grades_are_not_hidden_by_an_older_parquet_copy(tmp_path):
    from jev.learn.parquet import convert_run
    directory = corpus(tmp_path, duration=15)
    grade_run(directory, closed=True)
    convert_run(directory)
    # Complete the original observation stream with genuine fixture observations.
    full = corpus(tmp_path / "full")
    extra = read(full / "ticks.jsonl")[16:]
    with (directory / "ticks.jsonl").open("a") as handle:
        for row in extra:
            row["run_id"] = directory.name
            handle.write(json.dumps(row) + "\n")
    assert grade_run(directory, closed=True).good == 1
    assert len(build([directory])) == 1


def test_a_skipped_step_is_not_a_success_label(tmp_path):
    directory = corpus(tmp_path)
    rows = read(directory / "ticks.jsonl")
    for row in rows:
        if row["tracker_event"] == "advance":
            row["tracker_event"] = "rejoin_or_skip"
    (directory / "ticks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert grade_run(directory).good == 0


def test_a_sustained_blind_interval_is_not_observed_survival(tmp_path):
    directory = corpus(tmp_path)
    rows = read(directory / "ticks.jsonl")
    for row in rows:
        if 10 <= row["t"] <= 20:
            row["state"]["sense"] = {"addon_ok": False, "vision_conf": 0}
    (directory / "ticks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert grade_run(directory).good == 0
