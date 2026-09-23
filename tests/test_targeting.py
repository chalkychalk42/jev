"""Hover ownership must follow pointer arrival and never perform an interaction."""

from types import SimpleNamespace

import pytest

from jev.clients.targeting import HoverCode, PaintCode, Targeting


def radio(seq, **changes):
    return {"seq": seq, "target.has": True, "target.name_id": 123,
            "cursor.world": True, "cursor.has": True,
            "cursor.is_target": True, "cursor.name_id": 123, **changes}


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.targeting.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.targeting.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def probe(samples, *, delivered=True, checkpoint=None):
    calls = []
    remaining = iter(samples)
    last = [None]

    def read():
        last[0] = next(remaining, last[0])
        calls.append("read")
        return last[0]

    def move(x, y):
        calls.append((x, y))
        return delivered

    hid = SimpleNamespace(move_to=move, checkpoint=checkpoint)
    result = Targeting(hid, read, wait_s=0.2).probe((400, 500))
    return result, calls


def test_arrival_sample_is_not_endpoint_evidence(clock):
    result, calls = probe([radio(1), radio(2), radio(2),
                           radio(3, **{"cursor.has": False, "cursor.is_target": False})])
    assert result.code == HoverCode.GROUND
    assert result.before["seq"] == 1 and result.after["seq"] == 3
    assert calls == ["read", (400, 500), "read", "read", "read"]


@pytest.mark.parametrize("changes,code", [
    ({}, HoverCode.MATCH),
    ({"cursor.is_target": False}, HoverCode.OTHER),  # Same-name creature is not target.
    ({"cursor.has": False, "cursor.is_target": False}, HoverCode.GROUND),
    ({"cursor.world": False}, HoverCode.UI),  # Even a target portrait is not a body.
    ({"cursor.world": None}, HoverCode.UNKNOWN),
    ({"cursor.has": None}, HoverCode.UNKNOWN),
    ({"cursor.is_target": None}, HoverCode.UNKNOWN),
    ({"cursor.has": False}, HoverCode.UNKNOWN),
    ({"target.has": False}, HoverCode.NO_TARGET),
    ({"target.name_id": 999}, HoverCode.TARGET_CHANGED),
])
def test_fresh_ownership_classification(clock, changes, code):
    result, _ = probe([radio(1), radio(2), radio(3, **changes)])
    assert result.code == code


def test_stale_paint_times_out_without_claiming_match(clock):
    result, _ = probe([radio(1), radio(2)])
    assert result.code == HoverCode.UNKNOWN
    assert "no new paint" in result.detail
    assert clock[0] == pytest.approx(0.2)


def test_sequence_wrap_is_fresh(clock):
    result, _ = probe([radio(254), radio(255), radio(0)])
    assert result.code == HoverCode.MATCH


@pytest.mark.parametrize("actual", [(399, 501), (401, 499)])
def test_hover_reports_the_measured_landing_including_input_rounding(clock, actual):
    samples = iter([radio(1), radio(2), radio(3)])
    moves = []
    hid = SimpleNamespace(move_to=lambda *point: moves.append(point) or True,
                          cursor_position=lambda: actual, checkpoint=None)
    result = Targeting(hid, lambda: next(samples), wait_s=0.2).probe((400, 500))
    assert result.code is HoverCode.MATCH
    assert result.point == actual
    assert moves == [(400, 500)]


def test_pointer_drift_during_paint_wait_invalidates_hover_ownership(clock):
    point = [(400, 500)]
    samples = iter([radio(1), radio(2), radio(3)])

    def read():
        sample = next(samples)
        if sample["seq"] == 3:
            point[0] = (500, 600)
        return sample

    hid = SimpleNamespace(move_to=lambda *point: True,
                          cursor_position=lambda: point[0], checkpoint=None)
    result = Targeting(hid, read, wait_s=0.2).probe((400, 500))
    assert result.code is not HoverCode.MATCH
    assert result.point == (400, 500)


def test_old_schema_is_unknown(clock):
    samples = [{key: val for key, val in radio(seq).items() if not key.startswith("cursor.")}
               for seq in (1, 2, 3)]
    result, _ = probe(samples)
    assert result.code == HoverCode.UNKNOWN


@pytest.mark.parametrize("samples", [[None], [radio(1), None], [radio(1), radio(2), None]])
def test_missing_radio_is_blind(clock, samples):
    result, calls = probe(samples)
    assert result.code == HoverCode.BLIND
    if samples == [None]:
        assert calls == ["read"]


def test_no_target_does_not_move(clock):
    result, calls = probe([radio(1, **{"target.has": False})])
    assert result.code == HoverCode.NO_TARGET
    assert calls == ["read"]


def test_partial_movement_is_refused(clock):
    result, calls = probe([radio(1)], delivered=False)
    assert result.code == HoverCode.REFUSED
    assert calls == ["read", (400, 500)]


def test_cancellation_during_wait_propagates(clock):
    def checkpoint():
        if clock[0] > 0:
            raise InterruptedError("stop requested")

    with pytest.raises(InterruptedError, match="stop requested"):
        probe([radio(1), radio(2)], checkpoint=checkpoint)


@pytest.mark.parametrize("name", ["wait_s", "poll_s"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_wait_must_be_positive_and_finite(name, value):
    with pytest.raises(ValueError, match="positive and finite"):
        Targeting(None, lambda: None, **{name: value})


def test_post_action_freshness_does_not_claim_action_success(clock):
    old = radio(10, **{"target.has": False})
    fresh = radio(11, **{"target.has": False})
    readings = iter([old, old, fresh])
    result = Targeting(None, lambda: next(readings)).wait_for_paint()
    assert result.code is PaintCode.FRESH
    assert result.baseline is old and result.after is fresh
    assert result.after["target.has"] is False


def test_post_action_freshness_discards_every_repeat_of_arrival_paint(clock):
    arrival = radio(20)
    after = radio(21, **{"target.name_id": 999})
    readings = iter([arrival, arrival, arrival, after])
    result = Targeting(None, lambda: next(readings)).wait_for_paint()
    assert result.code is PaintCode.FRESH
    assert result.baseline["target.name_id"] == 123
    assert result.after["target.name_id"] == 999


def test_post_action_missing_sequence_is_unknown_without_guessing(clock):
    sample = {key: value for key, value in radio(1).items() if key != "seq"}
    result = Targeting(None, lambda: sample).wait_for_paint()
    assert result.code is PaintCode.UNKNOWN
    assert "no paint sequence" in result.detail


def test_an_unordered_paint_becomes_the_baseline_for_the_next_one(clock):
    """An older addon paints sequence 255 as "not available" once every 256 paints. That
    paint cannot be ordered, so the next known paint is the baseline and only a paint
    after it counts as fresh - measured live as a false `blind` on 23 September."""
    unordered = {**radio(1), "seq": None}
    first, second = radio(0), radio(1, **{"target.name_id": 999})
    readings = iter([unordered, first, first, second])
    result = Targeting(None, lambda: next(readings)).wait_for_paint()
    assert result.code is PaintCode.FRESH
    assert result.baseline is first and result.after is second


def test_the_sequence_never_paints_the_not_available_code():
    from jev.perceive.fields import FIELDS, SEQ_MODULUS

    seq = next(f for f in FIELDS if f.name == "seq")
    assert SEQ_MODULUS == seq.na == seq.span, "every painted value must be a real code"
    assert f"SEQ % {SEQ_MODULUS}" in seq.lua
