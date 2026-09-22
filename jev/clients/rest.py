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

from jev.run.evidence import event, traced
from jev.world.combat import CombatProfile, Role, for_class

# Which slot holds food is **not** a constant. Both consumables are item class 0,
# subclass 5 and neither says which is which in its name, so the generated profile tells
# them apart by what their spell restores: aura 84 is health and 85 is mana. On a human
# paladin that is Darnassian Bleu in slot 12 and Refreshing Spring Water in slot 11 —
# the opposite way round from the constant this file used to carry, which is why it
# reported "out of food" with a wheel of cheese sitting in the bar.
SLOT_KEYS = {**{n: str(n) for n in range(1, 10)}, 10: "0", 11: "minus", 12: "equals"}


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
    profile: CombatProfile | None = None

    slot: int | None = field(default=None, init=False)
    started_at: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    @traced("rest")
    def until(self, fraction: float = 0.92, *, role: Role = Role.FOOD,
              timeout_s: float = 45.0) -> Rested:
        """Eat until health reaches `fraction`, or say why not.

        `role` picks what to consume: food restores health, drink restores mana. Which
        slot that is comes from the profile, because it differs by race and the two are
        indistinguishable by item class.
        """
        self.detail = ""
        deadline = time.monotonic() + timeout_s
        eating = False
        gauge = "vitals.power" if role is Role.DRINK else "vitals.hp"
        event("rest.request", data={"role": role.value, "gauge": gauge,
                                    "fraction": fraction, "timeout_s": timeout_s})

        while time.monotonic() < deadline:
            v = self.read()
            event("rest.observed", code="blind" if v is None else "readable",
                  data={} if v is None else {key: v.get(key) for key in (
                      gauge, "vitals.combat", "bars.usable")})
            if v is None:
                return Rested.BLIND
            level = v.get(gauge)
            if level is not None and level >= fraction:
                return Rested.HEALTHY
            if v.get("vitals.combat") is True:
                self.detail = "in combat; not a moment to eat"
                return Rested.INTERRUPTED

            profile = self.profile or for_class(v.get("char.class_id"),
                                                v.get("char.race_id"))
            ability = profile.first(role)
            if ability is None:
                self.detail = f"this character has no {role.value} on its bar"
                return Rested.NO_FOOD
            self.slot = ability.slot

            usable = v.get("bars.usable")
            if usable is not None and not (usable & (1 << (ability.slot - 1))):
                self.detail = (f"slot {ability.slot} ({ability.name or role.value}) "
                               f"is not usable; out of {role.value}")
                return Rested.NO_FOOD

            if not eating:
                event("consume.request", data={"slot": ability.slot, "role": role.value})
                self.hid.tap(SLOT_KEYS.get(ability.slot, str(ability.slot)))
                self.started_at = time.monotonic()
                eating = True
            time.sleep(1.0)

        self.detail = f"{timeout_s:.0f}s and still short of {fraction:.0%}"
        return Rested.TIMEOUT
