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


def test_the_search_for_a_way_round_stops_at_its_budget():
    """V257: ranking twelve repairers through the search from Windows ran the level 9 mage's
    repair past its time with not a step walked (sessions 226-227)."""
    from jev.guide.exposure import EXPOSURE_BUDGET_S

    ticks = iter(range(1000))
    camp = [(200.0, 0.0), (210.0, 8.0), (195.0, -6.0)]
    inner = _OpenGround()
    query = ExposureQuery(inner, _spawns(camp),
                          clock=lambda: next(ticks) * (EXPOSURE_BUDGET_S / 3))
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert route.usable
    assert inner.asked <= 1 + 2 * 3, "a few legs, then the best found"


def test_the_ways_round_are_planned_below_the_layers_that_search_their_own():
    camp = [(200.0, 0.0), (210.0, 8.0), (195.0, -6.0)]
    inner, legs = _OpenGround(), _OpenGround()
    query = ExposureQuery(inner, _spawns(camp), legs=legs)
    route = query.path(0, (0.0, 0.0, 60.0), (400.0, 0.0, 60.0))
    assert len(route.points) > 2
    assert inner.asked == 1 and legs.asked > 2, "the direct route above, the legs below"
    assert query.estimate() is inner, "a walk's cost is asked without the search"


class _Valley(_OpenGround):
    """Open ground in a valley 200 yards wide along the x axis, and nothing beyond it."""

    def path(self, map_id, start, end):
        self.asked += 1
        if abs(start[1]) > 100.0 or abs(end[1]) > 100.0:
            return Path(PathStatus.NOPATH, ())
        return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))


def test_each_place_a_long_walk_passes_is_gone_round_on_its_own():
    """V270: one way round the middle of every spawn a long route passed was no way round;
    the level 12 walk from Elwynn's Prowlers to Sentinel Hill passed 27 spawns with it or
    without it, and 8 gone round place by place."""
    camps = [(495.0, 0.0), (500.0, 5.0), (505.0, -5.0),
             (2495.0, 0.0), (2500.0, 5.0), (2505.0, -5.0)]
    query = ExposureQuery(_Valley(), _spawns(camps))
    start, end = (0.0, 0.0, 60.0), (3000.0, 0.0, 60.0)
    route = query.path(0, start, end)
    assert not query.exposed(0, route.points, start, end), "both camps gone round"
    assert route.length_yards() < 3000.0 + 150.0, "each round its own place, not one wide way"
    assert "attack on sight" in route.detail
    assert route.points[0] == start and route.points[-1] == end


def test_going_round_places_stops_at_its_budget():
    from jev.guide.exposure import PLACES_BUDGET_S

    camps = [(495.0, 0.0), (500.0, 5.0), (505.0, -5.0),
             (2495.0, 0.0), (2500.0, 5.0), (2505.0, -5.0)]
    ticks = iter(range(10000))
    inner = _Valley()
    query = ExposureQuery(inner, _spawns(camps),
                          clock=lambda: next(ticks) * (PLACES_BUDGET_S / 3))
    route = query.path(0, (0.0, 0.0, 60.0), (3000.0, 0.0, 60.0))
    assert route.usable
    assert inner.asked <= 1 + 2 * 3 + 2 * 3, "a few legs each search, then the best found"


def test_a_way_round_spawns_does_not_go_back_through_where_the_character_died():
    """V307: after each of the level 8 mage Merany's first three deaths at one spot, its walk
    to a repairer was planned "round 21 units that attack on sight" and passed within 6 yards
    of the spot, kept in the route memory since the first (the hive, 28 Sep 12:15-12:23). The
    ways round are planned below the layer that keeps clear of deaths (V257); a way round that
    passes a place that layer went round is not taken, however few spawns it passes."""
    from jev.guide.route_memory import DANGER_YARDS, DangerAvoidingQuery, RouteMemory, near_route

    memory = RouteMemory()
    memory.died(0, (200.0, 0.0), now=1000.0)
    deaths = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0)
    start, end = (0.0, 0.0, 60.0), (400.0, 0.0, 60.0)
    kept_round = deaths.path(0, start, end)
    assert not near_route(kept_round, 200.0, 0.0, DANGER_YARDS)
    camp = [(200.0, 60.0), (190.0, 55.0), (210.0, 55.0)]       # on that way round
    before = ExposureQuery(deaths, _spawns(camp), legs=_OpenGround())
    assert near_route(before.path(0, start, end), 200.0, 0.0, DANGER_YARDS), \
        "without the layer's places kept, back through the death"
    query = ExposureQuery(deaths, _spawns(camp), legs=_OpenGround(), keep=deaths.keeper)
    route = query.path(0, start, end)
    assert not near_route(route, 200.0, 0.0, DANGER_YARDS), "back through where it died"
    assert not query.exposed(0, route.points, start, end), "and round the camp all the same"
    assert "attack on sight" in route.detail
