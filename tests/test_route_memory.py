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


def test_a_second_death_near_the_first_within_ten_minutes_makes_a_death_camp(tmp_path):
    """V307: Merany, a level 8 mage, died four times in 8 minutes at one spot by Raven Hill's
    graveyard (the hive, 28 Sep 12:15-12:23), and in the hive's runs begun 11:50-13:08, 172
    of 406 deaths came within 100 yards of the same character's death in the ten minutes
    before. A second death so near makes the place a death camp for its level for an hour."""
    from jev.guide.route_memory import CAMP_S, CAMP_WINDOW_S, CAMP_YARDS

    memory = RouteMemory(tmp_path / "memory.json")
    first = memory.died(0, (0.0, 0.0), now=1000.0, level=8)
    assert not first.camp(1000.0), "one death is a death spot"
    second = memory.died(0, (CAMP_YARDS - 5.0, 0.0), now=1000.0 + CAMP_WINDOW_S - 5.0, level=9)
    now = 1000.0 + CAMP_WINDOW_S
    assert second.camp(now) and first.camp(now), "both deaths are the camp"
    assert memory.camp_at(0, (-20.0, 0.0), now, level=8) is first
    assert memory.camp_at(0, (500.0, 0.0), now, level=8) is None, "out of the camp"
    assert memory.camp_at(0, (-20.0, 0.0), now, level=20) is None, "not a level 20's camp"
    assert memory.camp_at(0, (-20.0, 0.0), now + CAMP_S, level=8) is None, "an hour"
    again = RouteMemory(tmp_path / "memory.json")
    assert again.camp_at(0, (-20.0, 0.0), now, level=8) is not None, "kept on disk"

    apart = RouteMemory()
    apart.died(0, (0.0, 0.0), now=1000.0, level=8)
    late = apart.died(0, (10.0, 0.0), now=1000.0 + CAMP_WINDOW_S + 5.0, level=8)
    far = apart.died(0, (300.0, 0.0), now=1100.0, level=8)
    other = apart.died(0, (40.0, 0.0), now=1200.0, level=20)
    assert not late.camp(1700.0) and not far.camp(1700.0) and not other.camp(1700.0), \
        "too late, too far, another level"


def test_a_death_counts_for_about_the_level_it_happened_at():
    """V307, V161: the hive keeps every character's deaths in one memory, 348 on the Eastern
    Kingdoms at 12:23 on 28 Sep, and each was kept from every walk at every level."""
    from jev.guide.route_memory import DANGER_YARDS, DangerAvoidingQuery, near_route

    memory = RouteMemory()
    memory.died(0, (50.0, 0.0), now=1000.0, level=3)
    memory.died(0, (150.0, 0.0), now=1000.0)                # a death at no level read
    level = [3]
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0,
                                level=lambda: level[0])
    start, end = (0.0, 0.0, 60.0), (200.0, 0.0, 60.0)
    assert not near_route(query.path(0, start, end), 50.0, 0.0, DANGER_YARDS)
    level[0] = 12
    route = query.path(0, start, end)
    assert near_route(route, 50.0, 0.0, DANGER_YARDS), "a level 3's death, walked by at 12"
    assert not near_route(route, 150.0, 0.0, DANGER_YARDS), "one at no level counts for all"
    assert [d.level for d in memory.dangers_on(0, 1100.0, level=4)] == [3, None]
    assert [d.level for d in memory.dangers_on(0, 1100.0, level=12)] == [None]


class _Corridor(_OpenGround):
    """A corridor along the x axis: nothing off its line."""

    def path(self, map_id, start, end):
        from jev.guide.path import Path, PathStatus

        self.asked += 1
        if abs(end[1]) > 1.0 or abs(start[1]) > 1.0:
            return Path(PathStatus.NOPATH, ())
        return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))


class _FarRound(_OpenGround):
    """Open ground along the x axis; any leg off it goes round a ridge 500 yards out."""

    def path(self, map_id, start, end):
        from jev.guide.path import Path, PathStatus

        self.asked += 1
        if abs(start[1]) < 1.0 and abs(end[1]) < 1.0:
            return Path(PathStatus.COMPLETE, (tuple(start), tuple(end)))
        side = 500.0 if (end[1] + start[1]) >= 0 else -500.0
        return Path(PathStatus.COMPLETE, (tuple(start), (start[0], side, 60.0),
                                          (end[0], side, 60.0), tuple(end)))


def test_a_walk_keeps_a_death_camp_clear_however_long_the_way_round_or_is_refused():
    """V307: a death camp is kept 100 yards off by every walk not begun or ended in it, by a
    way round of any length where a death spot's may be twice the way through
    (`DANGER_DETOUR`), and a walk with none is refused: the planner given a death camp on its
    way returns a way round or no way."""
    from jev.guide.path import PathStatus
    from jev.guide.route_memory import CAMP_YARDS, DANGER_DETOUR, DangerAvoidingQuery, near_route

    start, end = (-200.0, 0.0, 60.0), (400.0, 0.0, 60.0)
    spot = RouteMemory()
    spot.died(0, (100.0, 0.0), now=1000.0, level=8)
    through = DangerAvoidingQuery(_FarRound(), spot, clock=lambda: 1200.0, level=lambda: 8)
    assert len(through.path(0, start, end).points) == 2, "a death spot's way round is too long"

    camp = RouteMemory()
    camp.died(0, (100.0, 0.0), now=1000.0, level=8)
    camp.died(0, (100.0, 4.0), now=1100.0, level=8)
    query = DangerAvoidingQuery(_FarRound(), camp, clock=lambda: 1200.0, level=lambda: 8)
    route = query.path(0, start, end)
    assert route.usable and route.detail == "round a death camp"
    assert not near_route(route, 100.0, 0.0, CAMP_YARDS)
    assert route.length_yards() > 600.0 * DANGER_DETOUR, "however long the way round"

    blocked = DangerAvoidingQuery(_Corridor(), camp, clock=lambda: 1200.0, level=lambda: 8)
    refused = blocked.path(0, start, end)
    assert refused.status is PathStatus.NOPATH and not refused.usable
    assert blocked.path(0, (90.0, 0.0, 60.0), end).usable, "a walk begun in the camp leaves it"
    assert blocked.path(0, start, (100.0, 0.0, 60.0)).usable, "a walk to the body goes there"
    later = DangerAvoidingQuery(_Corridor(), camp, clock=lambda: 1100.0 + 3601.0,
                                level=lambda: 8)
    assert later.path(0, start, end).usable, "an hour on, a death spot walked by as before"
    ghost = DangerAvoidingQuery(_Corridor(), camp, clock=lambda: 1200.0, level=lambda: 8,
                                ghost=lambda: True)
    assert len(ghost.path(0, start, end).points) == 2, "a ghost passes it unharmed"


def test_the_keeper_names_what_a_route_passes_of_what_the_layer_keeps_clear_of():
    """V307: for the exposure layer over it, whose ways round must not go back through."""
    from jev.guide.route_memory import DangerAvoidingQuery

    memory = RouteMemory()
    memory.died(0, (100.0, 0.0), now=1000.0)
    query = DangerAvoidingQuery(_OpenGround(), memory, clock=lambda: 1100.0)
    passed = query.keeper(0, (0.0, 0.0, 60.0), (200.0, 0.0, 60.0))
    assert passed(((0.0, 0.0, 60.0), (200.0, 0.0, 60.0))) == {(100.0, 0.0)}
    assert passed(((0.0, 0.0, 60.0), (100.0, 80.0, 60.0), (200.0, 0.0, 60.0))) == set()
    at_the_body = query.keeper(0, (0.0, 0.0, 60.0), (100.0, 0.0, 60.0))
    assert at_the_body(((0.0, 0.0, 60.0), (100.0, 0.0, 60.0))) == set(), "the walk's own end"


def test_a_walk_refused_through_a_death_camp_is_planned_once():
    """Review of 28 Sep (V307): a walk refused through a death camp asked the planner at each
    of `Client._plan`'s 33 start heights, two ring searches each, about 4,700 queries, and
    as often again at each re-plan and each ranking of merchants (V309). The refusal ends the
    plan, and the same walk asked again within `REFUSAL_S` is refused without a search."""
    from types import SimpleNamespace

    from jev.guide.path import PathStatus
    from jev.guide.route_memory import (CAMP_REFUSED, DANGER_RINGS, REFUSAL_S,
                                        VIA_BEARINGS, DangerAvoidingQuery)
    from jev.run.client import Client

    camp = RouteMemory()
    camp.died(0, (100.0, 0.0), now=1000.0, level=8)
    camp.died(0, (100.0, 4.0), now=1100.0, level=8)
    now = [1200.0]
    corridor = _Corridor()
    query = DangerAvoidingQuery(corridor, camp, clock=lambda: now[0], level=lambda: 8)
    client = SimpleNamespace(query=query, bounds=SimpleNamespace(map_id=0),
                             _ground=(-200.0, 0.0, 61.0))
    refused = Client._plan(client, (-200.0, 0.0), (400.0, 0.0, 60.0))
    assert refused.status is PathStatus.NOPATH and refused.detail == CAMP_REFUSED
    one_search = 1 + 2 * len(DANGER_RINGS) * VIA_BEARINGS * 2
    assert corridor.asked <= one_search, "one start height, not 33"
    asked = corridor.asked
    again = Client._plan(client, (-198.0, 1.0), (401.0, 0.0, 60.0))
    assert again.detail == CAMP_REFUSED and corridor.asked - asked == 1, "the way through alone"
    now[0] += REFUSAL_S
    asked = corridor.asked
    Client._plan(client, (-200.0, 0.0), (400.0, 0.0, 60.0))
    assert corridor.asked - asked > 1, "a minute on, searched again"
