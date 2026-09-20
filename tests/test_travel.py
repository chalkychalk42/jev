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


def test_a_detour_that_makes_things_worse_switches_side():
    """Alternating on every detour is not 'try the other way', it is oscillate: eight
    detours took a live character from ten yards out to twenty-nine."""
    t = _travel([(0.5, 0.5)])
    t.hid.require_focus = True                # nothing will actually press
    side = t._detour_side
    t.read_pos = lambda: (0.9, 0.9)           # far worse than where we started
    t._detour((0.5, 0.5), (0.51, 0.51))
    assert t._detour_side == -side


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
