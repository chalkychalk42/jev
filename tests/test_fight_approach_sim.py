"""The real `Fight` walking a simulated character in to melee reach.

Watched by the operator on 23 September: "4 paces, then 4 paces, then a couple tiny steps
until it swings". This runs the approach against a character driven by the keys it
presses (`walk_sim`) and a target that reports `in_melee` within ten yards and resolves a
swing within five yards when faced - the client's own reach, not a guessed stop.
"""

from __future__ import annotations

import math

import pytest

import jev.clients.fight as fight_module
from jev.clients.fight import MAX_APPROACH_S, MAX_CLOSE_BURSTS, Fight
from jev.clients.walk_sim import Circle, Segment, SimHid, SimTime, WalkWorld
from jev.guide.coords import ZoneBounds
from jev.perceive.units import Plate, RingColour

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)

WOLF = 2864
REACH_YARDS = 5.0
NEAR_YARDS = 9.9
SWING_S = 2.6


class Target:
    """A unit on the ground that the character's swings reach within five yards."""

    def __init__(self, world: WalkWorld, x: float, y: float, *, charges: bool = False):
        self.world, self.x, self.y, self.charges = world, x, y, charges
        self.swings = 0
        self.next_swing = 0.0

    def distance(self) -> float:
        return math.hypot(self.x - self.world.x, self.y - self.world.y)

    def bearing_off(self) -> float:
        angle = math.atan2(self.y - self.world.y, self.x - self.world.x) - self.world.heading
        return (angle + math.pi) % (2 * math.pi) - math.pi

    def update(self) -> None:
        if self.charges and self.distance() > 3.0:          # runs at the character
            d = self.distance()
            step = min(d - 3.0, 7.0 * 0.1)
            self.x -= (self.x - self.world.x) / d * step
            self.y -= (self.y - self.world.y) / d * step
        if (self.distance() <= REACH_YARDS and abs(self.bearing_off()) <= math.radians(60)
                and self.world.t >= self.next_swing):
            self.swings += 1
            self.next_swing = self.world.t + SWING_S

    def values(self) -> dict:
        self.update()
        mx, my = self.world.map_position()
        return {"pos.mx": mx, "pos.my": my,
                "target.has": True, "target.name_id": WOLF, "target.hp": 1.0,
                "target.in_melee": self.distance() <= NEAR_YARDS,
                "target.attacking_me": self.charges, "target.melee_range": None,
                "combat.swings": self.swings % 15, "vitals.dead": False, "ui.modal": False,
                "ui.error_count": 0}


class Steering:
    """The plate as the camera shows it: offset from centre by the bearing off the heading."""

    def __init__(self, world: WalkWorld, target: Target):
        self.world, self.target = world, target

    def track_selected(self, hint, **_):
        angle = self.target.bearing_off()
        if abs(angle) >= math.radians(50):
            return None
        offset = 0.5 * math.sin(angle) / math.sin(math.radians(50))
        return Plate(800 + offset * 1600, 400.0, 147, RingColour.RED), offset

    def turn_toward(self, offset):
        seconds = min(0.5, abs(offset)) * 0.9
        if seconds < 0.05:
            return True
        key = "d" if offset > 0 else "a"
        self.world.keys.add(key)
        self.world.advance(seconds)
        self.world.keys.discard(key)
        return True


def approach(target_at, *, charges=False, monkeypatch):
    world = WalkWorld(x=0.0, y=0.0, heading=0.0)
    target = Target(world, *target_at, charges=charges)
    monkeypatch.setattr(fight_module, "time", SimTime(world))
    hid = SimHid(world)
    fight = Fight(hid=hid, read=target.values, read_frame=lambda: None,
                  targeting=Steering(world, target))
    fight._selected_name_id = WOLF
    fight._swings = 0
    fight.last_plate = Plate(800.0, 400.0, 147, RingColour.RED)
    started = world.t
    reached = fight._close(target.values(), near=target.distance() <= NEAR_YARDS)
    return reached, world, target, world.t - started, hid


def test_a_standing_unit_is_reached_in_one_walk_and_the_walk_stops_at_the_swing(monkeypatch):
    reached, world, target, took, _ = approach((25.0, 3.0), monkeypatch=monkeypatch)
    assert reached, "walked without a swing reaching"
    downs = [p for p in world.presses if p[:2] == ("down", "w")]
    assert len(downs) == 1, "stopped and started on the way in"
    assert 2.0 <= target.distance() <= REACH_YARDS, "overran it or stopped short"
    assert took <= (25.0 - 4.0) / 7.0 + 1.0, "slower than one walk in"


def test_a_unit_running_at_the_character_is_met_not_passed(monkeypatch):
    _, _, target, took, _ = approach((30.0, 0.0), charges=True, monkeypatch=monkeypatch)
    assert target.distance() >= 2.0, "walked through it"
    assert took < 3.0


@pytest.mark.parametrize("side", [-12.0, 12.0])
def test_a_unit_off_to_one_side_is_steered_onto_while_walking(side, monkeypatch):
    reached, _, target, _, _ = approach((22.0, side), monkeypatch=monkeypatch)
    assert reached and target.distance() <= REACH_YARDS


def fight_to(target_at, obstacles, *, monkeypatch):
    """Approach after approach, facing the unit before each, as `Fight.run` does."""
    world = WalkWorld(x=0.0, y=0.0, heading=0.0, obstacles=tuple(obstacles))
    target = Target(world, *target_at)
    monkeypatch.setattr(fight_module, "time", SimTime(world))
    steering = Steering(world, target)
    fight = Fight(hid=SimHid(world), read=target.values, read_frame=lambda: None,
                  targeting=steering, bounds=ELWYNN)
    fight._selected_name_id = WOLF
    fight._swings = 0
    fight.last_plate = Plate(800.0, 400.0, 147, RingColour.RED)
    for _ in range(MAX_CLOSE_BURSTS):
        if fight._approach_s >= MAX_APPROACH_S:
            break
        while abs(target.bearing_off()) > math.radians(2):     # face it: engage's job
            world.heading += target.bearing_off()
        if fight._close(target.values(), near=target.distance() <= NEAR_YARDS):
            return True, world, target, fight
    return False, world, target, fight


PIT_PROP = Circle(8.0, 0.0, 1.0)


def test_a_unit_behind_a_post_is_reached_by_stepping_round_it(monkeypatch):
    """Measured 23 September at Echo Ridge Mine (run 20260923T184413-a386ff): a Kobold
    Laborer in plain view past a pit prop, and three walks of six seconds each pressed into
    the prop until the fight gave up, "closed 3 times over 18s and never came within
    reach"."""
    reached, _, target, fight = fight_to((20.0, 0.0), [PIT_PROP], monkeypatch=monkeypatch)
    assert reached and target.distance() <= REACH_YARDS
    assert fight.sidesteps >= 1


def test_a_post_walled_on_the_first_side_is_rounded_on_the_other(monkeypatch):
    """The first step goes right here, into rock: the search goes the other way."""
    rock = Segment(2.0, 1.3, 9.0, 1.3)
    reached, _, target, fight = fight_to((20.0, 0.0), [PIT_PROP, rock], monkeypatch=monkeypatch)
    assert reached and target.distance() <= REACH_YARDS
    assert fight.sidesteps >= 2


def test_near_a_unit_a_step_that_goes_nowhere_is_stepped_round_too(monkeypatch):
    """Within `in_melee` the approach steps rather than walks, and a post in the way
    stopped every step just the same."""
    reached, _, target, fight = fight_to((9.5, 0.0), [Circle(5.0, 0.0, 0.8)],
                                         monkeypatch=monkeypatch)
    assert reached and target.distance() <= REACH_YARDS
    assert fight.sidesteps >= 1


def test_open_ground_takes_no_sidestep(monkeypatch):
    reached, _, _, fight = fight_to((25.0, 3.0), [], monkeypatch=monkeypatch)
    assert reached and fight.sidesteps == 0


def test_which_side_the_first_sidestep_takes_is_drawn_per_fight():
    """One fixed side at every post is a pattern; with a humaniser the side is drawn."""
    import random

    from jev.clients.hid import Humaniser

    sides = set()
    for seed in range(12):
        world = WalkWorld(x=0.0, y=0.0, heading=0.0)
        fight = Fight(hid=SimHid(world, Humaniser(rng=random.Random(seed))),
                      read=lambda: None, read_frame=lambda: None, bounds=ELWYNN)
        fight.run(WOLF)
        sides.add(fight._side)
    assert sides == {1, -1}
