"""Blocked spots, deaths and hot cells: what walks keep clear of."""

from __future__ import annotations

import json

from jev.guide.path import Path, PathStatus
from jev.guide.route_memory import MAX_PER_MAP, RouteMemory, nearest_height


def _route(*points):
    return Path(PathStatus.COMPLETE, tuple((x, y, 80.0) for x, y in points))


def test_passages_already_learned_are_kept_as_data(tmp_path):
    """V178: escapes are no longer learned or taken, and a file that has them keeps them."""
    file = tmp_path / "route-memory.json"
    file.write_text(json.dumps({"format": 1, "passages": [
        {"map_id": 0, "x": 1.0, "y": 2.0, "via_x": 3.0, "via_y": 4.0, "hits": 2,
         "updated": 5.0, "z": 80.0, "floors": 1}], "blocked": [], "dangers": []}))
    memory = RouteMemory(file)
    memory.block(0, (50.0, 50.0, 80.0))
    again = RouteMemory(file)
    assert [(p.x, p.via_x, p.hits) for p in again.passages] == [(1.0, 3.0, 2)]
    assert not hasattr(memory, "patch") and not hasattr(memory, "learn")


def test_blocked_spots_are_bounded_per_map():
    memory = RouteMemory()
    for i in range(MAX_PER_MAP + 5):
        memory.block(0, (i * 10.0, 0.0, 80.0))
    assert len(memory.blocked) == MAX_PER_MAP


def confirmed(memory, spot, heading=None):
    """Blocked twice, as walking has to be before plans avoid a spot."""
    memory.block(0, spot, heading=heading)
    return memory.block(0, spot, heading=heading)


class _Straight:
    """A planner that knows no obstacles: every answer is the straight line."""

    def __init__(self, only_direct=False):
        self.asked = []
        self.only_direct = only_direct

    def path(self, map_id, start, end):
        self.asked.append((start, end))
        if self.only_direct and len(self.asked) > 1:
            return Path(PathStatus.NOPATH)
        return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))

    def close(self):
        pass


def test_a_blocked_spot_is_remembered_once_and_kept(tmp_path):
    file = tmp_path / "route-memory.json"
    memory = RouteMemory(file)
    memory.block(0, (0.0, -20.0, 91.0))
    memory.block(0, (0.5, -20.0, 91.0))
    assert [(b.x, b.y, b.z, b.hits) for b in memory.blocks(0)] == [(0.0, -20.0, 91.0, 2)]
    assert memory.blocks(1) == []
    again = RouteMemory(file)
    assert [(b.x, b.y, b.z, b.hits) for b in again.blocks(0)] == [(0.0, -20.0, 91.0, 2)]


def test_a_route_through_a_blocked_spot_is_planned_round_it():
    """Echo Ridge Mine: the mesh stepped onto the platform across a log, and the pocket
    beside it left no detour. Asked to stay clear of the crossing, the real mesh went by
    the platform's open front (offline, from the pocket: 115 yards against 109)."""
    from jev.guide.route_memory import AVOID_YARDS, AvoidingQuery, passes

    memory = RouteMemory()
    confirmed(memory, (0.0, -20.0, 80.0))
    planned = AvoidingQuery(_Straight(), memory).path(0, (0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert planned.usable and planned.points[0][:2] == (0.0, 0.0)
    assert planned.points[-1][:2] == (0.0, -40.0)
    assert not passes(planned, memory.blocks(0)[0])
    assert 40.0 < planned.length_yards() < 40.0 + 4 * AVOID_YARDS, "went the long way"


def test_a_route_clear_of_blocked_spots_is_the_planners_own():
    from jev.guide.route_memory import AvoidingQuery

    memory = RouteMemory()
    confirmed(memory, (30.0, -20.0, 80.0))
    confirmed(memory, (0.0, -20.0, 100.0))                  # a bridge ten yards up
    inner = _Straight()
    planned = AvoidingQuery(inner, memory).path(0, (0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert planned.points == ((0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert len(inner.asked) == 1


def test_a_blocked_spot_the_route_starts_or_ends_on_is_not_avoided():
    from jev.guide.route_memory import AvoidingQuery

    memory = RouteMemory()
    confirmed(memory, (0.0, -0.5, 80.0))
    confirmed(memory, (0.0, -39.5, 80.0))
    inner = _Straight()
    AvoidingQuery(inner, memory).path(0, (0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert len(inner.asked) == 1


def test_with_no_way_round_the_planners_answer_stands():
    """The follower's own recovery is still there; a way that might be walked beats none."""
    from jev.guide.route_memory import AvoidingQuery

    memory = RouteMemory()
    confirmed(memory, (0.0, -20.0, 80.0))
    planned = AvoidingQuery(_Straight(only_direct=True), memory).path(
        0, (0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert planned.points == ((0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert "no way round" in planned.detail


def test_from_a_blocked_spot_the_way_back_is_open_and_the_way_through_is_not():
    """Standing where the log stopped it, a route back the way the character came is the
    way out; one on across the log is the walk that just failed."""
    from jev.guide.route_memory import passes

    memory = RouteMemory()
    spot = memory.block(0, (0.0, -20.0, 80.0), heading=(0.0, -1.0))   # was going north
    assert passes(_route((0.0, -20.0), (0.0, -40.0)), spot), "on across the log"
    assert not passes(_route((0.0, -20.0), (0.0, 0.0)), spot), "back the way it came"
    assert not passes(_route((0.0, -20.0), (8.0, -20.0)), spot), "along the log"
    assert passes(_route((0.0, 0.0), (0.0, -40.0)), spot), "through it from further back"


def test_one_bump_is_not_a_blocked_spot():
    """Inside Northshire Abbey a detour cannot get past anything, so every bump in its
    halls was recorded blocked, and plans routed round the Abbey's only doorway for ten
    minutes (run 20260924T001027-84b25c). A spot is avoided once it has blocked twice."""
    from jev.guide.route_memory import AvoidingQuery

    memory = RouteMemory()
    memory.block(0, (0.0, -20.0, 80.0))
    assert memory.blocks(0) == []
    inner = _Straight()
    AvoidingQuery(inner, memory).path(0, (0.0, 0.0, 80.0), (0.0, -40.0, 80.0))
    assert len(inner.asked) == 1, "planned round a spot seen blocked once"
    memory.block(0, (0.3, -20.0, 80.0))
    assert len(memory.blocks(0)) == 1


def test_a_routes_height_is_read_along_its_segment():
    """Not the nearest waypoint's: 30 yards up a 100-yard leg rising 20 is 66, not 60
    (review, 25 September)."""
    route = Path(PathStatus.COMPLETE, ((0.0, 0.0, 60.0), (0.0, -100.0, 80.0)))
    distance, z = nearest_height(route.points, (0.0, -30.0))
    assert distance == 0.0 and abs(z - 66.0) < 1e-9


class _OpenGround:
    """A planner on open ground: every route is the straight line."""

    def __init__(self):
        self.asked = 0

    def path(self, map_id, start, end):
        from jev.guide.path import Path, PathStatus

        self.asked += 1
        return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))

    def close(self):
        pass


def test_walks_keep_clear_of_where_the_character_died(tmp_path):
    """Three deaths in twenty minutes at Jerod's Landing, the Defias camp between Ma
    Stonefield and Princess's pumpkin patch, each walked straight through it (sessions 122
    and 123)."""
    from jev.guide.route_memory import DANGER_YARDS, DangerAvoidingQuery, near_route

    memory = RouteMemory(tmp_path / "memory.json")
    memory.died(0, (50.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0)
    route = query.path(0, (0.0, 0.0, 60.0), (100.0, 0.0, 60.0))
    assert not near_route(route, 50.0, 0.0, DANGER_YARDS), "straight through the camp"
    assert route.length_yards() <= 200.0
    assert RouteMemory(tmp_path / "memory.json").dangers_on(0, 1100.0), "kept on disk"


def test_a_walk_to_or_from_where_it_died_goes_there(tmp_path):
    from jev.guide.route_memory import DangerAvoidingQuery

    memory = RouteMemory()
    memory.died(0, (50.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0)
    corpse_run = query.path(0, (0.0, 0.0, 60.0), (52.0, 0.0, 60.0))
    assert len(corpse_run.points) == 2, "the corpse is where the walk is going"
    way_out = query.path(0, (48.0, 0.0, 60.0), (100.0, 0.0, 60.0))
    assert len(way_out.points) == 2


def test_a_walk_from_inside_the_reach_of_a_death_keeps_the_distance_it_has(tmp_path):
    """Getting up 32 yards short of the body (`TRAP_RECLAIM_YARDS`) is inside the 35 kept
    from a death, and the walk on was let straight back through what had killed the
    character: it died there again 42 s after getting up (session 142)."""
    from jev.guide.route_memory import SPOT_TURN_YARDS, DangerAvoidingQuery, near_route

    memory = RouteMemory()
    memory.died(0, (50.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0)
    way_on = query.path(0, (18.0, 0.0, 60.0), (100.0, 0.0, 60.0))
    assert len(way_on.points) > 2, "straight back through the camp"
    assert not near_route(way_on, 50.0, 0.0, 32.0 - SPOT_TURN_YARDS)
    back = query.path(0, (100.0, 0.0, 60.0), (18.0, 0.0, 60.0))
    assert not near_route(back, 50.0, 0.0, 32.0 - SPOT_TURN_YARDS), "the corpse run's end too"


def test_an_old_death_is_walked_past_again(tmp_path):
    from jev.guide.route_memory import DANGER_S, DangerAvoidingQuery

    memory = RouteMemory()
    memory.died(0, (50.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1000.0 + DANGER_S + 1)
    assert len(query.path(0, (0.0, 0.0, 60.0), (100.0, 0.0, 60.0)).points) == 2


def test_a_planner_that_cannot_go_round_plans_as_before(tmp_path):
    from jev.guide.path import Path, PathStatus
    from jev.guide.route_memory import DangerAvoidingQuery

    class Corridor(_OpenGround):
        def path(self, map_id, start, end):
            self.asked += 1
            if abs(end[1]) > 1.0:                  # nothing off the corridor's line
                return Path(PathStatus.NOPATH, ())
            return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))

    memory = RouteMemory()
    memory.died(0, (50.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(Corridor(), memory, clock=lambda: 1100.0)
    assert len(query.path(0, (0.0, 0.0, 60.0), (100.0, 0.0, 60.0)).points) == 2


def test_walks_keep_clear_of_where_the_character_keeps_being_attacked():
    """The learned danger map's hot cells (`jev.learn.danger`, V161) are kept clear of like
    a death spot, unless the walk begins or ends at one: a hunt's camp is where it hunts."""
    from jev.guide.route_memory import HOT_YARDS, DangerAvoidingQuery, near_route

    asked = []

    def hot(map_id):
        asked.append(map_id)
        return [(105.0, 0.0, 1.2)] if map_id == 0 else []

    query = DangerAvoidingQuery(_OpenGround(), RouteMemory(), clock=lambda: 1100.0, hot=hot)
    route = query.path(0, (0.0, 0.0, 60.0), (200.0, 0.0, 60.0))
    assert not near_route(route, 105.0, 0.0, HOT_YARDS), "straight through the camp"
    assert route.detail == "round where the character keeps being attacked"
    assert route.length_yards() <= 400.0
    assert len(query.path(0, (0.0, 0.0, 60.0), (110.0, 0.0, 60.0)).points) == 2, \
        "a walk to the camp goes there"
    assert len(query.path(1, (0.0, 0.0, 60.0), (200.0, 0.0, 60.0)).points) == 2
    assert asked == [0, 0, 1]
