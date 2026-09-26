"""NPC selection delegates body verification and reports observed windows."""

from __future__ import annotations

import inspect
from unittest.mock import Mock

import pytest
from test_fight import _Hid, _Targeting

from jev.clients.interact import (
    INTERACT_LOOKS,
    MAX_CANDIDATES,
    SETTLE_S,
    SETTLE_TRIES,
    Interact,
    Result,
)
from jev.clients.targeting import ClickCode, ClickResult, PaintCode, PaintResult
from jev.guide.coords import ZoneBounds
from jev.perceive.radio_frame import name_id
from jev.perceive.units import Plate, RingColour

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)
PLATE = Plate(cx=996, cy=422, w=145, colour=RingColour.GREEN)
SELECTED = {"target.has": True, "target.name_id": name_id("Supplier"),
            "ui.quest_frame": False, "ui.gossip": False, "ui.vendor": False, "ui.loot": False}


@pytest.fixture(autouse=True)
def no_settle_delay(monkeypatch):
    monkeypatch.setattr("jev.clients.interact.time.sleep", lambda _: None)


def _interact(values=None, frame=None):
    hid = _Hid()
    def read():
        return values
    return Interact(hid=hid, bounds=ELWYNN, read=read,
                    read_frame=lambda: frame, read_pos=lambda: (0.5, 0.5),
                    window_centre=(800, 450), targeting=_Targeting(read, hid))


def test_selection_never_uses_chat():
    code = inspect.getsource(Interact)
    assert "slash(" not in code and "type_text(" not in code
    assert not hasattr(Interact, "_target")


def test_one_observed_candidate_set_and_bounded_selections(monkeypatch):
    plates = [Plate(650 + 45 * i, 300, 140, RingColour.GREEN) for i in range(8)]
    monkeypatch.setattr("jev.clients.interact.find_plates", lambda _: list(plates))
    inter = _interact(values={**SELECTED, "target.name_id": 9}, frame=object())
    captured = []
    inter.read_frame = lambda: captured.append(True) or object()
    assert inter.open_on("Supplier") is Result.NO_TARGET
    # One candidate set per look: ahead, then after each of three quarter turns.
    assert len(captured) == INTERACT_LOOKS
    assert len(inter.hid.clicks) == MAX_CANDIDATES * INTERACT_LOOKS
    assert MAX_CANDIDATES <= 3 and INTERACT_LOOKS <= 4
    assert all(not right for _, _, right in inter.hid.clicks)
    assert inter.targeting.requests == []
    assert len(inter.hid.holds) == INTERACT_LOOKS - 1


def test_navigation_is_injected_and_failed_approach_prevents_selection():
    inter = _interact(values=SELECTED)
    inter.approach = Mock(return_value=False)
    inter._candidates = Mock()
    destination = (1.0, 2.0, 3.0)
    assert inter.open_on("Supplier", node_world=destination) is Result.APPROACH_FAILED
    inter.approach.assert_called_once_with(destination)
    inter._candidates.assert_not_called()
    assert inter.hid.clicks == []


@pytest.mark.parametrize("near", [True, False])
def test_missing_visual_evidence_never_authorizes_a_spawn_centre_click(near):
    inter = _interact(values=SELECTED, frame=None)
    point = (0.5, 0.5) if near else (0.9, 0.9)
    assert inter.open_on("Supplier", node_map=point) is Result.NOT_VISIBLE
    assert inter.clicked is None
    assert inter.hid.clicks == []
    assert inter.targeting.requests == []


def test_a_unit_behind_the_character_is_found_by_looking_round(monkeypatch):
    """Beside Marshal McBride the character faced a wall with him out of view."""
    looks = []
    monkeypatch.setattr("jev.clients.interact.find_plates",
                        lambda _: [PLATE] if len(looks) >= 2 else [])
    inter = _interact(values=SELECTED, frame=object())
    original = inter.hid.hold

    def hold(key, seconds, **kw):
        looks.append(key)
        return original(key, seconds, **kw)

    inter.hid.hold = hold
    inter.targeting.click_selected = lambda **kw: ClickResult(ClickCode.CLICKED, (996, 470), "")
    inter._window_open = lambda: Result.QUEST
    assert inter.open_on("Supplier") is Result.QUEST
    assert len(looks) == 2, "two quarter turns found him"


def test_wrong_selected_identity_costs_only_the_selection():
    inter = _interact(values={"target.has": True, "target.name_id": 9})
    assert inter._try(PLATE, name_id("Supplier")) is None
    assert inter.hid.clicks == [(996, 422, False)]
    assert inter.targeting.requests == []
    assert inter.tried == [9]


def test_confirmed_selection_delegates_body_verification_before_observing_window():
    inter = _interact(values={**SELECTED, "ui.vendor": True})
    inter.targeting.action = ClickResult(ClickCode.CLICKED, (1007, 485), "delivered", 1)
    assert inter._try(PLATE, name_id("Supplier")) is Result.VENDOR
    assert inter.hid.clicks == [(996, 422, False), (1007, 485, True)]
    assert inter.targeting.requests == [{"kind": "living", "expected_name_id": name_id("Supplier"),
                                        "plate": PLATE}]


def test_refused_selection_cannot_use_a_preexisting_matching_target():
    inter = _interact(values={**SELECTED, "ui.vendor": True})
    inter.hid.click = Mock(return_value=False)
    assert inter._try(PLATE, name_id("Supplier")) is Result.REFUSED
    assert inter.targeting.paints == 0
    assert inter.targeting.requests == []
    assert inter.tried == []


@pytest.mark.parametrize("paint_code", [PaintCode.BLIND, PaintCode.UNKNOWN])
def test_unconfirmed_selection_paint_prevents_body_action(paint_code):
    inter = _interact(values=SELECTED)
    inter.targeting.wait_for_paint = lambda: PaintResult(paint_code, SELECTED, SELECTED, "not fresh")
    assert inter._try(PLATE, name_id("Supplier")) is Result.BLIND
    assert inter.targeting.requests == []
    assert inter.hid.clicks == [(996, 422, False)]


@pytest.mark.parametrize("action_code, expected, aims", [
    (ClickCode.NOT_VISIBLE, Result.NOT_VISIBLE, 1 + SETTLE_TRIES),
    (ClickCode.STALE, Result.NOT_VISIBLE, 1 + SETTLE_TRIES),
    (ClickCode.BLIND, Result.BLIND, 1), (ClickCode.REFUSED, Result.REFUSED, 1),
    (ClickCode.NO_TARGET, Result.NO_TARGET, 1), (ClickCode.WRONG_TARGET, Result.NO_TARGET, 1),
    (ClickCode.INTERRUPTED, Result.INTERRUPTED, 1),
])
def test_targeting_refusal_stops_local_interaction_without_another_selection(
        action_code, expected, aims):
    """A refusal is final for the selection; only a body point that would not hold still
    is aimed at again, after a pause (`SETTLE_S`)."""
    inter = _interact(values=SELECTED)
    inter.targeting.action = ClickResult(action_code, None, "unconfirmed body point")
    inter._candidates = lambda: [PLATE, PLATE]
    assert inter.open_on("Supplier", node_map=(0.5, 0.5)) is expected
    assert inter.hid.clicks == [(996, 422, False)]
    assert len(inter.targeting.requests) == aims


def test_a_body_point_that_would_not_hold_still_is_aimed_at_again_once_the_view_settles(
        monkeypatch):
    """The mage's check: walking up to Khelden Bremen raised the subzone's title across his
    plate, three proposals went stale, and the training failed with him selected."""
    pauses = []
    monkeypatch.setattr("jev.clients.interact.time.sleep", pauses.append)
    inter = _interact(values={**SELECTED, "ui.trainer": True})
    answers = [ClickResult(ClickCode.STALE, (1596, 496), "point no longer has current geometry", 3),
               ClickResult(ClickCode.CLICKED, (1620, 540), "delivered", 1)]
    inter.targeting.click_selected = lambda **kw: answers.pop(0)
    assert inter._try(PLATE, name_id("Supplier")) is Result.TRAINER
    assert SETTLE_S in pauses and not answers


@pytest.mark.parametrize("field, expected", [
    ("ui.gossip", Result.GOSSIP), ("ui.quest_frame", Result.QUEST),
    ("ui.vendor", Result.VENDOR), ("ui.loot", Result.LOOT),
])
def test_each_measured_window_is_a_successful_outcome(field, expected):
    inter = _interact(values={**SELECTED, field: True})
    assert inter._try(PLATE, name_id("Supplier")) is expected
    assert expected.opened


def test_delivered_body_input_without_a_window_is_not_success():
    inter = _interact(values=SELECTED)
    assert inter._try(PLATE, name_id("Supplier")) is Result.NO_WINDOW
    assert not Result.NO_WINDOW.opened


def test_camera_refusal_prevents_approach_and_selection():
    inter = _interact(values=SELECTED)
    inter.level = lambda: False
    inter.approach = Mock()
    assert inter.open_on("Supplier", node_world=(1, 2, 3)) is Result.REFUSED
    inter.approach.assert_not_called()
    assert inter.hid.clicks == []


@pytest.mark.parametrize("phase", ["wait_for_paint", "click_selected"])
def test_cancellation_propagates_from_shared_targeting(phase):
    from jev.run.supervisor import Cancelled

    inter = _interact(values=SELECTED)
    def cancelled(**_):
        raise Cancelled("stop requested")
    setattr(inter.targeting, phase, cancelled)
    with pytest.raises(Cancelled, match="stop requested"):
        inter._try(PLATE, name_id("Supplier"))


def test_refused_close_cannot_credit_an_old_window_to_a_new_interaction():
    inter = _interact(values={**SELECTED, "ui.vendor": True})
    inter.hid.tap = Mock(return_value=False)
    inter._candidates = Mock()
    assert inter.open_on("Supplier") is Result.REFUSED
    inter._candidates.assert_not_called()
    assert inter.hid.clicks == []
    assert inter.targeting.requests == []


def test_an_old_window_remaining_open_prevents_new_selection():
    inter = _interact(values={**SELECTED, "ui.quest_frame": True})
    assert inter.open_on("Supplier") is Result.WINDOW_OPEN
    assert inter.hid.taps == ["esc"]
    assert inter.hid.clicks == []
    assert not Result.WINDOW_OPEN.opened


@pytest.mark.parametrize("values", [None, {"target.has": True}])
def test_missing_initial_window_observation_prevents_selection(values):
    inter = _interact(values=values)
    assert inter.open_on("Supplier") is Result.BLIND
    assert inter.hid.taps == inter.hid.clicks == []


def test_confirmed_closure_allows_the_requested_interaction_to_proceed():
    inter = _interact(values=SELECTED)
    readings = iter([{**SELECTED, "ui.vendor": True}, SELECTED])
    inter.read = lambda: next(readings)
    # The shared freshness boundary confirms closure using its injected reader.
    inter._candidates = Mock(return_value=[])
    assert inter.open_on("Supplier") is Result.NOT_VISIBLE
    assert inter.hid.taps == ["esc"]
    assert inter._candidates.call_count == INTERACT_LOOKS, "every look asks for candidates"
