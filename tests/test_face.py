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

    def target_x(self):
        offset = self.offset()
        if self.visible and offset is not None and abs(offset) < 0.45:
            return WIDTH / 2 + offset * WIDTH
        return None

    def frame(self):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        if (x := self.target_x()) is not None:
            self._plate(frame, x)
        for cx in self.extra_plates:
            self._plate(frame, cx)
        return frame

    def hover(self, point):
        """The client's answer: the target's own plate is the selection, others are not."""
        x = self.target_x()
        if x is not None and abs(point[0] - x) < 20 and abs(point[1] - 383) < 10:
            return HoverCode.MATCH
        if any(abs(point[0] - cx) < 20 for cx in self.extra_plates):
            return HoverCode.OTHER
        return HoverCode.GROUND

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

    def probe(point, require_target=True):
        probes.append(point)
        code = (hover or world.hover)(point)
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


def test_the_selected_plate_is_proved_by_hover_then_tracked_without_more_hovers(monkeypatch):
    """A rabbit's plate measured as bright as a selected wolf's: brightness is no identity."""
    world = World(bearing=30.0, extra_plates=[300.0])
    targeting = targeting_for(world, monkeypatch)
    result = targeting.face_selected()
    assert result.faced, result.detail
    target_x = WIDTH / 2 + World(30.0).offset() * WIDTH
    assert targeting.probes[0] == (300, 383), "the other plate, nearer the centre, is asked first"
    assert len(targeting.probes) == 2 and abs(targeting.probes[1][0] - target_x) < 2, \
        "proved once, then tracked through every turn"
    assert result.turns >= 1


def test_a_hint_from_the_last_look_needs_no_hover(monkeypatch):
    from jev.perceive.units import Plate, RingColour

    world = World(bearing=20.0)
    targeting = targeting_for(world, monkeypatch)
    hint = Plate(cx=world.target_x(), cy=383.0, w=147, colour=RingColour.YELLOW)
    result = targeting.face_selected(hint=hint)
    assert result.faced and targeting.probes == []


def test_plates_that_hover_proves_are_other_units_are_never_faced(monkeypatch):
    world = World(bearing=30.0, visible=False, extra_plates=[300.0, 1300.0])
    targeting = targeting_for(world, monkeypatch)
    result = targeting.face_selected(search_s=0.3)
    assert result.code is FaceCode.NOT_VISIBLE
    assert all(key == "a" and seconds == FACE_SEARCH_STEP_S for key, seconds in world.holds), \
        "turned toward a plate that is not the target's"


def test_a_damaged_units_shrunken_bar_is_still_its_plate(monkeypatch):
    """Health fill shrinks from the right; the plate's left edge and centre stay put."""
    from jev.perceive import units

    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    frame[380:387, 727:756] = YELLOW                 # 20% of a 146 px plate from x=727
    [plate] = units.plate_candidates(frame)
    assert abs(plate.cx - (727 + units.PLATE_FULL_W_FRAC * WIDTH / 2)) < 1


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


def test_a_bar_whose_width_disagrees_with_the_target_health_is_never_hovered(monkeypatch):
    """Measured 23 September: yellow flowers pass the bar shape and ate the hover budget
    on every look. A health bar's fill is the unit's health times the plate width, so a
    flower-sized bar at full health is not the target's plate and is not worth a hover."""

    class Flowers(World):
        def frame(self):
            frame = super().frame()
            for cx in (500, 1100):                       # 30 px yellow bars, plate-shaped
                frame[380:387, cx - 15:cx + 15] = YELLOW
            return frame

    world = Flowers(bearing=1.0)
    targeting = targeting_for(world, monkeypatch)
    result = targeting.face_selected(expected_name_id=2864)
    assert result.faced
    assert all(abs(x - WIDTH / 2) < 80 for x, _ in targeting.probes), targeting.probes

    # At a fifth of its health the target's own fill is about 29 px: the full-width bar
    # is the one that disagrees, so it is not hovered, and the flower-sized ones are.
    hurt = Flowers(bearing=1.0, values=radio(**{"target.hp": 0.2}))
    hurt_targeting = targeting_for(hurt, monkeypatch)
    hurt_targeting.face_selected(expected_name_id=2864, search_s=0.0)
    assert hurt_targeting.probes and all(abs(x - WIDTH / 2) > 80 for x, _ in hurt_targeting.probes)


def test_an_open_loop_turn_toward_a_mark_is_bounded_and_goes_the_right_way(monkeypatch):
    from jev.clients.targeting import FACE_GAIN_S

    world = World(bearing=0.0)
    targeting = targeting_for(world, monkeypatch)
    assert targeting.turn_toward(0.2) is True
    assert world.holds == [("d", pytest.approx(0.2 * FACE_GAIN_S))]
    assert targeting.turn_toward(-3.0) is True                 # clamped to half a screen
    assert world.holds[-1] == ("a", pytest.approx(0.5 * FACE_GAIN_S))
    assert targeting.turn_toward(0.01) is True and len(world.holds) == 2, "a hair is not a turn"
    with pytest.raises(ValueError):
        targeting.turn_toward(float("nan"))
    world.refuse = True
    assert targeting.turn_toward(0.3) is False


def test_a_fight_stops_looking_for_its_target_to_heal(monkeypatch):
    """Session 126: three gnolls on the character, twelve seconds spent looking for the
    selected one's plate, health 94% to 22% with nothing pressed, and death."""
    world = World(bearing=150.0, values=radio(**{"vitals.hp": 0.3, "vitals.combat": True}))

    def stop(values):
        return "hurt" if values.get("vitals.combat") and values.get("vitals.hp", 1.0) < 0.4 else None

    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864, stop=stop)
    assert result.code is FaceCode.INTERRUPTED and result.detail == "hurt"
    assert world.holds == [], "no turning once health says heal"


def test_a_fights_search_is_bounded_by_the_clock_not_only_by_its_turning(monkeypatch):
    """The turning budget counted 2.8 s while the hovers proving each plate between turns
    took the rest of twelve seconds (session 126)."""
    from types import SimpleNamespace

    world = World(bearing=10.0, visible=False)
    now = [0.0]

    def monotonic():
        now[0] += 1.5                   # each look's hovers take seconds of the clock
        return now[0]

    monkeypatch.setattr("jev.clients.targeting.time", SimpleNamespace(monotonic=monotonic))
    result = targeting_for(world, monkeypatch).face_selected(expected_name_id=2864,
                                                             deadline_s=3.0)
    assert result.code is FaceCode.NOT_VISIBLE and "ran out" in result.detail
    assert sum(seconds for _key, seconds in world.holds) < FACE_SEARCH_MAX_S
