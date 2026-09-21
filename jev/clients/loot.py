"""Take what the corpse is holding. One click, because fast loot is on.

A great many quests are "bring me eight of these", and the eight come off corpses rather
than out of the air. Without this a kill counter can fill while the quest never does.

The corpse is where the kill was, and the client is still pointing at it: a unit stays
selected after it dies, so its **selection ring is still drawn** and `units._find_ring`
still finds it. That is the same primitive the fight already uses to turn, reused rather
than a second way of knowing where something is. Right-click just above the ring - a unit
stands on its own ring, alive or not - and with auto-loot enabled the client empties it
in one action.

What counts as having looted
----------------------------
Not "we clicked", and three signals in a deliberate order:

    1. the objective counter    the server's tally, the same rule kills already follow
    2. money                    a copper or two comes off almost everything
    3. `bags.free`              a slot filled

The counter first because it is the only one that answers the question actually being
asked - *did this corpse move the quest on* - and because it is the server's own count
rather than an inference from the bags.

`bags.free` is last because it **misses stacks**. Tough Wolf Meat 2 through 8 land on the
stack meat 1 made, so seven of the eight take no new slot: a bot that believes free slots
reports `nothing` for most of a collect quest while the counter climbs behind it.

The loot frame is **not** evidence and is not consulted. A frame appearing means a corpse
was opened, not that anything was taken, and it is only auto loot that makes the two
coincide - which is a client setting this code cannot see. `autoLootCorpse "1"` is set in
`WTF/Config.wtf`; if it is ever off, the frame would stand open and nothing would be in
the bags, and believing the frame would report a take on every empty wolf in the zone.

An empty corpse is a real and common outcome, so it is reported as `NOTHING` rather than
as a failure - a skill that treats an empty wolf as an error will retire itself on a
perfectly good camp.

Bags are checked **before** the click. A full bag makes looting silently do nothing, and
the fix for that is a vendor, not another click.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.perceive.units import _find_ring

# How long to wait for the bags or the loot frame to admit something happened.
SETTLE_S = 2.0

# Where to aim on a corpse: the ring itself.
#
# A living unit stands on its ring, so the fight aims above it to hit the body. A dead one
# **lies on** it, and aiming above a corpse clicks the empty air it used to occupy. First
# live attempt: `killed` then `loot: nothing`, on a wolf with an eighty percent quest drop
# and a counter that did not move.
#
# A small lift, because the carcass has some height and the ring's lower arc is ground.
ABOVE_RING_PX = 0
CORPSE_LIFT_FRACTION = 0.25


class Looted(StrEnum):
    TOOK = "took"              # bags changed, or the loot frame opened
    NOTHING = "nothing"        # clicked, and the corpse was empty. Not a failure.
    NO_CORPSE = "no_corpse"    # nothing selected to loot
    BAGS_FULL = "bags_full"    # would not fit; a vendor is the answer, not a click
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self in (Looted.TOOK, Looted.NOTHING)


@dataclass
class Loot:
    hid: object
    read: Callable[[], dict | None]
    read_frame: Callable[[], object | None]
    window_origin: tuple[int, int] = (0, 0)
    # A corpse has a ring only if the camera is pointing at it. See `Fight.level`.
    level: Callable[[], object] | None = None
    _progress: Callable[[], tuple[int | None, int | None]] | None = field(
        default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    took: int = field(default=0, init=False)
    detail: str = field(default="", init=False)

    def run(self, *, settle_s: float = SETTLE_S,
            progress: Callable[[], tuple[int | None, int | None]] | None = None
            ) -> Looted:
        """`progress` is the objective counter, passed by whoever knows which quest is
        being worked. `Loot` has no idea and should not: it is handed a way to ask what
        the server thinks, exactly as `Hunt` is."""
        self.clicked = None
        self.detail = ""
        self._progress = progress
        if self.level is not None:
            self.level()

        v = self.read()
        if v is None:
            return Looted.BLIND
        if v.get("bags.free") == 0:
            self.detail = "bags are full; looting would take nothing"
            return Looted.BAGS_FULL
        before = {**v, "objective": self._counter()}

        frame = self.read_frame()
        ring = None if frame is None else _find_ring(frame)
        if ring is None:
            self.detail = "no ring, so nothing on screen to loot"
            return Looted.NO_CORPSE

        ox, oy = self.window_origin
        lift = max(ABOVE_RING_PX, round(ring.h * CORPSE_LIFT_FRACTION))
        self.clicked = (ox + round(ring.cx), oy + round(ring.cy - lift))
        self.hid.click(*self.clicked, right=True)

        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            after = self.read()
            if after is None:
                continue
            why = self._what_changed(before, after)
            if why:
                self.took += 1
                self.detail = why
                self._close_if_open(after)
                return Looted.TOOK

        self._close_if_open(self.read() or {})
        self.detail = "clicked the corpse and nothing came off it"
        return Looted.NOTHING

    def _counter(self) -> int | None:
        if self._progress is None:
            return None
        try:
            have, _need = self._progress()
        except Exception:                    # a reader that fails is not a loot failure
            return None
        return have

    def _what_changed(self, before: dict, after: dict) -> str:
        """Which signal moved, in the order that they are worth believing."""
        have_before, have_now = before.get("objective"), self._counter()
        if (have_before is not None and have_now is not None
                and have_now > have_before):
            return f"objective {have_before} -> {have_now}"

        for key, unit in (("bags.money_silver", "silver"),):
            was, now = before.get(key), after.get(key)
            if was is not None and now is not None and now > was:
                return f"{now - was} {unit}"

        was, now = before.get("bags.free"), after.get("bags.free")
        if was is not None and now is not None and now < was:
            return f"{was - now} bag slot{'s' if was - now > 1 else ''}"
        return ""

    def _close_if_open(self, values: dict) -> None:
        """Auto-loot usually closes itself. When it does not, a left-open loot window
        swallows the next click, so it is shut - against a screen the radio has measured,
        not a blind Escape."""
        if values.get("ui.loot") is True:
            time.sleep(0.4)
            still = self.read()
            if still is not None and still.get("ui.loot") is True:
                self.hid.tap("esc")
