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
Not "we clicked". `bags.free` falling is the honest signal, and the loot frame appearing
is the other one. An empty corpse is a real and common outcome, so it is reported as
`NOTHING` rather than as a failure - a skill that treats an empty wolf as an error will
retire itself on a perfectly good camp.

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

    clicked: tuple[int, int] | None = field(default=None, init=False)
    took: int = field(default=0, init=False)
    detail: str = field(default="", init=False)

    def run(self, *, settle_s: float = SETTLE_S) -> Looted:
        self.clicked = None
        self.detail = ""

        v = self.read()
        if v is None:
            return Looted.BLIND
        free_before = v.get("bags.free")
        if free_before == 0:
            self.detail = "bags are full; looting would take nothing"
            return Looted.BAGS_FULL

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
            free_now = after.get("bags.free")
            if free_before is not None and free_now is not None and free_now < free_before:
                self.took += 1
                self._close_if_open(after)
                return Looted.TOOK
            if after.get("ui.loot") is True:
                self.took += 1
                self._close_if_open(after)
                return Looted.TOOK

        self.detail = "clicked the corpse and nothing came off it"
        return Looted.NOTHING

    def _close_if_open(self, values: dict) -> None:
        """Auto-loot usually closes itself. When it does not, a left-open loot window
        swallows the next click, so it is shut - against a screen the radio has measured,
        not a blind Escape."""
        if values.get("ui.loot") is True:
            time.sleep(0.4)
            still = self.read()
            if still is not None and still.get("ui.loot") is True:
                self.hid.tap("esc")
