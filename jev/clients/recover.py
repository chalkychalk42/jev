"""Get back up after dying. Release, walk to the corpse, resurrect.

Dying is not an exceptional case in a levelling bot; it is a Tuesday. A level 1 paladin
walked into the middle of a kobold camp, pulled four of them, and that is how this file
came to exist.

Three facts make it simple, and all three come from the radio rather than from a guess:

    vitals.dead      the character is down; the release popup is up
    vitals.ghost     released, and standing at the graveyard
    ui.advance_x/y   where the popup's button is, painted by the addon

That third one is the same field the quest frames use, and deliberately so. "Release
Spirit" and "Resurrect Now" are the same intent as "Accept" — *move this forward* — so
they are the same painted point and there is no second mechanism to keep working. The
addon paints it; **whether pressing it is right is decided here**, against `vitals.dead`,
because a StaticPopup is also how the game asks whether to destroy an item.

Where the corpse is
-------------------
Read before releasing. A dead character lies where it fell and the strip still paints its
position, so the corpse is simply where we were standing a moment ago. After releasing,
that is no longer true — the ghost is at the graveyard — and there is no API in 2.4.3 that
will give the corpse back. Recording it first is the whole trick.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Recovered(StrEnum):
    ALIVE = "alive"                # back on our feet
    NOT_DEAD = "not_dead"          # nothing to do
    NO_BUTTON = "no_button"        # dead, but no popup painted to press
    NO_CORPSE = "no_corpse"        # could not read where we fell
    STILL_GHOST = "still_ghost"    # released and walked, but did not get up
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self is Recovered.ALIVE


@dataclass
class Recover:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    # Map point -> did we get there. Injected, like `Interact.approach`: the planner is
    # the guide layer's and this skill has no business knowing about navmeshes.
    walk_to: Callable[[tuple[float, float]], bool] | None = None

    corpse: tuple[float, float] | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def run(self, corpse: tuple[float, float] | None = None, *,
            settle_s: float = 3.0, tries: int = 20) -> Recovered:
        """Release, walk back, and get up. Reports where it stopped.

        `corpse` is for the case this skill cannot recover from on its own: a character
        left dead long enough **auto-releases**, and by the time anything asks, the ghost
        is at the graveyard and the body's position is gone for good. A caller that saw
        the death knows where it happened and can hand it back.
        """
        self.corpse = corpse
        self.detail = ""

        v = self.read()
        if v is None:
            return Recovered.BLIND
        if v.get("vitals.dead") is not True and v.get("vitals.ghost") is not True:
            return Recovered.NOT_DEAD

        if v.get("vitals.ghost") is not True:
            # Still at the corpse, so this is the only moment its position is readable.
            if v.get("pos.mx") is None:
                self.detail = "dead, but the strip is not painting a position"
                return Recovered.NO_CORPSE
            self.corpse = (v["pos.mx"], v["pos.my"])   # read before releasing
            if not self._press(v):
                self.detail = "dead, but no popup button is painted to release with"
                return Recovered.NO_BUTTON
            time.sleep(settle_s)

        if self.corpse is None:
            self.detail = ("a ghost with no corpse position; it auto-released before "
                           "anything recorded where it fell")
            return Recovered.NO_CORPSE
        if self.walk_to is not None:
            self.walk_to(self.corpse)

        # At the corpse the game offers the same kind of popup again.
        for _ in range(tries):
            v = self.read()
            if v is None:
                return Recovered.BLIND
            if v.get("vitals.dead") is not True and v.get("vitals.ghost") is not True:
                return Recovered.ALIVE
            self._press(v)
            time.sleep(1.0)

        self.detail = "released and walked back, but still a ghost"
        return Recovered.STILL_GHOST

    def _press(self, values: dict) -> bool:
        """Click the painted popup button, if there is one."""
        fx, fy = values.get("ui.advance_x"), values.get("ui.advance_y")
        if fx is None or fy is None:
            return False
        ox, oy = self.window_origin
        w, h = self.window_size
        self.hid.click(ox + round(fx * w), oy + round(fy * h))
        return True
