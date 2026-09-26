"""The flight recorder."""

from __future__ import annotations

import pathlib

from jev.coach.situation import situation_key
from jev.learn.episode import DecisionRow, Recorder, TickRow, read
from jev.world.state_v1 import ArmedBy, State


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
    """The shadow columns stay in the schema, as abstentions since V224: one corpus schema."""
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


def test_a_skill_duration_uses_one_clock():
    """A first live row claimed a skill took fifty-six years: the caller passed
    `time.monotonic()` and the journal subtracted it from `time.time()`. A corpus keeps
    a number like that forever."""
    import tempfile
    import time as _t

    from jev.learn.episode import Recorder, SkillOutcome, read
    from jev.run.journal import Journal

    with tempfile.TemporaryDirectory() as d:
        j = Journal(Recorder(root=d), client_id="t")
        j.skill("FIGHT", SkillOutcome.SUCCEEDED, started_at=_t.monotonic() - 2.0)
        j.close()
        row = read(j.recorder.dir / "skills.jsonl")[0]
        assert 1.5 < row["duration_s"] < 10.0, row["duration_s"]
        assert row["t"] > 1_600_000_000, "t should stay wall-clock for joining"
