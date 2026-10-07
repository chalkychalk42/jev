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
from typing import NamedTuple

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
    CONJURE = "conjure"            # makes an item: pressed out of combat, never in a fight
    ROOT = "root"                  # holds what is round the caster: then a step clear
    CC = "cc"                      # holds one attacker out of the fight: Polymorph (V287, V395)
    GUARD = "guard"                # holds off a crowd's blows: Evasion, Psychic Scream (V396)
    MARK = "mark"                  # the unit marked as a fight opens: Hunter's Mark (V404)
    SLOW = "slow"                  # a unit coming for the character slowed: Concussive Shot (V404)


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

REST_MANA = 0.35
"""Drink below this much mana out of combat, and to `DRINK_TO`."""
DRINK_TO = 0.75
CASTER_REST_MANA = 0.55
"""A caster drinks sooner and fuller: its mana is its damage. A level-1 mage's Fireball is 30
of 165 mana, against a Kobold Vermin that takes about three (V164)."""
CASTER_DRINK_TO = 0.95


def rest_mana(caster: bool) -> float:
    return CASTER_REST_MANA if caster else REST_MANA


def drink_to(caster: bool) -> float:
    return CASTER_DRINK_TO if caster else DRINK_TO


RANGED_YD = 20.0
"""An attack that reaches this far is cast from where the unit was found, not walked into
melee with (V164). A nameplate proves a unit within about 20 yards (V45), so such a spell
reaches whatever a fight can select: Fireball 35 yards, Frostbolt 30. Judgement's 10
does not."""

GCD_S = 1.5
"""The global cooldown of a spell, the least of the character's time any press takes (V360)."""

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


def kill_xp(level: int, target: int) -> float:
    """The experience a kill of a normal creature of level `target` gives a character of
    `level`, as the server reckons it (`MaNGOS::XP::BaseGain`): the base, 5 a level plus 45,
    five in a hundred more a level above to four, less a share of it a level below, down to
    nothing at the grey level."""
    base = level * 5 + 45
    if target >= level:
        return base * (1 + 0.05 * min(target - level, 4))
    if target <= grey_level(level):
        return 0.0
    zero = (5 if level < 8 else 6 if level < 10 else 7 if level < 12 else 8 if level < 16
            else 9 if level < 20 else 11 if level < 30 else 12 if level < 40 else 13 if level < 45
            else 14 if level < 50 else 15 if level < 55 else 16 if level < 60 else 17)
    return base * (zero + target - level) / zero


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
    # The item a conjure makes (Conjured Water, 5350).
    creates: int | None = None
    # A hold's or a guard's (V395, V396): how long it lasts, the creature types it takes (the
    # spell's TargetCreatureType mask, 0 for any), whether it only pins its unit where it
    # stands (a root) and whether it reaches the enemies round the character (a scream).
    holds_s: float = 0.0
    creatures: int = 0
    pins: bool = False
    around: bool = False

    @property
    def self_cast(self) -> bool:
        """Cast on the caster whatever is selected. A solo character heals only itself."""
        return self.role in (Role.HEAL, Role.LAST_RESORT) or self.friendly


class Reach(NamedTuple):
    """How far a spell reaches and how long it takes, from the client's own data."""

    min_yd: float
    max_yd: float
    cast_s: float
    channel: bool = False
    # Waits for the next melee swing (V306): no cast, no global cooldown, nothing spent until
    # the swing lands.
    next_swing: bool = False
    # Repeats until stopped (V358): Auto Shot, a wand's Shoot. Pressed once it shoots on every
    # ranged swing; pressed again while it repeats, the client stops it.
    repeats: bool = False
    # What a cast deals (V360, `tools/gen_spell_reach.py`): at once, over time and for how
    # long; a weapon blow's, a finisher's by its combo points and a script's are not read.
    dmg: float = 0.0
    dot: float = 0.0
    dot_s: float = 0.0
    weapon: bool = False
    combo: bool = False
    unread: bool = False
    gcd_s: float = 0.0

    @property
    def instant(self) -> bool:
        return self.cast_s == 0 and not self.channel

    @property
    def per_s(self) -> float | None:
        """Damage a second of the character's time (V360): a cast's at once and over time over
        its cast, or its global cooldown for an instant. `None` for damage the data does not
        say: a weapon blow's, a finisher's, a script's, and a channel's, which every hit taken
        cuts short (Arcane Missiles)."""
        if self.weapon or self.combo or self.unread or self.repeats or self.channel:
            return None
        return (self.dmg + self.dot) / max(self.cast_s, self.gcd_s or GCD_S, GCD_S)

    @property
    def lingers(self) -> bool:
        """Damage over time that is most of the cast's (V360): Immolate, Moonfire, Flame Shock,
        Corruption, Serpent Sting - on a unit once, not again while it lasts. Not Fireball's
        burn, nor a channel."""
        return not self.channel and self.dot > 0 and self.dot >= self.dmg and self.dot_s > 0


@cache
def _reaches() -> dict[int, Reach]:
    try:
        raw = json.loads(REACH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): Reach(v["min_yd"], v["max_yd"], v["cast_s"], bool(v.get("channel")),
                          bool(v.get("next_swing")), bool(v.get("repeats")),
                          float(v.get("dmg", 0.0)), float(v.get("dot", 0.0)),
                          float(v.get("dot_s", 0.0)), bool(v.get("weapon")),
                          bool(v.get("combo")), bool(v.get("unread")),
                          float(v.get("gcd_s", 0.0)))
            for k, v in (raw.get("spells") or {}).items()}


def reach(spell_id: int | None) -> Reach | None:
    """A spell's reach (`tools/gen_spell_reach.py`); `None` when it is not known there."""
    return None if spell_id is None else _reaches().get(spell_id)


def ranged(ability: Ability) -> bool:
    """An attack cast from range (`RANGED_YD`): not melee's toggle, and a spell that reaches."""
    facts = reach(ability.spell_id)
    return (ability.role is Role.ATTACK and not ability.toggle and facts is not None
            and facts[1] >= RANGED_YD)


def repeats(ability: Ability) -> bool:
    """A ranged attack that repeats until stopped (`Reach.repeats`): Auto Shot (V358)."""
    facts = reach(ability.spell_id)
    return ranged(ability) and facts is not None and facts.repeats


def per_s(ability: Ability) -> float | None:
    """An attack's damage a second of the character's time (`Reach.per_s`, V360); `None` for
    melee's toggle, a repeating shot, and damage the data does not say."""
    facts = reach(ability.spell_id)
    if ability.toggle or facts is None:
        return None
    return facts.per_s


def lingers(ability: Ability) -> bool:
    """An attack whose damage is mostly over time (`Reach.lingers`, V360)."""
    facts = reach(ability.spell_id)
    return ability.role is Role.ATTACK and facts is not None and facts.lingers


def instant_blow(ability: Ability) -> bool:
    """An instant of damage at once, not over time (Fire Blast, Arcane Shot): what a caster
    keeps for contact, where nothing can push it back (V165)."""
    facts = reach(ability.spell_id)
    return facts is not None and facts.instant and not facts.lingers


def by_value(attacks: tuple[Ability, ...], keep=None) -> tuple[Ability, ...]:
    """The attacks in the bar's order, those whose damage the data says (`per_s`) taking one
    another's places by damage a second, the most first (V360). Mind Blast before Smite,
    Immolate before Shadow Bolt; a weapon blow, a finisher, a script and a channel keep their
    places, their damage being the weapon's, the combo points', the server's or the hits
    taken's; so does any attack `keep` names (a caster's instant blows, `instant_blow`)."""
    valued = [i for i, a in enumerate(attacks)
              if per_s(a) is not None and not (keep is not None and keep(a))]
    ranked = sorted((attacks[i] for i in valued), key=lambda a: -per_s(a))
    out = list(attacks)
    for i, attack in zip(valued, ranked, strict=True):
        out[i] = attack
    return tuple(out)


def _nuke(ability: Ability) -> bool:
    """A ranged attack with a cast time: a caster's main attack (Fireball, Smite, Shadow
    Bolt), not an instant racial a blood elf's paladin starts with (Mana Tap)."""
    facts = reach(ability.spell_id)
    return ranged(ability) and facts is not None and not facts.instant


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

    @property
    def shooter(self) -> bool:
        """A class whose main attack repeats from range at no cost (`repeats`): a hunter's
        Auto Shot (V358). It opens from range and shoots while the unit is not at hand, as a
        caster casts while it has the mana; at hand it fights in melee. A wand's Shoot
        would make a caster one too, which it already is."""
        return any(repeats(a) for a in self.abilities)

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
                    toggle=r.get("toggle", False), friendly=r.get("friendly", False),
                    spell_id=r.get("spell"),
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
                 "save": Role.SAVE, "stun": Role.STUN, "last_resort": Role.LAST_RESORT,
                 "conjure": Role.CONJURE, "root": Role.ROOT, "cc": Role.CC,
                 "guard": Role.GUARD,
                 # A mark as a fight opens and a slow at a unit coming (V404).
                 "mark": Role.MARK, "slow": Role.SLOW,
                 # A wand's shot: an attack from range that repeats (V397), a caster's when its
                 # mana is spent or its unit nearly dead.
                 "wand": Role.ATTACK,
                 # Damage to every enemy round the character: an attack the fight presses with
                 # more than one at hand (V277).
                 "area": Role.ATTACK,
                 # Damage over time: an attack put on a unit once while it lasts (V361, V360).
                 "dot": Role.ATTACK}


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
                            lasting=lasting, spends=facts.spends, spell_id=facts.spell_id,
                            creates=facts.creates, holds_s=facts.holds_s,
                            creatures=facts.creatures, pins=facts.pins, around=facts.around))
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
