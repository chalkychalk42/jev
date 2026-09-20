"""The flight recorder, and the rule that a wrong teacher call costs nothing."""

from __future__ import annotations

import pathlib

from jev.coach.situation import situation_key
from jev.learn.episode import (
    DecisionRow,
    Outcome,
    Recorder,
    TickRow,
    grade,
    level_progress,
    read,
    reward,
)
from jev.world.state_v1 import ArmedBy, Char, State, Vitals


def _tick(run: str, i: int, s: State, **kw) -> TickRow:
    return TickRow(
        run_id=run, tick_id=i, t=s.t, client_id="c01",
        state=s.model_dump(mode="json"), situation_key=situation_key(s),
        armed_skill=kw.pop("armed_skill", "GRIND_UNTIL"),
        armed_intent=kw.pop("armed_intent", "grind_rib"),
        armed_by=kw.pop("armed_by", ArmedBy.POLICY), **kw,
    )


def _decision(run: str, **kw) -> DecisionRow:
    return DecisionRow(
        run_id=run, decision_id="d1", tick_id=1, t=0.0, client_id="c01",
        situation_key="k", author=ArmedBy.TEACHER, model="claude-sub",
        intent="grind_rib", skill="GRIND_UNTIL", **kw,
    )


def test_level_progress_is_monotone_across_a_ding():
    before = State(t=0, client_id="c", char=Char(level=11, xp_pct=0.98))
    after = State(t=1, client_id="c", char=Char(level=12, xp_pct=0.02))
    assert level_progress(after) > level_progress(before), "xp_pct alone resets at a ding"


def test_step_advance_leads_the_reward():
    """Rewarding xp alone teaches grinding in place; the step has to lead."""
    advanced = reward(step_advanced=True, level_progress_delta=0.0, died=False,
                      stuck_s=0, off_route_s=0)
    just_xp = reward(step_advanced=False, level_progress_delta=0.3, died=False,
                     stuck_s=0, off_route_s=0)
    assert advanced > just_xp


def test_standing_still_is_not_rewarded():
    """A policy that never moves never dies. It must not score for that."""
    assert reward(step_advanced=False, level_progress_delta=0.0, died=False,
                  stuck_s=0, off_route_s=0) == 0.0


def test_a_skippable_step_scores_nothing_either_way():
    assert reward(step_advanced=True, level_progress_delta=5.0, died=True,
                  stuck_s=99, off_route_s=99, skippable=True) == 0.0


def test_a_good_window_becomes_a_training_example(state: State):
    run = "r1"
    window = [
        _tick(run, 1, state),
        _tick(run, 2, state.model_copy(update={
            "t": 60.0, "guide": state.guide.model_copy(update={"step_id": "s2"})})),
    ]
    g = grade(_decision(run), window)
    assert g.outcome is Outcome.ADVANCED
    assert g.step_advanced and g.good


def test_a_death_is_not_a_training_example(state: State):
    run = "r1"
    dead = state.model_copy(update={"t": 60.0, "vitals": Vitals(hp=0.0, dead=True)})
    g = grade(_decision(run), [_tick(run, 1, state), _tick(run, 2, dead)])
    assert g.outcome is Outcome.DIED
    assert g.died and not g.good


def test_a_run_that_crashed_is_not_evidence_either_way(state: State):
    """Absence of an outcome is not a bad outcome."""
    run = "r1"
    window = [_tick(run, 1, state), _tick(run, 2, state.model_copy(update={"t": 12.0}))]
    g = grade(_decision(run), window, run_ended=True)
    assert g.outcome is Outcome.ABANDONED
    assert not g.good


def test_a_call_that_never_completed_is_not_a_training_example(state: State):
    """A timeout is the transport failing, not the model being wrong about the game."""
    run = "r1"
    window = [
        _tick(run, 1, state),
        _tick(run, 2, state.model_copy(update={
            "t": 60.0, "guide": state.guide.model_copy(update={"step_id": "s2"})})),
    ]
    g = grade(_decision(run, status="timeout"), window)
    assert g.step_advanced, "the world still moved"
    assert not g.good, "but it is not evidence about a decision that never arrived"


def test_off_route_time_is_measured_not_counted(state: State):
    run = "r1"
    off = state.guide.model_copy(update={"on_route": False})
    window = [
        _tick(run, 1, state.model_copy(update={"t": 0.0, "guide": off})),
        _tick(run, 2, state.model_copy(update={"t": 40.0, "guide": off})),
        _tick(run, 3, state.model_copy(update={"t": 60.0})),
    ]
    g = grade(_decision(run), window)
    assert g.off_route_s == 60.0


def test_the_recorder_survives_a_truncated_tail(tmp_path: pathlib.Path, state: State):
    """The normal way a run ends is a crash mid-write. One lost tick must not cost the run."""
    with Recorder(root=tmp_path) as rec:
        for _ in range(3):
            rec.tick(_tick(rec.run_id, rec.next_tick_id(), state))
        path = rec.dir / "ticks.jsonl"

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"run_id": "partial", "tick_i')

    rows = read(path)
    assert len(rows) == 3, "complete rows survive; the torn one is dropped"


def test_armed_by_is_required_on_every_tick():
    """A corpus you cannot attribute is not a training set, however large."""
    import inspect

    sig = inspect.signature(TickRow.__init__)
    assert sig.parameters["armed_by"].default is inspect.Parameter.empty


def test_shadow_prediction_exists_from_the_first_row(state: State):
    """Gate C's agreement number is only measurable if it was measured all along."""
    row = _tick("r", 1, state, shadow_intent="advance", shadow_confidence=0.8)
    assert row.shadow_intent == "advance"
    assert "shadow_intent" in {f.name for f in __import__("dataclasses").fields(TickRow)}


def test_a_decision_records_the_artifacts_it_produced():
    """V11 makes durable artifacts the teacher's preferred output. If the decision stream
    cannot hold them, the ratio we optimise for is invisible in the corpus — and a ratio
    nobody can compute is a ratio nobody improves."""
    row = _decision("r", artifacts=[
        {"kind": "skill_draft", "target": "VENDOR_REPAIR", "payload": {}},
        {"kind": "on_fail_edge", "target": "step_12", "payload": {"goto": "rib"}},
    ])
    assert len(row.artifacts) == 2
    assert row.artifacts[0]["kind"] == "skill_draft"


def test_a_decision_with_no_artifacts_is_the_default_not_an_error():
    assert _decision("r").artifacts == []
