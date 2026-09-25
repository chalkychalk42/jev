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
# A gauge that has not risen by `RISE` for `STALL_S` has finished its food or drink: another
# is taken, `TAKES` in all. With nothing to take, the body's own regeneration is waited on,
# until it too stops rising for `REGEN_STALL_S` (V173).
RISE = 0.005
STALL_S = 5.0
TAKES = 3
REGEN_STALL_S = 10.0


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
    # When the bar's slot for the role is empty: eat or drink from the bags instead, `True`
    # if something was taken (a caster's conjured water, V166).
    use_item: Callable[[Role], bool] | None = None

    slot: int | None = field(default=None, init=False)
    started_at: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def _take(self, v: dict, role: Role) -> bool | None:
        """Begin eating or drinking: the bar's slot, else the bags (`use_item`). `True` if
        something was taken, `False` if there is nothing, `None` if the bar has no such row."""
        profile = self.profile or for_class(v.get("char.class_id"), v.get("char.race_id"))
        ability = profile.first(role)
        if ability is None:
            self.detail = f"this character has no {role.value} on its bar"
            return None
        self.slot = ability.slot
        usable = v.get("bars.usable")
        if usable is None or usable & (1 << (ability.slot - 1)):
            event("consume.request", data={"slot": ability.slot, "role": role.value})
            self.hid.tap(SLOT_KEYS.get(ability.slot, str(ability.slot)))
            return True
        if self.use_item is not None and self.use_item(role):
            return True
        self.detail = (f"slot {ability.slot} ({ability.name or role.value}) is not usable "
                       f"and the bags hold none; out of {role.value}")
        return False

    @traced("rest")
    def until(self, fraction: float = 0.92, *, role: Role = Role.FOOD,
              timeout_s: float = 45.0) -> Rested:
        """Eat until health reaches `fraction`, or say why not.

        `role` picks what to consume: food restores health, drink restores mana. Which
        slot that is comes from the profile, because it differs by race and the two are
        indistinguishable by item class. When one runs out short of the mark (the gauge has
        stopped rising for `STALL_S`), another is taken, `TAKES` in all (V173). With
        nothing to take, the body's own regeneration is waited on while it still rises: out
        of drink stopped whole sessions, and a caster's conjured water can run out before
        the next conjure.
        """
        return self._until({role: fraction}, timeout_s)

    @traced("rest.both")
    def until_both(self, health: float, mana: float, *, timeout_s: float = 45.0) -> Rested:
        """Eat and drink at once, until health reaches `health` and mana `mana` (V167).

        The two go together in the game, and a caster after a fight is often short of both:
        one after the other was two meals' time for one meal's worth."""
        return self._until({Role.FOOD: health, Role.DRINK: mana}, timeout_s)

    def _until(self, marks: dict[Role, float], timeout_s: float) -> Rested:
        self.detail = ""
        deadline = time.monotonic() + timeout_s
        gauges = {Role.FOOD: "vitals.hp", Role.DRINK: "vitals.power"}
        takes = dict.fromkeys(marks, 0)
        regen = dict.fromkeys(marks, False)      # nothing left to take for this role
        best: dict[Role, float | None] = dict.fromkeys(marks)
        rose_at = dict.fromkeys(marks, time.monotonic())
        event("rest.request", data={"marks": {r.value: f for r, f in marks.items()},
                                    "timeout_s": timeout_s})
        while time.monotonic() < deadline:
            v = self.read()
            event("rest.observed", code="blind" if v is None else "readable",
                  data={} if v is None else {key: v.get(key) for key in (
                      "vitals.hp", "vitals.power", "vitals.combat", "bars.usable")})
            if v is None:
                return Rested.BLIND
            short = [r for r, mark in marks.items()
                     if (v.get(gauges[r]) is not None and v.get(gauges[r]) < mark)]
            if not short:
                return Rested.HEALTHY
            if v.get("vitals.combat") is True:
                self.detail = "in combat; not a moment to eat"
                return Rested.INTERRUPTED
            now = time.monotonic()
            for role in short:
                level = v.get(gauges[role])
                if best[role] is None or level > best[role] + RISE:
                    best[role], rose_at[role] = level, now
                stalled = now - rose_at[role] >= STALL_S
                if regen[role]:
                    if now - rose_at[role] >= REGEN_STALL_S:
                        # A timeout, not "no food": that stops the session, and the policy's
                        # restock is what answers an empty bar.
                        self.detail = (f"nothing to {'eat' if role is Role.FOOD else 'drink'} "
                                       f"and {gauges[role]} no longer rising")
                        return Rested.TIMEOUT
                    continue
                if takes[role] and (not stalled or takes[role] >= TAKES):
                    continue
                taken = self._take(v, role)
                if taken is None:
                    return Rested.NO_FOOD
                if taken:
                    takes[role] += 1
                    self.started_at = now
                else:
                    regen[role] = True       # wait on regeneration while it still rises
                rose_at[role] = now
            time.sleep(1.0)
        self.detail = f"{timeout_s:.0f}s and still short"
        return Rested.TIMEOUT
