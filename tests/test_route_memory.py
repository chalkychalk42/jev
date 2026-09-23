"""Learned passages: where walking got stuck, and the point that got past it."""

from __future__ import annotations

import json

from jev.guide.path import Path, PathStatus
from jev.guide.route_memory import MAX_PER_MAP, RouteMemory


def _route(*points):
    return Path(PathStatus.COMPLETE, tuple((x, y, 80.0) for x, y in points))


def test_a_route_through_a_known_spot_goes_by_its_learned_point():
    memory = RouteMemory()
    memory.learn(0, (0.0, -20.0), (31.0, -21.0))           # a fence, passed at its end
    patched = memory.patch(0, _route((0.0, 0.0), (0.0, -40.0)))
    assert [p[:2] for p in patched.points] == [(0.0, 0.0), (31.0, -21.0), (0.0, -40.0)]
    assert "learned passage" in patched.detail
    assert memory.patch(1, _route((0.0, 0.0), (0.0, -40.0))).points[1][:2] == (0.0, -40.0), \
        "a passage belongs to its map"
    assert memory.patch(0, _route((5.0, 0.0), (5.0, -40.0))).points[1][:2] == (5.0, -40.0), \
        "a route five yards away does not pass the spot"


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
