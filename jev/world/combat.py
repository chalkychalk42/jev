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
from dataclasses import dataclass
from enum import StrEnum

PROFILES_PATH = (pathlib.Path(__file__).resolve().parent.parent.parent
                 / "content" / "tbc" / "combat-profiles.json")


class Role(StrEnum):
    ATTACK = "attack"
    BUFF = "buff"
    HEAL = "heal"
    FOOD = "food"
    DRINK = "drink"


# -- policy -------------------------------------------------------------------------
# How low is low. These are decisions, not facts, which is why they are here and not in
# the generated data.

HEAL_IN_COMBAT = 0.40
"""Heal mid-fight below this. Low, because a heal is a global cooldown not spent
swinging, and a fight is usually lost several seconds before the character falls over."""

HEAL_OUT_OF_COMBAT = 0.55
"""Top up below this between fights, but only when it is cheaper than sitting down."""

EAT_BELOW = 0.60
"""Sit down below this out of combat. Twenty seconds against a two-hundred-yard corpse
run is a trade worth making every time."""

MIN_MANA_TO_HEAL = 0.08
"""Do not start a heal that leaves nothing behind. Out of mana means fall through to
food, or to a vendor, or break the fight off — never a drink loop inside a fight."""


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


@dataclass(frozen=True)
class CombatProfile:
    name: str
    abilities: tuple[Ability, ...]

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
        out[key] = CombatProfile(
            name=entry.get("name", key),
            abilities=tuple(
                Ability(slot=r["slot"], role=Role(r["role"]), name=r.get("name", ""),
                        mana=r.get("mana", 0), every_s=r.get("every_s", 0.0),
                        toggle=r.get("toggle", False))
                for r in entry.get("rows", ())
            ),
        )
    return out


PROFILES: dict[str, CombatProfile] = _load()


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
