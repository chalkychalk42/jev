"""Use the hearthstone: home to where the character is bound, in one ten-second cast.

For getting out of somewhere the character cannot survive. A level 3 paladin whose failed
hand-in had sent it into level 6 wolf country died there again and again (runs
20260923T174132-d01302 to ...181209-bc03ba): every way home was a walk through the same
wolves, and the hearthstone every new character carries is bound to its starting inn.

The stone is found the way selling finds junk. The strip paints one bag slot per paint
(`inventory.*`): its item, and its screen point once its bag is open, or its bag's opener
while it is not. Arrival is the position moving by more than a walk could in the cast.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced

HEARTHSTONE = 6948
CAST_S = 10.0
# A census of every bag slot comes round in a few seconds at the strip's paint rate.
FIND_S = 8.0
# Map fractions the character must have moved by: about seventy yards in Elwynn, which
# ten seconds standing still casting cannot cover.
MOVED = 0.02
# Or between two reads a quarter of a second apart, which no walk covers: bound in the same
# inn, the stone moved a character wedged upstairs in the Lion's Pride Inn 21 yards, to
# Innkeeper Farley in the hall, and that was called "did not move" (session 111).
JUMP = 0.003


class Hearthed(StrEnum):
    HOME = "home"
    NO_STONE = "no_stone"        # no hearthstone came round in the bag census
    NOT_READY = "not_ready"      # pressed, and the character did not move: cooldown, or cancelled
    IN_COMBAT = "in_combat"      # the cast cannot be made, or was broken, in combat
    BLIND = "blind"
    REFUSED = "refused"

    @property
    def ok(self) -> bool:
        return self is Hearthed.HOME


@dataclass
class Hearth:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    detail: str = field(default="", init=False)

    @traced("hearth")
    def run(self) -> Hearthed:
        self.detail = ""
        v = self.read()
        if v is None or v.get("pos.mx") is None or v.get("pos.my") is None:
            return Hearthed.BLIND
        if v.get("vitals.combat") is True:
            return Hearthed.IN_COMBAT
        start = (v["pos.mx"], v["pos.my"])
        stone, opener = self._find()
        if isinstance(stone, Hearthed):
            return stone
        event("hearth.request", data={"bag": stone.get("inventory.bag"),
                                      "slot": stone.get("inventory.slot"), "from": list(start)})
        # What was opened is closed however the press ends. Left open after a stone on
        # cooldown, the backpack covered the screen's lower right, the stone's tooltip
        # over it, for the rest of the session (session 158).
        try:
            return self._press(stone, start)
        finally:
            if opener is not None:
                self._click(opener, "inventory.open_")

    def _press(self, stone: dict, start: tuple[float, float]) -> Hearthed:
        if not self._click(stone, "inventory.", right=True):
            return Hearthed.REFUSED
        deadline = self.monotonic() + CAST_S + 8.0
        last = start
        while self.monotonic() < deadline:
            v = self.read()
            if v is not None and v.get("pos.mx") is not None and v.get("pos.my") is not None:
                at, last = last, (v["pos.mx"], v["pos.my"])
                if math.dist(start, last) > MOVED or math.dist(at, last) > JUMP:
                    event("hearth.arrived", data={"to": [v["pos.mx"], v["pos.my"]]})
                    return Hearthed.HOME
                if v.get("vitals.combat") is True:
                    self.detail = "combat broke the cast"
                    return Hearthed.IN_COMBAT
            self.sleep(0.25)
        self.detail = "pressed the hearthstone and did not move: cooldown, or the cast failed"
        return Hearthed.NOT_READY

    def _find(self):
        """The stone's painted slot, and the bag opener clicked to show it (or `None`)."""
        opener = None
        deadline = self.monotonic() + FIND_S
        while self.monotonic() < deadline:
            v = self.read()
            if v is not None and v.get("inventory.item_id") == HEARTHSTONE:
                if v.get("inventory.x") is not None and v.get("inventory.y") is not None:
                    return v, opener
                if opener is None and v.get("inventory.open_x") is not None:
                    if not self._click(v, "inventory.open_"):
                        return Hearthed.REFUSED, None
                    opener = v
            self.sleep(0.05)
        self.detail = "no hearthstone came round in the bag census"
        return Hearthed.NO_STONE, None

    def _click(self, values: dict, prefix: str, *, right: bool = False) -> bool:
        x, y = values.get(prefix + "x"), values.get(prefix + "y")
        if x is None or y is None:
            return False
        ox, oy = self.window_origin
        w, h = self.window_size
        point = (ox + round(x * w), oy + round(y * h))
        event("hearth.click", data={"prefix": prefix, "right": right, "point": list(point)})
        return self.hid.click(*point, right=right) is not False
