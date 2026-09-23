"""The parts of walking that can be checked without a window.

Geometry, the stuck signature and the detour policy are pure. What cannot be tested here
is whether a building is in the way, which is exactly the thing that has to be measured
live — and was.
"""

from __future__ import annotations

import math

import pytest

from jev.clients.hid import Hid
from jev.clients.travel import Outcome, Travel, _wrap
from jev.guide.coords import ZoneBounds, bounds_by_radio_id

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)


def _travel(positions: list[tuple[float, float] | None]) -> Travel:
    seq = iter(positions)

    def read():
        return next(seq, positions[-1] if positions else None)

    return Travel(hid=Hid(hwnd=1), bounds=ELWYNN, read_pos=read)


def test_angles_wrap_the_short_way():
    """Turning 350 degrees left is turning 10 degrees right, and a loop that does not
    know that spins."""
    assert _wrap(math.radians(350)) == pytest.approx(math.radians(-10), abs=1e-6)
    assert _wrap(math.radians(-350)) == pytest.approx(math.radians(10), abs=1e-6)
    assert _wrap(0.0) == 0.0


def test_distance_is_in_yards_not_map_fractions():
    """Map space is stretched 1.5:1 in Elwynn, so a raw hypot is not a distance and an
    angle taken in it is wrong by up to eleven degrees."""
    t = _travel([(0.5, 0.5)])
    horizontal = t.distance((0.5, 0.5), (0.51, 0.5))
    vertical = t.distance((0.5, 0.5), (0.5, 0.51))
    assert horizontal == pytest.approx(34.7, abs=0.5)
    assert vertical == pytest.approx(23.1, abs=0.5)
    assert horizontal > vertical, "the stretch has to survive into the distance"


def test_a_bearing_needs_real_movement_behind_it():
    """Half a yard is inside the readout's own noise; an angle from it is invented."""
    t = _travel([(0.5, 0.5)])
    assert t.bearing((0.5, 0.5), (0.5, 0.5)) is None
    assert t.bearing((0.5, 0.5), (0.52, 0.52)) is not None


def test_the_zone_table_resolves_the_id_the_radio_paints():
    """The addon cannot send an area id, so it sends a hash of the map file name."""
    bounds = bounds_by_radio_id("data/zones-tbc-243.json")
    assert 12080 in bounds, "Elwynn's radio id is missing from the table"
    assert bounds[12080].area_id == 12


def test_a_transient_decode_failure_is_not_a_lost_position():
    """One torn frame skipped all five recovery attempts in a live run and reported
    'could not free the character' having tried nothing."""
    t = _travel([None, None, (0.5, 0.5)])
    assert t.position() == (0.5, 0.5)


def test_a_position_that_never_reads_is_reported_as_lost():
    t = _travel([None])
    t.read_retries = 2
    assert t.position() is None


def _detour_sides(t: Travel, walks_clear: list[bool]) -> list[int]:
    """The side each detour went, where each detour's own walk got clear or was blocked.
    Nothing is pressed: the device here is refused, and the walk's result is scripted."""
    sides, here = [], (0.5, 0.5)
    for clear in walks_clear:
        sides.append(t._detour_side)
        after = (here[0] + 0.01, here[1]) if clear else here     # about 35 yards, or none
        t.read_pos = lambda after=after: after
        t._detour(here)
        here = after
    return sides


def test_the_way_round_an_obstacle_is_searched_by_doubling():
    """One detour one way, two the other - one back, one of new ground - then four. Both
    earlier rules oscillated: alternating every detour took a live character from ten
    yards out to twenty-nine, and flipping whenever a detour ended farther away walked a
    simulated character back and forth along a 60-yard fence until the walk timed out."""
    assert _detour_sides(_travel([(0.5, 0.5)]), [True] * 7) == [1, -1, -1, 1, 1, 1, 1]


def test_a_detour_blocked_on_its_own_walk_sends_the_rest_of_the_search_the_other_way():
    assert _detour_sides(_travel([(0.5, 0.5)]), [False, True, True, True, True]) == \
        [1, -1, -1, -1, -1]


def test_arrival_is_measured_in_yards():
    t = _travel([(0.5, 0.5)])
    assert t.arrival_yards == 5.0
    assert t.distance((0.5, 0.5), (0.5005, 0.5005)) < t.arrival_yards


def test_outcomes_name_what_happened():
    """A run that ends has to say whether it arrived, was blocked, or ran out of time —
    'stuck' and 'timeout' need different fixes."""
    assert {o.value for o in Outcome} >= {"arrived", "stuck", "timeout", "lost", "aborted"}


# --------------------------------------------------------------------- the controller

def test_a_heading_is_never_taken_across_a_turn():
    """The oscillator. A pulse arcs the character, so a window spanning the pulse
    measures the arc: the heading reads past the target, the error flips sign, and the
    next tick corrects the other way. Live, that was 50 turns in 37 seconds — a pulse
    every 0.7 s — on a three-point path a person would walk as two straight lines.
    """
    t = _travel([(0.5, 0.5)])
    # A full window of motion, all of it before the pulse ended.
    t._track.extend((i * 0.1, (0.5 + i * 0.001, 0.5)) for i in range(10))
    assert t._heading_now() is not None, "clean motion should give a heading"

    t._pulse_ended_at = t._track[-1][0]
    assert t._heading_now() is None, "a heading must not be taken from turned motion"


def test_a_partial_window_is_not_a_heading():
    """A partial window is a partial arc. Waiting beats steering on it."""
    t = _travel([(0.5, 0.5)])
    t._track.extend((i * 0.05, (0.5 + i * 0.002, 0.5)) for i in range(3))
    assert t._heading_now() is None


def test_the_deadband_is_wide_underway_and_tight_on_approach():
    """Ten degrees is a docking tolerance. The readout quantises at 0.12 yards and a
    heading needs 1.5 yards behind it, so ten degrees is inside the noise on a long leg
    and every tick finds a reason to twitch."""
    t = _travel([(0.5, 0.5)])
    assert t._deadband(60.0) > t._deadband(5.0)
    assert math.degrees(t._deadband(60.0)) == pytest.approx(22.0, abs=0.1)
    assert math.degrees(t._deadband(5.0)) == pytest.approx(10.0, abs=0.1)


def test_a_planned_leg_does_not_detour():
    """`_detour` is the wall heuristic for walking at a raw node. On a navmesh polyline
    it is the follower arguing with the planner, and it showed up as three detours on a
    route that had already been solved."""
    import inspect

    from jev.clients.travel import Travel as T

    src = inspect.getsource(T.to)
    assert "allow_detour" in inspect.signature(T.to).parameters
    assert "re-plan from here" in src, "a blocked planned leg must say so, not improvise"


def test_follow_asks_the_planner_again_rather_than_improvising():
    """The mesh knows about the door; the follower does not and should not learn."""
    import inspect

    from jev.clients.travel import Travel as T

    assert "replan" in inspect.signature(T.follow).parameters


def test_remaining_yards_is_measured_against_the_destination_not_the_leg():
    """A run that gave up 46 yards from Marshal McBride reported "3.1 yards left",
    because 3.1 yards was all that remained of a waypoint in the middle of the courtyard.
    Every caller of `follow` reads this number as "how far short did we stop"."""
    legs = [(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]

    class _T(Travel):
        def distance(self, a, b):  # yards, made linear so the arithmetic is readable
            return abs(a[0] - b[0]) * 100.0

    t = _T(hid=None, bounds=None, read_pos=lambda: None)
    # Stopped at the first waypoint: nothing left of leg 0, half the journey left overall.
    assert t._short_by((0.5, 0.0), legs) == 50.0
    assert t._short_by(legs[-1], legs) == 0.0
    assert t._short_by(None, legs) is None


def test_unstick_tries_more_than_one_heading():
    """Every recovery attempt acts along the current facing, so a character wedged in a
    corner can leave along exactly one heading. A ghost pinned against a tree in
    Northshire survived two corpse runs, a relog and every attempt at its original
    heading, then came free on the third heading on jump-forward."""
    import inspect

    body = inspect.getsource(Travel._unstick)
    assert "for heading in range(self.unstick_headings)" in body
    assert 'self.hid.hold("d", self.unstick_turn_s)' in body, "it never turns"
    t = Travel(hid=None, bounds=None, read_pos=lambda: None)
    assert t.unstick_headings >= 3, "one quarter-turn is not a sweep"


def test_input_refused_is_reported_as_refused_not_as_stuck():
    """`Hid` refuses whenever the game window is not focused, so a character that was
    never sent a keystroke looks exactly like one wedged against a tree. A whole live run
    went into unsticking a character standing still because a console window had stolen
    the foreground."""
    class _Hid:
        refused = 0
        hwnd = 1

        def key_down(self, _k):
            _Hid.refused += 1
            return False

        def key_up(self, _k):
            return False

        def release_all(self):
            pass

        def hold(self, *_a, **_k):
            _Hid.refused += 1
            return False

        def tap(self, *_a, **_k):
            return False

    here = (0.5, 0.5)
    t = Travel(hid=_Hid(), bounds=ELWYNN, read_pos=lambda: here,
               arrival_yards=1.0, stuck_after_s=0.4)
    result = t.to((0.9, 0.9), timeout_s=12.0, allow_detour=False)
    assert result.outcome is Outcome.REFUSED
    assert "focus" in result.detail


def test_a_blocked_leg_no_detour_gets_past_is_remembered_and_routed_round(monkeypatch):
    """Echo Ridge Mine (runs 20260923T184413-a386ff, ...190938-3b2dd7): the mesh stepped
    onto the mine's platform across a log, and the pocket beside it defeated every
    detour. The spot is blocked where the route met it, and the planner is asked once
    more for a way that stays clear of it - which the mesh had all along."""
    from jev.guide.coords import map_to_world, world_to_map
    from jev.guide.path import Path, PathStatus
    from jev.guide.route_memory import AvoidingQuery, RouteMemory

    def world(mx, my):
        x, y = map_to_world(mx, my, ELWYNN)
        return (x, y, 90.0)

    start, crossing, goal = (0.5000, 0.5000), (0.5000, 0.4950), (0.5000, 0.4800)
    front = (0.5040, 0.4990)                       # the platform's open front

    def side(point):
        m = world_to_map(point[0], point[1], ELWYNN)
        return "platform" if m[1] < 0.4951 or m[0] > 0.5030 else "slope"

    class Mesh:
        """Two ways from the slope onto the platform, the crossing and the front, and it
        takes whichever is shorter. It does not know about the log."""

        def path(self, map_id, a, b):
            if side(a) == side(b):
                return Path(PathStatus.COMPLETE, (a, b))
            portal = min((world(*crossing), world(*front)),
                         key=lambda p: math.dist(a[:2], p[:2]) + math.dist(p[:2], b[:2]))
            return Path(PathStatus.COMPLETE, (a, portal, b))

        def close(self):
            pass

    memory = RouteMemory()
    query = AvoidingQuery(Mesh(), memory)
    walked = []
    here = [start]

    class Scripted(Travel):
        def to(self, target, **kw):
            walked.append(tuple(round(v, 4) for v in target))
            blocked = math.dist(target, goal) < 1e-6 and math.dist(here[0], crossing) < 1e-6
            if blocked:                              # the log: every walk at it stops
                self.last_stuck_at = (0.4999, 0.4952)
                return self._result(Outcome.STUCK, here[0], here[0], target, 1.0,
                                    "blocked on a planned leg; re-plan from here")
            here[0] = target
            return self._result(Outcome.ARRIVED, here[0], target, target, 1.0, "")

        def _detour(self, where):
            self.detours += 1

    travel = Scripted(hid=None, bounds=ELWYNN, read_pos=lambda: here[0])
    monkeypatch.setattr(travel, "position", lambda: here[0])
    first = query.path(0, world(*start), world(*goal))

    def replan(position):
        return query.path(0, world(*position), world(*goal))

    result = travel.follow(first, replan=replan, memory=memory, max_replans=0)
    assert result.outcome is Outcome.ARRIVED, result.detail
    assert "blocked spot" in result.detail
    [spot] = memory.blocks(0)
    assert math.dist(world_to_map(spot.x, spot.y, ELWYNN), crossing) < 0.0005, \
        "blocked where the route met the log, not where the character slid to"
    assert math.dist(here[0], goal) < 1e-6
    assert math.dist(walked[-1], goal) < 1e-6
    assert math.dist(walked[-2], crossing) > 1e-4, "the way round went by the crossing again"
