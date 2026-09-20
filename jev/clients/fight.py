"""Kill one unit. Select it, engage it, hold the rotation, confirm it died.

The three hard parts are not the rotation.

**Finding it** is the nameplate, exactly as in `Interact`, and `Tab` only as a fallback.
Tab was the obvious choice and was wrong: it selects by distance in the *world*, so it
happily picks a kobold thirty yards off through a tent, and the first live run spent
ninety seconds pressing abilities at a unit it could neither see nor reach — `in_melee`
false, no sighting, target at full health throughout. A unit with a nameplate on screen is
by construction one the client is drawing near enough to fight. Identity still comes from
`target.name_id` after the click, never from the plate.

**Engaging it** is a right-click on the model, which targets, *turns the character to
face*, and starts auto-attack in one action. That matters because 2.4.3 has no facing API
at all — `GetPlayerFacing` arrived in 3.0 and `pos.facing` reads `None` on every live
frame — so the only way this bot can aim its character at anything is to click it. If the
unit cannot be seen properly, this skill refuses rather than swinging at the air, and
`not_facing` in the radio's error field is what that failure looks like when it is not
refused.

**Closing to it** uses the facing that the right-click just set. There is no other way
round: with no facing API, the character can only be pointed at something by clicking it,
and `W` then walks along that heading. So `W` is held in short bursts and the thing being
watched is the target's **health**, not a range flag — `target.in_melee` is
`CheckInteractDistance` index 3, about eleven yards, and a paladin swings at five.

**Knowing it died** is the one that invites lying. A target that vanishes has either died
or been lost, and those are the same observation. So the health it was last seen at
decides: gone from full is `LOST`, gone from nothing is `KILLED`. The caller that wants
certainty counts `quests.o0_have` instead, which is the server's own tally.

The rotation is the easy part and is deliberately dumb: press the highest-priority slot
the client says is ready, respecting the global cooldown. Priorities are data
(`jev.world.combat`), because what is in slot 3 is configuration, not something to deduce.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.perceive.units import Plate, find, find_plates
from jev.world.combat import CombatProfile, for_class

# How dead is dead. The strip carries health as a fraction in 10 bits, so "zero" arrives as
# a very small number rather than exactly nothing.
DEAD_HP = 0.02

# A unit that vanished while this healthy was not killed by us.
LOST_HP = 0.5

# Nameplates to try before falling back to Tab. Small, and ordered by how central they
# are: the character is standing in the camp facing it, so the nearest plate to the middle
# of the screen is the thing in front of it.
MAX_CANDIDATES = 3

# How far another nameplate has to be before a target counts as **alone**.
#
# A level 1 paladin beats a level 1 kobold and loses to three, and it lost to three twice:
# both live deaths were a pull in the middle of a camp, not a fight it could not win. So
# an isolated plate is preferred over a central one.
#
# Pixels are a proxy for yards and an imperfect one — two mobs thirty yards away look
# close together — so this is a *preference*, not a filter. When nothing is isolated the
# most central plate is still tried, because refusing to fight at all is worse than
# fighting carefully.
CROWD_PX = 260

# Health to start a fight at, and health to break one off at. Below the first, rest; below
# the second, the fight is lost and pressing on is how a character ends up running back
# from the graveyard.
MIN_START_HP = 0.55
FLEE_HP = 0.30

# Tab presses before giving up on finding something attackable.
MAX_SELECTS = 4

# Closing to melee. Held in bursts rather than one long press so the loop can stop the
# moment damage starts, and bounded because walking at something that is not getting
# closer is walking into a fence.
ENGAGE_LOOKS = 5
CLOSE_BURST_S = 0.45
MAX_CLOSE_BURSTS = 8

# Which key an action slot is. The default bindings run 1-9, then 0, then the two keys
# left of Backspace — which is where a fresh character's food and water sit, so getting
# 10-12 wrong is not academic.
SLOT_KEYS: dict[int, str] = {
    **{n: str(n) for n in range(1, 10)}, 10: "0", 11: "minus", 12: "equals",
}


class Fought(StrEnum):
    KILLED = "killed"
    NO_TARGET = "no_target"          # Tab found nothing attackable
    NOT_VISIBLE = "not_visible"      # selected, but not clickable, so not faceable
    LOST = "lost"                    # target gone while still healthy: fled, or evaded
    UNREACHABLE = "unreachable"      # engaged, but never got close enough to land a hit
    TOO_HURT = "too_hurt"            # not healthy enough to start
    LOSING = "losing"                # broke off; the caller decides what to do about it
    DIED = "died"                    # we did
    TIMEOUT = "timeout"
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self is Fought.KILLED


@dataclass
class Fight:
    hid: object
    read: Callable[[], dict | None]
    read_frame: Callable[[], object | None]
    window_origin: tuple[int, int] = (0, 0)
    window_centre_x: int = 800
    profile: CombatProfile | None = None

    pressed: list[int] = field(default_factory=list, init=False)
    closed: int = field(default=0, init=False)
    # The plate that produced the current selection, if a plate did.
    selected_plate: Plate | None = field(default=None, init=False)
    last_hp: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)
    _last_use: dict[int, float] = field(default_factory=dict, init=False)

    # -- the skill -----------------------------------------------------------

    def run(self, name_id: int | None = None, *, timeout_s: float = 45.0) -> Fought:
        """Select, engage, and hold the rotation until something settles it."""
        self.pressed = []
        self.closed = 0
        self.last_hp = None
        self.detail = ""
        self._last_use = {}

        v = self.read()
        if v is None:
            return Fought.BLIND
        # The health guard is about **picking** fights, not about surviving one already
        # under way. Refusing to swing back because health is low is how a character
        # stands there being hit at 49%, declines to eat because it is in combat, and
        # does nothing at all until it falls over.
        in_combat = v.get("vitals.combat") is True
        hp = v.get("vitals.hp")
        if not in_combat and hp is not None and hp < MIN_START_HP:
            self.detail = f"{hp:.0%} health; not starting a fight on that"
            return Fought.TOO_HURT

        # Already engaged with something alive: that is the fight, and shopping for a
        # better one just adds a second attacker.
        engaged = (in_combat and v.get("target.has") is True
                   and (v.get("target.hp") or 1.0) > DEAD_HP)
        if not engaged:
            # In combat, the name filter comes off. Something is already hitting us and
            # it does not have to be the quest mob — a Kobold Worker beat this character
            # to 27% health while every attempt refused to fight anything but a Kobold
            # Vermin, selected nothing, and reported "not visible" twenty times in a row.
            acquired = self.acquire(None if in_combat else name_id)
            if acquired is not None:
                return acquired
        if not self.engage():
            return Fought.NOT_VISIBLE
        if not self.close_in():
            # Eight bursts of walking and the target has taken nothing. Standing in the
            # rotation for another ninety seconds does not change that, and a live run
            # spent exactly that pressing abilities at a full-health kobold it never
            # reached. Give the attempt up and let the caller pick something else.
            self.detail = (f"closed {self.closed} times and landed nothing; "
                           "cannot reach it")
            return Fought.UNREACHABLE


        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            v = self.read()
            if v is None:
                return Fought.BLIND
            if v.get("vitals.dead") is True or v.get("vitals.ghost") is True:
                self.detail = "the character died"
                return Fought.DIED
            mine = v.get("vitals.hp")
            if mine is not None and mine < FLEE_HP:
                # Break off rather than finish the fight standing up. Both live deaths
                # were fights that were already lost several seconds earlier.
                self.detail = f"broke off at {mine:.0%} health"
                return Fought.LOSING

            hp = v.get("target.hp")
            if hp is not None:
                self.last_hp = hp
            if v.get("target.has") is not True or (hp is not None and hp <= DEAD_HP):
                return self._settle()

            self._rotate(v)
            time.sleep(0.2)

        self.detail = f"{timeout_s:.0f}s and it is still standing"
        return Fought.TIMEOUT

    # -- pieces --------------------------------------------------------------

    def acquire(self, name_id: int | None) -> Fought | None:
        """Select something worth fighting. `None` means it worked.

        Nameplates first, because a plate means the client is drawing the unit near enough
        to fight, and `Tab` does not care how far away or how occluded its pick is.
        """
        self.selected_plate = None
        frame = self.read_frame()
        if frame is not None:
            for plate in self._candidates(frame):
                self.hid.click(self.window_origin[0] + round(plate.cx),
                               self.window_origin[1] + round(plate.cy))
                time.sleep(0.35)
                if self._acceptable(name_id) is True:
                    self.selected_plate = plate
                    return None
        return self.select(name_id)

    def _candidates(self, frame) -> list[Plate]:
        """Plates worth clicking: alone first, then central. An ordering, not an ID.

        Isolation leads because a pull in the middle of a camp is what killed this
        character twice, and a plate with no neighbour is the best available evidence that
        a mob has none either.
        """
        plates = find_plates(frame)
        centre = self.window_centre_x
        # A snapshot, because `list.sort` empties the list while it computes keys — so a
        # key function that reads `plates` sees nothing, every plate looks isolated, and
        # the ordering silently collapses back to plain centrality.
        others = list(plates)

        def crowding(plate: Plate) -> float:
            near = [abs(p.cx - plate.cx) for p in others if p is not plate]
            return min(near) if near else float("inf")

        plates.sort(key=lambda p: (crowding(p) < CROWD_PX, abs(p.cx - centre)))
        return plates[:MAX_CANDIDATES]

    def _acceptable(self, name_id: int | None) -> bool | None:
        """Is what we just selected worth fighting? `None` if nothing is readable."""
        v = self.read()
        if v is None:
            return None
        if v.get("target.has") is not True:
            return False
        hp = v.get("target.hp")
        if hp is not None and hp <= DEAD_HP:
            return False                       # a corpse is selectable and not a fight
        return name_id is None or v.get("target.name_id") == name_id

    def close_in(self) -> bool:
        """Walk onto it, along the heading the right-click just set.

        Watches **health**, not a range flag: `target.in_melee` is
        `CheckInteractDistance` index 3 — about eleven yards — and a melee class swings at
        five, so it reads true from well outside the range that matters.
        """
        start = self.last_hp
        for _ in range(MAX_CLOSE_BURSTS):
            v = self.read()
            if v is None:
                return False
            hp = v.get("target.hp")
            if hp is not None:
                self.last_hp = hp
            if v.get("target.has") is not True:
                return False
            if hp is not None and (hp <= DEAD_HP or (start is not None and hp < start)):
                return True                    # damage is landing; we are close enough
            if v.get("vitals.combat") is True and v.get("target.in_melee") is True:
                return True
            self.hid.hold("w", CLOSE_BURST_S)
            self.closed += 1
        return False

    def select(self, name_id: int | None) -> Fought | None:
        """`Tab`, as a fallback when no nameplate was clickable.

        The client picks; the radio says what it picked. Kept because a plate can be
        occluded by terrain while the unit is perfectly fightable.
        """
        for _ in range(MAX_SELECTS):
            self.hid.tap("tab")
            time.sleep(0.35)
            v = self.read()
            if v is None:
                return Fought.BLIND
            if v.get("target.has") is not True:
                continue
            if v.get("target.hp") is not None and v["target.hp"] <= DEAD_HP:
                continue                       # a corpse is selectable and not a fight
            if name_id is not None and v.get("target.name_id") != name_id:
                continue
            return None
        self.detail = "no nameplate and no Tab target worth fighting"
        return Fought.NO_TARGET

    def engage(self) -> bool:
        """Right-click the model: faces the character and starts auto-attack.

        The only way to aim this character at anything. If the unit is not properly
        visible — ring **and** nameplate — this refuses, because the alternative is
        swinging at whatever the camera happens to be pointed at.
        """
        ox, oy = self.window_origin
        # The ring is drawn a moment after the selection, so looking once loses races the
        # client was always going to win eventually.
        for _ in range(ENGAGE_LOOKS):
            frame = self.read_frame()
            sighting = None if frame is None else find(frame)
            if sighting is not None:
                self.hid.click(ox + sighting.torso[0], oy + sighting.torso[1], right=True)
                time.sleep(0.5)
                return True
            time.sleep(0.25)

        if self.selected_plate is not None:
            # No ring — the unit's feet are behind a rise, or grass, or the model itself.
            # Its plate is still on screen and **the radio has already confirmed who is
            # selected**, so the approximation below the plate is aimed at a known unit
            # rather than a hopeful pixel. Missing costs a click that opens nothing.
            point = self.selected_plate.unit_below()
            self.hid.click(ox + point[0], oy + point[1], right=True)
            time.sleep(0.5)
            self.detail = "no ring; aimed below the nameplate instead"
            return True

        self.detail = "selected, but no ring and nameplate to click, so no way to face it"
        return False

    def _rotate(self, values: dict) -> None:
        """Press the highest-priority slot the client says is ready."""
        if values.get("bars.casting") is True:
            return
        gcd = values.get("bars.gcd")
        if gcd is not None and gcd > 0.0:
            return
        ready = values.get("bars.ready")
        usable = values.get("bars.usable")
        if ready is None or usable is None:
            return

        profile = self.profile or for_class(values.get("char.class_id"))
        now = time.monotonic()
        for ability in profile.abilities:
            bit = 1 << (ability.slot - 1)
            if not (ready & bit) or not (usable & bit):
                continue
            last = self._last_use.get(ability.slot)
            if last is not None and now - last < ability.every_s:
                continue
            key = SLOT_KEYS.get(ability.slot)
            if key is None:
                continue
            self.hid.tap(key)
            self._last_use[ability.slot] = now
            self.pressed.append(ability.slot)
            return

    def _settle(self) -> Fought:
        """It is gone. Did we kill it?

        The last health seen decides, because vanishing is one observation with two
        causes. A caller that needs certainty counts `quests.o0_have`, which is the
        server's tally and not an inference.
        """
        if self.last_hp is not None and self.last_hp >= LOST_HP:
            self.detail = f"target vanished at {self.last_hp:.0%} health; not ours"
            return Fought.LOST
        return Fought.KILLED
