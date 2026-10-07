"""Somewhere no walk leads out of (V384): a plan is begun on the floor the character is on, and
a walk whose plan ends far short goes home by hearthstone where home is a walk from there.

At the foot of Thunder Bluff's lift by its graveyard hive-609, tracked on the ground at 70, was
planned to Tand's on the mesa from the mesa 71 yards over it, and walked up the cliff by the
hive's straight moves (6 Oct 08:49). On the mesas five of the 27 bots stuck there were bound
elsewhere, and their walks to Mulgore's and the Barrens' steps ended 140 to 2,300 yards short
for hours.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from test_live_body import body

from jev.clients.hearth import Hearthed
from jev.guide.coords import bounds_by_radio_id, map_to_world
from jev.guide.path import Path, PathStatus
from jev.perceive.radio_frame import zone_id
from jev.run.client import Client
from jev.world.home import save_home

ZONES = bounds_by_radio_id("data/zones-tbc-243.json")
MULGORE = ZONES[zone_id("Mulgore")]

FOOT = (-1039.4, -18.6)                    # the deck at the lift's foot, the ground's floor
TAND = (-1088.2, 4.7, 140.7)               # a merchant on the mesa


class Stack:
    """The navmesh at the lift's foot: from a start at the mesa's height the snap is onto the
    mesa over the spot, and that plan is complete; from the ground's it ends at the foot."""

    def path(self, map_id, start, end):
        if start[2] > 100:
            return Path(PathStatus.COMPLETE, ((start[0], start[1], 141.1), end), "mmap")
        if start[2] > 40:
            return Path(PathStatus.PARTIAL, ((start[0], start[1], 69.8),
                                             (start[0] - 2.0, start[1], 69.8)), "mmap")
        return Path(PathStatus.NOPATH, detail="nothing under here")


def client():
    c = Client(hwnd=1, hid=Mock(), cap=Mock(), origin=(0, 0), size=(1600, 900))
    c.bounds, c.query = MULGORE, Stack()
    return c


def test_a_plan_is_begun_on_the_floor_the_character_is_on():
    c = client()
    c._ground = (*FOOT, 69.8)
    planned = c._plan(FOOT, TAND)
    assert planned.status is PathStatus.PARTIAL and planned.points[0][2] == 69.8, \
        "not the mesa's plan, 71 yards over the ground it was tracked on"


def test_with_the_floor_unknown_or_none_under_it_any_height_plans_as_before():
    c = client()
    assert c._plan(FOOT, TAND).status is PathStatus.COMPLETE, "the height unknown"
    c._ground = (*FOOT, 20.0)                   # tracked on a floor the mesh has none of here
    assert c._plan(FOOT, TAND).status is PathStatus.COMPLETE


class Ground:
    """A planner where nothing on the mesa (height over 100) walks to the ground."""

    def path(self, map_id, start, end):
        if (start[2] > 100) != (end[2] > 100):
            return Path(PathStatus.PARTIAL, (start, (start[0], start[1] - 3.0, start[2])), "mmap")
        return Path(PathStatus.COMPLETE, (start, end), "mmap")


STEP = (900.0, 900.0, 5.0)                  # the step's place, far below and off the mesa
SHORT = Path(PathStatus.PARTIAL, ((50.0, 50.0, 130.0), (52.0, 50.0, 130.0)), "mmap")


def stuck_body(tmp_path, home):
    b = body()
    b.home_memory = tmp_path / "character.home.json"
    if home is not None:
        save_home(b.home_memory, home, name="the inn")
    b.client.read = lambda: {"vitals.hp": 1.0, "vitals.combat": False, "pos.indoors": False}
    b.client.query = Ground()
    b.client.last_plan = SHORT
    b._position = lambda: (0.5, 0.5)
    b.homes = []
    b.hearth = SimpleNamespace(run=lambda: b.homes.append(1) or Hearthed.HOME, detail="")
    lines = []
    b.say = lines.append
    b.lines = lines
    return b


def test_no_way_from_here_goes_home_by_hearthstone_and_walks_on_from_there(tmp_path):
    b = stuck_body(tmp_path, (1000.0, 1000.0, 5.0))
    walks = iter([False, True])
    b.client.approach = Mock(side_effect=lambda world, timeout_s=0: next(walks))
    assert b._approach(STEP) is True
    assert b.homes == [1] and b.client.approach.call_count == 2
    assert any("no walk from here to (900, 900): its plan ended" in line
               and "home is a 141-yard walk from it: hearthstone home" in line
               for line in b.lines), b.lines


def test_home_is_not_gone_to_where_no_walk_leads_from_it_or_the_plan_nearly_got_there(tmp_path):
    here = map_to_world(0.5, 0.5, body().client.bounds)
    for home, plan in (((1000.0, 1000.0, 129.0), SHORT),      # home on the mesa too
                       ((1000.0, 1000.0, 5.0), Path(PathStatus.PARTIAL,
                                                    ((50.0, 50.0, 5.0), (880.0, 880.0, 5.0)))),
                       ((here[0] + 10.0, here[1], 5.0), SHORT),  # home is here
                       (None, SHORT)):
        b = stuck_body(tmp_path, home)
        b.client.last_plan = plan
        b.client.approach = Mock(return_value=False)
        assert b._approach(STEP) is False
        assert b.homes == [] and b.client.approach.call_count == 1
        (tmp_path / "character.home.json").unlink(missing_ok=True)


def test_a_hearthstone_arrival_at_the_inn_remembered_keeps_its_height(tmp_path):
    b = body()
    b.home_memory = tmp_path / "character.home.json"
    here = map_to_world(0.5, 0.5, b.client.bounds)
    save_home(b.home_memory, (here[0] + 3.0, here[1], 129.3), name="Innkeeper Pala")
    b.hearth = SimpleNamespace(run=lambda: Hearthed.HOME, detail="")
    b._position = lambda: (0.5, 0.5)
    b._save_purse = lambda: None
    assert b._go_home() is Hearthed.HOME
    from jev.world.home import load_home
    assert load_home(b.home_memory)[2] == 129.3
