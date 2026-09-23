"""Facing is turning until the selected unit's own plate is centred, measured each pulse.

The world here is a heading and a bearing. Turn keys move the heading at the measured
134 deg/s, and each look paints a real bright nameplate into a real frame, so the actual
plate detector decides where the unit is. Nothing here clicks or walks.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from jev.clients.targeting import (
    FACE_MAX_TURNS,
    FACE_SEARCH_MAX_S,
    FACE_SEARCH_STEP_S,
    FACE_TOLERANCE,
    FaceCode,
    HoverCode,
    HoverResult,
    PaintCode,
    PaintResult,
    Targeting,
    TargetView,
)

WIDTH, HEIGHT = 1600, 900
TURN_DEG_S = 134.0
YELLOW = (230, 200, 10)


def radio(**changes):
    return {"seq": 1, "target.has": True, "target.name_id": 2864, "target.hp": 1.0,
            "target.reaction": 4, "vitals.dead": False, "vitals.ghost": False,
            "ui.modal": False, **changes}


class World:
    """One unit at a bearing; `spread` compresses offsets as a unit beside the character does."""

    def __init__(self, bearing, *, spread=1.0, visible=True, extra_plates=(), values=None):
        self.heading = 0.0
        self.bearing = bearing
        self.spread = spread
        self.visible = visible
        self.extra_plates = list(extra_plates)
        self.values = values or radio()
        self.holds = []
        self.refuse = False

    # -- the HID the facing loop drives ------------------------------------------------
    TURN_LEFT, TURN_RIGHT = "a", "d"
    checkpoint = None

    def hold(self, key, seconds, **_):
        self.holds.append((key, seconds))
        if self.refuse:
            return False
        self.heading += (1 if key == "d" else -1) * TURN_DEG_S * seconds
        return True

    # -- what the camera shows ---------------------------------------------------------
    def offset(self):
        angle = (self.bearing - self.heading + 180.0) % 360.0 - 180.0
        if abs(angle) >= 90:
            return None                           # behind the camera: no plate drawn
        # A far unit moves across the screen almost linearly with the turn; a unit
        # beside the character barely moves at first (spread < 1).
        return 0.5 * math.sin(math.radians(angle)) * self.spread / math.sin(math.radians(50))

    def frame(self):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        offset = self.offset()
        if self.visible and offset is not None and abs(offset) < 0.45:
            self._plate(frame, WIDTH / 2 + offset * WIDTH)
        for cx in self.extra_plates:
            self._plate(frame, cx)
        return frame

    @staticmethod
    def _plate(frame, cx):
        left = round(cx - 73)
        frame[380:387, max(0, left):left + 147] = YELLOW


def targeting_for(world, monkeypatch, *, hover=None):
    targeting = Targeting(world, lambda: world.values)

    def view():
        return TargetView(world.frame(), world.values, 0.0, 0.0)

    monkeypatch.setattr(targeting, "_view", view)
    monkeypatch.setattr(targeting, "wait_for_paint",
                        lambda: PaintResult(PaintCode.FRESH, None, world.values, "fresh"))
    probes = []

    def probe(point):
        probes.append(point)
        code = hover(point) if hover else HoverCode.GROUND
        return HoverResult(code, point, world.values, world.values, str(code))

    monkeypatch.setattr(targeting, "probe", probe)
    targeting.probes = probes
    return targeting


def test_a_centred_plate_is_already_faced_and_nothing_is_pressed(monkeypatch):
    world = World(bearing=1.0)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.faced and result.turns == 0
    assert world.holds == []


@pytest.mark.parametrize("bearing", [40.0, -40.0, 20.0])
def test_a_far_unit_is_faced_by_turning_toward_its_plate(monkeypatch, bearing):
    world = World(bearing=bearing)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.faced, result.detail
    assert abs(result.offset) <= FACE_TOLERANCE
    first = world.holds[0][0]
    assert first == ("d" if bearing > 0 else "a"), "turned away from the unit"
    assert result.turns <= 4


def test_a_unit_whose_plate_is_off_screen_is_searched_for_then_faced(monkeypatch):
    """No plate means no direction: the search turns one way until the plate appears."""
    world = World(bearing=75.0)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.faced, result.detail
    assert world.holds[0] == ("a", FACE_SEARCH_STEP_S)


def test_a_unit_beside_the_character_is_faced_by_the_measured_turn_rate(monkeypatch):
    """Frames 99-104: the wolf two yards away at the character's side barely moves on
    screen per degree of turn. Proportional pulses alone would crawl; the loop measures
    what each pulse did and uses that rate for the next."""
    world = World(bearing=-80.0, spread=0.25)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.faced, result.detail
    assert result.turns <= FACE_MAX_TURNS
    assert abs(((world.bearing - world.heading + 180) % 360) - 180) < 20


def test_a_selected_unit_behind_the_camera_is_found_by_a_bounded_search(monkeypatch):
    world = World(bearing=-150.0)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.faced, result.detail
    assert world.holds[0] == ("a", FACE_SEARCH_STEP_S)


def test_a_unit_with_no_plate_anywhere_is_not_visible_after_one_search_turn(monkeypatch):
    world = World(bearing=10.0, visible=False)
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.code is FaceCode.NOT_VISIBLE
    assert sum(seconds for _key, seconds in world.holds) <= FACE_SEARCH_MAX_S + FACE_SEARCH_STEP_S
    assert all(key == "a" for key, _ in world.holds), "searched back and forth"


def test_two_bright_plates_are_settled_by_hover_ownership_not_position(monkeypatch):
    world = World(bearing=30.0, extra_plates=[300.0])
    own = []

    def hover(point):
        own.append(point)
        # The fixed plate at x=300 belongs to another unit; the moving one is the target.
        return HoverCode.OTHER if abs(point[0] - 300) < 20 else HoverCode.MATCH

    result = targeting_for(world, monkeypatch, hover=hover).face_selected()
    assert result.faced, result.detail
    assert own, "chose between two plates without asking the client"


def test_two_bright_plates_that_hover_cannot_settle_are_ambiguous(monkeypatch):
    world = World(bearing=30.0, extra_plates=[300.0])
    targeting = targeting_for(world, monkeypatch, hover=lambda _p: HoverCode.OTHER)
    result = targeting.face_selected()
    assert result.code is FaceCode.AMBIGUOUS
    assert world.holds == [], "turned toward a plate nothing proved was the target"


def test_a_refused_turn_stops_facing(monkeypatch):
    world = World(bearing=40.0)
    world.refuse = True
    result = targeting_for(world, monkeypatch).face_selected()
    assert result.code is FaceCode.REFUSED
    assert len(world.holds) == 1


@pytest.mark.parametrize(("changes", "code"), [
    ({"target.has": False}, FaceCode.NO_TARGET),
    ({"target.name_id": 999}, FaceCode.WRONG_TARGET),
    ({"target.hp": 0.0}, FaceCode.WRONG_KIND),
    ({"ui.modal": True}, FaceCode.INTERRUPTED),
])
def test_facing_needs_the_expected_living_selection(monkeypatch, changes, code):
    world = World(bearing=40.0, values=radio(**changes))
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864)
    assert result.code is code
    assert world.holds == []


def test_turns_are_bounded(monkeypatch):
    world = World(bearing=40.0)
    result = targeting_for(world, monkeypatch).face_selected(max_turns=0)
    assert result.code is FaceCode.UNSETTLED
    assert world.holds == []
