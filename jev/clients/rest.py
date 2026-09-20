"""Eat until healthy. The cheapest thing that stops a levelling bot dying.

A character that fights every pull at whatever health it happens to have will eventually
meet one it cannot win, and then it is a corpse run — which on this server costs two
hundred yards and several minutes. Sitting still for twenty seconds is cheaper than that
every single time.

There is nothing clever here, and that is the point:

    press the food slot        which slot is configuration, like the rotation
    watch `vitals.hp`          the client's own number, not a timer
    stop the moment combat starts   eating through a pull is how it gets worse

`bars.usable` is what says whether there is any food left. A slot with nothing in it is
not usable, so an empty bag reports `NO_FOOD` rather than sitting there pressing a blank
button and waiting for health that is never coming.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

# Where food and water live on a fresh character's bar. Default bindings run 1-9, 0, then
# the two keys left of Backspace, and this is where the game itself puts them.
FOOD_SLOT = 11
DRINK_SLOT = 12
SLOT_KEYS = {11: "minus", 12: "equals"}


class Rested(StrEnum):
    HEALTHY = "healthy"        # got there
    NO_FOOD = "no_food"        # the slot is empty, or unusable
    INTERRUPTED = "interrupted"  # something attacked us
    TIMEOUT = "timeout"
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self is Rested.HEALTHY


@dataclass
class Rest:
    hid: object
    read: Callable[[], dict | None]
    slot: int = FOOD_SLOT

    started_at: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def until(self, fraction: float = 0.92, *, timeout_s: float = 45.0) -> Rested:
        """Eat until health reaches `fraction`, or say why not."""
        self.detail = ""
        deadline = time.monotonic() + timeout_s
        eating = False

        while time.monotonic() < deadline:
            v = self.read()
            if v is None:
                return Rested.BLIND
            hp = v.get("vitals.hp")
            if hp is not None and hp >= fraction:
                return Rested.HEALTHY
            if v.get("vitals.combat") is True:
                self.detail = "in combat; not a moment to eat"
                return Rested.INTERRUPTED

            usable = v.get("bars.usable")
            if usable is not None and not (usable & (1 << (self.slot - 1))):
                self.detail = f"slot {self.slot} is not usable; out of food"
                return Rested.NO_FOOD

            if not eating:
                key = SLOT_KEYS.get(self.slot, str(self.slot))
                self.hid.tap(key)
                self.started_at = time.monotonic()
                eating = True
            time.sleep(1.0)

        self.detail = f"{timeout_s:.0f}s and still short of {fraction:.0%}"
        return Rested.TIMEOUT
