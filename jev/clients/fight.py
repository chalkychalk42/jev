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
from jev.world.combat import (
    HEAL_IN_COMBAT,
    MIN_MANA_TO_HEAL,
    Ability,
    CombatProfile,
    Role,
    for_class,
)

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
    heals_landed: int = field(default=0, init=False)
    heals_ignored: int = field(default=0, init=False)
    _toggled: bool = field(default=False, init=False)
    _pending_heal: tuple[float, float] | None = field(default=None, init=False)
    last_hp: float | None = field(default=None, init=False)
    detail: str = field(default="", init=False)
    _last_use: dict[int, float] = field(default_factory=dict, init=False)

    # -- the skill -----------------------------------------------------------

    def run(self, name_id: int | None = None, *, timeout_s: float = 45.0) -> Fought:
        """Select, engage, and hold the rotation until something settles it."""
        self.pressed = []
        self.closed = 0
        self._toggled = False
        self._pending_heal = None
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

            # Walking and swinging are the same loop, not one after the other.
            #
            # Closing used to be a gate: walk until the target takes damage, *then* start
            # the rotation. Damage comes from swinging, swinging is the rotation, and the
            # rotation was behind the gate — so a live run reported
            # `unreachable pressed [] closed 8` eight times over. It had walked at the
            # kobold and never once pressed anything at it.
            landing = self.last_hp is not None and self.last_hp < 1.0
            if not landing and self.closed < MAX_CLOSE_BURSTS:
                # Not while casting: movement cancels a cast, and the only thing being
                # cast here is a heal that is keeping us alive.
                if v.get("bars.casting") is not True:
                    self.hid.hold("w", CLOSE_BURST_S)
                    self.closed += 1
            elif not landing and not self.pressed:
                self.detail = (f"closed {self.closed} times, pressed nothing, and landed "
                               "nothing; cannot reach it")
                return Fought.UNREACHABLE

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
        """Press the highest-priority row the client says is ready.

        Roles, not classes. There is no `if paladin` here and there is not going to be:
        the engine asks for a row with `role=heal` and presses it if the bars say it is
        ready, so a warrior is the same list with one fewer row and nothing changes.
        """
        if values.get("bars.casting") is True:
            return
        gcd = values.get("bars.gcd")
        if gcd is not None and gcd > 0.0:
            return
        ready, usable = values.get("bars.ready"), values.get("bars.usable")
        if ready is None or usable is None:
            return

        profile = self.profile or for_class(values.get("char.class_id"),
                                            values.get("char.race_id"))
        self._watch_heal(values, ready)

        def pressable(a: Ability) -> bool:
            bit = 1 << (a.slot - 1)
            return bool(ready & bit) and bool(usable & bit)

        # 1. Stay alive. A heal is a global cooldown not spent swinging, so the line is
        #    low — but standing there at 20% because healing is "not the rotation" is how
        #    a character ends up running back from the graveyard.
        heal = profile.first(Role.HEAL)
        hp = values.get("vitals.hp")
        if (heal is not None and pressable(heal)
                and values.get("vitals.combat") is True
                and hp is not None and hp < HEAL_IN_COMBAT
                and self._has_mana_for(heal, values)):
            self._press(heal)
            self._pending_heal = (hp, time.monotonic())
            return

        # 2. Keep the buff up, and only when it is actually lapsing: `bars.ready` says a
        #    seal is pressable on every single tick, so without the interval the
        #    character stands there re-sealing and never swings.
        now = time.monotonic()
        for buff in profile.by_role(Role.BUFF):
            last = self._last_use.get(buff.slot)
            if pressable(buff) and (last is None or now - last >= buff.every_s):
                self._press(buff)
                return

        # 3. Swing. A toggle is pressed at most once and only before anything has landed,
        #    because pressing melee auto-attack while already swinging **stops** it.
        for attack in profile.by_role(Role.ATTACK):
            if not pressable(attack):
                continue
            if attack.toggle:
                landed = self.last_hp is not None and self.last_hp < 1.0
                if self._toggled or landed:
                    continue
                self._toggled = True
            self._press(attack)
            return

    def _press(self, ability: Ability) -> None:
        key = SLOT_KEYS.get(ability.slot)
        if key is None:
            return
        self.hid.tap(key)
        self._last_use[ability.slot] = time.monotonic()
        self.pressed.append(ability.slot)

    def _has_mana_for(self, ability: Ability, values: dict) -> bool:
        """Enough mana for this, and enough left afterwards to matter.

        Out of mana is not a reason to keep pressing: it falls through to swinging, and
        the caller falls through to food, a vendor, or breaking the fight off. There is
        deliberately no drinking inside a fight.
        """
        frac = values.get("vitals.power")
        if frac is None:
            return True                      # unreadable is not a refusal
        if frac < MIN_MANA_TO_HEAL:
            return False
        pool = values.get("vitals.power_max")
        if pool and ability.mana:
            return frac * pool >= ability.mana
        return True

    def _watch_heal(self, values: dict, ready: int) -> None:
        """Did the last heal actually fire?

        Confirmed on health rising or the slot going unready — **not** on having tapped
        the key. A press that the client ignored looks identical to one that worked if
        nobody checks, and the failure it hides is a picker predicate that never fires.
        """
        if self._pending_heal is None:
            return
        at_press, when = self._pending_heal
        hp = values.get("vitals.hp")
        heal = (self.profile or for_class(values.get("char.class_id"),
                                          values.get("char.race_id"))).first(Role.HEAL)
        went_unready = heal is not None and not (ready & (1 << (heal.slot - 1)))
        if (hp is not None and hp > at_press + 0.02) or went_unready:
            self.heals_landed += 1
            self._pending_heal = None
        elif time.monotonic() - when > 2.5:
            self.heals_ignored += 1
            self._pending_heal = None

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
