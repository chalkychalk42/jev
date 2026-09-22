"""A close request must be delivered and followed by observed closure."""

from types import SimpleNamespace

import pytest

from jev.clients.windows import CloseCode, close_observed


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.windows.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.windows.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def close(samples, *, delivered=True, settle_s=0, checkpoint=None):
    sequence = iter(samples)
    last = [None]
    events = []

    def read():
        last[0] = next(sequence, last[0])
        events.append("read")
        return last[0]

    def tap(key):
        events.append(key)
        return delivered

    result = close_observed(SimpleNamespace(tap=tap, checkpoint=checkpoint), read,
                            ("ui.loot",), settle_s=settle_s)
    return result, events


def state(seq, opened=False, **other):
    return {"seq": seq, "ui.loot": opened, "ui.modal": False, **other}


def test_an_observed_closed_window_receives_no_escape():
    result, events = close([state(1)])
    assert result.code is CloseCode.CLOSED
    assert events == ["read"]


@pytest.mark.parametrize("sample", [None, {}, {"ui.loot": None}])
def test_unknown_window_visibility_does_not_become_closed(sample):
    result, events = close([sample])
    assert result.code is CloseCode.BLIND
    assert events == ["read"]


def test_auto_loot_can_close_itself_before_escape():
    result, events = close([state(1, True), state(2)], settle_s=0.4)
    assert result.code is CloseCode.CLOSED
    assert events == ["read", "read"]


def test_refused_escape_never_claims_closed():
    result, events = close([state(1, True)], delivered=False)
    assert result.code is CloseCode.REFUSED
    assert events == ["read", "esc"]


def test_new_paint_after_escape_confirms_closed():
    result, events = close([state(1, True), state(2, True), state(3)])
    assert result.code is CloseCode.CLOSED
    assert result.values == state(3)
    assert events == ["read", "esc", "read", "read"]


def test_closed_arrival_paint_is_not_fresh_post_action_confirmation():
    result, events = close([state(1, True), state(2)])
    assert result.code is CloseCode.BLIND
    assert events.count("esc") == 1
    assert "no new paint" in result.detail


def test_delivered_escape_with_window_still_open_is_a_failure():
    result, events = close([state(1, True), state(2, True), state(3, True)])
    assert result.code is CloseCode.NOT_CLOSED
    assert events.count("esc") == 1


def test_escape_menu_replacing_loot_is_still_a_blocking_window():
    result, _ = close([state(1, True), state(2), state(3, **{"ui.modal": True})])
    assert result.code is CloseCode.NOT_CLOSED
    assert "modal" in result.detail


def test_lost_radio_after_escape_is_unconfirmed():
    result, _ = close([state(1, True), None])
    assert result.code is CloseCode.BLIND


def test_closing_one_requested_window_does_not_hide_another():
    values = iter([
        {"ui.vendor": True, "ui.quest_frame": False, "seq": 1},
        {"ui.vendor": False, "ui.quest_frame": False, "seq": 2},
        {"ui.vendor": False, "ui.quest_frame": True, "seq": 3},
    ])
    result = close_observed(SimpleNamespace(tap=lambda _: True), lambda: next(values),
                            ("ui.vendor", "ui.quest_frame"))
    assert result.code is CloseCode.NOT_CLOSED


def test_cancellation_during_closure_confirmation_propagates(clock):
    def checkpoint():
        if clock[0] > 0:
            raise InterruptedError("stop requested")

    with pytest.raises(InterruptedError, match="stop requested"):
        close([state(1, True), state(2, True)], checkpoint=checkpoint)
