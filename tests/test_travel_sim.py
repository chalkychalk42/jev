"""The real `Travel` walking a simulated character through the obstacles that stopped it.

`jev.clients.walk_sim` moves a character with the keys `Travel` presses, at measured
speeds, through trunks, walls and fences with sliding collision, on a virtual clock.
"""

from __future__ import annotations

import math
import random

import pytest

import jev.clients.travel as travel_module
from jev.clients.hid import Humaniser
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


def walk(points, obstacles, heading, *, memory=None, humaniser=None, monkeypatch):
    world = WalkWorld(x=X0 + points[0][0], y=Y0 + points[0][1], heading=heading,
                      obstacles=tuple(_shifted(o) for o in obstacles),
                      width_yards=W, height_yards=H)
    monkeypatch.setattr(travel_module, "time", SimTime(world))

    def route(pts):
        return Path(PathStatus.COMPLETE,
                    tuple(world.world(X0 + x, Y0 + y, ELWYNN, 80.0) for x, y in pts))

    def replan(here):                       # the mesh does not know the obstacle
        return route([(here[0] * W - X0, here[1] * H - Y0), *points[1:]])

    travel = Travel(hid=SimHid(world, humaniser), bounds=ELWYNN, read_pos=world.map_position)
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


def test_a_fence_met_nearly_square_is_unstuck_not_pressed_into(monkeypatch):
    """Two degrees off square, a character slides along a fence at a quarter of a yard a
    second: not frozen, and far too slow to take a heading from. Before the stuck test
    asked for a heading's worth of travel, this walk pressed into the fence for its whole
    two minutes without a single stuck event."""
    result, _ = walk([(0, 0), (0, -40)], FENCE, -math.pi / 2 + math.radians(2),
                     monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED and result.stuck_events >= 1
    assert result.elapsed_s < 60.0


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize(("points", "obstacles", "heading"), [
    ([(0, 0), (0, -40)], FENCE, -math.pi / 2),
    ([(-10, 1.0), (12, 1.0), (14, 8.0)], WEDGE, 0.0),
], ids=["long fence", "tree beside a wall"])
def test_with_drawn_holds_and_waits_every_spot_is_still_learned_and_rounded(
        points, obstacles, heading, seed, monkeypatch):
    """In play every hold and loop wait is drawn (`Humaniser`), so a walk never repeats
    one exact trajectory. Simulated over forty draws each, every first trip arrived and
    learned, and every second trip arrived faster with no stuck event."""
    memory, humaniser = RouteMemory(), Humaniser(rng=random.Random(seed))
    first, _ = walk(points, obstacles, heading, memory=memory, humaniser=humaniser,
                    monkeypatch=monkeypatch)
    assert first.outcome is Outcome.ARRIVED and memory.passages
    second, _ = walk(points, obstacles, heading, memory=memory, humaniser=humaniser,
                     monkeypatch=monkeypatch)
    assert second.outcome is Outcome.ARRIVED and second.stuck_events == 0
    assert second.elapsed_s < first.elapsed_s


def test_open_ground_is_walked_without_any_recovery(monkeypatch):
    result, _ = walk([(0, 0), (40, 0), (40, 20)], [], 0.0, memory=RouteMemory(),
                     monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED
    assert result.stuck_events == 0 and result.detours == 0


def test_a_walk_that_has_spent_its_re_plans_still_rounds_the_next_obstacle(monkeypatch):
    """Measured 23 September (run 20260923T184413-a386ff): a 366-yard walk to Echo Ridge
    Mine spent its re-plans on the Abbey's corners, then met a pit prop at the mine's mouth
    with none left, and stopped 95 yards short without trying the wall heuristic once."""
    walls = [Segment(-6, -20 - 40 * k, 6, -20 - 40 * k) for k in range(4)]
    result, _ = walk([(0, 0), (0, -40), (0, -80), (0, -120), (0, -160)], walls,
                     -math.pi / 2, monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED, result.detail
    assert result.stuck_events >= len(walls), "a wall was never met"


@pytest.mark.parametrize("yards", [4.0, 6.0, 8.0])
@pytest.mark.parametrize("bearing", [60, 90, 270, 300])
def test_a_point_close_and_to_one_side_is_reached_not_circled(yards, bearing, monkeypatch):
    """Turned while walking, each correction is an arc of three yards before the new
    heading can be measured, and near the point the arcs closed into a circle: a point
    four to eight yards off at sixty to ninety degrees was circled until the walk timed
    out, 16 walks of 72, and a learned passage's point was circled for 295 s."""
    world = WalkWorld(x=X0, y=Y0, heading=0.0, width_yards=W, height_yards=H)
    monkeypatch.setattr(travel_module, "time", SimTime(world))
    target = ((X0 + yards * math.cos(math.radians(bearing))) / W,
              (Y0 + yards * math.sin(math.radians(bearing))) / H)
    travel = Travel(hid=SimHid(world), bounds=ELWYNN, read_pos=world.map_position,
                    arrival_yards=3.0)
    result = travel.to(target, timeout_s=60.0)
    assert result.outcome is Outcome.ARRIVED and result.elapsed_s < 8.0


def test_a_walk_that_passes_its_destination_early_arrives_there(monkeypatch):
    """Inside the Lion's Pride Inn a character passed 3.9 yards from William Pestle and
    walked two more minutes of route round the building; the hand-in ran out of time
    (session 108). Within arrival of the destination on any leg is arrival."""
    points = [(0, 0), (0, -30), (20, -30), (20, -10), (2, -18)]
    result, _ = walk(points, [], -math.pi / 2, monkeypatch=monkeypatch)
    assert result.outcome is Outcome.ARRIVED
    assert result.elapsed_s < 8.0, "walked the whole loop round to where it had been"
