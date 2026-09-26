"""Buy what a class trainer will teach: the spells worth buying, best first (V237).

The stock trainer window (Blizzard_TrainerUI 2.4.3) selects the first service the character
can learn when it opens, and again after every purchase, and its Train button buys the
selected service, enabled only while that service is learnable now and affordable. The
addon paints Train as the advance button only while it is enabled. The window lists
services by skill line and then by name, so pressing Train over and over buys in that
order: the mage, with a spell's money a visit, bought Conjure Water before Frostbolt at
level 5 and Conjure Food before Fire Blast and Fireball rank 2 at level 6 (26 September),
and from level 8 the same order buys Polymorph, which nothing presses.

With the trainer's list on the strip (schema 18), the desk reads the rows learnable now,
names each against the trainer's offers by name and rank, and picks the one worth most that
the purse pays for (`jev.world.training.worth_buying`, `buy_order`). It scrolls that row
into view with the list's own scroll buttons, clicks it, sees the window select it, and
presses Train; a press is a purchase only when the money falls. It goes on while anything
worth buying is affordable. A header is never clicked, except one folded shut, to open it:
a click on a header folds its group away or opens it.

Without the list (a schema-17 addon, or a census the addon fails to paint) or without the
trainer's offers, Train is pressed on whatever the window selects, as before: press it, see
the money fall, wait for the next selection, and stop when the button goes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from jev.perceive.radio_frame import name_id
from jev.perceive.trainer import AVAILABLE, FOLDED, TrainerCensus, TrainerRow, list_key
from jev.run.evidence import event, traced
from jev.world.training import Offer, Trainer, shopping, spell, starting_bar

OPEN_S = 4.0
BOUGHT_S = 3.0
# The server answers a purchase with the trainer's new list, and only then does the frame
# select the next service and enable Train again.
NEXT_S = 2.5
MAX_PURCHASES = 40

# The first schema that paints the trainer's list.
LIST_SCHEMA = 18
# The rows learnable now and the headers come two paints in three, each row at the golden
# ratio's turn (Helpers.lua): seven rows for the level 8 mage, all seen in about a second
# when every paint is read. Worked out for a reader landing on one paint in two, three or
# four, from any start: twelve rows within 10.2 s, a row seen again within 14.4 s, and
# never more than 2 s a row up to fifty. The list grows with the spells never bought
# (a rank 1 of Polymorph, of Blizzard, stays learnable), so the waits grow with it.
LIST_S = 20.0
ROW_S = 15.0
ROW_WAIT_S = 2.0
# The list painted at all, when the window is: every paint while it is open carries it.
LIST_SEEN_S = 3.0
# The list rebuilt under a choice this many times running, the desk gives up.
MAX_CHANGES = 5
# From a row's click to the window showing it selected, with Train enabled for it.
SELECT_S = 3.0
# From a scroll click to the list showing other rows: the next paint, or two.
SCROLL_S = 1.0
# The list scrolls about five rows a click (half the scroll bar), and a class trainer's list
# is at most a couple of hundred rows long.
SCROLL_CLICKS = 50
# Rows the stock list shows at once (CLASS_TRAINER_SKILLS_DISPLAYED).
SHOWN_ROWS = 11
# Headers folded shut opened in one visit: each is one click.
MAX_UNFOLDS = 4


class Trained(StrEnum):
    DONE = "done"
    NOTHING = "nothing"
    NO_TRAINER = "no_trainer"
    BLIND = "blind"
    INTERRUPTED = "interrupted"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    # The spell chosen could not be brought into view, or its row clicked did not select it.
    NOT_SELECTED = "not_selected"

    @property
    def ok(self) -> bool:
        return self in (Trained.DONE, Trained.NOTHING)


class _Stop(Exception):
    def __init__(self, result: Trained, detail: str):
        super().__init__(detail)
        self.result, self.detail = result, detail


class _Changed(Exception):
    """The trainer's list changed under a choice: read it again before clicking anything."""


@dataclass
class TrainerDesk:
    hid: object
    read: Callable[[], dict | None]
    visit: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    # What is worth buying here: the trainer's offers, and what the character knows and has
    # on its bar (the censuses; an unread bar is taken for the class's starting one).
    trainer: Trainer | None = None
    known: Iterable[int] | None = None
    bar: Mapping[int, int | None] | None = None
    race_id: int | None = None

    bought: int = field(default=0, init=False)
    spent: int = field(default=0, init=False)
    detail: str = field(default="", init=False)
    # The spells bought this visit, by id, when the list named them.
    learned: list[int] = field(default_factory=list, init=False)
    _deadline: float = field(default=0.0, init=False)
    _census: TrainerCensus = field(default_factory=TrainerCensus, init=False)

    @traced("trainer")
    def run(self, *, timeout_s: float = 300.0) -> Trained:
        self.bought = self.spent = 0
        self.detail = ""
        self.learned = []
        self._census.reset()
        self._deadline = self.clock() + timeout_s
        interrupted = False
        try:
            self._safe(self.read())
            if not self.visit():
                raise _Stop(Trained.NO_TRAINER, "could not open the trainer's window")
            values = self._await(lambda v: v.get("ui.trainer") is True, OPEN_S, "trainer window")
            if self.trainer is not None and (values.get("schema") or 0) >= LIST_SCHEMA:
                self._by_value()
            else:
                self._in_window_order(values)
            return Trained.DONE if self.bought else Trained.NOTHING
        except _Stop as stop:
            self.detail = stop.detail
            return stop.result
        except BaseException:
            interrupted = True
            raise
        finally:
            latest = None if interrupted else self.read()
            if latest and latest.get("ui.trainer") is True and latest.get("ui.modal") is False:
                self.hid.tap("esc")

    # -- the window's own order (schema 17) ----------------------------------------------

    def _in_window_order(self, values: dict) -> None:
        """Press Train while it is painted, whatever the window has selected."""
        while self.bought < MAX_PURCHASES:
            if self._train_point(values) is None:
                values = self._await_optional(lambda v: self._train_point(v) is not None,
                                              NEXT_S, window=True)
                if values is None:
                    break
            money = values.get("bags.money_copper")
            if money is None:
                raise _Stop(Trained.BLIND, "money unread")
            point = self._train_point(values)
            event("trainer.train", data={"point": list(point), "money": money})
            if self.hid.click(*point) is False:
                raise _Stop(Trained.REFUSED, "Train click refused")
            after = self._await_optional(lambda v, m=money: _spent(v, m), BOUGHT_S,
                                         window=True)
            if after is None:
                self.detail = "Train pressed and no money spent"
                break
            self.bought += 1
            self.spent += money - after["bags.money_copper"]
            values = after

    # -- by value (schema 18) -------------------------------------------------------------

    def _by_value(self) -> None:
        if self._await_optional(lambda v: list_key(v) is not None, LIST_SEEN_S,
                                window=True) is None:
            # Schema 18 and no list painted: the census failed in the addon. Without a
            # census, the window's own order, as before.
            self._in_window_order(self._look())
            self.detail = self.detail or "no trainer's list painted: bought in the window's order"
            return
        offers = self._offers()
        bar = self.bar if self.bar is not None else starting_bar(self.trainer.class_id,
                                                                  self.race_id)
        unfolded = changed = 0
        while self.bought < MAX_PURCHASES:
            rows = self._whole_list()
            folded = next((r for r in rows if r.kind == FOLDED), None)
            if folded is not None and unfolded < MAX_UNFOLDS:
                unfolded += 1
                self._unfold(folded)
                continue
            money = self._look().get("bags.money_copper")
            if money is None:
                raise _Stop(Trained.BLIND, "money unread")
            choice = self._choose(rows, offers, bar, money)
            if choice is None:
                return
            row, offer = choice
            try:
                selected = self._select(row)
            except _Changed:
                changed += 1
                if changed >= MAX_CHANGES:
                    raise _Stop(Trained.NOT_SELECTED, f"the trainer's list changed under the "
                                                      f"choice {changed} times") from None
                continue
            if not self._train(selected, row, offer):
                return
            changed = 0

    def _offers(self) -> dict[tuple[int, int], Offer]:
        """The trainer's offers by the name hash and rank a row of its list shows. Where two
        spells share both, neither is named: a row that cannot be told apart is not bought
        (in the whole catalog, only a Shattrath portal, a teleport and Cure Disease)."""
        by_row: dict[tuple[int, int], list[Offer]] = {}
        for offer in self.trainer.offers:
            facts = spell(offer.spell_id)
            if facts is not None:
                by_row.setdefault((name_id(facts.name), facts.rank), []).append(offer)
        return {key: found[0] for key, found in by_row.items()
                if len({o.spell_id for o in found}) == 1}

    def _choose(self, rows: tuple[TrainerRow, ...], offers: dict[tuple[int, int], Offer],
                bar: Mapping[int, int | None], money: int) -> tuple[TrainerRow, Offer] | None:
        """The row learnable now worth most that the purse pays for, of the offers worth
        buying all together (`shopping`); `None` when there is none. A cheaper spell is
        not bought into the last free slot a better one, not yet affordable, needs."""
        known = {*(self.known or ()), *self.learned}
        by_offer: dict[Offer, TrainerRow] = {}
        seen = []
        for row in rows:
            if row.kind != AVAILABLE:
                continue
            offer = offers.get((row.name_id, row.rank))
            seen.append([row.index, offer.spell_id if offer else None, row.cost])
            if offer is not None and offer.spell_id not in known and row.cost is not None:
                by_offer[offer] = row
        wanted = shopping(by_offer, known, bar)
        best = next(((by_offer[o], o) for o in wanted if by_offer[o].cost <= money), None)
        event("trainer.choose", data={"money": money, "learnable": seen,
                                      "wanted": [o.spell_id for o in wanted],
                                      "chosen": best[1].spell_id if best else None})
        return best

    def _wait_s(self, least: float) -> float:
        """A wait for rows of the short cycle: `least`, or `ROW_WAIT_S` a row of it."""
        key = self._census.key
        return max(least, ROW_WAIT_S * key[2]) if key is not None else least

    def _whole_list(self) -> tuple[TrainerRow, ...]:
        """Every header and every service learnable now, read under one list."""
        start = self.clock()
        while True:
            values = self._look()
            rows = self._census.short
            if rows is not None:
                return rows
            if values.get("ui.trainer") is False:
                raise _Stop(Trained.NO_TRAINER, "the trainer window closed")
            wait = self._wait_s(LIST_S)
            if self.clock() >= start + wait:
                raise _Stop(Trained.TIMEOUT, f"the trainer's list not read whole in {wait:.0f} s")
            self.sleep(0.05)

    def _select(self, row: TrainerRow) -> dict:
        """Bring `row` into view, click it, and see the window select it with Train enabled:
        the paint that shows so. `_Changed` if the list changes first."""
        key = self._census.key
        values = self._in_view(row, key)
        point = self._point(values, "trainer.")
        event("trainer.select", data={"index": row.index, "point": list(point)})
        if self.hid.click(*point) is False:
            raise _Stop(Trained.REFUSED, "row click refused")
        selected = self._await_optional(
            lambda v: list_key(v) != key or (v.get("trainer.selected") == row.index
                                             and self._train_point(v) is not None),
            SELECT_S, window=True)
        if selected is None:
            now = self._look().get("trainer.selected")
            raise _Stop(Trained.NOT_SELECTED, f"row {row.index} clicked: " + (
                "selected, and Train not enabled" if now == row.index
                else f"the window has row {now} selected"))
        if list_key(selected) != key:
            raise _Changed
        return selected

    def _train(self, selected: dict, row: TrainerRow, offer: Offer) -> bool:
        """Press Train over the selected row, and see the money fall: whether it did."""
        money = selected.get("bags.money_copper")
        if money is None:
            raise _Stop(Trained.BLIND, "money unread")
        point = self._train_point(selected)
        key = list_key(selected)
        event("trainer.train", data={"point": list(point), "money": money,
                                     "spell": offer.spell_id, "cost": row.cost})
        if self.hid.click(*point) is False:
            raise _Stop(Trained.REFUSED, "Train click refused")
        after = self._await_optional(lambda v, m=money: _spent(v, m), BOUGHT_S, window=True)
        if after is None:
            self.detail = f"Train pressed for spell {offer.spell_id} and no money spent"
            return False
        self.bought += 1
        self.spent += money - after["bags.money_copper"]
        self.learned.append(offer.spell_id)
        # The server answers with the list rebuilt, the spell bought out of it: the rows
        # read before are numbered wrong from here. They are read again in any case, so a
        # list rebuilt late is not chosen from, then pulled from under a click.
        self._await_optional(lambda v: list_key(v) != key, NEXT_S, window=True)
        self._census.reset()
        return True

    def _unfold(self, header: TrainerRow) -> None:
        """Click a header folded shut, to open its group and list its services."""
        key = self._census.key
        try:
            values = self._in_view(header, key)
        except _Changed:
            return
        point = self._point(values, "trainer.")
        event("trainer.unfold", data={"index": header.index, "point": list(point)})
        if self.hid.click(*point) is False:
            raise _Stop(Trained.REFUSED, "header click refused")
        self._await_optional(lambda v: list_key(v) != key, NEXT_S, window=True)

    def _in_view(self, row: TrainerRow, key: tuple) -> dict:
        """A paint of `row` with its button in view. Out of view, the list's scroll button
        toward it (`trainer.go_*`) is clicked until the first row shown (`trainer.top`)
        puts it among the eleven, each click seen in the next paint; the row's own paint
        says when it is there. `_Changed` if the list changes first."""
        clicks = 0
        while True:
            values = self._await_optional(
                lambda v: list_key(v) != key or v.get("trainer.index") == row.index,
                self._wait_s(ROW_S), window=True)
            if values is None:
                raise _Stop(Trained.TIMEOUT, f"no paint of the list's row {row.index}")
            if list_key(values) != key or (values.get("trainer.name_id"),
                                           values.get("trainer.rank")) != (row.name_id, row.rank):
                raise _Changed
            if self._point(values, "trainer.") is not None:
                return values
            go = self._point(values, "trainer.go_")
            if go is None:
                raise _Stop(Trained.NOT_SELECTED, f"row {row.index} out of view, no way to it")
            top = values.get("trainer.top")
            while True:
                if clicks >= SCROLL_CLICKS:
                    raise _Stop(Trained.NOT_SELECTED,
                                f"row {row.index} not in view after {clicks} scroll clicks")
                event("trainer.scroll", data={"index": row.index, "top": top,
                                              "point": list(go)})
                if self.hid.click(*go) is False:
                    raise _Stop(Trained.REFUSED, "scroll click refused")
                clicks += 1
                moved = self._await_optional(
                    lambda v, t=top: list_key(v) != key or v.get("trainer.top") != t,
                    SCROLL_S, window=True)
                if moved is None:
                    break                       # the list did not move: ask the row again
                if list_key(moved) != key:
                    raise _Changed
                top = moved.get("trainer.top")
                if top is None or top <= row.index < top + SHOWN_ROWS:
                    break

    # -- reading ----------------------------------------------------------------------

    def _look(self) -> dict:
        """A safe look, whose trainer's row goes into the census."""
        values = self._safe(self.read())
        self._census.observe(values)
        return values

    def _safe(self, values: dict | None) -> dict:
        if values is None:
            raise _Stop(Trained.BLIND, "strip unreadable")
        if values.get("vitals.combat") is True or values.get("vitals.dead") is True \
                or values.get("vitals.ghost") is True:
            raise _Stop(Trained.INTERRUPTED, "combat or death at the trainer")
        if self.clock() >= self._deadline:
            raise _Stop(Trained.TIMEOUT, "training deadline exhausted")
        return values

    def _point(self, values: dict, prefix: str) -> tuple[int, int] | None:
        x, y = values.get(prefix + "x"), values.get(prefix + "y")
        if x is None or y is None or not 0 <= x <= 1 or not 0 <= y <= 1:
            return None
        ox, oy = self.window_origin
        w, h = self.window_size
        return ox + round(x * w), oy + round(y * h)

    def _train_point(self, values: dict) -> tuple[int, int] | None:
        """Train, where it is: the advance button while the trainer window alone is up."""
        if values.get("ui.trainer") is not True or values.get("ui.modal") is not False:
            return None
        return self._point(values, "ui.advance_")

    def _await(self, predicate, seconds: float, what: str) -> dict:
        values = self._await_optional(predicate, seconds)
        if values is None:
            raise _Stop(Trained.TIMEOUT, f"no {what} within {seconds:.0f} s")
        return values

    def _await_optional(self, predicate, seconds: float, *, window: bool = False) -> dict | None:
        """The first look `predicate` accepts within `seconds`, else `None`. With `window`,
        the trainer window closing is the end of training, not something to wait out."""
        until = min(self._deadline, self.clock() + seconds)
        while self.clock() < until:
            values = self._look()
            if predicate(values):
                return values
            if window and values.get("ui.trainer") is False:
                raise _Stop(Trained.NO_TRAINER, "the trainer window closed")
            self.sleep(0.05)
        return None


def _spent(values: dict, before: int) -> bool:
    money = values.get("bags.money_copper")
    return money is not None and money < before
