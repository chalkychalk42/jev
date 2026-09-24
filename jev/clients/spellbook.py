"""Put spells on the main action bar: open the spellbook, drag, check the slot, close it.

The strip paints both ends of every drag (schema 15): each spellbook entry's button while
its page is showing, else the tab or page button that brings it into view, and each
main-bar button with the spell it holds. A drag is the mouse held down on the spellbook
button and let go over the bar button: the stock spellbook picks the spell up when the
drag starts and the stock bar places it on release. A click without the drag would cast
it instead, so the hold moves straight off the button; and a drop that never happened
leaves nothing on the bar and nothing cast.

Dropped on a slot that holds something (a new rank over the old one), the stock bar hands
the old action back on the cursor. A click on open world drops it; `cursor.holding` says
whether anything is still held, and nothing else is done while it is.

What goes where is `jev.world.training.placements`; this only carries it out, and a drop
is done when the slot's census says the spell is there.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced
from jev.world.training import Placement

# The stock binding that opens and closes the spellbook (TOGGLESPELLBOOK).
SPELLBOOK_KEY = "p"
# Open world to drop a handed-back action on: upper right of the middle, clear of the
# spellbook (a left panel), the strip (top middle), the minimap and the bars.
DROP_POINT = (0.66, 0.32)
# Clicks on tabs and page buttons to bring one entry into view: tabs, then pages.
NAVIGATE_CLICKS = 8
OPEN_S = 3.0
# One entry of each census is painted per paint, ten a second, and a look (a capture and a
# decode) takes a paint or three: a reader can go several seconds without landing on one
# entry. Session 56 waited 4 s for Divine Protection's, of 17, and gave up with two of four
# spells placed. Waits end as soon as the entry is seen, or every entry has been.
ENTRY_S = 12.0
SLOT_S = 8.0
PLACED_S = 8.0


class Placed(StrEnum):
    DONE = "done"
    NOTHING = "nothing"
    BLIND = "blind"
    NO_BOOK = "no_book"
    NO_BUTTON = "no_button"
    NOT_PLACED = "not_placed"
    HOLDING = "holding"
    INTERRUPTED = "interrupted"
    REFUSED = "refused"
    TIMEOUT = "timeout"

    @property
    def ok(self) -> bool:
        return self in (Placed.DONE, Placed.NOTHING)


class _Stop(Exception):
    def __init__(self, result: Placed, detail: str):
        super().__init__(detail)
        self.result, self.detail = result, detail


@dataclass
class Spellbook:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    placed: list[Placement] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)
    _deadline: float = field(default=0.0, init=False)

    @traced("spellbook.place")
    def place(self, plan: Sequence[Placement], *, timeout_s: float = 90.0) -> Placed:
        """Carry out `plan` in order; stop at the first drop that does not land."""
        self.placed, self.detail = [], ""
        if not plan:
            return Placed.NOTHING
        self._deadline = self.clock() + timeout_s
        opened = False
        try:
            values = self._safe(self.read())
            if values.get("cursor.holding") is True:
                # Not this routine's to drop: an item dropped on open world asks to be
                # destroyed. Only the rank a swap hands back is dropped, below.
                raise _Stop(Placed.HOLDING, "the cursor already holds something")
            opened = self._open()
            for placement in plan:
                self._place(placement)
                self.placed.append(placement)
            return Placed.DONE
        except _Stop as stop:
            self.detail = stop.detail
            return stop.result
        finally:
            if opened:
                self._close()

    # -- pieces --------------------------------------------------------------

    def _safe(self, values: dict | None) -> dict:
        if values is None:
            raise _Stop(Placed.BLIND, "strip unreadable")
        if values.get("vitals.combat") is True or values.get("vitals.dead") is True \
                or values.get("vitals.ghost") is True:
            raise _Stop(Placed.INTERRUPTED, "combat or death")
        if values.get("ui.spellbook") is None or values.get("bars.slot") is None:
            raise _Stop(Placed.BLIND, "this addon paints no spellbook or bar census")
        if self.clock() >= self._deadline:
            raise _Stop(Placed.TIMEOUT, "spell placement deadline exhausted")
        return values

    def _await(self, predicate, seconds: float, what: str) -> dict:
        until = min(self._deadline, self.clock() + seconds)
        while self.clock() < until:
            values = self._safe(self.read())
            if predicate(values):
                return values
            self.sleep(0.05)
        raise _Stop(Placed.TIMEOUT, f"no {what} within {seconds:.0f} s")

    def _point(self, values: dict, prefix: str) -> tuple[int, int] | None:
        x, y = values.get(prefix + "x"), values.get(prefix + "y")
        if x is None or y is None or not 0 <= x <= 1 or not 0 <= y <= 1:
            return None
        ox, oy = self.window_origin
        w, h = self.window_size
        return ox + round(x * w), oy + round(y * h)

    def _open(self) -> bool:
        if self._safe(self.read()).get("ui.spellbook") is True:
            return True
        if self.hid.tap(SPELLBOOK_KEY) is False:
            raise _Stop(Placed.REFUSED, "spellbook key refused")
        self._await(lambda v: v.get("ui.spellbook") is True, OPEN_S, "open spellbook")
        return True

    def _close(self) -> None:
        values = self.read()
        if values and values.get("ui.spellbook") is True and values.get("cursor.holding") is not True:
            self.hid.tap(SPELLBOOK_KEY)

    def _entry(self, spell_id: int) -> dict:
        """A paint describing `spell_id`'s spellbook entry, its button in view."""
        for _ in range(NAVIGATE_CLICKS + 1):
            seen: set[int] = set()

            def found(v, seen=seen):
                if v.get("spells.id") == spell_id:
                    return True
                if v.get("spells.index") is not None:
                    seen.add(v["spells.index"])
                total = v.get("spells.total")
                if total and len(seen) >= total:
                    raise _Stop(Placed.NO_BUTTON, f"spell {spell_id} is not in the spellbook")
                return False

            values = self._await(found, ENTRY_S, f"spellbook entry for spell {spell_id}")
            if self._point(values, "spells.") is not None:
                return values
            go = self._point(values, "spells.go_")
            if go is None:
                raise _Stop(Placed.NO_BUTTON, f"spell {spell_id}: no button and no way to its page")
            event("spellbook.navigate", data={"spell": spell_id, "point": list(go)})
            if self.hid.click(*go) is False:
                raise _Stop(Placed.REFUSED, "spellbook navigation click refused")
            self.sleep(0.3)
        raise _Stop(Placed.NO_BUTTON, f"spell {spell_id}: not in view after {NAVIGATE_CLICKS} clicks")

    def _slot(self, slot: int) -> dict:
        return self._await(lambda v: v.get("bars.slot") == slot
                           and self._point(v, "bars.slot_") is not None,
                           SLOT_S, f"bar slot {slot}")

    def _place(self, placement: Placement) -> None:
        here = self._await(lambda v: v.get("bars.slot") == placement.slot, SLOT_S,
                           f"bar slot {placement.slot}")
        if here.get("bars.slot_spell") == placement.spell_id:
            return                                     # already there
        entry = self._entry(placement.spell_id)
        start = self._point(entry, "spells.")
        event("spellbook.drag", data={"spell": placement.spell_id, "slot": placement.slot,
                                      "replaces": placement.replaces, "from": list(start)})
        self._drag(start, placement.slot)
        try:
            self._await(lambda v: v.get("bars.slot") == placement.slot
                        and v.get("bars.slot_spell") == placement.spell_id,
                        PLACED_S, f"spell {placement.spell_id} on slot {placement.slot}")
        except _Stop as stop:
            if stop.result is Placed.TIMEOUT:
                raise _Stop(Placed.NOT_PLACED, stop.detail) from None
            raise
        values = self._safe(self.read())
        if values.get("cursor.holding") is True:
            self._drop_held()

    def _drag(self, start: tuple[int, int], slot: int) -> None:
        """Pick the spell up, then find the slot's button, then let go over it.

        In that order because the stock bar hides an empty button until something is being
        dragged (ACTIONBAR_SHOWGRID): the first live placement waited three seconds for an
        empty slot 4 the strip could not paint. Anything failing mid-drag lets go over open
        world instead, where the spell drops, lands nowhere and casts nothing.
        """
        if not self.hid.move_to(*start) or not self.hid.button(True):
            raise _Stop(Placed.REFUSED, "drag input refused")
        try:
            # Off the button first, a short way, so the drag starts on the spell and not
            # on whatever the long move passes over.
            if not self.hid.move_to(start[0] + 24, start[1] + 12):
                raise _Stop(Placed.REFUSED, "drag input refused")
            target = self._slot(slot)
            end = self._point(target, "bars.slot_")
            event("spellbook.drop", data={"slot": slot, "to": list(end)})
            if not self.hid.move_to(*end):
                raise _Stop(Placed.REFUSED, "drag input refused")
            self.sleep(0.1)
        except BaseException:
            self.hid.move_to(*self._drop_point())
            raise
        finally:
            self.hid.button(False)
            self.sleep(0.2)

    def _drop_point(self) -> tuple[int, int]:
        ox, oy = self.window_origin
        w, h = self.window_size
        return ox + round(DROP_POINT[0] * w), oy + round(DROP_POINT[1] * h)

    def _drop_held(self) -> None:
        """Drop what the cursor holds on open world, and check it went."""
        point = self._drop_point()
        event("spellbook.drop_held", data={"point": list(point)})
        if self.hid.click(*point) is False:
            raise _Stop(Placed.REFUSED, "drop click refused")
        try:
            self._await(lambda v: v.get("cursor.holding") is False, 2.0, "empty cursor")
        except _Stop as stop:
            if stop.result is Placed.TIMEOUT:
                raise _Stop(Placed.HOLDING, "the cursor still holds an action") from None
            raise
