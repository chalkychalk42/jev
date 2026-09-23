"""Get back up after dying. Release, walk to the corpse, resurrect.

Dying is not an exceptional case in a levelling bot; it is a Tuesday. A level 1 paladin
walked into the middle of a kobold camp, pulled four of them, and that is how this file
came to exist.

Three facts make it simple, and all three come from the radio rather than from a guess:

    vitals.dead      the character is down; the release popup is up
    vitals.ghost     released, and standing at the graveyard
    pos.corpse_mx/my where the body is, straight from the game
    ui.advance_x/y   where the popup's button is, painted by the addon

That third one is the same field the quest frames use, and deliberately so. "Release
Spirit" and "Resurrect Now" are the same intent as "Accept" — *move this forward* — so
they are the same painted point and there is no second mechanism to keep working. The
addon paints it; **whether pressing it is right is decided here**, against `vitals.dead`,
because a StaticPopup is also how the game asks whether to destroy an item.

Where the corpse is
-------------------
`GetCorpseMapPosition()`, painted as `pos.corpse_mx/my`. It answers while dead **and**
while a ghost, in the same coordinates as `pos.mx/my`, and it is the only source here that
is not a guess.

This file used to say there was no such API, and recovered by noting where the character
was standing before it released. That works right up to the first auto-release, after
which the position is gone and the caller has to invent one — usually the node the run was
working. A ghost then walked to a node it had died a hundred and fifty yards short of,
found no body, and reported `still_ghost`; thirteen passes and a ring of seven stations
around the guess all arrived correctly at the wrong place. The remembered position is kept
as a fallback for a strip that will not paint, and the caller's guess behind that, but
neither is ever preferred to the game's own answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced

# The graveyard's resurrector, visible only to the dead. Resurrecting there costs
# durability, and below level 10 nothing else.
SPIRIT_HEALER = "Spirit Healer"
# A ghost this close to where it appeared is still beside the Spirit Healer (map fractions,
# about fifteen yards in Elwynn).
GRAVEYARD_REACH = 0.004


class Recovered(StrEnum):
    RELEASED = "released"        # ghost positively observed after releasing
    NOT_RELEASED = "not_released"
    ALIVE = "alive"                # back on our feet
    NOT_DEAD = "not_dead"          # nothing to do
    NO_BUTTON = "no_button"        # dead, but no popup painted to press
    NO_CORPSE = "no_corpse"        # could not read where we fell
    STILL_GHOST = "still_ghost"    # released and walked, but did not get up
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self in (Recovered.ALIVE, Recovered.RELEASED)


def _painted(values: dict | None) -> tuple[float, float] | None:
    """The corpse position the strip is carrying, if it is carrying one.

    Both halves or neither: a corpse at `(x, None)` is a decode that went wrong, and
    walking to it would be walking to the equator.
    """
    if not values:
        return None
    mx, my = values.get("pos.corpse_mx"), values.get("pos.corpse_my")
    if mx is None or my is None:
        return None
    return (float(mx), float(my))


@dataclass
class Recover:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    # Map point -> did we get there. Injected, like `Interact.approach`: the planner is
    # the guide layer's and this skill has no business knowing about navmeshes.
    walk_to: Callable[[tuple[float, float]], bool] | None = None
    # Right-click a named unit through the shared, identity-checked interaction.
    interact: Callable[[str], object] | None = None

    corpse: tuple[float, float] | None = field(default=None, init=False)
    # Where the ghost appeared: the graveyard, and its Spirit Healer.
    graveyard: tuple[float, float] | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    @traced("recovery")
    def run(self, corpse: tuple[float, float] | None = None, *,
            settle_s: float = 3.0, tries: int = 20, release_only: bool = False) -> Recovered:
        """Release, walk back, and get up. Reports where it stopped.

        `corpse` is for the case this skill cannot recover from on its own: a character
        left dead long enough **auto-releases**, and by the time anything asks, the ghost
        is at the graveyard and the body's position is gone for good. A caller that saw
        the death knows where it happened and can hand it back.
        """
        self.corpse = corpse           # the caller's guess, until the strip says better
        self.detail = ""
        event("recovery.request", data={"caller_corpse": corpse,
                                         "release_only": release_only, "tries": tries})

        v = self.read()
        self._observe(v)
        if v is None:
            return Recovered.BLIND
        if v.get("vitals.dead") is not True and v.get("vitals.ghost") is not True:
            return (Recovered.NOT_DEAD if v.get("vitals.dead") is False
                    and v.get("vitals.ghost") is False else Recovered.BLIND)

        self.corpse = _painted(v) or self.corpse
        if v.get("vitals.ghost") is not True:
            # Lying where we fell, so our own position is a corpse position too - but
            # only as a fallback, and only if the game is not already telling us.
            if self.corpse is None and v.get("pos.mx") is None:
                self.detail = "dead, but the strip is not painting a position"
                return Recovered.NO_CORPSE
            if self.corpse is None:
                self.corpse = (v["pos.mx"], v["pos.my"])
            if not self._press(v):
                self.detail = "dead, but no popup button is painted to release with"
                return Recovered.NO_BUTTON
            time.sleep(settle_s)
            after = self.read()
            self._observe(after)
            if after is not None:
                # Releasing is what makes the corpse a corpse; ask again now that it is.
                self.corpse = _painted(after) or self.corpse
                if after.get("vitals.ghost") is True and after.get("pos.mx") is not None:
                    self.graveyard = (after["pos.mx"], after["pos.my"])
            if release_only:
                return (Recovered.RELEASED if after and after.get("vitals.ghost") is True
                        else Recovered.NOT_RELEASED)

        if release_only:
            return Recovered.RELEASED

        if self.corpse is None:
            self.detail = ("a ghost with no corpse position; it auto-released before "
                           "anything recorded where it fell")
            return Recovered.NO_CORPSE
        if self.walk_to is not None:
            event("corpse.approach", data={"destination": self.corpse})
            self.walk_to(self.corpse)

        # At the corpse the game offers the same kind of popup again.
        for _ in range(tries):
            v = self.read()
            self._observe(v)
            if v is None:
                return Recovered.BLIND
            if v.get("vitals.dead") is False and v.get("vitals.ghost") is False:
                return Recovered.ALIVE
            self.corpse = _painted(v) or self.corpse
            self._press(v)
            time.sleep(1.0)

        self.detail = "released and walked back, but still a ghost"
        return Recovered.STILL_GHOST

    @traced("recovery.spirit_healer")
    def run_spirit_healer(self, tries: int = 8) -> Recovered:
        """Get up at the graveyard instead of at the body.

        For a body lying where something that kills this character still stands: run
        20260923T181209-bc03ba resurrected beside a level 6 wolf three times, at half
        health each time, and died each time. The Spirit Healer answers a right-click with
        the same kind of popup as the body, so the same painted button accepts it.
        """
        self.detail = ""
        v = self.read()
        self._observe(v)
        if v is None:
            return Recovered.BLIND
        if v.get("vitals.ghost") is not True:
            return (Recovered.NOT_DEAD if v.get("vitals.dead") is False
                    else Recovered.BLIND if v.get("vitals.dead") is None else Recovered.NO_BUTTON)
        if self.graveyard is None or self.interact is None:
            self.detail = "no graveyard seen, or nothing to talk to its Spirit Healer with"
            return Recovered.STILL_GHOST
        here = (v.get("pos.mx"), v.get("pos.my"))
        far = None in here or max(abs(here[0] - self.graveyard[0]),
                                  abs(here[1] - self.graveyard[1])) > GRAVEYARD_REACH
        if far and self.walk_to is not None:
            event("graveyard.approach", data={"destination": self.graveyard})
            self.walk_to(self.graveyard)
        opened = self.interact(SPIRIT_HEALER)
        event("spirit_healer.interact", data={"result": str(opened)})
        for _ in range(tries):
            v = self.read()
            self._observe(v)
            if v is None:
                return Recovered.BLIND
            if v.get("vitals.dead") is False and v.get("vitals.ghost") is False:
                return Recovered.ALIVE
            if v.get("ui.modal") is True:
                self._press(v)
            time.sleep(1.0)
        self.detail = f"talked to the Spirit Healer ({opened}) and did not get up"
        return Recovered.STILL_GHOST

    def _press(self, values: dict) -> bool:
        """Click the painted popup button, if there is one."""
        fx, fy = values.get("ui.advance_x"), values.get("ui.advance_y")
        if fx is None or fy is None:
            return False
        ox, oy = self.window_origin
        w, h = self.window_size
        event("recovery.click", data={"point": [ox + round(fx * w), oy + round(fy * h)],
                                      "dead": values.get("vitals.dead"),
                                      "ghost": values.get("vitals.ghost")})
        self.hid.click(ox + round(fx * w), oy + round(fy * h))
        return True

    @staticmethod
    def _observe(values: dict | None) -> None:
        event("recovery.observed", code="blind" if values is None else "readable",
              data={} if values is None else {key: values.get(key) for key in (
                  "vitals.dead", "vitals.ghost", "pos.mx", "pos.my",
                  "pos.corpse_mx", "pos.corpse_my", "ui.advance_x", "ui.advance_y")})
