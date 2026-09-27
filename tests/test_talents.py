"""Spending talent points on the class's build (V261)."""

from __future__ import annotations

import pytest

from jev.clients.talents import Spent, TalentDesk, build_for, next_point

W, H = 1600, 900
TAB_XY = {1: (0.10, 0.13), 2: (0.14, 0.13), 3: (0.18, 0.13)}
MAGE = [{"name": "Improved Fireball", "tab": 2, "tier": 0, "column": 1, "rank": r}
        for r in range(1, 6)] + [{"name": "Ignite", "tab": 2, "tier": 1, "column": 0, "rank": 1}]


class _UI:
    """The stock talent frame as the strip shows it: the census one talent a read."""

    def __init__(self, points=1, shown=2, schema=19, combat=False):
        self.talents = [
            {"tab": 1, "tier": 0, "column": 1, "rank": 0, "xy": (0.08, 0.60)},
            {"tab": 2, "tier": 0, "column": 1, "rank": 0, "xy": (0.09, 0.60)},
            {"tab": 2, "tier": 0, "column": 2, "rank": 0, "xy": (0.13, 0.60)},
            {"tab": 2, "tier": 1, "column": 0, "rank": 0, "xy": (0.05, 0.66)},
            {"tab": 3, "tier": 0, "column": 0, "rank": 0, "xy": (0.05, 0.60)},
        ]
        self.points, self.shown, self.schema, self.combat = points, shown, schema, combat
        self.open = False
        self.cursor = 0
        self.taps, self.clicks = [], []

    def read(self):
        t = self.talents[self.cursor % len(self.talents)]
        self.cursor += 1
        v = {"schema": self.schema, "char.talent_points": self.points, "ui.talents": self.open,
             "vitals.combat": self.combat, "vitals.dead": False, "vitals.ghost": False,
             "talents.tab": t["tab"], "talents.tier": t["tier"], "talents.column": t["column"],
             "talents.rank": t["rank"], "talents.shown": False, "talents.x": None,
             "talents.y": None}
        if self.open:
            v["talents.shown"] = t["tab"] == self.shown
            v["talents.x"], v["talents.y"] = t["xy"] if t["tab"] == self.shown else TAB_XY[t["tab"]]
        return v

    def tap(self, key):
        self.taps.append(key)
        self.open = not self.open
        return True

    def click(self, x, y):
        self.clicks.append((x, y))
        for tab, (tx, ty) in TAB_XY.items():
            if (x, y) == (round(tx * W), round(ty * H)):
                self.shown = tab
                return True
        for t in self.talents:
            if t["tab"] == self.shown and (x, y) == (round(t["xy"][0] * W), round(t["xy"][1] * H)):
                if self.points > 0:
                    t["rank"] += 1
                    self.points -= 1
        return True


def _desk(ui):
    clock = iter(i * 0.05 for i in range(100000))
    return TalentDesk(ui, ui.read, window_size=(W, H), clock=lambda: next(clock),
                      sleep=lambda s: None)


def test_the_builds_next_point_goes_to_its_talent_and_the_frame_is_shut_after():
    """V261: the paladin reached 15.87 with six talent points unspent."""
    ui = _UI(points=1)
    desk = _desk(ui)
    assert desk.run(MAGE) is Spent.DONE, desk.detail
    assert ui.talents[1]["rank"] == 1 and ui.points == 0
    assert desk.learned == ["Improved Fireball 1"]
    assert ui.taps == ["n", "n"] and ui.open is False, "opened, then shut"


def test_another_tab_showing_is_clicked_away_first():
    ui = _UI(points=2, shown=1)
    desk = _desk(ui)
    assert desk.run(MAGE) is Spent.DONE, desk.detail
    assert ui.shown == 2 and ui.talents[1]["rank"] == 2
    assert ui.clicks[0] == (round(TAB_XY[2][0] * W), round(TAB_XY[2][1] * H)), "the Fire tab"


def test_nothing_is_opened_without_points_or_a_schema_19_strip():
    ui = _UI(points=0)
    assert _desk(ui).run(MAGE) is Spent.NOTHING and ui.taps == []
    old = _UI(points=3, schema=18)
    assert _desk(old).run(MAGE) is Spent.OLD_STRIP and old.taps == []


def test_a_fight_stops_the_visit_and_the_frame_is_shut():
    ui = _UI(points=1)
    desk = _desk(ui)
    ui.combat = True
    assert desk.run(MAGE) is Spent.INTERRUPTED
    assert ui.open is False


def test_the_next_point_is_the_first_the_census_reads_short():
    assert next_point(MAGE, {(2, 0, 1): 5, (2, 1, 0): 0})["name"] == "Ignite"
    assert next_point(MAGE, {(2, 0, 1): 5, (2, 1, 0): 1}) is None, "the build complete"
    assert next_point(MAGE, {(2, 0, 1): 3})["rank"] == 4
    assert next_point(MAGE, {}) is None, "a talent not read is not assumed"


@pytest.mark.parametrize("class_id", [2, 8])
def test_the_generated_builds_climb_their_trees_in_order(class_id):
    build = build_for(class_id)
    assert len(build) >= 20
    spent = {}
    for point in build:
        assert spent.get(point["tab"], 0) >= 5 * point["tier"], point
        spent[point["tab"]] = spent.get(point["tab"], 0) + 1
