"""Training at a class trainer and putting spells on the bar, against responsive fakes."""

from __future__ import annotations

from jev.clients.spellbook import Placed, Spellbook
from jev.clients.trainer import Trained, TrainerDesk
from jev.world.training import Placement


class Desk:
    """A trainer window: Train is painted while an affordable service is selected."""

    def __init__(self, prices=(10, 100, 100), money=250):
        self.now = 0.0
        self.prices = list(prices)
        self.clicks, self.keys = [], []
        self.v = {"ui.trainer": False, "ui.modal": False, "vitals.combat": False,
                  "vitals.dead": False, "vitals.ghost": False, "bags.money_copper": money}
        self._paint()

    def _paint(self):
        affordable = self.v["ui.trainer"] and self.prices and self.prices[0] <= self.v["bags.money_copper"]
        self.v["ui.advance_x"], self.v["ui.advance_y"] = (0.2, 0.7) if affordable else (None, None)

    def read(self):
        return dict(self.v)

    def visit(self):
        self.v["ui.trainer"] = True
        self._paint()
        return True

    def sleep(self, seconds):
        self.now += seconds

    def click(self, x, y, right=False):
        self.clicks.append((x, y))
        price = self.prices.pop(0)
        self.v["bags.money_copper"] -= price
        self._paint()
        return True

    def tap(self, key):
        self.keys.append(key)
        self.v["ui.trainer"] = False
        return True

    def desk(self):
        return TrainerDesk(self, self.read, self.visit, clock=lambda: self.now, sleep=self.sleep)


def test_train_is_pressed_while_it_is_painted_and_each_press_is_money_spent():
    desk = Desk()
    trained = desk.desk()
    assert trained.run() is Trained.DONE
    assert (trained.bought, trained.spent) == (3, 210)
    assert desk.keys == ["esc"]                          # the window closed after


def test_training_stops_where_the_purse_does():
    desk = Desk(prices=(10, 100, 100), money=150)
    trained = desk.desk()
    assert trained.run() is Trained.DONE
    assert trained.bought == 2 and desk.v["bags.money_copper"] == 40


def test_nothing_affordable_is_nothing_bought():
    desk = Desk(prices=(500,), money=100)
    trained = desk.desk()
    assert trained.run() is Trained.NOTHING
    assert desk.clicks == []


def test_a_trainer_that_never_opens_is_reported():
    desk = Desk()
    trained = TrainerDesk(desk, desk.read, lambda: False, clock=lambda: desk.now,
                          sleep=desk.sleep)
    assert trained.run() is Trained.NO_TRAINER


def test_combat_stops_training():
    desk = Desk()
    desk.v["vitals.combat"] = True
    assert desk.desk().run() is Trained.INTERRUPTED


class Book:
    """A spellbook of two tabs and a bar, painting one census entry of each per read."""

    def __init__(self):
        self.now = 0.0
        self.open = False
        self.tab = 1
        self.holding = None
        self.bar = {1: 6603, 2: 20154, 3: 635, **{s: 0 for s in range(4, 11)}, 11: None, 12: None}
        # (spell id, tab)
        self.entries = [(6603, 1), (20154, 2), (635, 2), (639, 2), (465, 2)]
        self.i = self.slot = 0
        self.events, self.keys = [], []
        self.pos = (0, 0)
        self.down = None
        self.drops = True

    def read(self):
        self.i = self.i % len(self.entries) + 1
        self.slot = self.slot % 12 + 1
        spell, tab = self.entries[self.i - 1]
        v = {"vitals.combat": False, "vitals.dead": False, "vitals.ghost": False,
             "ui.spellbook": self.open, "cursor.holding": self.holding is not None,
             "spells.index": self.i, "spells.id": spell, "spells.total": len(self.entries),
             "bars.slot": self.slot, "bars.slot_spell": self.bar[self.slot]}
        # The stock bar hides an empty button until something is being dragged.
        dragging = self.down is not None and self.pos != self.down
        if self.bar[self.slot] != 0 or dragging:
            v["bars.slot_x"], v["bars.slot_y"] = 0.3 + self.slot / 100, 0.95
        if self.open and tab == self.tab:
            v["spells.x"], v["spells.y"] = 0.1, 0.1 + self.i / 20
        elif self.open:
            v["spells.go_x"], v["spells.go_y"] = 0.25, 0.1 + tab / 10
        return v

    def sleep(self, seconds):
        self.now += seconds

    def tap(self, key):
        self.keys.append(key)
        if key == "p":
            self.open = not self.open
        return True

    def click(self, x, y, right=False):
        self.events.append(("click", x, y))
        if x == 400 and self.open:                      # a tab button (0.25 of 1600)
            self.tab = 2
        elif self.holding is not None:                  # open world drops what is held
            self.holding = None
        return True

    def move_to(self, x, y, steps=0):
        self.pos = (x, y)
        return True

    def button(self, down, right=False):
        if down:
            self.down = self.pos
            return True
        start, end = self.down, self.pos
        self.down = None
        self.events.append(("drag", start, end))
        if end == (1056, 288) or not self.drops:        # let go on open world: dropped
            return True
        index = round(((start[1] / 900) - 0.1) * 20)
        slot = round((end[0] / 1600 - 0.3) * 100)
        spell = self.entries[index - 1][0]
        old = self.bar[slot]
        self.bar[slot] = spell
        if old:
            self.holding = old
        return True

    def book(self):
        return Spellbook(self, self.read, clock=lambda: self.now, sleep=self.sleep)


def test_a_new_rank_is_dragged_over_the_old_and_the_old_one_dropped():
    book = Book()
    placer = book.book()
    result = placer.place([Placement(639, 3, 635), Placement(465, 4)])
    assert result is Placed.DONE, placer.detail
    assert book.bar[3] == 639 and book.bar[4] == 465
    assert book.holding is None
    assert book.tab == 2                                # the tab was clicked to reach them
    assert book.keys == ["p", "p"]                      # opened, then closed
    assert [e[0] for e in book.events].count("drag") == 2


def test_nothing_to_place_opens_nothing():
    book = Book()
    assert book.book().place([]) is Placed.NOTHING
    assert book.keys == []


def test_a_drop_that_does_not_land_stops_the_placing():
    book = Book()
    book.drops = False                                 # picked up, and the drop never lands
    placer = book.book()
    assert placer.place([Placement(465, 4)]) is Placed.NOT_PLACED
    assert book.bar[4] == 0 and placer.placed == []


def test_an_addon_without_the_censuses_is_blind():
    book = Book()
    placer = Spellbook(book, lambda: {"vitals.combat": False}, clock=lambda: book.now,
                       sleep=book.sleep)
    assert placer.place([Placement(465, 4)]) is Placed.BLIND


def test_something_already_on_the_cursor_is_never_dropped():
    """An item dropped on open world asks to be destroyed: not this routine's to drop."""
    book = Book()
    book.holding = 2070
    placer = book.book()
    assert placer.place([Placement(465, 4)]) is Placed.HOLDING
    assert book.events == [] and book.holding == 2070 and book.keys == []


def test_an_empty_slot_is_found_while_the_spell_is_being_dragged():
    """The stock bar shows an empty button only during a drag: the first live placement
    waited for slot 4's button before picking anything up, and timed out."""
    book = Book()
    placer = book.book()
    assert placer.place([Placement(465, 4)]) is Placed.DONE, placer.detail
    assert book.bar[4] == 465


def test_a_drag_that_cannot_find_its_slot_lets_go_on_open_world():
    book = Book()
    book.bar[4] = None                                 # an item there: never painted
    real = book.read

    def read():
        v = real()
        v.pop("bars.slot_x", None)
        v.pop("bars.slot_y", None)
        return v

    placer = Spellbook(book, read, clock=lambda: book.now, sleep=book.sleep)
    assert placer.place([Placement(465, 4)]) is Placed.TIMEOUT
    drag = [e for e in book.events if e[0] == "drag"]
    assert drag and drag[-1][2] == (1056, 288), "let go somewhere a spell could land"


def test_a_spell_the_spellbook_does_not_hold_is_given_up_once_every_entry_is_seen():
    book = Book()
    placer = book.book()
    assert placer.place([Placement(853, 5)]) is Placed.NO_BUTTON
    assert "not in the spellbook" in placer.detail
    assert book.now < 2.0, "waited out the whole deadline for an entry that is not there"
