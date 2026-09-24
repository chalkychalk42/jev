"""World objects are taken only where a fresh hover names them, and judged by the counter."""

from __future__ import annotations

import pytest

from jev.clients import gather as module
from jev.clients.gather import Gather, Gathered, search_points
from jev.clients.targeting import HoverCode, HoverResult, PaintCode, PaintResult
from jev.perceive.radio_frame import name_id

CRATE = name_id("Milly's Harvest")


class World:
    """A client with one crate at one screen point; a right-click there takes it."""

    def __init__(self, at=(0.55, 0.65), *, gives=True, name=CRATE, unit=False):
        self.at, self.gives, self.name, self.unit = at, gives, name, unit
        self.have, self.need = 3, 8
        self.values = {"bags.free": 5, "vitals.combat": False, "ui.modal": False,
                       "ui.loot": False}
        self.hovered, self.clicks = [], []

    def _here(self, point):
        return (round(point[0] / 1600, 2), round(point[1] / 900, 2)) == self.at

    # Targeting.probe
    def probe(self, point, *, require_target=True):
        self.hovered.append(point)
        over = self._here(point)
        after = {**self.values, "cursor.world": True, "cursor.has": over and self.unit,
                 "cursor.object_id": self.name if over and not self.unit else None}
        return HoverResult(HoverCode.GROUND, point, self.values, after, "")

    def wait_for_paint(self):
        return PaintResult(PaintCode.FRESH, self.values, dict(self.values), "fresh")

    # Hid
    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))
        if right and self.gives and self._here((x, y)):
            self.have += 1
        return True

    def tap(self, key):
        return True

    def read(self):
        return dict(self.values)

    def progress(self):
        return self.have, self.need


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(module, "OPEN_S", 0.05)
    monkeypatch.setattr(module, "OPEN_LOOK_S", 0.01)


def picker(world):
    return Gather(hid=world, read=world.read, targeting=world, window_size=(1600, 900))


def test_the_search_starts_ahead_of_the_feet_and_rings_outward():
    points = search_points()
    assert points[0] == module.SEARCH_ORIGIN
    assert len(points) == 49 and len(set(points)) == 49
    distances = [round(max(abs(x - points[0][0]), abs(y - points[0][1])) / 0.05)
                 for x, y in points]
    assert distances == sorted(distances), "ring by ring, nearest first"


def test_an_object_named_by_a_fresh_hover_is_right_clicked_and_counted():
    world = World()
    g = picker(world)
    assert g.pick(CRATE, world.progress) is Gathered.TOOK
    assert world.clicks == [(880, 585, True)] and "objective 3 -> 4" in g.detail


@pytest.mark.parametrize("change", [{"name": name_id("Bundle of Wood")}, {"unit": True}])
def test_another_object_or_a_unit_is_never_clicked(change):
    world = World(**change)
    assert picker(world).pick(CRATE, world.progress) is Gathered.NOT_HERE
    assert world.clicks == []
    assert len(world.hovered) == 49


def test_a_click_that_changes_nothing_is_nothing_not_a_take():
    world = World(gives=False)
    assert picker(world).pick(CRATE, world.progress) is Gathered.NOTHING


def test_full_bags_or_a_fight_reach_for_nothing():
    world = World()
    world.values["bags.free"] = 0
    assert picker(world).pick(CRATE, world.progress) is Gathered.BAGS_FULL
    world.values.update({"bags.free": 5, "vitals.combat": True})
    assert picker(world).pick(CRATE, world.progress) is Gathered.INTERRUPTED
    assert world.hovered == [] and world.clicks == []
