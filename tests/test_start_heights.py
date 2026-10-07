"""A plan's first floor where no height is tracked (V406): the landing points near the start,
then every floor under it.

The strip paints no height. After a hearth, a release, a flight, a teleport or at a session's
start a first plan began at the destination's height and 60 yards either side, and from
Darnassus, 1,378 yards up the tree, every one of those to Vesprystus lies under the tree: the
hive's flight check was "nopath" twice where a plan from the character's floor walks through
the portal (6 Oct, 21:30). The hive gave its plans the server's height; the live client has
the world database's landing points and the navmesh's floors.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from jev.guide.path import Path, PathStatus
from jev.perceive.radio_frame import RadioReading
from jev.run.client import START_HEIGHTS, Client, surfaces_wide
from jev.world.landings import near, world_points
from jev.world.state_v1 import SenseFault

# Tarion on Darnassus's upper level at the flight check of 6 Oct, 21:30; Vesprystus in
# Rut'theran, 1,355 yards under it; where the flight back landed him.
TARION = (10078.7, 2062.9, 1378.0)
VESPRYSTUS = (8640.58, 841.12, 23.35)
LANDED = (8643.6, 841.0, 23.3)
BOUNDS = SimpleNamespace(map_id=1, degenerate=False, left=3833.33, right=-1066.67,
                         top=11516.66, bottom=8250.0)


class Mesh:
    """A navmesh in miniature: the floors over each spot, a snap answering the floor nearest
    the asked height within 200 yards up or down (jevpath's `EXTENT_Y`), and a walk complete
    from the floors in `walks`, else partial where it starts."""

    def __init__(self, floors, walks=()):
        self.floors = floors
        self.walks = set(walks)
        self.asked = []

    def _floor(self, x, y, z):
        heights = [f for (fx, fy), fs in self.floors.items() if math.dist((fx, fy), (x, y)) < 1.0
                   for f in fs if abs(f - z) <= 200.0]
        return min(heights, key=lambda f: abs(f - z)) if heights else None

    def path(self, map_id, start, end):
        self.asked.append(tuple(start))
        floor = self._floor(*start)
        if floor is None:
            return Path(PathStatus.NOPATH, detail="no floor near the start")
        here = (start[0], start[1], floor)
        if tuple(start) == tuple(end):
            return Path(PathStatus.COMPLETE, (here,), "mmap")
        if any(math.dist(here, w) < 1.0 for w in self.walks):
            return Path(PathStatus.COMPLETE, (here, tuple(end)), "mmap")
        return Path(PathStatus.PARTIAL, (here, (start[0] + 1.0, start[1], floor)), "mmap")

    def close(self):
        pass


def client(mesh, *, landings=(), ground=None, indoors=None):
    c = Client(hwnd=0, hid=SimpleNamespace(), cap=None, origin=(0, 0), size=(1600, 900))
    c.bounds, c.query, c._ground = BOUNDS, mesh, ground
    c.landings = lambda: list(landings)
    if indoors is not None:
        import time

        c._last_reading = (time.monotonic(), time.time(),
                           RadioReading({"pos.indoors": indoors}, True, SenseFault.NONE))
    return c


def test_with_no_floor_tracked_every_floor_under_the_start_is_tried():
    """The flight check's walk from Darnassus: the destination's heights find no floor at
    the start, and the floors under it do."""
    mesh = Mesh({TARION[:2]: [1378.0]}, walks=[TARION])
    plan = client(mesh)._plan(TARION[:2], VESPRYSTUS)
    assert plan.status is PathStatus.COMPLETE and plan.points[0][2] == pytest.approx(1378.0)
    guesses = len(START_HEIGHTS)
    assert all(z < 300 for _, _, z in mesh.asked[:guesses]), "the destination's heights first"


def test_a_landing_point_near_gives_the_plan_its_first_floor():
    """Landed at Rut'theran, the walk up to Darnassus: the flight node's height, not the
    destination's 1,355 yards over it, and nothing probed."""
    mesh = Mesh({LANDED[:2]: [23.3]}, walks=[LANDED])
    c = client(mesh, landings=[VESPRYSTUS, (8000.0, 800.0, 400.0)])
    plan = c._plan(LANDED[:2], TARION)
    assert plan.status is PathStatus.COMPLETE
    assert mesh.asked[0][2] == pytest.approx(VESPRYSTUS[2]) and len(mesh.asked) == 1


def test_a_floor_tracked_here_plans_as_before_and_probes_nothing():
    """With the height tracked (or, in the hive with its crutch, the server's), nothing new is
    asked: the tracked floor, then the destination's heights."""
    mesh = Mesh({TARION[:2]: [1378.0]})
    c = client(mesh, landings=[(TARION[0] + 5, TARION[1], 1378.0)],
               ground=(TARION[0], TARION[1], 1378.0))
    c._plan(TARION[:2], VESPRYSTUS)
    assert [z for _, _, z in mesh.asked] == [1378.0, *(VESPRYSTUS[2] + dz for dz in START_HEIGHTS)]


@pytest.mark.parametrize(("indoors", "first"), [(False, 60.0), (None, 60.0), (True, 10.0)])
def test_outdoors_the_top_floor_is_tried_first_indoors_the_lowest(indoors, first):
    """A hill over a cave, both with a way to the destination: the character outdoors stands
    on the hill, indoors in the cave."""
    spot = (1000.0, 1000.0)
    mesh = Mesh({spot: [10.0, 60.0]}, walks=[(*spot, 10.0), (*spot, 60.0)])
    plan = client(mesh, indoors=indoors)._plan(spot, (1500.0, 1500.0, 900.0))
    assert plan.points[0][2] == pytest.approx(first)


def test_every_floor_under_a_spot_is_found_from_under_the_undercity_to_teldrassils_crown():
    mesh = Mesh({TARION[:2]: [-45.0, 1290.0, 1378.0, 1386.0]})
    assert surfaces_wide(mesh, 1, *TARION[:2]) == pytest.approx([-45.0, 1290.0, 1378.0, 1386.0])


def test_the_world_databases_landing_points_hold_rutherans_node_not_darnassuss_crown():
    points = world_points(1)
    assert near(points, *VESPRYSTUS[:2]) == pytest.approx([VESPRYSTUS[2]], abs=2.0)
    assert near(points, *TARION[:2]) == []
    graveyards = world_points(0)
    assert any(math.dist(p[:2], (1832.0, 220.0)) < 50 for p in graveyards), \
        "the Ruins of Lordaeron's graveyard"
