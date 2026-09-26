"""Routes that pass fewer units that attack on sight (V248)."""

from __future__ import annotations

import math

from jev.guide.exposure import END_YARDS, EXPOSED_YARDS, ExposureQuery
from jev.guide.path import Path, PathStatus


class _OpenGround:
    """Straight lines everywhere, as open ground would give."""

    def __init__(self):
        self.asked = 0

    def path(self, map_id, start, end):
        self.asked += 1
        return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))

    def close(self):
        pass


def _spawns(points):
    def hostile(map_id, x, y, radius):
        return [(sx, sy, 60.0) for sx, sy in points if math.dist((sx, sy), (x, y)) <= radius]
    return hostile


def test_a_walk_goes_round_a_camp_in_its_way():
    """V248: 103 of 110 attacks on the walking mage began within 20 yards of a spawn, and 23
    of its 34 deaths came on walks (sessions 195-219)."""
    camp = [(200.0, 0.0), (210.0, 8.0), (195.0, -6.0), (205.0, -10.0)]
    query = ExposureQuery(_OpenGround(), _spawns(camp))
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) > 2, "straight through the camp"
    assert not query.exposed(0, route.points, (0.0, 0.0), (400.0, 0.0))
    assert route.length_yards() <= 400.0 * 1.6
    assert "attack on sight" in route.detail


def test_a_clear_walk_is_walked_straight():
    query = ExposureQuery(_OpenGround(), _spawns([(200.0, 80.0)]))
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) == 2


def test_the_camp_a_walk_goes_to_or_starts_in_is_its_own():
    """A hunt walks to its camp; a character stands where it stands."""
    query = ExposureQuery(_OpenGround(), _spawns([(395.0, 5.0), (5.0, -5.0)]))
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) == 2
    assert END_YARDS > EXPOSED_YARDS


class _Pass(_OpenGround):
    """A pass along the x axis: anything off it is round a mountain, 150 yards further."""

    def path(self, map_id, start, end):
        self.asked += 1
        if abs(start[1]) < 1.0 and abs(end[1]) < 1.0:
            return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))
        mid = ((start[0] + end[0]) / 2, 75.0 * (1 if end[1] >= start[1] else -1), 60.0)
        return Path(PathStatus.COMPLETE, (tuple(start), mid, tuple(end)))


def test_one_spawn_is_not_worth_a_long_way_round():
    """Passing one costs about ten seconds of walking; a way round costing more is not
    taken, and on open ground a short one is."""
    query = ExposureQuery(_Pass(), _spawns([(60.0, 0.0)]))
    route = query.path(0, (0.0, 0.0, 60.0), (120.0, 0.0, 60.0))
    assert len(route.points) == 2, "round the mountain for one wolf"
    open_ground = ExposureQuery(_OpenGround(), _spawns([(60.0, 0.0)]))
    assert len(open_ground.path(0, (0.0, 0.0, 60.0), (120.0, 0.0, 60.0)).points) > 2


def test_a_ghost_or_an_unread_side_walks_as_before():
    query = ExposureQuery(_OpenGround(), lambda *a: [])
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) == 2


def test_anything_going_wrong_plans_as_before():
    def broken(*a):
        raise RuntimeError("index unreadable")
    query = ExposureQuery(_OpenGround(), broken)
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) == 2
