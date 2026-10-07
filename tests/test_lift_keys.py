"""A lift ridden by keys (V407): the platform's times, and the ride by them.

Nothing the live client reads shows where a lift's platform is: the strip paints no height,
the platform has no plate, and a ride changes no x or y. Its place is the server's clock modulo
its cycle (`ElevatorTransport::Update`). So the keys ride only by the platform's times, known
from a sighting and kept by the rides they foretell; and every step on or off is made inside
the stretch the times say the platform surely stands at the stop.
"""

from __future__ import annotations

import math

import pytest

from jev.clients.lift import LiftClock, LiftRide
from jev.guide.coords import bounds_by_radio_id, map_to_world, world_to_map
from jev.guide.path import Lift, LiftLeg, LiftStop
from jev.perceive.radio_frame import zone_id

ZONES = bounds_by_radio_id("data/zones-tbc-243.json")
MULGORE = ZONES[zone_id("Mulgore")]
# The Mesa Elevator by Thunder Bluff's graveyard (guid 18298), as `load_lifts` reads it, with
# its decks as `lift_decks` finds them on the map 1 tiles: the lower dock 6 yards east of the
# shaft at the bottom stop's height, the upper deck 6 yards south at the top's.
LIFT = Lift(guid=18298, entry=4170, name="Mesa Elevator", map_id=1, x=-1286.2, y=189.7,
            period_ms=30000, stops=(LiftStop(68.8, 15000, 20000), LiftStop(130.1, 0, 5000)))
DOCK = (-1281.0, 192.7, 68.9)
TOP_DECK = (-1283.2, 184.5, 130.5)
UP = LiftLeg(LIFT, 0, 1, DOCK, TOP_DECK)
DOWN = LiftLeg(LIFT, 1, 0, TOP_DECK, DOCK)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_a_sighting_gives_the_platforms_times_at_both_stops_a_cycle_apart():
    wall = Clock()
    clock = LiftClock(wall=wall)
    assert not clock.known(LIFT) and clock.window(LIFT, 0, wall(), 1.0) is None
    # Seen standing at the top at t, having arrived within a second: there until t + 4, and at
    # the bottom from 15 s after its arrival at the top, five seconds a cycle.
    t = wall()
    clock.saw(LIFT, 1, t, arrived_within_s=1.0)
    assert clock.window(LIFT, 1, t, 2.0) == pytest.approx((t, t + 4.0))
    assert clock.window(LIFT, 1, t + 3.5, 2.0) == pytest.approx((t + 30.0, t + 34.0))
    assert clock.window(LIFT, 0, t, 2.0) == pytest.approx((t + 15.0, t + 19.0))
    # Seen again a few cycles on, a second earlier in its stand: the stretch narrows to it.
    clock.saw(LIFT, 1, t + 90.5, arrived_within_s=0.25)
    start, end = clock.window(LIFT, 1, t + 89.0, 2.0)
    assert (start, end) == pytest.approx((t + 90.5, t + 94.75))
    # A sighting the times do not meet is a restarted server's: it starts them afresh.
    clock.saw(LIFT, 1, t + 107.0, arrived_within_s=0.5)
    assert clock.window(LIFT, 1, t + 106.0, 2.0) == pytest.approx((t + 107.0, t + 111.5))
    # Too loose to step in: nothing.
    loose = LiftClock(wall=wall)
    loose.saw(LIFT, 1, t)                                # arrived any time in its stand
    assert loose.window(LIFT, 1, t, 0.5) is None


def test_the_times_are_kept_in_a_file_until_they_are_old_or_a_ride_fails(tmp_path):
    wall = Clock()
    path = tmp_path / "lift-clock.json"
    LiftClock(path, wall=wall).saw(LIFT, 1, wall(), arrived_within_s=1.0)
    again = LiftClock(path, wall=wall)
    assert again.known(LIFT)
    wall.now += 3 * 3600
    again.kept(LIFT)                                     # a confirmed ride
    wall.now += 3 * 3600
    assert LiftClock(path, wall=wall).known(LIFT), "kept by the ride"
    wall.now += 5 * 3600
    assert not LiftClock(path, wall=wall).known(LIFT), "too old"
    fresh = LiftClock(path, wall=wall)
    fresh.saw(LIFT, 1, wall(), arrived_within_s=1.0)
    fresh.forget()
    assert not LiftClock(path, wall=wall).known(LIFT), "a reconnect forgets every lift"


class World:
    """The character and the platform, as the server has them: the platform at the bottom or
    the top by the true clock (`offset_s`: when its cycle began), the character carried while
    on it, and a step into the shaft with the platform away a fall."""

    SHAFT_YARDS = 3.0

    def __init__(self, wall, offset_s, start=DOCK, facing=0.0):
        self.wall, self.offset_s = wall, offset_s
        self.x, self.y, self.level = start[0], start[1], 0
        self.facing, self.fell, self.held = facing, False, []
        self.checkpoint = None

    def platform(self):
        """The stop the platform stands at now, or None while it moves."""
        t = ((self.wall() - self.offset_s) * 1000) % LIFT.period_ms
        for i, stop in enumerate(LIFT.stops):
            a, b = stop.arrives_ms, stop.leaves_ms
            if (a <= t < b) if a < b else (t >= a or t < b):
                return i
        return None

    def on_shaft(self):
        return math.dist((self.x, self.y), (LIFT.x, LIFT.y)) <= self.SHAFT_YARDS

    def tick(self):
        if self.on_shaft() and not self.fell:
            stop = self.platform()
            if stop is not None:
                self.level = stop                       # aboard: carried to where it stands
        return self

    def hold(self, key, seconds, **kw):
        self.held.append((key, round(seconds, 3)))
        if key in ("a", "d"):
            self.facing += (1 if key == "d" else -1) * math.radians(134.0) * seconds
            self.wall.sleep(seconds)
            return True
        steps = max(1, int(seconds / 0.05))
        for _ in range(steps):
            self.wall.sleep(seconds / steps)
            was_on = self.on_shaft()
            # Map frames run x across and y down: a bearing in map yards, as the follower's.
            mx, my = world_to_map(self.x, self.y, MULGORE)
            w = abs(MULGORE.left - MULGORE.right)
            h = abs(MULGORE.top - MULGORE.bottom)
            mx += 7.0 * seconds / steps * math.cos(self.facing) / w
            my += 7.0 * seconds / steps * math.sin(self.facing) / h
            self.x, self.y = map_to_world(mx, my, MULGORE)[:2]
            if self.on_shaft() and not was_on and self.platform() != self.level:
                self.fell = True                        # into an empty shaft
            self.tick()
        return True

    def release_all(self):
        pass

    def position(self):
        self.tick()
        return world_to_map(self.x, self.y, MULGORE)

    def read(self):
        self.tick()
        return {"pos.zone_id": zone_id("ThunderBluff") if self.level == 1 and not self.on_shaft()
                else zone_id("Mulgore")}


def ride(world, clock, wall):
    return LiftRide(hid=world, bounds=MULGORE, read=world.read, read_pos=world.position,
                    heading=lambda: world.facing, turn_rate=lambda: math.radians(134.0),
                    clock=clock, wall=wall, sleep=wall.sleep)


@pytest.mark.parametrize("phase_s", [0.0, 7.3, 16.0, 29.5])
def test_the_keys_ride_up_and_down_by_the_platforms_times_and_never_into_an_empty_shaft(phase_s):
    wall = Clock()
    offset = wall() - phase_s
    clock = LiftClock(wall=wall)
    # The times from a sighting at the top, arriving within half a second of it.
    first_top = offset + math.ceil((wall() - offset) / 30.0) * 30.0
    clock.saw(LIFT, 1, first_top + 0.5, arrived_within_s=0.5)
    world = World(wall, offset)
    keys = ride(world, clock, wall)
    assert keys.rideable(UP) and keys.rideable(DOWN)
    landed = keys.ride(UP)
    assert landed == TOP_DECK and not world.fell and world.level == 1, keys.detail
    assert world.read()["pos.zone_id"] == zone_id("ThunderBluff")
    world.x, world.y = TOP_DECK[:2]
    back = keys.ride(DOWN)
    assert back == DOCK and not world.fell and world.level == 0, keys.detail


def test_with_the_times_wrong_the_ride_is_not_confirmed_and_the_times_are_forgotten():
    """Here a server restarted since the sighting: the platform is a third of a cycle off.
    Stepped onto at the bottom, where the dock lies under the shaft, the character is not
    carried, and the walk off at the time the top was due finds the zone as it was."""
    wall = Clock()
    clock = LiftClock(wall=wall)
    clock.saw(LIFT, 1, wall() + 0.5, arrived_within_s=0.5)
    world = World(wall, offset_s=wall() + 10.0)
    world.SHAFT_YARDS = 0.0                    # a dock under the bottom stop: no fall there
    keys = ride(world, clock, wall)
    assert keys.ride(UP) is None and "the zone as before" in keys.detail
    assert "times forgotten" in keys.detail
    assert not clock.known(LIFT) and not keys.rideable(UP)


def test_no_times_no_ride_and_no_deck_under_a_stop():
    wall = Clock()
    keys = ride(World(wall, 0.0), LiftClock(wall=wall), wall)
    assert not keys.rideable(UP) and keys.ride(UP) is None
    assert "times are not known" in keys.detail
    clock = LiftClock(wall=wall)
    clock.saw(LIFT, 1, wall(), arrived_within_s=0.5)
    keys = ride(World(wall, 0.0), clock, wall)
    pit = LiftLeg(LIFT, 1, 0, TOP_DECK, (LIFT.x, LIFT.y, 59.7))   # the ground under the dock
    assert keys.rideable(UP) and not keys.rideable(pit)


def test_the_live_follower_is_offered_only_the_lifts_it_can_ride():
    """V382 offers a lift to a follower that can ride one; the live follower's keys ride the
    legs whose times are known and whose decks are at their stops' heights."""
    from jev.clients.travel import Travel
    from jev.run.client import _rideable

    travel = Travel(hid=None, bounds=MULGORE, read_pos=lambda: None)
    assert callable(travel.ride) and not _rideable(travel, UP), "no times: no lift"
    wall = Clock()
    clock = LiftClock(wall=wall)
    clock.saw(LIFT, 1, wall(), arrived_within_s=0.5)
    travel.lifts = ride(World(wall, 0.0), clock, wall)
    assert _rideable(travel, UP)
    assert _rideable(object(), UP), "a follower with no word of its own (the hive's) rides all"
