"""Learned passages: where walking got stuck, and the point that got past it."""

from __future__ import annotations

import json

from jev.guide.path import Path, PathStatus
from jev.guide.route_memory import MAX_PER_MAP, RouteMemory


def _route(*points):
    return Path(PathStatus.COMPLETE, tuple((x, y, 80.0) for x, y in points))


def test_a_route_through_a_known_spot_goes_by_its_learned_point():
    memory = RouteMemory()
    memory.learn(0, (0.0, -20.0), (31.0, -21.0), z=80.0)   # a fence, passed at its end
    patched = memory.patch(0, _route((0.0, 0.0), (0.0, -40.0)))
    assert [p[:2] for p in patched.points] == [(0.0, 0.0), (31.0, -21.0), (0.0, -40.0)]
    assert "learned passage" in patched.detail
    assert memory.patch(1, _route((0.0, 0.0), (0.0, -40.0))).points[1][:2] == (0.0, -40.0), \
        "a passage belongs to its map"
    assert memory.patch(0, _route((5.0, 0.0), (5.0, -40.0))).points[1][:2] == (5.0, -40.0), \
        "a route five yards away does not pass the spot"


def test_a_passage_is_only_taken_on_its_own_floor():
    """Beside William Pestle an escape learned on one floor bent every route to him
    towards the stairs, and the character walked the floor above him (session 95)."""
    memory = RouteMemory()
    memory.learn(0, (0.0, -20.0), (31.0, -21.0), z=87.0)   # learned one floor up
    assert len(memory.patch(0, _route((0.0, 0.0), (0.0, -40.0))).points) == 2
    memory.learn(0, (0.5, -20.0), (-12.0, -20.0), z=80.0)  # the same spot, this floor
    assert len(memory.passages) == 2, "floors apart are two passages"
    assert memory.patch(0, _route((0.0, 0.0), (0.0, -40.0))).points[1][:2] == (-12.0, -20.0)


def test_a_passage_without_a_height_is_given_its_floor_or_left_untaken(tmp_path):
    file = tmp_path / "route-memory.json"
    memory = RouteMemory(file)
    memory.learn(0, (0.0, -20.0), (31.0, -21.0))           # learned before heights were kept
    memory.learn(0, (100.0, -20.0), (131.0, -21.0))
    floors = {0.0: [80.2], 100.0: [57.0, 64.0, 74.5]}      # open ground; the inn's three floors
    assert memory.backfill(0, lambda x, y: floors[x]) == 2
    assert [(p.z, p.floors) for p in memory.passages] == [(80.2, 1), (None, 3)]
    assert memory.patch(0, _route((100.0, 0.0), (100.0, -40.0))).points[1][:2] == (100.0, -40.0), \
        "a spot over several floors is not guessed at"
    assert memory.patch(0, _route((0.0, 0.0), (0.0, -40.0))).points[1][:2] == (31.0, -21.0)
    again = RouteMemory(file)
    assert again.backfill(0, lambda x, y: [0.0]) == 0, "looked up once, the answer kept"


def test_a_spot_at_the_route_start_is_left_to_the_follower():
    memory = RouteMemory()
    memory.learn(0, (0.0, 0.0), (10.0, 0.0))
    assert len(memory.patch(0, _route((0.5, 0.0), (0.0, -40.0))).points) == 2


def test_the_same_obstacle_learned_twice_keeps_the_newer_escape(tmp_path):
    file = tmp_path / "route-memory.json"
    memory = RouteMemory(file)
    memory.learn(0, (0.0, -20.0), (14.0, -20.0))
    memory.learn(0, (1.5, -20.0), (31.0, -21.0))
    assert len(memory.passages) == 1
    assert (memory.passages[0].via_x, memory.passages[0].hits) == (31.0, 2)
    again = RouteMemory(file)                                  # persisted, and read back
    assert [(p.x, p.y, p.via_x, p.via_y, p.hits) for p in again.passages] == [
        (1.5, -20.0, 31.0, -21.0, 2)]
    assert json.loads(file.read_text())["format"] == 1


def test_memory_is_bounded_per_map():
    memory = RouteMemory()
    for i in range(MAX_PER_MAP + 5):
        memory.learn(0, (i * 10.0, 0.0), (i * 10.0, 5.0))
    assert len(memory.passages) == MAX_PER_MAP


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
