"""Use a quest's own item on a creature of its kind, and stand through its channel (V387).

Taming the Beast (6062) hands a hunter a Taming Rod to use on a Dire Mottled Boar. The rod's
spell (19694) is a twenty-second channel at a beast within thirty yards whose first effect
is threat: the boar attacks from the first second, and the channel is broken by moving, not
by being hit (its `ChannelInterruptFlags` hold movement and no damage). When the channel runs
out with the boar alive the server casts the quest's own spell at it (19681), which charms the
boar for fifteen minutes and marks the quest complete (CMaNGOS `Aura::HandleAuraDummy`). So a
hunt for such an objective pulls by using the item, not by fighting: a living, untagged unit
of the kind selected as a fight selects one, the item used on it, and the character standing
still under its blows until the channel ends. The quest's positive complete flag is the only
success, as an exploration's is.

A use is not begun with something fighting the character: that is fought first, as any hunt
fights what attacks it. Nor with the character's own charm from the last such quest standing
by it: the server holds one charm at a time, and the next quest's spell fails at the channel's
end while the first is held (SPELL_FAILED_ALREADY_HAVE_CHARM, `Spell::CheckCast`, reported to
nobody since the spell is the aura's), a rod's charge spent for nothing - each rod has three.
The charm is fought beside until it goes, as the quest says to practise with it. Below
`BREAK_HP` in the channel the character steps out of it and fights the creature instead.

The item is found and used the way the hearthstone is (`jev.clients.hearth`): its slot from the
strip's bag census, right-clicked with the creature selected. A server body uses it by its id
on the selection's guid (the hive's `ServerUseOn`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.clients.fight import Fight, Fought
from jev.run.evidence import event, traced

# The rods' channel is twenty seconds (`DurationIndex` 18); a use still channelling after this
# long has been read wrong.
CHANNEL_MAX_S = 30.0
# A use that shows no cast by then did not take: out of reach, out of sight, refused.
CHANNEL_START_S = 2.5
# The complete flag after the channel's end, as an exploration's credit is waited for.
CREDIT_S = 3.0
POLL_S = 0.25
# Health below which a channel is stepped out of and the creature fought. An Armored Scorpid
# (level 7-8, 9-13 damage x 1.45 every 2 s in the world database) hits a level 10 hunter for
# about 14 after armour, some 130 over the channel, near half of its 240-300 health; the hunt
# pulls at 80% (`PULL_LINE`), so the creature used on leaves it near a third, and this line is
# for a second attacker.
BREAK_HP = 0.2
# A bag census comes round in a few seconds at the strip's paint rate (`jev.clients.hearth`).
FIND_S = 8.0
# Steps toward a selection the item does not reach (the live client's Tab picks to 40 yards).
CLOSER_S = 1.0
CLOSER_STEPS = 2


@dataclass
class UseOn:
    """The engagement an item-use objective's hunt runs in place of its fight (`Hunt.fight`):
    `run` uses the item on a unit of the kind, or fights. Whatever else a hunt asks of its fight
    - its heal line, top-up, buffs, what it pressed - is the fight's own."""

    fight: Fight
    read: Callable[[], dict | None]
    item_id: int
    complete: Callable[[], bool | None]
    hid: object = None
    # The name ids of the creatures a guide's items are used on: one of them standing friendly
    # by the character is its charm from the last such use (`charmed`).
    charms: frozenset[int] = frozenset()
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    detail: str = field(default="", init=False)
    uses: int = field(default=0, init=False)       # items used this hunt
    lost: int = field(default=0, init=False)       # uses whose channel ended without the credit

    def __getattr__(self, name: str):
        fight = self.__dict__.get("fight")
        if fight is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(fight, name)

    @traced("use")
    def run(self, name_id=None, *, timeout_s: float = 45.0) -> Fought:
        self.detail = ""
        values = self.read()
        if values is None:
            return Fought.BLIND
        if values.get("vitals.dead") is True or values.get("vitals.ghost") is True:
            return Fought.DIED
        if self.complete() is True:
            self.detail = "the quest is complete"
            return Fought.USED
        if values.get("vitals.combat") is True or self.charmed():
            why = ("something is fighting the character" if values.get("vitals.combat") is True
                   else "its own charm from the last use stands by")
            event("use.fight", data={"why": why, "item": self.item_id})
            outcome = self.fight.run(name_id, timeout_s=timeout_s)
            self.detail = f"{why}: {self.fight.detail}" if self.fight.detail else why
            return outcome
        picked = self.fight.acquire(name_id)
        if picked is not None:
            self.detail = self.fight.detail
            return picked
        return self._use(name_id, timeout_s)

    def _use(self, name_id, timeout_s: float) -> Fought:
        before = self.read() or {}
        event("use.request", data={"item": self.item_id, "target": before.get("target.guid")})
        outcome = self._channel(name_id, timeout_s, before)
        event("use.outcome", code=outcome.value, detail=self.detail,
              data={"item": self.item_id, "uses": self.uses, "lost": self.lost})
        return outcome

    def _channel(self, name_id, timeout_s: float, before: dict) -> Fought:
        """Use the item and stand through its channel; its end is the credit or a try lost."""
        for _attempt in range(1 + CLOSER_STEPS):
            errors = before.get("ui.error_count")
            pressed = self._press(before)
            if pressed is not None:
                return pressed
            self.uses += 1
            began, started = False, self.monotonic()
            while self.monotonic() - started < CHANNEL_MAX_S:
                v = self.read()
                if v is None:
                    return Fought.BLIND
                if v.get("vitals.dead") is True or v.get("vitals.ghost") is True:
                    self.detail = "died in the channel"
                    return Fought.DIED
                if self.complete() is True:
                    self.detail = "the item's channel ran out: the quest is complete"
                    return Fought.USED
                if v.get("bars.casting") is True:
                    began = True
                    hp = v.get("vitals.hp")
                    if isinstance(hp, (int, float)) and hp < BREAK_HP:
                        event("use.break", data={"hp": hp})
                        self._break()
                        self.lost += 1
                        outcome = self.fight.run(name_id, timeout_s=timeout_s)
                        self.detail = (f"stepped out of the channel at {hp:.0%}: "
                                       f"{self.fight.detail}")
                        return outcome
                elif began or self.monotonic() - started > CHANNEL_START_S:
                    break
                self.sleep(POLL_S)
            if began or not self._too_far(self.read() or {}, errors):
                break
            # Selected beyond the item's reach (the live client's Tab picks to 40 yards): a
            # step toward it, and the item again.
            self._closer()
            before = self.read() or {}
        deadline = self.monotonic() + CREDIT_S
        while self.monotonic() < deadline:
            if self.complete() is True:
                self.detail = "the item's channel ran out: the quest is complete"
                return Fought.USED
            self.sleep(POLL_S)
        self.lost += 1
        self.detail = ("the item's channel ended and the quest is not complete" if began
                       else "the item was used and no channel began")
        return Fought.LOST

    def charmed(self) -> bool:
        """Does the character's own charm from the last use stand by it? The live strip paints
        no pet, so the live client cannot tell and uses on; a server body can (the hive's)."""
        return False

    def _too_far(self, values: dict, errors) -> bool:
        count = values.get("ui.error_count")
        if count is None or count == errors:
            return False
        from jev.perceive.radio_frame import UI_ERROR_KEYS

        last = values.get("ui.error_last")
        return isinstance(last, int) and 0 < last < len(UI_ERROR_KEYS) \
            and UI_ERROR_KEYS[last] == "out_of_range"

    def _closer(self) -> None:
        """A step toward the selection: faced, then forward."""
        self.fight._targeting().face_selected()
        if self.hid is not None:
            self.hid.hold("w", CLOSER_S)

    def _break(self) -> None:
        """Out of the channel: a step back breaks it, as any movement does."""
        if self.hid is not None:
            self.hid.hold("s", 0.3)

    # -- the item, as the hearthstone is found and pressed -------------------------------

    def _press(self, values: dict) -> Fought | None:
        """Right-click the item in the bags with the creature selected: `None` when pressed,
        else why not - not in the bags or a click refused stops the hunt (nothing at another
        station changes it), and a spell left waiting for a target click (the selection was
        not one the item takes) is cancelled as a try that did not take."""
        slot, opener = self._find()
        if slot is None:
            return Fought.REFUSED
        try:
            if not self._click(slot, "inventory.", right=True):
                self.detail = "the item's click was refused"
                return Fought.REFUSED
        finally:
            if opener is not None:
                self._click(opener, "inventory.open_")
        if self.fight._targeting().cancel_pending_spell():
            self.detail = "the item asked for another target"
            return Fought.LOST
        return None

    def _find(self):
        opener = None
        deadline = self.monotonic() + FIND_S
        while self.monotonic() < deadline:
            v = self.read()
            if v is not None and v.get("inventory.item_id") == self.item_id:
                if v.get("inventory.x") is not None and v.get("inventory.y") is not None:
                    return v, opener
                if opener is None and v.get("inventory.open_x") is not None:
                    if not self._click(v, "inventory.open_"):
                        self.detail = "the bag's click was refused"
                        return None, None
                    opener = v
            self.sleep(0.05)
        self.detail = f"item {self.item_id} did not come round in the bag census"
        return None, opener

    def _click(self, values: dict, prefix: str, *, right: bool = False) -> bool:
        x, y = values.get(prefix + "x"), values.get(prefix + "y")
        if x is None or y is None or self.hid is None:
            return False
        ox, oy = self.window_origin
        w, h = self.window_size
        point = (ox + round(x * w), oy + round(y * h))
        event("use.click", data={"prefix": prefix, "right": right, "point": list(point)})
        return self.hid.click(*point, right=right) is not False
