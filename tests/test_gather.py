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


def test_a_body_that_gives_a_quest_is_opened_by_the_name_its_tooltip_gives():
    body = World(name=name_id("A half-eaten body"))
    original = body.click

    def click(x, y, right=False):
        original(x, y, right)
        if right and body._here((x, y)):
            body.values["ui.quest_frame"] = True
        return True

    body.click = click
    assert picker(body).open(name_id("A half-eaten body")) is True
    assert body.clicks == [(880, 585, True)]
    elsewhere = World(name=name_id("Rolf's corpse"))
    assert picker(elsewhere).open(name_id("A half-eaten body")) is False
    assert elsewhere.clicks == []


class FarWorld(World):
    """The crate answers only once the character has stepped in `reach` times."""

    def __init__(self, reach=1, **kw):
        super().__init__(**kw)
        self.reach, self.steps, self.turns = reach, [], []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))
        if right and self._here((x, y)) and len(self.steps) >= self.reach:
            self.have += 1
        return True

    def turn_toward(self, offset):
        self.turns.append(round(offset, 3))
        return True

    def hold(self, key, seconds, **_):
        self.steps.append(key)
        return True


@pytest.fixture
def answer_fast(monkeypatch):
    monkeypatch.setattr(module, "OPEN_S", 0.2)
    monkeypatch.setattr(module, "OPEN_ANSWER_S", 0.03)


def test_a_click_nothing_answers_steps_in_toward_the_object_and_clicks_again(answer_fast):
    """The first live crate of Milly's Harvest answered nothing for 8 s: out of reach
    (run 20260924T062715-7433c3)."""
    world = FarWorld(reach=1)
    g = picker(world)
    assert g.pick(CRATE, world.progress) is Gathered.TOOK
    assert len(world.clicks) == 2 and world.steps == ["w"]
    assert world.turns == [0.05], "turned toward the crate right of centre"


def test_an_object_that_never_answers_is_nothing_after_the_steps(answer_fast):
    world = FarWorld(reach=99)
    g = picker(world)
    assert g.pick(CRATE, world.progress) is Gathered.NOTHING
    assert len(world.clicks) == module.REACH_STEPS + 1
    assert world.steps == ["w"] * (module.REACH_STEPS + 1)
    assert "answered none" in g.detail


def test_a_click_that_is_answered_is_waited_on_not_stepped_from(answer_fast):
    world = FarWorld(reach=99)
    original = world.click

    def click(x, y, right=False):
        original(x, y, right)
        world.values["bars.casting"] = True      # an opening cast: the server answered
        return True

    world.click = click
    assert picker(world).pick(CRATE, world.progress) is Gathered.NOTHING
    assert world.steps == [] and len(world.clicks) == 1
