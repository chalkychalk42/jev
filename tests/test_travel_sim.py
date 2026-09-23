"""The real `Travel` walking a simulated character through the obstacles that stopped it.

`jev.clients.walk_sim` moves a character with the keys `Travel` presses, at measured
speeds, through trunks, walls and fences with sliding collision, on a virtual clock.
"""

from __future__ import annotations

import math

import pytest

import jev.clients.travel as travel_module
from jev.clients.travel import Outcome, Travel
from jev.clients.walk_sim import Circle, Segment, SimHid, SimTime, WalkWorld
from jev.guide.coords import ZoneBounds
from jev.guide.path import Path, PathStatus
from jev.guide.route_memory import RouteMemory

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)
W, H = abs(ELWYNN.left - ELWYNN.right), abs(ELWYNN.top - ELWYNN.bottom)
X0, Y0 = 1700.0, 1100.0          # somewhere inside the zone's box, in map-yards


def _shifted(ob):
    if isinstance(ob, Circle):
        return Circle(X0 + ob.x, Y0 + ob.y, ob.r, ob.height)
    return Segment(X0 + ob.ax, Y0 + ob.ay, X0 + ob.bx, Y0 + ob.by, ob.thickness, ob.height)


def walk(points, obstacles, heading, *, memory=None, monkeypatch):
    world = WalkWorld(x=X0 + points[0][0], y=Y0 + points[0][1], heading=heading,
                      obstacles=tuple(_shifted(o) for o in obstacles),
                      width_yards=W, height_yards=H)
    monkeypatch.setattr(travel_module, "time", SimTime(world))

    def route(pts):
        return Path(PathStatus.COMPLETE,
                    tuple(world.world(X0 + x, Y0 + y, ELWYNN, 80.0) for x, y in pts))

    def replan(here):                       # the mesh does not know the obstacle
        return route([(here[0] * W - X0, here[1] * H - Y0), *points[1:]])

    travel = Travel(hid=SimHid(world), bounds=ELWYNN, read_pos=world.map_position)
    result = travel.follow(route(points), timeout_s=120.0, replan=replan, memory=memory)
    return result, world


FENCE = [Segment(-30, -20, 30, -20)]                         # 60 yards across the path
WEDGE = [Segment(-20, 0, 20, 0), Circle(0, 1.4, 0.7)]       # a trunk a body-width off a wall


@pytest.mark.parametrize(("name", "points", "obstacles", "heading"), [
    ("long fence", [(0, 0), (0, -40)], FENCE, -math.pi / 2),
    ("tree beside a wall", [(-10, 1.0), (12, 1.0), (14, 8.0)], WEDGE, 0.0),
])
def test_a_spot_the_mesh_calls_open_is_learned_once_and_then_walked_round(
        name, points, obstacles, heading, monkeypatch):
    """Measured 23 September: routes through a rail fence and a gap between a trunk and
    Northshire Abbey's wall stopped the character on every trip."""
    memory = RouteMemory()
    first, _ = walk(points, obstacles, heading, memory=memory, monkeypatch=monkeypatch)
    assert first.outcome is Outcome.ARRIVED and first.stuck_events >= 1
    assert memory.passages, "the escape was not learned"
    second, _ = walk(points, obstacles, heading, memory=memory, monkeypatch=monkeypatch)
    assert second.outcome is Outcome.ARRIVED
    assert second.stuck_events == 0, "walked into a spot it had already learned"
    assert second.elapsed_s < first.elapsed_s


def test_the_learned_point_is_where_the_escape_went_round_not_where_it_started(monkeypatch):
    memory = RouteMemory()
    walk([(0, 0), (0, -40)], FENCE, -math.pi / 2, memory=memory, monkeypatch=monkeypatch)
    [passage] = memory.passages
    # World x/y back to map-yards: the via point is at an end of the 60-yard fence.
    world = WalkWorld(x=0, y=0, heading=0, width_yards=W, height_yards=H)
    via = next(((X0 + dx, Y0 + dy) for dx in range(-40, 41) for dy in range(-30, 10)
                if math.dist(world.world(X0 + dx, Y0 + dy, ELWYNN)[:2],
                             (passage.via_x, passage.via_y)) < 1.0), None)
    assert via is not None and abs(via[0] - X0) >= 28, "learned a point still in front of the fence"


def test_a_blocked_leg_is_rounded_without_walking_into_it_twice(monkeypatch):
    """When re-planning returns the same blocked route, the detour starts at once."""
    result, _ = walk([(0, 0), (0, -40)], [Segment(-6, -20, 6, -20)], -math.pi / 2,
                     monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED and result.stuck_events == 1


def test_open_ground_is_walked_without_any_recovery(monkeypatch):
    result, _ = walk([(0, 0), (40, 0), (40, 20)], [], 0.0, memory=RouteMemory(),
                     monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED
    assert result.stuck_events == 0 and result.detours == 0
