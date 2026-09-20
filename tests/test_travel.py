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
