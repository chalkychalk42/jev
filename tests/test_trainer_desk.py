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


def test_a_strip_without_the_trainer_list_presses_train_as_before():
    """Schema 17 paints no list: told the trainer, the desk still buys the window's own
    selection, press after press (V237 keeps today's behaviour there)."""
    from jev.world import training

    desk = Desk()
    desk.v["schema"] = 17
    wilhelm = next(t for t in training.trainers(2, 1, 0) if t.name == "Brother Wilhelm")
    trained = TrainerDesk(desk, desk.read, desk.visit, clock=lambda: desk.now,
                          sleep=desk.sleep, trainer=wilhelm, known={6603})
    assert trained.run() is Trained.DONE
    assert (trained.bought, trained.spent) == (3, 210) and trained.learned == []


# --- schema 18: the list painted, bought by value (V237) ---------------------------------

import math  # noqa: E402
import re  # noqa: E402

from jev.perceive.radio_frame import name_id  # noqa: E402
from jev.world import training  # noqa: E402
from tools.gen_addon_fields import SOURCE  # noqa: E402

# Which row each paint describes: Helpers.lua's own constants (tests/test_addon_runs.py runs
# the addon against the same rule).
_HELPERS = (SOURCE / "Helpers.lua").read_text(encoding="utf-8")
GOLDEN = float(re.search(r"^local GOLDEN = ([0-9.]+)$", _HELPERS, re.MULTILINE).group(1))
_SHARE = re.search(r"^local TRAINER_SHORT_SHARE = (\d+) / (\d+)$", _HELPERS, re.MULTILINE)
SHORT_SHARE = int(_SHARE.group(1)) / int(_SHARE.group(2))

ZALDIMAR = next(t for t in training.trainers(8, 1, 0) if t.name == "Zaldimar Wefhellt")
# The mage as it stood on 26 September: Arcane Intellect, Conjure Water, Conjure Food and
# Fire Blast bought one a visit in the window's order; no Frostbolt, no Fireball rank 2.
MAGE_KNOWN = frozenset({6603, 133, 168, 1459, 5504, 587, 2136})
MAGE_BAR = {1: 6603, 2: 133, 3: 168, 4: 1459, 5: 5504, 6: 587, 7: 2136, 8: 0, 9: 0, 10: 0,
            11: None, 12: None}
ROW_X, ROW_Y, ROW_STEP = 168, 180, 18          # the list's first row button, in pixels
UP, DOWN, TRAIN = (480, 180), (480, 540), (224, 738)
KINDS = {"header": 0, "available": 1, "unavailable": 2, "used": 3}


def _service(spell_id: int) -> dict:
    facts = training.spell(spell_id)
    offer = next(o for o in ZALDIMAR.offers if o.spell_id == spell_id)
    before = next((o.spell_id for o in ZALDIMAR.offers
                   if (f := training.spell(o.spell_id)) is not None and f.name == facts.name
                   and f.rank == facts.rank - 1), None)
    return {"name": facts.name, "rank": facts.rank, "cost": offer.cost, "level": offer.level,
            "spell": spell_id, "needs": before}


def _header(name: str) -> dict:
    return {"name": name, "rank": 0, "header": True, "expanded": True}


# Zaldimar Wefhellt's list for this mage at level 8, each skill line sorted by name, as the
# stock window lists it, with the used filter off.
LEVEL_8_LIST = (
    _header("Arcane"), *(_service(s) for s in (1449, 1460, 5143, 597, 5505, 604, 118, 130)),
    _header("Fire"), *(_service(s) for s in (2137, 143, 145, 2120)),
    _header("Frost"), *(_service(s) for s in (7300, 122, 116, 205)),
)


class TrainerWindow:
    """The stock trainer window (Blizzard_TrainerUI 2.4.3) with the schema-18 list painted
    one row a read: eleven rows shown, the first learnable row selected and scrolled to the
    top at every update, a known rank out of the list (the used filter is off), a rank
    learnable only after the one before it, and Train enabled only over a selected row
    learnable now and affordable. A header click folds its group or opens it."""

    def __init__(self, services=LEVEL_8_LIST, *, money=200, level=8, known=MAGE_KNOWN):
        self.services = [dict(s) for s in services]
        self.money, self.level, self.learned = money, level, set(known)
        self.now, self.open, self.revision = 0.0, False, 0
        self.offset, self.selected = 0, None
        self.tick = 0
        self.clicks, self.keys, self.bought = [], [], []
        self.ignore_rows = False          # a click that lands and selects nothing
        self.shift_on_click = False       # the list rebuilt as a row is clicked

    def kind(self, s: dict) -> str:
        if s.get("header"):
            return "header"
        if s["spell"] in self.learned:
            return "used"
        if s["level"] > self.level or (s["needs"] and s["needs"] not in self.learned):
            return "unavailable"
        return "available"

    def rows(self) -> list[dict]:
        out, shown = [], True
        for s in self.services:
            if s.get("header"):
                out.append(s)
                shown = s["expanded"]
            elif shown and self.kind(s) != "used":
                out.append(s)
        return out

    def _update(self) -> None:
        """TRAINER_UPDATE: the list rebuilt, the first learnable row selected and scrolled
        to the top (ClassTrainer_SelectFirstLearnableSkill)."""
        self.revision += 1
        rows = self.rows()
        first = next((i for i, r in enumerate(rows, 1) if self.kind(r) == "available"), None)
        self.selected = first
        if first is not None and first >= 2:
            self.offset = min(first - 1, max(0, len(rows) - 11))

    def visit(self):
        self.open = True
        self._update()
        return True

    def sleep(self, seconds):
        self.now += seconds

    def tap(self, key):
        self.keys.append(key)
        self.open = False
        return True

    def read(self):
        v = {"schema": 18, "ui.trainer": self.open, "ui.modal": False, "vitals.combat": False,
             "vitals.dead": False, "vitals.ghost": False, "bags.money_copper": self.money}
        if not self.open:
            return v
        rows = self.rows()
        short = [i for i, r in enumerate(rows, 1) if self.kind(r) in ("header", "available")]
        self.tick += 1
        # Helpers.lua's rule: one fraction of the golden ratio's multiples picks the cycle
        # and the row in it.
        f = (self.tick * GOLDEN) % 1
        if f < SHORT_SHARE and short:
            index = short[min(len(short), math.floor(f / SHORT_SHARE * len(short)) + 1) - 1]
        else:
            if short:
                f = (f - SHORT_SHARE) / (1 - SHORT_SHARE)
            index = min(len(rows), math.floor(f * len(rows)) + 1)
        row, kind = rows[index - 1], self.kind(rows[index - 1])
        v.update({"trainer.revision": self.revision % 255, "trainer.total": len(rows),
                  "trainer.short": len(short), "trainer.selected": self.selected,
                  "trainer.top": self.offset + 1, "trainer.index": index,
                  "trainer.name_id": name_id(row["name"]), "trainer.rank": row["rank"],
                  "trainer.type": 4 if kind == "header" and not row["expanded"] else KINDS[kind],
                  "trainer.cost": row.get("cost")})
        if self.offset < index <= self.offset + 11:
            v["trainer.x"] = ROW_X / 1600
            v["trainer.y"] = (ROW_Y + ROW_STEP * (index - self.offset - 1)) / 900
        elif index <= self.offset:
            v["trainer.go_x"], v["trainer.go_y"] = UP[0] / 1600, UP[1] / 900
        elif self.offset < len(rows) - 11:
            v["trainer.go_x"], v["trainer.go_y"] = DOWN[0] / 1600, DOWN[1] / 900
        chosen = rows[self.selected - 1] if self.selected else None
        if chosen is not None and self.kind(chosen) == "available" and chosen["cost"] <= self.money:
            v["ui.advance_x"], v["ui.advance_y"] = TRAIN[0] / 1600, TRAIN[1] / 900
        return v

    def click(self, x, y, right=False):
        self.clicks.append((x, y))
        rows = self.rows()
        if (x, y) == TRAIN:
            chosen = rows[self.selected - 1] if self.selected else None
            if chosen is not None and self.kind(chosen) == "available" \
                    and chosen["cost"] <= self.money:
                self.money -= chosen["cost"]
                self.learned.add(chosen["spell"])
                self.bought.append(chosen["spell"])
                self._update()
        elif (x, y) == UP:
            self.offset = max(0, self.offset - 5)
        elif (x, y) == DOWN:
            self.offset = min(max(0, len(rows) - 11), self.offset + 5)
        elif x == ROW_X and (y - ROW_Y) % ROW_STEP == 0 and 0 <= (y - ROW_Y) // ROW_STEP < 11:
            if self.shift_on_click:
                self.shift_on_click = False
                self.services.insert(1, {"name": "Arcane Brilliance", "rank": 1, "cost": 0,
                                         "level": 56, "spell": 23028, "needs": None})
                self._update()
                rows = self.rows()
            index = self.offset + (y - ROW_Y) // ROW_STEP + 1
            if index <= len(rows):
                row = rows[index - 1]
                if row.get("header"):
                    row["expanded"] = not row["expanded"]
                    self._update()
                elif not self.ignore_rows:
                    self.selected = index
        return True

    def desk(self, **kw):
        return TrainerDesk(self, self.read, self.visit, clock=lambda: self.now,
                           sleep=self.sleep, trainer=ZALDIMAR, known=MAGE_KNOWN, bar=MAGE_BAR,
                           **kw)


def test_the_mage_buys_frostbolt_then_fireball_rank_2_not_the_window_s_first_row():
    """Level 8 with 200 copper, one spell's money: the window selects Arcane Missiles, the
    first row learnable now, and the old desk bought it. Frostbolt (the caster's opener,
    taught at 4) is scrolled to and bought, then Fireball rank 2 (taught at 6) with the
    100 left. Polymorph is never clicked."""
    window = TrainerWindow()
    trained = window.desk()
    assert trained.run() is Trained.DONE, trained.detail
    assert window.bought == trained.learned == [116, 143]
    assert (trained.bought, trained.spent, window.money) == (2, 200, 0)
    assert DOWN in window.clicks, "Frostbolt is below the eleven rows shown"
    assert window.keys == ["esc"]


def test_a_reader_landing_on_one_paint_in_two_three_or_four_still_buys():
    """Session 56's reader, at a steady fraction of the paint rate, saw the same few bar
    slots for minutes. Painted in alternation, a reader on every fourth paint never saw
    half of the six rows left after the first purchase, and the visit ended in a timeout."""
    for step in (2, 3, 4):
        for start in range(step):
            window = TrainerWindow()
            for _ in range(start):
                window.read()

            def read(window=window, step=step):
                for _ in range(step - 1):
                    window.read()
                return window.read()

            trained = TrainerDesk(window, read, window.visit, clock=lambda w=window: w.now,
                                  sleep=window.sleep, trainer=ZALDIMAR, known=MAGE_KNOWN,
                                  bar=MAGE_BAR)
            assert trained.run() is Trained.DONE, (step, start, trained.detail)
            assert window.bought == [116, 143], (step, start)


def test_the_old_desk_would_have_bought_the_window_s_first_row():
    """The same window without a trainer to value its rows by: Train on the selection."""
    window = TrainerWindow()
    trained = TrainerDesk(window, window.read, window.visit, clock=lambda: window.now,
                          sleep=window.sleep)
    assert trained.run() is Trained.DONE
    assert window.bought == [5143], "Arcane Missiles, the first row learnable now"


def test_nothing_worth_buying_is_nothing_bought():
    """Everything else known: only Polymorph is learnable now, and nothing presses it."""
    known = MAGE_KNOWN | {143, 116, 205, 5143}
    window = TrainerWindow(money=5000, known=known)
    trained = TrainerDesk(window, window.read, window.visit, clock=lambda: window.now,
                          sleep=window.sleep, trainer=ZALDIMAR, known=known, bar=MAGE_BAR)
    assert trained.run() is Trained.NOTHING, trained.detail
    assert window.bought == [] and window.clicks == []


def test_a_folded_header_is_opened_before_the_choice():
    """The Frost header folded shut hides Frostbolt: the header is clicked open, and
    Frostbolt bought with the 100 copper, not Fireball rank 2."""
    window = TrainerWindow(money=100)
    next(s for s in window.services if s["name"] == "Frost")["expanded"] = False
    trained = window.desk()
    assert trained.run() is Trained.DONE, trained.detail
    assert window.bought == [116]


def test_a_row_click_that_selects_nothing_presses_no_train():
    window = TrainerWindow()
    window.ignore_rows = True
    trained = window.desk()
    assert trained.run() is Trained.NOT_SELECTED
    assert trained.detail == "row 18 clicked: the window has row 4 selected"
    assert window.bought == [] and TRAIN not in window.clicks and window.money == 200


def test_a_list_rebuilt_under_the_click_is_read_again_before_train():
    """A row came into the list as the chosen one was clicked: every row below moved down
    one and the stock window selected its first learnable again. Train is pressed only
    over the row the new list names Frostbolt."""
    window = TrainerWindow(money=100)
    window.shift_on_click = True
    trained = window.desk()
    assert trained.run() is Trained.DONE, trained.detail
    assert window.bought == [116]


def test_a_window_shut_before_its_list_is_read_ends_the_visit():
    window = TrainerWindow()
    real = window.read

    def read():
        v = real()
        v.pop("trainer.short", None)
        if window.tick >= 3:
            window.open = False                      # shut by a hand at the desk
        return v

    trained = TrainerDesk(window, read, window.visit, clock=lambda: window.now,
                          sleep=window.sleep, trainer=ZALDIMAR, known=MAGE_KNOWN, bar=MAGE_BAR)
    assert trained.run() is Trained.NO_TRAINER
    assert window.now < 1.0 and window.clicks == []


def test_the_list_unread_whole_is_no_purchase():
    window = TrainerWindow()
    real = window.read

    def read():
        v = real()
        v.pop("trainer.short", None)                 # never a whole short cycle
        return v

    trained = TrainerDesk(window, read, window.visit, clock=lambda: window.now,
                          sleep=window.sleep, trainer=ZALDIMAR, known=MAGE_KNOWN, bar=MAGE_BAR)
    assert trained.run() is Trained.TIMEOUT
    assert window.bought == [] and window.clicks == []


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
