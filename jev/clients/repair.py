"""Get the gear working again. Walk to a merchant who repairs, press Repair All.

Durability is not a cosmetic. A weapon at zero is an **unequipped** weapon: the character
swings its fists, a level 2 paladin does 4-5 damage with them, and every pull runs the
full timeout and ends either `timeout` or `died` - which takes another 10% off everything
else. That is a spiral with no floor, and the bot rode it for an evening while
`bags.durability_min` sat at 0.0 and nothing looked at it.

So this is not a convenience skill. It is the other half of dying, and every class and
every level has it.

Why there is no new button mechanism
------------------------------------
`MerchantRepairAllButton` is in the addon's `ADVANCE_BUTTONS` list, so it arrives as
`ui.advance_x/y` exactly like Accept, Complete Quest and Release Spirit do. They are all
the same intent - *move this forward* - and the decision about whether pressing is right
belongs here rather than in the addon. This one checks `ui.vendor`, because a painted
button with no merchant open is some other frame's.

The button only exists on a merchant that can actually repair, which is also the check for
"is this the right NPC": if it is not painted, this is a merchant who sells cheese.

What counts as repaired
-----------------------
`bags.durability_min` rising. Not the click, and not the frame: a click with no money
behind it leaves the button sitting there looking pressed, and 27 copper does not go far.
Reporting `too_poor` honestly is what lets the caller go and sell something instead of
walking back to the same vendor forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced

# Below this, go and repair. Not zero: arriving at a vendor with the weapon already broken
# means the fights that broke it were already being lost, and a corpse run costs more than
# a walk to the Abbey.
REPAIR_BELOW = 0.35

# Broken. Nothing that uses gear should be attempted at this, at any distance.
BROKEN = 0.0

# How long to let the merchant frame open, and the repair land.
OPEN_S = 4.0
SETTLE_S = 1.5


class Repaired(StrEnum):
    DONE = "done"                # durability came back up
    NOT_NEEDED = "not_needed"    # gear is fine, or nothing equipped has durability
    NO_VENDOR = "no_vendor"      # could not get a merchant frame open
    NO_BUTTON = "no_button"      # merchant open, but it does not repair
    TOO_POOR = "too_poor"        # pressed, and nothing changed
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self in (Repaired.DONE, Repaired.NOT_NEEDED)


@dataclass
class Repair:
    hid: object
    read: Callable[[], dict | None]
    # Approach a repair merchant and right-click it. Injected for the same reason
    # `Recover.walk_to` is: choosing the NPC is the guide's job and clicking it is
    # `Interact`'s, and this skill has no business doing either itself.
    visit: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)

    before: float | None = field(default=None, init=False)
    after: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def needed(self, values: dict | None = None) -> bool:
        """Is it worth the walk? `None` durability is no observation, not full repair."""
        v = values if values is not None else self.read()
        if v is None:
            return False
        worst = v.get("bags.durability_min")
        return worst is not None and worst <= REPAIR_BELOW

    @traced("repair")
    def run(self) -> Repaired:
        self.before = self.after = None
        self.detail = ""

        v = self.read()
        self._observe(v)
        if v is None:
            return Repaired.BLIND
        self.before = v.get("bags.durability_min")
        if not self.needed(v):
            return Repaired.NOT_NEEDED

        if not self.visit():
            self.detail = "could not get to a merchant that repairs"
            return Repaired.NO_VENDOR

        v = self._await(lambda r: r.get("ui.vendor") is True, OPEN_S)
        if v is None:
            self.detail = "clicked the merchant, but no vendor frame opened"
            return Repaired.NO_VENDOR

        if not self._press(v):
            self._close()
            self.detail = "vendor frame is open, but this one does not repair"
            return Repaired.NO_BUTTON

        # The button is painted whether or not the money is there, so believe the
        # durability rather than the click.
        landed = self._await(
            lambda r: (r.get("bags.durability_min") or 0.0) > (self.before or 0.0),
            SETTLE_S + OPEN_S)
        self._close()
        after = self.read()
        self._observe(after)
        self.after = after.get("bags.durability_min") if after else None
        if landed is None:
            self.detail = (f"pressed Repair All and the worst item is still "
                           f"{(self.before or 0.0):.0%}; not enough money")
            return Repaired.TOO_POOR
        return Repaired.DONE

    def _await(self, ready, seconds: float) -> dict | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            v = self.read()
            self._observe(v)
            if v is not None and ready(v):
                return v
            time.sleep(0.25)
        return None

    def _press(self, values: dict) -> bool:
        fx, fy = values.get("ui.advance_x"), values.get("ui.advance_y")
        if fx is None or fy is None:
            return False
        ox, oy = self.window_origin
        w, h = self.window_size
        event("repair.request", data={"point": [ox + round(fx * w), oy + round(fy * h)],
                                      "durability": values.get("bags.durability_min"),
                                      "money_copper": values.get("bags.money_copper")})
        self.hid.click(ox + round(fx * w), oy + round(fy * h))
        return True

    def _close(self) -> None:
        """Leave the merchant shut. An open frame swallows the next click."""
        self.hid.tap("esc")
        time.sleep(0.4)

    @staticmethod
    def _observe(values: dict | None) -> None:
        event("repair.observed", code="blind" if values is None else "readable",
              data={} if values is None else {key: values.get(key) for key in (
                  "ui.vendor", "bags.durability_min", "bags.money_copper",
                  "ui.advance_x", "ui.advance_y")})
