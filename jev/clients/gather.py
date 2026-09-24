"""Collect a quest's world objects: crates of Milly's Harvest, Bundles of Wood.

An object has no nameplate and no unit behind it, so neither the plate search nor the
mouseover token can find one. The stock tooltip names it, and the strip paints that name
(`cursor.object_id`) while the pointer is over the world with no unit under it. Standing at
one of the object's spawn points, it lies at or just ahead of the character's feet, so the
pointer searches outward from there, and the right-click goes only where a fresh hover
names the object wanted - never on a guess.

What was taken is judged as looting is (`jev.clients.loot`): the objective counter first,
because it is the server's own tally, then a bag slot filling. A right-click that changed
neither is `NOTHING`, and an empty spawn point is `NOT_HERE`: someone took it, or it has
not respawned.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.clients.targeting import HoverCode, Targeting
from jev.clients.windows import CloseCode, close_observed
from jev.run.evidence import event, traced

# Where the pointer looks, as fractions of the client: from just ahead of the character's
# feet outward, square ring by square ring. The camera sits behind and above the character,
# so ground a step or two ahead draws a little below the centre of the screen.
SEARCH_ORIGIN = (0.5, 0.60)
SEARCH_STEP = (0.05, 0.05)
SEARCH_RINGS = 3
# An opening can be a cast of a few seconds before the loot, and auto loot then takes it.
OPEN_S = 8.0
OPEN_LOOK_S = 0.25
# A poster or a body answers a click with its quest window at once.
OPEN_WINDOW_S = 3.0


def search_points(origin: tuple[float, float] = SEARCH_ORIGIN,
                  step: tuple[float, float] = SEARCH_STEP,
                  rings: int = SEARCH_RINGS) -> tuple[tuple[float, float], ...]:
    """The origin, then each square ring round it, nearest first within a ring."""
    points = [origin]
    for ring in range(1, rings + 1):
        edge = [(origin[0] + dx * step[0], origin[1] + dy * step[1])
                for dx in range(-ring, ring + 1) for dy in range(-ring, ring + 1)
                if max(abs(dx), abs(dy)) == ring]
        points += sorted(edge, key=lambda p: (p[0] - origin[0]) ** 2 + (p[1] - origin[1]) ** 2)
    return tuple((round(x, 4), round(y, 4)) for x, y in points if 0 <= x <= 1 and 0 <= y <= 1)


class Gathered(StrEnum):
    TOOK = "took"              # the objective moved, or a bag slot filled
    NOTHING = "nothing"        # clicked the named object and nothing observed changed
    NOT_HERE = "not_here"      # no hover named the object anywhere searched
    BAGS_FULL = "bags_full"
    BLIND = "blind"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"
    WINDOW_OPEN = "window_open"

    @property
    def ok(self) -> bool:
        return self in (Gathered.TOOK, Gathered.NOTHING, Gathered.NOT_HERE)


@dataclass
class Gather:
    hid: object
    read: Callable[[], dict | None]
    targeting: Targeting
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    @traced("gather")
    def pick(self, name_id: int,
             progress: Callable[[], tuple[int | None, int | None]] | None = None) -> Gathered:
        """Find the object named `name_id` near the character and take it."""
        self.clicked, self.detail = None, ""
        values = self.read()
        if values is None:
            return Gathered.BLIND
        if values.get("bags.free") == 0:
            self.detail = "bags are full; the object would give nothing"
            return Gathered.BAGS_FULL
        if values.get("vitals.combat") is True or values.get("ui.modal") is True:
            self.detail = "combat or a dialog; not reaching for an object"
            return Gathered.INTERRUPTED
        counter = tuple(progress()) if progress is not None else (None, None)
        (ox, oy), (w, h) = self.window_origin, self.window_size
        for fx, fy in search_points():
            point = (ox + round(fx * w), oy + round(fy * h))
            hover = self.targeting.probe(point, require_target=False)
            if hover.code is HoverCode.REFUSED:
                self.detail = "pointer input refused"
                return Gathered.REFUSED
            if hover.code is HoverCode.BLIND:
                self.detail = hover.detail
                return Gathered.BLIND
            after = hover.after or {}
            if (after.get("cursor.object_id") != name_id or after.get("cursor.has") is not False
                    or after.get("cursor.world") is not True):
                continue
            event("gather.request", data={"point": list(hover.point), "name_id": name_id})
            if not self.hid.click(*hover.point, right=True):
                self.detail = "object click refused"
                return Gathered.REFUSED
            self.clicked = hover.point
            return self._observe(values, counter, progress)
        self.detail = "no hover named the object near this spawn point"
        return Gathered.NOT_HERE

    @traced("gather.open")
    def open(self, name_id: int, *, wait_s: float = OPEN_WINDOW_S) -> bool:
        """Right-click the object named `name_id` near the character - a wanted poster, a
        half-eaten body - and say whether a quest or gossip window is observed open."""
        self.clicked, self.detail = None, ""
        (ox, oy), (w, h) = self.window_origin, self.window_size
        for fx, fy in search_points():
            point = (ox + round(fx * w), oy + round(fy * h))
            hover = self.targeting.probe(point, require_target=False)
            if hover.code in (HoverCode.REFUSED, HoverCode.BLIND):
                self.detail = hover.detail or "pointer input refused"
                return False
            after = hover.after or {}
            if (after.get("cursor.object_id") != name_id or after.get("cursor.has") is not False
                    or after.get("cursor.world") is not True):
                continue
            event("gather.open", data={"point": list(hover.point), "name_id": name_id})
            if not self.hid.click(*hover.point, right=True):
                self.detail = "object click refused"
                return False
            self.clicked = hover.point
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                time.sleep(OPEN_LOOK_S)
                values = self.read() or {}
                if values.get("ui.quest_frame") is True or values.get("ui.gossip") is True:
                    return True
            self.detail = "the object was clicked and no quest or gossip window opened"
            return False
        self.detail = "no hover named the object near its placed point"
        return False

    def _observe(self, before: dict, counter, progress) -> Gathered:
        deadline = time.monotonic() + OPEN_S
        after = before
        while time.monotonic() < deadline:
            time.sleep(OPEN_LOOK_S)
            after = self.read()
            if after is None:
                self.detail = "radio lost after the object click; outcome unobserved"
                return Gathered.BLIND
            why = self._what_changed(before, after, counter,
                                     tuple(progress()) if progress is not None else (None, None))
            if why:
                self.detail = why
                event("gather.change", code="observed", detail=why)
                return self._close(after) or Gathered.TOOK
            if after.get("vitals.combat") is True:
                self.detail = "combat interrupted the opening"
                return self._close(after) or Gathered.INTERRUPTED
        self.detail = "no observed objective or bag-slot change after the object click"
        return self._close(after) or Gathered.NOTHING

    @staticmethod
    def _what_changed(before: dict, after: dict, was, now) -> str:
        (have_before, need_before), (have_now, need_now) = was, now
        if have_before is not None and have_now is not None:
            if need_now == need_before and have_now > have_before:
                return f"objective {have_before} -> {have_now}"
            if (need_before is not None and have_before < need_before
                    and need_now is not None and have_now >= need_now):
                return f"objective {have_before}/{need_before} -> complete"
        free_before, free_now = before.get("bags.free"), after.get("bags.free")
        if free_before is not None and free_now is not None and free_now < free_before:
            return f"{free_before - free_now} bag slot{'s' if free_before - free_now > 1 else ''}"
        return ""

    def _close(self, values: dict) -> Gathered | None:
        """Auto loot closes the window itself; one left open would swallow the next click."""
        closed = close_observed(self.hid, self.read, ("ui.loot",), values=values, settle_s=0.4,
                                wait_for_paint=self.targeting.wait_for_paint)
        if closed.code is CloseCode.CLOSED:
            return None
        self.detail = f"{self.detail}; {closed.detail}" if self.detail else closed.detail
        return {CloseCode.REFUSED: Gathered.REFUSED, CloseCode.BLIND: Gathered.BLIND,
                CloseCode.NOT_CLOSED: Gathered.WINDOW_OPEN}[closed.code]
