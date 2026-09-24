"""Take what the corpse is holding. One click, because fast loot is on.

A great many quests are "bring me eight of these", and the eight come off corpses rather
than out of the air. Without this a kill counter can fill while the quest never does.

A corpse has no nameplate, so a fresh hover that reports the *selected* unit dead can only
be its body. Shared `Targeting.click_corpse` hovers a bounded set of points - the column of
the last plate seen while the unit lived, then the centre line the kill left it on - and
clicks only where the client says the selected corpse is. This skill observes what changed;
input delivery alone never establishes a take or an empty corpse.

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

The loot frame alone is **not** evidence of a take. A frame appearing means a corpse
was opened, not that anything was taken, and it is only auto loot that makes the two
coincide - which is a client setting this code cannot see. `autoLootCorpse "1"` is set in
`WTF/Config.wtf`; if it is ever off, the frame would stand open and nothing would be in
the bags, and believing the frame would report a take on every empty wolf in the zone.
Window visibility is used only to verify that cleanup leaves the next action unblocked.

An empty corpse is a real and common outcome. `NOTHING` means no observed change after
the click; that remains non-fatal, but cannot prove whether the corpse was empty or the
click missed. The execution stream retains the geometry and observed deltas separately.

Bags are checked **before** the click. A full bag makes looting silently do nothing, and
the fix for that is a vendor, not another click.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.clients.targeting import ClickCode, PaintCode, Targeting
from jev.clients.windows import CloseCode, close_observed
from jev.perceive.units import Plate
from jev.run.evidence import event, traced

# How long to wait for the bags or the loot frame to admit something happened.
SETTLE_S = 2.0
# Hover probes for a corpse (`corpse_probe_points`). In combat the next attacker is hitting
# the character while it searches, and every corpse found on 24 September was within seven
# probes; below half health with an attacker still on it, the search waits for another time.
LOOT_PROBES = 24
LOOT_IN_COMBAT_PROBES = 6
LOOT_IN_COMBAT_HP = 0.5

class Looted(StrEnum):
    TOOK = "took"              # objective, money or bag capacity changed
    NOTHING = "nothing"        # no observed change after clicking; emptiness is unconfirmed
    NO_CORPSE = "no_corpse"    # nothing selected to loot
    BAGS_FULL = "bags_full"    # would not fit; a vendor is the answer, not a click
    BLIND = "blind"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"
    WINDOW_OPEN = "window_open"   # observed loot UI could not be confirmed closed

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
    targeting: Targeting | None = None
    _progress: Callable[[], tuple[int | None, int | None]] | None = field(
        default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    took: int = field(default=0, init=False)
    detail: str = field(default="", init=False)

    @traced("loot")
    def run(self, *, settle_s: float = SETTLE_S,
            progress: Callable[[], tuple[int | None, int | None]] | None = None,
            anchor: Plate | None = None, name_id: int | None = None) -> Looted:
        """`progress` is the objective counter, passed by whoever knows which quest is
        being worked. `Loot` has no idea and should not: it is handed a way to ask what
        the server thinks, exactly as `Hunt` is. `anchor` is the last plate the unit had
        while alive and `name_id` the name of the unit killed, when the caller knows them:
        the client can clear the selection at the kill, and then the corpse is found by
        a dead hover of that name."""
        self.clicked = None
        self.detail = ""
        self._progress = progress
        if self.level is not None and self.level() is False:
            self.detail = "camera input refused"
            return Looted.REFUSED

        v = self.read()
        if v is None:
            return Looted.BLIND
        if v.get("bags.free") == 0:
            self.detail = "bags are full; looting would take nothing"
            return self._close_if_open(v) or Looted.BAGS_FULL
        before = {**v, "objective": self._counter()}
        event("loot.before", data={key: before.get(key) for key in (
            "objective", "bags.money_copper", "bags.money_silver", "bags.free",
            "target.has", "target.name_id", "target.hp", "ui.loot")})

        targeting = self.targeting or Targeting(self.hid, self.read, read_frame=self.read_frame,
                                                window_origin=self.window_origin)
        selected = v.get("target.has") is True
        # The client moved the selection on at the kill: in a pack, to the next unit of the
        # same name, which the corpse search then refused as not dead - 20 corpses in the
        # runs of 24 September, none looted. The corpse is found by its name instead.
        moved_on = selected and v.get("target.hp") != 0 and name_id is not None
        wanted = name_id if moved_on or not selected else v.get("target.name_id")
        fighting = v.get("vitals.combat") is True
        hp = v.get("vitals.hp")
        if fighting and moved_on and isinstance(hp, (int, float)) and hp < LOOT_IN_COMBAT_HP:
            self.detail = f"at {hp:.0%} health with something still attacking; not now"
            return Looted.NO_CORPSE
        action = targeting.click_corpse(
            expected_name_id=wanted, anchor=anchor, past_selection=moved_on,
            max_probes=LOOT_IN_COMBAT_PROBES if fighting else LOOT_PROBES)
        self.clicked, self.detail = action.point, action.detail
        event("loot.request", code=action.code.value,
              data={"point": self.clicked, "method": "verified_corpse"})
        if not action.delivered:
            return {ClickCode.REFUSED: Looted.REFUSED, ClickCode.BLIND: Looted.BLIND,
                    ClickCode.INTERRUPTED: Looted.INTERRUPTED}.get(action.code, Looted.NO_CORPSE)

        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            after = self.read()
            event("loot.observed", code="blind" if after is None else "readable",
                  data={} if after is None else {key: after.get(key) for key in (
                      "bags.money_copper", "bags.money_silver", "bags.free", "ui.loot")})
            if after is None:
                self.detail = "radio lost after corpse input; outcome unobserved"
                return Looted.BLIND
            why = self._what_changed(before, after)
            if why:
                return self._finish(after, why)

        after = self.read()
        if after is None:
            self.detail = "radio lost at final loot observation; outcome unobserved"
            return Looted.BLIND
        return self._finish(after, self._what_changed(before, after))

    def _finish(self, after: dict, why: str) -> Looted:
        """Retain the observed take even when closing the resulting UI fails."""
        if why:
            event("loot.change", code="observed", detail=why)
            self.took += 1
            self.detail = why
        else:
            self.detail = "no observed objective, money or bag-slot change after the click"
        closed = self._close_if_open(after)
        if closed is not None:
            return closed
        self._release_corpse()
        return Looted.TOOK if why else Looted.NOTHING

    def _release_corpse(self) -> None:
        """Clear a looted corpse from the selection.

        Left selected, it put the tutor in its loot situation after every kill: 43 tutor
        loot attempts on emptied corpses in sessions 30-50, not one of them taking anything.
        Escape clears a selection when no window is open, and opens the game menu when there
        is no selection either, so only on a dead unit observed selected, and the menu is
        shut again if it opened anyway.
        """
        v = self.read()
        if (not v or v.get("target.has") is not True or v.get("target.hp") != 0
                or any(v.get(k) is True for k in ("ui.loot", "ui.modal", "ui.gossip",
                                                  "ui.vendor", "ui.quest_frame"))):
            return
        event("loot.release", data={"name_id": v.get("target.name_id")})
        if not self.hid.tap("esc"):
            return
        targeting = self.targeting or Targeting(self.hid, self.read)
        paint = targeting.wait_for_paint()
        after = paint.after if paint.code is PaintCode.FRESH else self.read()
        if after and after.get("ui.modal") is True:
            self.hid.tap("esc")

    def _counter(self) -> tuple[int | None, int | None] | None:
        if self._progress is None:
            return None
        return tuple(self._progress())

    def _what_changed(self, before: dict, after: dict) -> str:
        """Which signal moved, in the order that they are worth believing.

        A completing objective can stop painting its counter and report only the quest's
        complete flag, which the progress readers pass as 1/1: measured 23 September, the
        eighth Tough Wolf Meat read 7/8 -> 1/1, a stack that already had its bag slot,
        and "nothing" - while the quest log said 8/8. Short before and complete after
        is the objective moving.
        """
        was, now = before.get("objective") or (None, None), self._counter() or (None, None)
        event("loot.objective", data={"before": list(was), "after": list(now)})
        (have_before, need_before), (have_now, need_now) = was, now
        if have_before is not None and have_now is not None:
            if need_now == need_before and have_now > have_before:
                return f"objective {have_before} -> {have_now}"
            if (need_before is not None and have_before < need_before
                    and need_now is not None and have_now >= need_now):
                return f"objective {have_before}/{need_before} -> complete"

        money_key = ("bags.money_copper" if before.get("bags.money_copper") is not None
                     and after.get("bags.money_copper") is not None else "bags.money_silver")
        for key, unit in ((money_key, "copper" if money_key.endswith("copper") else "silver"),):
            was, now = before.get(key), after.get(key)
            if was is not None and now is not None and now > was:
                return f"{now - was} {unit}"

        was, now = before.get("bags.free"), after.get("bags.free")
        if was is not None and now is not None and now < was:
            return f"{was - now} bag slot{'s' if was - now > 1 else ''}"

        # An item onto a stack it already has takes no slot: Stringy Wolf Meat looted
        # beside a stack of it read "nothing" (13 of 24 loots in runs 20260924T043637 to
        # ...050644). The strip's revision moves on every bag update; a meal moves it too,
        # so only when neither supply went down.
        was, now = before.get("inventory.revision"), after.get("inventory.revision")
        if (was is not None and now is not None and now != was
                and not any(_fell(before, after, f"bags.{s}_count") for s in ("food", "drink"))):
            return "an item onto a stack"
        return ""

    def _close_if_open(self, values: dict) -> Looted | None:
        """Auto-loot usually closes itself. When it does not, a left-open loot window
        swallows the next click, so it is shut - against a screen the radio has measured,
        not a blind Escape."""
        targeting = self.targeting or Targeting(self.hid, self.read)
        closed = close_observed(self.hid, self.read, ("ui.loot",), values=values,
                                settle_s=0.4, wait_for_paint=targeting.wait_for_paint)
        if closed.code is CloseCode.CLOSED:
            return None
        self.detail = f"{self.detail}; {closed.detail}" if self.detail else closed.detail
        return {CloseCode.REFUSED: Looted.REFUSED, CloseCode.BLIND: Looted.BLIND,
                CloseCode.NOT_CLOSED: Looted.WINDOW_OPEN}[closed.code]


def _fell(before: dict, after: dict, key: str) -> bool:
    was, now = before.get(key), after.get(key)
    return isinstance(was, int) and isinstance(now, int) and now < was
