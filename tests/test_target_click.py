"""A hover match authorizes no button until current geometry and ownership agree."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from jev.clients.targeting import ClickCode, Targeting, TargetView
from jev.perceive import units


def radio(seq, **changes):
    return {
        "seq": seq, "target.has": True, "target.name_id": 123, "target.hp": 1.0,
        "vitals.dead": False, "vitals.ghost": False, "ui.modal": False,
        "cursor.has": True, "cursor.is_target": True, "cursor.world": True,
        "cursor.name_id": 123, "cursor.dead": False, **changes,
    }


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.targeting.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.targeting.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


class FakeHid:
    def __init__(self):
        self.moves = []
        self.buttons = []
        self.position = None
        self.positions = []
        self.landing_override = None
        self.landing_offset = (0, 0)
        self.move_delivered = True
        self.button_delivered = True
        self.checkpoint = None

    def move_to(self, x, y):
        self.moves.append((x, y))
        self.position = self.landing_override or (
            x + self.landing_offset[0], y + self.landing_offset[1],
        )
        return self.move_delivered

    def cursor_position(self):
        if self.positions:
            return self.positions.pop(0)
        return self.position

    def click(self, *args, **kwargs):
        if self.checkpoint is not None:
            self.checkpoint()
        self.buttons.append((args, kwargs))
        return self.button_delivered


class ClickHarness:
    def __init__(self, monkeypatch, clock):
        self.clock = clock
        self.hid = FakeHid()
        self.frames = [np.zeros((900, 1600, 3), dtype=np.uint8)]
        self.observations = [radio(1), radio(4), radio(5)]
        self.samples = [radio(1), radio(2), radio(3)]
        self.view_age = 0.0
        self.revalidated = []
        self.captured = []
        self.on_revalidate = None
        ring = units.Ring(800.0, 500.0, 60, 20, units.RingColour.YELLOW, 200,
                          bounds=(770, 490, 830, 510))
        plate = units.Plate(800.0, 405.0, 146, units.RingColour.YELLOW, h=10,
                            bounds=(727, 400, 873, 410))
        self.proposal = units.Sighting(ring, plate, (800, 450))
        self.proposals = [self.proposal]
        self.corpse_points = [(800, 495)]
        self.accept_geometry = True
        self.targeting = Targeting(self.hid, self.read, wait_s=0.2, poll_s=0.05)
        monkeypatch.setattr(self.targeting, "_view", self.view)
        self.original_geometry = {name: getattr(units, name) for name in (
            "candidates", "corpse_probe_points", "revalidate",
        )}
        monkeypatch.setattr(units, "candidates", lambda frame, **kw: tuple(self.proposals))
        monkeypatch.setattr(units, "corpse_probe_points",
                            lambda frame, anchor=None, **kw: list(self.corpse_points))
        monkeypatch.setattr(units, "revalidate", self.revalidate)

    @staticmethod
    def take(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def read(self):
        return self.take(self.samples)

    def view(self):
        self.targeting._checkpoint()
        values, frame = self.take(self.observations), self.take(self.frames)
        self.captured.append((frame, values))
        return TargetView(frame, values, 1000.0 + self.clock[0],
                          self.clock[0] - self.view_age,
                          "none" if values is not None else "missing_frame")

    def revalidate(self, frame, point, **kwargs):
        self.revalidated.append((frame, point, False))
        if self.on_revalidate is not None:
            self.on_revalidate()
        proposal = self.proposal
        return proposal if self.accept_geometry and proposal.admits(point) else None

    def run(self, **kwargs):
        return self.targeting.click_selected(**{"max_probes": 1, **kwargs})


@pytest.fixture
def harness(monkeypatch, clock):
    return ClickHarness(monkeypatch, clock)


def test_verified_living_point_delivers_only_the_button_at_the_actual_cursor(harness):
    harness.targeting.window_origin = (25, 40)
    result = harness.run(expected_name_id=123)
    assert result.code is ClickCode.CLICKED and result.delivered
    assert result.point == (825, 490)
    assert result.attempts == 1
    assert harness.hid.moves == [(825, 490)]
    assert harness.hid.buttons == [((), {"right": True})]
    assert harness.revalidated[0][1] == (800, 450)
    assert "effect unconfirmed" in result.detail


def test_missing_proposals_never_move_or_click(harness):
    harness.proposals = []
    result = harness.run()
    assert result.code is ClickCode.NOT_VISIBLE
    assert result.attempts == 0
    assert harness.hid.moves == harness.hid.buttons == []


def test_selected_nameplate_hover_is_rejected_by_current_real_geometry(harness, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "live-dermot-targeted.npz"
    with np.load(fixture, allow_pickle=False) as archive:
        harness.frames = [archive["frame"]]
    for name, implementation in harness.original_geometry.items():
        monkeypatch.setattr(units, name, implementation)
    # Ownership can match a native nameplate. The physical endpoint is visibly on
    # Dermot's actual health-bar surface, and must not become a body click.
    harness.hid.landing_override = (1000, 398)
    result = harness.run()
    assert result.code is ClickCode.STALE
    assert harness.hid.moves
    assert harness.hid.buttons == []


@pytest.mark.parametrize("changes", [
    {"cursor.is_target": False},  # The other unit has the same name hash.
    {"cursor.world": False},
    {"cursor.has": False, "cursor.is_target": False},
    {"cursor.has": None},
    {"cursor.is_target": None},
    {"cursor.world": None},
])
def test_nonmatching_or_unknown_hover_cannot_click(harness, changes):
    harness.samples[-1] = radio(3, **changes)
    result = harness.run()
    assert result.code is ClickCode.UNKNOWN
    assert harness.hid.buttons == []
    assert harness.revalidated == []


@pytest.mark.parametrize("changes,expected", [
    ({"target.has": False}, ClickCode.NO_TARGET),
    ({"target.name_id": 456}, ClickCode.WRONG_TARGET),
    ({"target.hp": 0.0}, ClickCode.WRONG_KIND),
    ({"vitals.dead": True}, ClickCode.INTERRUPTED),
    ({"vitals.ghost": True}, ClickCode.INTERRUPTED),
    ({"ui.modal": True}, ClickCode.INTERRUPTED),
])
def test_ineligible_initial_observation_never_moves(harness, changes, expected):
    harness.observations[0] = radio(1, **changes)
    result = harness.run(expected_name_id=123)
    assert result.code is expected
    assert harness.hid.moves == harness.hid.buttons == []


def test_living_target_cannot_be_looted(harness):
    assert harness.targeting.click_corpse().code is ClickCode.WRONG_KIND
    assert harness.hid.moves == harness.hid.buttons == []


def test_the_living_click_refuses_corpses_outright(harness):
    with pytest.raises(ValueError, match="click_corpse"):
        harness.targeting.click_selected(kind="corpse")


def test_dead_mouseover_cannot_authorize_a_living_action(harness):
    harness.observations[1] = radio(4, **{"cursor.dead": True})
    assert harness.run().code is ClickCode.WRONG_KIND
    assert harness.hid.buttons == []


def test_a_corpse_is_clicked_where_a_fresh_hover_reports_the_selected_unit_dead(harness):
    """A dead unit has no nameplate, so a dead selected-unit hover can only be its body."""
    harness.observations = [radio(seq, **{"target.hp": 0.0, "cursor.dead": True})
                            for seq in (1, 4, 5)]
    harness.samples = [radio(seq, **{"target.hp": 0.0, "cursor.dead": True})
                       for seq in (1, 2, 3)]
    harness.targeting.window_origin = (25, 40)
    result = harness.targeting.click_corpse()
    assert result.code is ClickCode.CLICKED
    assert result.point == (825, 535)
    assert harness.revalidated == [], "a dead hover needs no living-body geometry"
    assert harness.hid.moves == [(825, 535)]
    assert harness.hid.buttons == [((), {"right": True})]


def test_live_mouseover_cannot_authorize_looting_despite_zero_target_hp(harness):
    harness.observations = [radio(seq, **{"target.hp": 0.0}) for seq in (1, 4, 5)]
    harness.samples = [radio(seq, **{"target.hp": 0.0}) for seq in (1, 2, 3)]
    result = harness.targeting.click_corpse()
    assert result.code is ClickCode.WRONG_KIND
    assert harness.hid.buttons == []


def test_the_corpse_search_moves_on_from_ground_and_is_bounded(harness):
    harness.observations = [radio(1, **{"target.hp": 0.0})]
    ground = {"target.hp": 0.0, "cursor.has": False, "cursor.is_target": False}
    harness.samples = [radio(seq, **ground) for seq in range(1, 40)]
    harness.corpse_points = [(700 + 10 * i, 500) for i in range(20)]
    result = harness.targeting.click_corpse(max_probes=5)
    assert result.code is ClickCode.NOT_VISIBLE
    assert result.attempts == 20, "every proposed point is one hover"
    assert harness.hid.buttons == []


def test_matching_hover_with_moved_body_geometry_cannot_click(harness):
    harness.accept_geometry = False
    result = harness.run()
    assert result.code is ClickCode.STALE
    assert harness.hid.moves == [(800, 450)]
    assert harness.hid.buttons == []


@pytest.mark.parametrize("changes,expected", [
    ({"cursor.is_target": False}, ClickCode.UNKNOWN),
    ({"cursor.dead": None}, ClickCode.UNKNOWN),
    ({"target.name_id": 456}, ClickCode.WRONG_TARGET),
    ({"target.has": False}, ClickCode.NO_TARGET),
    ({"ui.modal": True}, ClickCode.INTERRUPTED),
])
def test_target_and_ownership_are_rechecked_after_hover(harness, changes, expected):
    harness.observations[1] = radio(4, **changes)
    assert harness.run().code is expected
    assert harness.hid.buttons == []


def test_prearrival_frame_cannot_reauthorize_a_fresh_hover(harness):
    harness.observations[1] = radio(1)
    assert harness.run().code is ClickCode.STALE
    assert harness.hid.buttons == []


@pytest.mark.parametrize("paint_sequences,final_sequence", [((1, 2, 3), 3), ((253, 254, 255), 0)])
def test_same_hover_paint_and_sequence_wrap_remain_usable(harness, paint_sequences, final_sequence):
    harness.samples = [radio(seq) for seq in paint_sequences]
    harness.observations = [radio(paint_sequences[0]), radio(final_sequence), radio(1)]
    assert harness.run().code is ClickCode.CLICKED
    assert harness.hid.buttons == [((), {"right": True})]


def test_current_geometry_with_unknown_paint_sequence_cannot_click(harness):
    harness.observations[1] = radio(None)
    assert harness.run().code is ClickCode.STALE
    assert harness.hid.buttons == []


def test_a_slow_current_capture_expires_before_button_delivery(harness):
    harness.view_age = 0.6
    assert harness.run(max_view_age_s=0.5).code is ClickCode.STALE
    assert harness.hid.buttons == []


def test_cursor_movement_after_geometry_check_invalidates_the_point(harness):
    harness.on_revalidate = lambda: setattr(harness.hid, "position", (950, 550))
    assert harness.run().code is ClickCode.STALE
    assert harness.hid.buttons == []


def test_cursor_movement_after_hover_cannot_borrow_ownership_for_a_new_point(harness, monkeypatch):
    probe = harness.targeting.probe

    def moved_after_hover(point):
        result = probe(point)
        harness.hid.position = (850, 470)
        return result

    monkeypatch.setattr(harness.targeting, "probe", moved_after_hover)
    assert harness.run().code is ClickCode.STALE
    assert harness.revalidated == []
    assert harness.hid.buttons == []


@pytest.mark.parametrize("offset", [(-1, 1), (1, -1)])
def test_verified_actual_landing_rounding_is_the_point_revalidated_and_clicked(harness, offset):
    harness.hid.landing_offset = offset
    result = harness.run()
    actual = (800 + offset[0], 450 + offset[1])
    assert result.code is ClickCode.CLICKED
    assert result.point == actual
    assert harness.hid.moves == [(800, 450)]
    assert harness.revalidated[0][1] == actual
    assert harness.hid.buttons == [((), {"right": True})]


def test_unavailable_physical_cursor_refuses_button_delivery(harness):
    harness.hid.positions = [None]
    assert harness.run().code is ClickCode.REFUSED
    assert harness.hid.buttons == []


def test_refused_movement_never_proceeds_to_button_delivery(harness):
    harness.hid.move_delivered = False
    assert harness.run().code is ClickCode.REFUSED
    assert harness.hid.buttons == []


def test_refused_button_is_never_reported_as_delivered(harness):
    harness.hid.button_delivered = False
    result = harness.run()
    assert result.code is ClickCode.REFUSED
    assert not result.delivered
    assert harness.hid.buttons == [((), {"right": True})]


@pytest.mark.parametrize("when", ["initial_frame", "before_hover", "after_arrival", "final_frame"])
def test_lost_observations_cannot_authorize_a_button(harness, when):
    if when == "initial_frame":
        harness.observations[0] = None
    elif when == "before_hover":
        harness.samples[0] = None
    elif when == "after_arrival":
        harness.samples[1] = None
    else:
        harness.observations[1] = None
    assert harness.run().code is ClickCode.BLIND
    assert harness.hid.buttons == []


def test_frozen_radio_times_out_without_clicking(harness, clock):
    harness.samples = [radio(1), radio(2)]
    result = harness.run()
    assert result.code is ClickCode.UNKNOWN
    assert clock[0] == pytest.approx(0.2)
    assert harness.hid.buttons == []


def test_rejected_hypothesis_can_advance_to_a_verified_proposal(harness):
    second = replace(harness.proposal, torso=(820, 450))
    harness.proposals.append(second)
    harness.samples = [radio(1), radio(2), radio(3, **{"cursor.is_target": False}),
                       radio(4), radio(5), radio(6)]
    harness.observations = [radio(1), radio(4), radio(7), radio(8)]
    result = harness.run(max_probes=2)
    assert result.code is ClickCode.CLICKED
    assert result.attempts == 2
    assert harness.hid.moves == [(800, 450), (820, 450)]
    assert harness.hid.buttons == [((), {"right": True})]


def test_probe_budget_stops_before_trying_every_possible_component(harness):
    harness.proposals.extend(replace(harness.proposal, torso=(x, 450)) for x in (820, 840))
    ground = {"cursor.has": False, "cursor.is_target": False}
    harness.samples = [radio(1), radio(2), radio(3, **ground),
                       radio(4), radio(5), radio(6, **ground)]
    result = harness.run(max_probes=2)
    assert result.code is ClickCode.UNKNOWN
    assert result.attempts == 2
    assert harness.hid.moves == [(800, 450), (820, 450)]
    assert harness.hid.buttons == []


def test_action_deadline_expiring_during_hover_prevents_button(harness, clock):
    assert harness.run(timeout_s=0.01).code is ClickCode.STALE
    assert clock[0] >= 0.01
    assert harness.hid.buttons == []


def test_cancellation_during_hover_propagates_without_button(harness, clock):
    def checkpoint():
        if clock[0] > 0:
            raise InterruptedError("stop requested")

    harness.hid.checkpoint = checkpoint
    with pytest.raises(InterruptedError, match="stop requested"):
        harness.run()
    assert harness.hid.buttons == []


def test_post_click_cancellation_retains_the_exact_pre_click_observation(harness, monkeypatch):
    retained = []
    view = harness.targeting._view

    def interrupted_view():
        if harness.hid.buttons:
            raise InterruptedError("stop after delivery")
        return view()

    monkeypatch.setattr(harness.targeting, "_view", interrupted_view)
    harness.targeting.record_frame = lambda label, frame, **kw: retained.append(
        (label, frame, kw)) or {"status": "saved"}
    with pytest.raises(InterruptedError, match="stop after delivery"):
        harness.run()
    assert harness.hid.buttons == [((), {"right": True})]
    assert len(retained) == 1 and retained[0][0] == "target-before-click"
    assert retained[0][1] is harness.captured[-1][0]
    assert retained[0][2]["captured_at"] == pytest.approx(1000.05)


def test_geometry_and_selection_are_decoded_from_the_same_real_frame():
    fixture = Path(__file__).parent / "fixtures" / "live-dermot-targeted.npz"
    with np.load(fixture, allow_pickle=False) as archive:
        frame = archive["frame"]
    hid = FakeHid()
    # An independent newer read claims the requested target; the captured geometry
    # still belongs to the merchant. Combining those observations would be incorrect.
    targeting = Targeting(hid, lambda: radio(8, **{"target.name_id": 2864}),
                          read_frame=lambda: frame)
    result = targeting.click_selected(expected_name_id=2864)
    assert result.code is ClickCode.WRONG_TARGET
    assert hid.moves == hid.buttons == []


DESELECTED = {"target.has": False, "target.name_id": None, "target.hp": None}


def _deselected_corpse_harness(harness, **hovered):
    harness.observations = [radio(1, **DESELECTED)]
    under = {**DESELECTED, "cursor.has": True, "cursor.is_target": False, "cursor.world": True,
             "cursor.dead": True, "cursor.name_id": 2864, **hovered}
    harness.samples = [radio(1, **DESELECTED), radio(2, **under), radio(3, **under)]
    return harness


def test_a_corpse_the_kill_deselected_is_found_by_its_dead_hover_and_name(harness):
    """Live, 23 Sep: the selection cleared at the kill. A dead unit has no nameplate, so a
    fresh dead hover of the killed unit's name is its body."""
    result = _deselected_corpse_harness(harness).targeting.click_corpse(expected_name_id=2864)
    assert result.code is ClickCode.CLICKED
    assert harness.hid.buttons == [((), {"right": True})]


@pytest.mark.parametrize("hovered", [{"cursor.dead": False}, {"cursor.name_id": 1648},
                                     {"cursor.has": False, "cursor.name_id": None}])
def test_a_deselected_corpse_needs_a_dead_hover_of_the_killed_name(harness, hovered):
    result = _deselected_corpse_harness(harness, **hovered).targeting.click_corpse(
        expected_name_id=2864)
    assert result.code is not ClickCode.CLICKED
    assert harness.hid.buttons == []


def test_without_a_selection_or_a_name_there_is_no_corpse_to_look_for(harness):
    result = _deselected_corpse_harness(harness).targeting.click_corpse()
    assert result.code is ClickCode.NO_TARGET
    assert harness.hid.moves == harness.hid.buttons == []


def test_a_quest_giver_whose_ring_the_character_hides_is_clicked_under_its_plate(
        harness, monkeypatch):
    """Measured 23 September: Eagan Peltskinner stood just beyond the character with his
    plate clear and his ring hidden by the character's own model, and the turn-in failed
    with "no eligible target geometry". A fresh owning hover below the bar is his body."""
    fixture = Path(__file__).parent / "fixtures" / "live-eagan-ring-hidden.npz"
    with np.load(fixture, allow_pickle=False) as archive:
        harness.frames = [archive["frame"]]
    for name, implementation in harness.original_geometry.items():
        monkeypatch.setattr(units, name, implementation)
    assert units.candidates(harness.frames[0]) == (), "the ring bracket is really missing"
    [first, *_] = units.body_candidates(harness.frames[0])
    assert first.plate.colour is units.RingColour.GREEN and first.torso[1] > first.plate.bounds[3]
    result = harness.run()
    assert result.code is ClickCode.CLICKED
    assert harness.hid.buttons == [((), {"right": True})]
    assert isinstance(result.proposal, units.BodyProposal)


def test_a_point_on_the_plate_itself_is_never_a_body_point():
    fixture = Path(__file__).parent / "fixtures" / "live-eagan-ring-hidden.npz"
    with np.load(fixture, allow_pickle=False) as archive:
        frame = archive["frame"]
    [first, *_] = units.body_candidates(frame)
    _left, _top, right, bottom = first.plate.bounds
    assert units.revalidate_body(frame, (round(first.plate.cx), bottom - 2)) is None
    assert units.revalidate_body(frame, (round(first.plate.cx), bottom + 5)) is None
    assert units.revalidate_body(frame, (right + 30, bottom + 40)) is None
    assert units.revalidate_body(frame, first.torso) is not None


def test_a_corpse_is_still_looted_when_the_client_selects_the_next_attacker_meanwhile(harness):
    """A Defias Thug was chosen for us the moment the hover landed on its dead packmate;
    three corpses in a row went unlooted (run 20260924T054447-632295)."""
    packmate = {"target.has": True, "target.name_id": 2864, "target.hp": 1.0}
    result = _deselected_corpse_harness(harness, **packmate).targeting.click_corpse(
        expected_name_id=2864)
    assert result.code is ClickCode.CLICKED
    assert harness.hid.buttons == [((), {"right": True})]


@pytest.mark.parametrize("hovered", [{"cursor.dead": False}, {"cursor.name_id": 1648}])
def test_a_retarget_mid_hover_still_needs_the_dead_unit_of_the_name(harness, hovered):
    packmate = {"target.has": True, "target.name_id": 2864, "target.hp": 1.0}
    result = _deselected_corpse_harness(harness, **packmate, **hovered).targeting.click_corpse(
        expected_name_id=2864)
    assert result.code is not ClickCode.CLICKED
    assert harness.hid.buttons == []
