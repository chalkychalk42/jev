"""Generate the spell reach table: how far each spell on a bar reaches and how long it casts.

A caster opens from range (`jev.world.combat.ranged`, DECISIONS V164): Fireball reaches 35
yards, a paladin's Judgement 10. The facts are the client's own, from the exact server's
world snapshot (`data/knowledge/tbc-243.sqlite`): a spell's `RangeIndex` into
`SpellRange.dbc` (minimum and maximum, in yards, stored as floats) and its
`CastingTimeIndex` into `SpellCastTimes.dbc` (milliseconds), and whether it is channelled.

Covered: every spell in the trainer catalog, and every spell a class starts with on its
bar (`content/tbc/combat-profiles.json`).

Usage: .venv/bin/python tools/gen_spell_reach.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import struct

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data/knowledge/tbc-243.sqlite"
OUT = ROOT / "content/tbc/spell-reach.json"
# AttributesEx bits of a channelled spell (Arcane Missiles, Evocation): no cast time, and
# yet not instant - the character stands still while it lasts.
CHANNELED = 0x4 | 0x40
# Attributes bits of a spell that waits for the next melee swing (Heroic Strike, Cleave, Raptor
# Strike, Maul; the server's `IsNextMeleeSwingSpell`): pressed, it starts no cast, no global
# cooldown and spends nothing until the swing lands.
NEXT_SWING = 0x4 | 0x400
# AttributesEx2 bit of a spell that repeats until stopped (Auto Shot, a wand's Shoot; the
# server's `SPELL_ATTR_EX2_AUTOREPEAT_FLAG`): pressed once, it shoots on every ranged swing,
# and on the client pressed again while it repeats it stops (V358).
AUTO_REPEAT = 0x20
# Bits of a blow struck only from behind its unit (V501; the server's `Spell::CheckTarget`):
# AttributesEx2's INITIATE_COMBAT_POST_CAST with AttributesEx's INITIATES_COMBAT_ENABLES_AUTO_
# ATTACK, a druid's Pounce aside (its facing limit went in 2.0.1, the flags stayed), or the
# server's own FACING_BACK. Backstab, Ambush, Garrote: a unit fighting the character faces it,
# and the client's bar paints them usable all the same.
BEHIND_EX2, BEHIND_EX, FACING_BACK_SS = 0x00100000, 0x00000200, 0x00000008
SPELLFAMILY_DRUID, POUNCE_FAMILY_FLAG = 7, 0x0000000000020000
# What a cast deals (V360): its first, second and third effects' school damage (2) at the mean
# of their roll, and the auras that deal it over time - periodic damage (3), a periodic leech
# (53) and a periodic trigger of a damaging spell (23, Arcane Missiles' missiles) - over their
# duration; a weapon blow (17, 31, 58, 121) is marked, its damage being the weapon's.
EFFECT_SCHOOL_DAMAGE = 2
EFFECT_APPLY_AURA = 6
EFFECTS_WEAPON = (17, 31, 58, 121)
AURAS_PERIODIC_DAMAGE = (3, 53)
AURA_PERIODIC_TRIGGER = 23
EFFECTS_UNREAD = (3, 77)


def _float(bits: int) -> float:
    """A DBC float column, stored as its 32 bits."""
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def _signed(value: int | None) -> int:
    """A DBC integer column read back unsigned: the shots' cast time (Arcane Shot, Serpent
    Sting, Concussive Shot: `SpellCastTimes` 18) is stored as -1,000,000 ms, which read as
    4,293,967 s made every one a cast longer than any fight (V358). Negative is instant."""
    value = int(value or 0)
    return value - (1 << 32) if value >= 1 << 31 else value


def _mean(base: int | None, sides: int | None) -> float:
    """An effect's mean roll: base points and a die of `sides` (the client's 1..sides)."""
    base, sides = int(base or 0), int(sides or 0)
    return base + (sides + 1) / 2 if sides > 0 else base + 1


def damage(db: sqlite3.Connection, spell_id: int, *, depth: int = 0) -> dict:
    """What one cast deals (V360): `dmg` at once, `dot` over `dot_s`, `weapon` for a blow
    whose damage is the weapon's, and `gcd_s`, its global cooldown. Empty for none."""
    row = db.execute(
        "SELECT Effect1, Effect2, Effect3, EffectApplyAuraName1, EffectApplyAuraName2, "
        "EffectApplyAuraName3, EffectBasePoints1, EffectBasePoints2, EffectBasePoints3, "
        "EffectDieSides1, EffectDieSides2, EffectDieSides3, EffectAmplitude1, EffectAmplitude2, "
        "EffectAmplitude3, EffectTriggerSpell1, EffectTriggerSpell2, EffectTriggerSpell3, "
        "DurationIndex, StartRecoveryTime, EffectPointsPerComboPoint1, "
        "EffectPointsPerComboPoint2, EffectPointsPerComboPoint3 FROM world_spell_template "
        "WHERE Id=?",
        (spell_id,)).fetchone()
    if row is None:
        return {}
    effects, auras, bases = row[0:3], row[3:6], row[6:9]
    sides, amplitudes, triggers = row[9:12], row[12:15], row[15:18]
    found = db.execute("SELECT c1 FROM dbc_SpellDuration WHERE id=?", (row[18],)).fetchone()
    duration_ms = max(0, _signed(found[0])) if found and row[18] else 0
    direct = periodic = 0.0
    weapon = unread = False
    for effect, aura, base, side, amplitude, trigger in zip(effects, auras, bases, sides,
                                                            amplitudes, triggers, strict=True):
        if effect in EFFECTS_UNREAD:
            unread = True
        if effect == EFFECT_SCHOOL_DAMAGE:
            direct += max(0.0, _mean(base, side))
        elif effect in EFFECTS_WEAPON:
            weapon = True
        elif effect == EFFECT_APPLY_AURA and amplitude and duration_ms:
            ticks = duration_ms // amplitude
            if aura in AURAS_PERIODIC_DAMAGE:
                periodic += ticks * max(0.0, _mean(base, side))
            elif aura == AURA_PERIODIC_TRIGGER and trigger and depth == 0:
                periodic += ticks * damage(db, trigger, depth=1).get("dmg", 0.0)
    out: dict = {}
    if direct:
        out["dmg"] = round(direct, 1)
    if periodic:
        out["dot"], out["dot_s"] = round(periodic, 1), round(duration_ms / 1000, 1)
    if weapon:
        out["weapon"] = True
    # Damage the data does not say: a finisher's by the combo points spent (Eviscerate), a
    # dummy's or a script's by the server's code (Judgement's by the seal).
    if any(row[20:23]):
        out["combo"] = True
    if unread:
        out["unread"] = True
    if out and row[19]:
        out["gcd_s"] = round(row[19] / 1000, 2)
    return out


def spell_ids() -> list[int]:
    catalog = json.loads((ROOT / "content/tbc/trainer-catalog.json").read_text(encoding="utf-8"))
    profiles = json.loads((ROOT / "content/tbc/combat-profiles.json").read_text(encoding="utf-8"))
    ids = {int(s) for s in catalog.get("spells", {})}
    ids |= {row["spell"] for entry in profiles.values() for row in entry.get("rows", ())
            if isinstance(row.get("spell"), int)}
    return sorted(ids)


def reach(db: sqlite3.Connection, ids: list[int]) -> dict[str, dict]:
    ranges = {row[0]: (_float(row[1]), _float(row[2]))
              for row in db.execute("SELECT id, c1, c2 FROM dbc_SpellRange")}
    casts = {row[0]: row[1] for row in db.execute("SELECT id, c1 FROM dbc_SpellCastTimes")}
    out = {}
    for (spell_id, range_index, cast_index, attributes_ex, attributes, attributes_ex2, server,
         family, family_flags) in db.execute(
            f"SELECT Id, RangeIndex, CastingTimeIndex, AttributesEx, Attributes, AttributesEx2, "
            f"AttributesServerside, SpellFamilyName, SpellFamilyFlags "
            f"FROM world_spell_template WHERE Id IN ({','.join('?' * len(ids))})", ids):
        low, high = ranges.get(range_index, (0.0, 0.0))
        out[str(spell_id)] = {"min_yd": round(low, 1), "max_yd": round(high, 1),
                              "cast_s": round(max(0, _signed(casts.get(cast_index, 0))) / 1000, 2)}
        if (attributes_ex or 0) & CHANNELED:
            out[str(spell_id)]["channel"] = True
        if (attributes or 0) & NEXT_SWING:
            out[str(spell_id)]["next_swing"] = True
        if (attributes_ex2 or 0) & AUTO_REPEAT:
            out[str(spell_id)]["repeats"] = True
        pounce = family == SPELLFAMILY_DRUID and int(family_flags or 0) & POUNCE_FAMILY_FLAG
        if ((((attributes_ex2 or 0) & BEHIND_EX2 and (attributes_ex or 0) & BEHIND_EX)
                and not pounce) or (server or 0) & FACING_BACK_SS):
            out[str(spell_id)]["behind"] = True
        out[str(spell_id)].update(damage(db, spell_id))
    return dict(sorted(out.items(), key=lambda item: int(item[0])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DB)
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    args = parser.parse_args(argv)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    table = reach(db, spell_ids())
    args.out.write_text(json.dumps({"format": 1, "spells": table}, indent=1) + "\n",
                        encoding="utf-8")
    print(f"{len(table)} spells -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
