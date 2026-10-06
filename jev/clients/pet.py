"""A hunter's pet, tended (V389): what a pet spell's cast answered, the beasts a taming takes,
and Tame Beast cast on one as the Taming Rods are used (`jev.clients.use`, V387).

Tame Beast is a twenty-second channel at a beast within thirty yards (spell 1515, `DurationIndex`
18, `RangeIndex` 4) whose first effect is threat: the beast attacks from the first second, and the
channel is broken by moving, not by being hit (`ChannelInterruptFlags` 48140, the rods' own). When
it runs out with the beast alive the server makes it the hunter's pet at its own level (13481,
`Spell::EffectTameCreature`). So a taming is the rods' engagement with a cast in place of the
item: a living, untagged beast of the kind and of the hunter's level or one below selected as a
fight selects one, the spell cast at it, the hunter standing still under its blows, stepping out
and fighting below a fifth of its health, and done once a pet is out.

The live strip paints no pet, so the live client never asks for one; a server body (the hive's)
casts by the spell's id on the selection's guid and reads the server's answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev.clients.fight import HOSTILE_REACTIONS, NORMAL_RANK, Fought, Kinds
from jev.clients.use import UseOn
from jev.world.pets import TAME_BEAST

# A neutral beast (reaction 4) is as tameable as a hostile one; a friendly one is no wild beast.
TAME_REACTIONS = (*HOSTILE_REACTIONS, 4)


@dataclass(frozen=True)
class Tameable(Kinds):
    """The beasts a taming takes (V389): one of `names`, of normal rank, not friendly, and of a
    level within `low`-`high`, the hunter's own and one below - the server refuses a beast above
    the hunter, and a pet keeps the level it was tamed at. A `Kinds`, so the fight's selection
    takes it as it takes a dry rib's wider kinds (V337)."""

    def named(self, name: int | None) -> bool:
        return name is not None and name in self.names

    def takes(self, values: dict) -> bool:
        level = values.get("target.level")
        return (values.get("target.name_id") in self.names and isinstance(level, int)
                and self.low <= level <= self.high
                and values.get("target.reaction") in TAME_REACTIONS
                and values.get("target.classification") == NORMAL_RANK)


@dataclass(frozen=True)
class PetCast:
    """What a pet spell's cast answered (V389): whether the cast was sent, the server's refusals
    of it (`SpellCastResult` codes) and its taming refusals (`PetTameFailureReason`: 5 another
    pet held or kept, 10 the pet dead, 11 not dead), and why nothing was sent."""

    sent: bool
    codes: tuple[int, ...] = ()
    tame: tuple[int, ...] = ()
    detail: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.codes or self.tame) or not self.sent


# `SpellCastResult` codes a pet spell is refused with (CMaNGOS `SpellDefines.h`).
AFFECTING_COMBAT = 0x00
ALREADY_HAVE_CHARM = 0x05
ALREADY_HAVE_SUMMON = 0x06
FOOD_LOWLEVEL = 0x21
NO_PET = 0x4F
NO_POWER = 0x50
TARGETS_DEAD = 0x68
WRONG_PET_FOOD = 0x82
# `PetTameFailureReason`.
ANOTHER_SUMMON_ACTIVE = 5
TOO_HIGH_LEVEL = 9
PET_DEAD = 10
PET_NOT_DEAD = 11


@dataclass
class TameOn(UseOn):
    """The engagement a taming's hunt runs in place of its fight (V389): `UseOn` with Tame Beast
    cast at the selection where the rod was used, `complete` a pet out. What attacks the
    character is fought first; below `jev.clients.use.BREAK_HP` in the channel it steps out and
    fights the beast."""

    spell_id: int = TAME_BEAST
    # The cast's mana (48% of the hunter's base mana, `jev.world.pets.mana_cost`): short of it
    # out of combat no beast is pulled, and the taming ends to drink. `None`, not known.
    mana: int | None = None

    def run(self, name_id=None, *, timeout_s: float = 45.0) -> Fought:
        values = self.read() or {}
        power, most = values.get("vitals.power"), values.get("vitals.power_max")
        if (self.mana is not None and values.get("vitals.combat") is not True
                and isinstance(power, (int, float)) and most and power * most < self.mana):
            self.detail = f"short of Tame Beast's {self.mana} mana"
            return Fought.REFUSED
        return super().run(name_id, timeout_s=timeout_s)

    def _press(self, values: dict) -> Fought | None:
        """The live client's strip paints no pet, and the live policy never asks for one: a
        taming is a server body's (`hive.skills.ServerTameOn`)."""
        self.detail = "the live strip paints no pet: Tame Beast is cast by a server body"
        return Fought.REFUSED
