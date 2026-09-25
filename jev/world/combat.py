"""What each action-bar button is for, and when it is worth pressing.

The data is generated from the world database by `tools/gen_combat_profiles.py`, which
reads what the game itself puts on a fresh character's bar and derives each button's
**role** from what it does: a first effect of 10 is a heal, 6 applies an aura, 78 is melee
auto-attack, and a consumable's aura says whether it restores health or mana.

That is the whole reason there is no `PaladinHeal.py`. The engine never asks what class it
is; it asks for a row with `role=heal` and presses it if the client says it is ready. A
warrior is the same list with no heal row, and nothing above has to know.

Policy lives here, facts live in the generated file
---------------------------------------------------
When to heal and how low is low are decisions, so they are named constants below rather
than baked into the data. What the buttons *are* is a fact about the game and is not
written by hand.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cache

PROFILES_PATH = (pathlib.Path(__file__).resolve().parent.parent.parent
                 / "content" / "tbc" / "combat-profiles.json")
REACH_PATH = PROFILES_PATH.parent / "spell-reach.json"


class Role(StrEnum):
    ATTACK = "attack"
    BUFF = "buff"
    HEAL = "heal"
    FOOD = "food"
    DRINK = "drink"
    # Trained spells (`jev.world.training`), on the bar once the character has trained.
    AURA = "aura"                  # kept up for good: pressed once, again after a death
    SAVE = "save"                  # immune for a few seconds: pressed before a heal
    STUN = "stun"                  # the attacker held still: pressed before a heal
    LAST_RESORT = "last_resort"    # a full heal on a long cooldown, at the very end


# -- policy -------------------------------------------------------------------------
# How low is low. These are decisions, not facts, which is why they are here and not in
# the generated data.

HEAL_IN_COMBAT = 0.40
"""Heal mid-fight below this. Low, because a heal is a global cooldown not spent
swinging, and a fight is usually lost several seconds before the character falls over."""

HEAL_OUT_OF_COMBAT = 0.80
"""Top up below this between fights.

Much higher than the in-combat line, and for a different reason. In a fight a heal is a
global cooldown not spent swinging, so it is a last resort. Between fights it costs
nothing but mana and a few seconds, and going into the next pull at 80% instead of 45% is
the difference between winning it and a two-hundred-yard corpse run.

It is also the band where the spell actually works: Holy Light is a two and a half second
cast that pushback stops from ever completing while something is hitting the character,
and completes fine when nothing is."""

EAT_BELOW = 0.60
"""Sit down below this out of combat. Twenty seconds against a two-hundred-yard corpse
run is a trade worth making every time."""

MIN_MANA_TO_HEAL = 0.08
"""Do not start a heal that leaves nothing behind. Out of mana means fall through to
food, or to a vendor, or break the fight off — never a drink loop inside a fight."""

LAST_RESORT_BELOW = 0.15
"""A last resort heals to full and then waits an hour: for a fight about to be lost, when
a heal would not finish in time."""

RANGED_YD = 20.0
"""An attack that reaches this far is cast from where the unit was found, not walked into
melee with (V164). A nameplate proves a unit within about 20 yards (V45), so such a spell
reaches whatever a fight can select: Fireball 35 yards, Frostbolt 30. Judgement's 10
does not."""

LASTING_S = 60.0
"""A starting buff that lasts this long or longer outlasts a fight, and its clock is kept
between fights: Frost Armor, thirty minutes, was cast again at every pull, 60 of a level-1
mage's 165 mana each time."""


def grey_level(level: int) -> int:
    """The highest target level worth no experience to a character of `level`.

    The server's rule (`MaNGOS::XP::GetGrayLevel`); a kill at or below it grants nothing.
    """
    if level <= 5:
        return 0
    if level <= 39:
        return level - 5 - level // 10
    if level <= 59:
        return level - 1 - level // 5
    return level - 9


# Casting on oneself. With a hostile or dead unit selected, a helpful spell does not fall
# back to the caster unless the client's auto-self-cast option is on; it waits for a target
# click, and every click meant to select a unit then tries to cast it there. Measured 23
# September: a between-fights Holy Light with a looted corpse selected left its button lit
# and the next three fights selected nothing. The stock self-cast modifier (the 2.4.3
# default, saved on this client as `modifiedclick ALT SELFCAST`) casts on the caster.
SELF_CAST_MODIFIER = "alt"


@dataclass(frozen=True)
class Ability:
    """One action slot and what it is for."""

    slot: int
    role: Role
    name: str = ""
    mana: int = 0
    every_s: float = 0.0
    # Melee auto-attack is a toggle: pressing it while already swinging stops the swing.
    toggle: bool = False
    # Aimed at a friend, so at the caster: a blessing, as a heal is.
    friendly: bool = False
    # A buff that outlasts a fight (a blessing, an aura): its clock is kept between fights.
    lasting: bool = False
    # Usable only in a state a buff sets, and spends it: Judgement releases the seal.
    spends: bool = False
    spell_id: int | None = None

    @property
    def self_cast(self) -> bool:
        """Cast on the caster whatever is selected. A solo character heals only itself."""
        return self.role in (Role.HEAL, Role.LAST_RESORT) or self.friendly


@cache
def _reaches() -> dict[int, tuple[float, float, float]]:
    try:
        raw = json.loads(REACH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): (v["min_yd"], v["max_yd"], v["cast_s"])
            for k, v in (raw.get("spells") or {}).items()}


def reach(spell_id: int | None) -> tuple[float, float, float] | None:
    """A spell's (minimum yards, maximum yards, cast seconds), from the client's own data
    (`tools/gen_spell_reach.py`); `None` when the spell is not known there."""
    return None if spell_id is None else _reaches().get(spell_id)


def ranged(ability: Ability) -> bool:
    """An attack cast from range (`RANGED_YD`): not melee's toggle, and a spell that reaches."""
    facts = reach(ability.spell_id)
    return (ability.role is Role.ATTACK and not ability.toggle and facts is not None
            and facts[1] >= RANGED_YD)


def _nuke(ability: Ability) -> bool:
    """A ranged attack with a cast time: a caster's main attack (Fireball, Smite, Shadow
    Bolt), not an instant racial a blood elf's paladin starts with (Mana Tap)."""
    facts = reach(ability.spell_id)
    return ranged(ability) and facts is not None and facts[2] > 0


@dataclass(frozen=True)
class CombatProfile:
    name: str
    abilities: tuple[Ability, ...]
    # A class that starts with an attack cast from range with a cast time (`_nuke`): it opens
    # with spells and keeps its distance while it has the mana (V164). A mage does; a
    # paladin does not, whatever its race.
    caster: bool = False

    def by_role(self, role: Role) -> tuple[Ability, ...]:
        return tuple(a for a in self.abilities if a.role is role)

    def first(self, role: Role) -> Ability | None:
        found = self.by_role(role)
        return found[0] if found else None

    def slots(self) -> tuple[int, ...]:
        return tuple(a.slot for a in self.abilities)


GENERIC = CombatProfile(
    name="generic",
    abilities=(Ability(slot=1, role=Role.ATTACK, name="attack", toggle=True),
               Ability(slot=2, role=Role.ATTACK),
               Ability(slot=3, role=Role.ATTACK)),
)
"""For a race and class the generated file has never seen. Not an invented rotation: the
first three slots pressed when the client says they are ready, which is what a class with
no entry would do anyway."""


def _load() -> dict[str, CombatProfile]:
    try:
        raw = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, CombatProfile] = {}
    for key, entry in raw.items():
        abilities = tuple(
            Ability(slot=r["slot"], role=Role(r["role"]), name=r.get("name", ""),
                    mana=r.get("mana", 0), every_s=r.get("every_s", 0.0),
                    toggle=r.get("toggle", False), spell_id=r.get("spell"),
                    lasting=Role(r["role"]) is Role.BUFF and r.get("every_s", 0.0) >= LASTING_S)
            for r in entry.get("rows", ())
        )
        out[key] = CombatProfile(name=entry.get("name", key), abilities=abilities,
                                 caster=any(_nuke(a) for a in abilities))
    return out


PROFILES: dict[str, CombatProfile] = _load()


# A trained spell's role (`jev.world.training`) as a bar row's.
TRAINED_ROLES = {"attack": Role.ATTACK, "strike": Role.ATTACK, "heal": Role.HEAL,
                 "short_buff": Role.BUFF, "long_buff": Role.BUFF, "aura": Role.AURA,
                 "save": Role.SAVE, "stun": Role.STUN, "last_resort": Role.LAST_RESORT}


def from_bar(bar: dict[int, int | None] | None, base: CombatProfile) -> CombatProfile:
    """The profile for what the bar holds now: each slot's spell by its role.

    `bar` is the strip's bar census (`jev.perceive.spellbook`): a spell id per slot, 0 for
    an empty slot, `None` for an item or a spell unnamed. Such a slot keeps the base
    profile's row (the food and the water), an empty slot has none, and a spell with no
    role worth pressing (a dispel, a passive) has none either. Without a census the base
    profile stands.
    """
    if not bar:
        return base
    from jev.world.training import spell

    # The census speaks in main-bar buttons, 1-12; a class that starts in a stance keeps
    # its starting rows on that stance's page (a warrior's 73-84), the same twelve keys.
    by_slot = {(a.slot - 1) % 12 + 1: replace(a, slot=(a.slot - 1) % 12 + 1)
               for a in base.abilities}
    by_name = {a.name: a for a in base.abilities if a.name}
    rows: list[Ability] = []
    for slot in sorted(set(by_slot) | set(bar)):
        held = bar.get(slot)
        if held is None:
            # An item, or a spell the addon could not name: what the base profile says.
            if slot in by_slot:
                rows.append(by_slot[slot])
            continue
        facts = spell(held)
        if facts is None:
            # A spell the catalog does not know keeps what the base profile says of its
            # slot: the Seal of Righteousness that learning Judgement put in the starting
            # seal's place (21084 for 20154) went unpressed a fight, and Judgement with it.
            if slot in by_slot:
                rows.append(by_slot[slot])
            continue
        lasting = facts.role in ("long_buff", "aura")
        started = by_name.get(facts.name)
        if started is not None:
            # A spell the class starts with, at any rank, wherever it now is: its starting
            # role (a racial's, a weapon blow's), and a long buff's clock kept.
            rows.append(replace(started, slot=slot, mana=facts.mana or started.mana,
                                every_s=facts.every_s or started.every_s, lasting=lasting,
                                spell_id=facts.spell_id))
            continue
        role = TRAINED_ROLES.get(facts.role)
        if role is None:
            continue
        rows.append(Ability(slot=slot, role=role, name=facts.name, mana=facts.mana,
                            every_s=(float("inf") if role is Role.AURA else facts.every_s),
                            toggle=facts.role == "attack", friendly=facts.self_cast,
                            lasting=lasting, spends=facts.spends, spell_id=facts.spell_id))
    return CombatProfile(name=base.name, abilities=tuple(rows), caster=base.caster)


def is_caster(class_name: str | None) -> bool:
    """Whether the class of this name (`State.char.cls`: "mage") opens with spells cast
    from range (`CombatProfile.caster`)."""
    return any(p.caster for p in PROFILES.values() if p.name == class_name)


def for_class(class_id: int | None, race_id: int | None = None) -> CombatProfile:
    """The profile for this character. Never `None`.

    Exact race and class first, then any race with that class, then the generic one. Races
    differ in their starting consumables — a night elf gets different bread — but not in
    which slot holds the seal, so falling back on class alone is safe.
    """
    if class_id is None:
        return GENERIC
    exact = PROFILES.get(f"{race_id}:{class_id}")
    if exact is not None:
        return exact
    for key, profile in PROFILES.items():
        if key.endswith(f":{class_id}"):
            return profile
    return GENERIC
