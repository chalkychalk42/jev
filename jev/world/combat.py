"""What to press, per class. Data, not a rotation engine.

The bot cannot ask the client what is in action slot 3. `bars.usable` and `bars.ready` say
*which slots are pressable right now* and nothing about what they do, and painting a spell
id per slot would cost twelve fields to answer a question that has a much cheaper answer:
**the action bar layout is part of the bot's configuration**, like the addon. A profile
says what each slot is for; a setup step puts the right ability there.

That is the honest division. Guessing a spell from a slot number is a guess; being told is
not. And it makes every class one table entry rather than one code path.

Priority, not sequence
----------------------
An ordered list re-evaluated every tick, not a fixed rotation. A sequence has to be
restarted when anything interrupts it, and everything interrupts it — a miss, a parry, the
target dying early. A priority list has no state to lose.

`every_s` is what stops a seal or a buff being re-pressed on cooldown-free spam. It is not
a cooldown: the client owns cooldowns and reports them through `bars.ready`. It is how
often it is *worth* pressing, which the client has no opinion about.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Ability:
    """One action slot, and how often it is worth pressing."""

    slot: int              # 1-12, numbered as the action bar is
    every_s: float = 0.0   # 0 = whenever it is ready
    note: str = ""


@dataclass(frozen=True)
class CombatProfile:
    """A class's slots in priority order, highest first."""

    name: str
    abilities: tuple[Ability, ...]

    def slots(self) -> tuple[int, ...]:
        return tuple(a.slot for a in self.abilities)


# Keyed by `char.class_id`, which the radio paints from `UnitClass`. See `CLASS_ID` in
# `Helpers.lua` — 1 Warrior, 2 Paladin, and so on.
#
# Only the profile that has been used against a live mob carries specifics. The rest are
# the generic fallback rather than invented rotations: an unverified priority list is the
# same kind of guess as an unmeasured colour rule, and this codebase has already paid for
# one of those.
GENERIC = CombatProfile(
    name="generic",
    abilities=(Ability(slot=1, note="whatever the opener is"),
               Ability(slot=2),
               Ability(slot=3)),
)

PROFILES: dict[int, CombatProfile] = {
    2: CombatProfile(
        name="paladin",
        abilities=(
            # Seal of Righteousness. A seal is a self-buff that lasts thirty seconds, and
            # `bars.ready` will happily say it is pressable every single tick — so without
            # `every_s` the character stands there re-sealing and never swings.
            Ability(slot=1, every_s=25.0, note="seal; re-press before it lapses"),
            Ability(slot=2, note="judgement or filler, whenever ready"),
            Ability(slot=3),
        ),
    ),
}


def for_class(class_id: int | None) -> CombatProfile:
    """The profile for this class, or the generic one. Never `None`.

    A missing profile is not a reason to refuse to fight: auto-attack does most of the
    work at low level, and pressing the first three slots when they are ready is what a
    class with no entry here would do anyway.
    """
    return PROFILES.get(class_id or -1, GENERIC)
