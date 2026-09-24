"""Generate the trainer catalog: who trains each class, what they teach, what each spell is for.

A character trains at its class trainer and puts what it learned on its bar
(`jev.world.training`). That needs facts the strip cannot give, from this server's world
database, like the vendor and gear catalogs:

- `trainers`: every class trainer with a spawn, its class, which sides it serves (by its
  faction template's masks), where it stands, and the gossip line that opens its training
  window ("I would like to train further in the ways of the Light.") where it has one;
- `offers`: per trainer, the spells it teaches - the spell learned (a trainer spell whose
  effect is "learn spell" teaches its trigger spell: Judgement is taught by 10321), the
  level and the price;
- `spells`: for every spell taught (all of a trainer spell's "learn spell" effects), every
  spell a class starts with on its bar, and every spell that replaces one of those on the
  bar when learned, what it is: its name and rank, and a **role** read from what it does.

Roles, from the spell's own data, never from its name:

    heal         first effect heals (10)
    last_resort  heals to full (67), or a heal on a cooldown of ten minutes or more
    aura         an area aura on the party that never lapses (35, infinite duration)
    long_buff    an aura on a friend or the caster lasting a minute or more (not threat)
    short_buff   an aura on the caster lasting under a minute (a seal)
    save         the caster or a friend made immune to damage (aura 39 or 40, any effect)
    stun         the enemy stunned (aura 12)
    strike       damage, a weapon blow or a drain on the enemy target, on a cooldown or not
    attack       melee auto-attack (78), a toggle
    passive      a passive spell: nothing to press
    utility      anything else (dispels, resurrection, a strike only some creatures take)

Usage: .venv/bin/python tools/gen_trainer_catalog.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Faction template masks.
MASK_PLAYER, MASK_ALLIANCE, MASK_HORDE = 1, 2, 4
SIDES = {"alliance": MASK_ALLIANCE, "horde": MASK_HORDE}

# Spell effects and auras that decide a role.
EFFECT_SCHOOL_DAMAGE = 2
EFFECT_DUMMY = 3
EFFECT_APPLY_AURA = 6
EFFECT_POWER_DRAIN = 8
# Weapon damage: without school (Heroic Strike), by percent, plain (Raptor Strike, Auto
# Shot) and normalised (Sinister Strike).
EFFECTS_WEAPON = (17, 31, 58, 121)
EFFECT_HEAL = 10
EFFECT_AREA_AURA_PARTY = 35
EFFECT_LEARN_SPELL = 36
EFFECT_HEAL_MAX_HEALTH = 67
EFFECT_SCRIPT = 77
EFFECT_ATTACK = 78
AURA_STUN = 12
# Threat (Righteous Fury): a tank's buff, of no use to a character fighting alone.
AURA_THREAT = 10
AURAS_IMMUNE = (39, 40)
TARGET_SELF = 1
TARGET_ENEMY = 6
TARGETS_FRIEND = (21, 57)
DURATION_INFINITE = 21
ATTR_PASSIVE = 0x40

LONG_BUFF_S = 60.0
LAST_RESORT_COOLDOWN_S = 600.0
# How long before a buff lapses to press it again (as tools/gen_combat_profiles.py).
BUFF_MARGIN_S = 5.0


def _rank(text: str | None) -> int:
    match = re.search(r"(\d+)", text or "")
    return int(match.group(1)) if match else 0


def spell_facts(db: sqlite3.Connection, spell_id: int) -> dict | None:
    row = db.execute(
        "select SpellName, Rank1, Attributes, Effect1, EffectApplyAuraName1, "
        "EffectImplicitTargetA1, DurationIndex, RecoveryTime, CategoryRecoveryTime, "
        "ManaCost, ManaCostPercentage, CasterAuraState, TargetCreatureType, "
        "EffectApplyAuraName2, EffectApplyAuraName3 "
        "from world_spell_template where Id=?", (spell_id,)).fetchone()
    if row is None:
        return None
    (name, rank, attributes, effect, aura, target, duration_index, recovery, category,
     mana, mana_pct, caster_state, creature_type, aura2, aura3) = row
    # Divine Protection pacifies first and makes immune second: any effect's aura counts.
    auras = {aura, aura2, aura3} - {0, None}
    duration_ms = None
    if duration_index:
        found = db.execute("select c1 from dbc_SpellDuration where id=?",
                           (duration_index,)).fetchone()
        duration_ms = found[0] if found else None
    lasting = duration_index == DURATION_INFINITE
    duration_s = None if lasting or not duration_ms else duration_ms / 1000.0
    cooldown_s = max(recovery or 0, category or 0) / 1000.0
    facts = {"name": name, "rank": _rank(rank), "mana": mana or 0,
             "cooldown_s": cooldown_s, "target": ("self" if target == TARGET_SELF else
                                                  "enemy" if target == TARGET_ENEMY else
                                                  "friend" if target in TARGETS_FRIEND else
                                                  "other")}
    if mana_pct:
        facts["mana_pct"] = mana_pct
    if caster_state:
        # Usable only in a state another spell sets, and spent by it: Judgement needs a
        # seal and releases it.
        facts["spends"] = True
    if attributes & ATTR_PASSIVE:
        facts["role"] = "passive"
    elif effect == EFFECT_ATTACK:
        facts["role"] = "attack"
    elif effect == EFFECT_HEAL_MAX_HEALTH or (effect == EFFECT_HEAL
                                              and cooldown_s >= LAST_RESORT_COOLDOWN_S):
        facts["role"] = "last_resort"
    elif effect == EFFECT_HEAL:
        facts["role"] = "heal"
    elif effect == EFFECT_AREA_AURA_PARTY and lasting and target == TARGET_SELF:
        facts["role"] = "aura"
        facts["aura"] = aura
    elif effect == EFFECT_APPLY_AURA and auras & set(AURAS_IMMUNE):
        facts["role"] = "save"
    elif effect == EFFECT_APPLY_AURA and aura == AURA_STUN and target == TARGET_ENEMY:
        facts["role"] = "stun"
    elif (effect in (EFFECT_APPLY_AURA, EFFECT_AREA_AURA_PARTY) and duration_s
          and (target == TARGET_SELF or target in TARGETS_FRIEND)
          and aura != AURA_THREAT):
        facts["role"] = "long_buff" if duration_s >= LONG_BUFF_S else "short_buff"
        facts["aura"] = aura
        facts["every_s"] = max(1.0, duration_s - BUFF_MARGIN_S)
    elif (effect in (EFFECT_SCHOOL_DAMAGE, EFFECT_DUMMY, EFFECT_SCRIPT, EFFECT_POWER_DRAIN,
                     *EFFECTS_WEAPON)
          and target == TARGET_ENEMY and not creature_type):
        facts["role"] = "strike"
    else:
        facts["role"] = "utility"
    return facts


def _sides(db: sqlite3.Connection, template: int) -> list[str]:
    row = db.execute("select c3, c4, c5 from dbc_FactionTemplate where id=?",
                     (template,)).fetchone()
    if row is None:
        return []
    ours, friends, enemies = row
    return [side for side, mask in SIDES.items()
            if not enemies & mask and (ours | friends) & (mask | MASK_PLAYER)]


def _gossip(db: sqlite3.Connection, menu: int) -> str | None:
    """The line that opens the training window: gossip option 5 (GOSSIP_OPTION_TRAINER)."""
    if not menu:
        return None
    row = db.execute("select option_text from world_gossip_menu_option where menu_id=? "
                     "and option_id=5 order by id limit 1", (menu,)).fetchone()
    return row[0] if row and row[0] else None


def _learned(db: sqlite3.Connection, spell: int) -> list[int]:
    """What a trainer's spell teaches: itself, or every spell its "learn spell" effects
    name. Judgement's (10321) teaches Judgement and a Seal of Righteousness (21084) that
    replaces the one on the bar."""
    row = db.execute("select Effect1, EffectTriggerSpell1, Effect2, EffectTriggerSpell2, "
                     "Effect3, EffectTriggerSpell3 from world_spell_template where Id=?",
                     (spell,)).fetchone()
    if row is None:
        return [spell]
    taught = [trigger for effect, trigger in zip(row[::2], row[1::2], strict=True)
              if effect == EFFECT_LEARN_SPELL and trigger]
    return taught or [spell]


def _successors(db: sqlite3.Connection, spells: set[int]) -> set[int]:
    """The spells that replace these on a bar when learned (the skill line's successor,
    SkillLineAbility's forward spell), and theirs in turn."""
    out, frontier = set(spells), set(spells)
    while frontier:
        found = set()
        for spell in frontier:
            for (forward,) in db.execute("select c8 from dbc_SkillLineAbility where c2=? "
                                         "and c8>0", (spell,)):
                if forward not in out:
                    found.add(forward)
        out |= found
        frontier = found
    return out


def generate(db: sqlite3.Connection) -> dict:
    trainers, offers, taught = [], {}, set()
    for entry, name, faction, klass, template, menu in db.execute(
            "select Entry, Name, Faction, TrainerClass, TrainerTemplateId, GossipMenuId "
            "from world_creature_template where TrainerType=0 and TrainerClass>0 "
            "order by Entry"):
        if not name or name.startswith("["):
            continue
        spawns = db.execute("select map, position_x, position_y, position_z from "
                            "world_creature where id=? order by guid", (entry,)).fetchall()
        sides = _sides(db, faction)
        if not spawns or not sides:
            continue
        key = f"t{template}" if template else f"n{entry}"
        if key not in offers:
            table, owner = (("world_npc_trainer_template", template) if template
                            else ("world_npc_trainer", entry))
            rows = []
            for spell, cost, skill, level in db.execute(
                    f"select spell, spellcost, reqskill, reqlevel from {table} "
                    "where entry=? and condition_id=0 order by reqlevel, spell", (owner,)):
                if skill:
                    continue                      # a profession's recipe, not a class spell
                learned = _learned(db, spell)
                rows.append({"spell": learned[0], "level": level, "cost": cost})
                taught.update(learned)
            offers[key] = rows
        for map_id, x, y, z in spawns:
            trainers.append({"entry": entry, "name": name, "class": klass, "sides": sides,
                             "map_id": map_id, "world": [float(x), float(y), float(z)],
                             "gossip": _gossip(db, menu), "offers": key})
    starting = {row[0] for row in db.execute(
        "select distinct action from world_playercreateinfo_action where type=0")}
    spells = {}
    for spell_id in sorted(_successors(db, taught | starting)):
        facts = spell_facts(db, spell_id)
        if facts is not None:
            spells[str(spell_id)] = facts
    return {"format": 1, "trainers": trainers, "offers": offers, "spells": spells}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=ROOT / "data/knowledge/tbc-243.sqlite")
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "content/tbc/trainer-catalog.json")
    args = parser.parse_args(argv)
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
        catalog = generate(db)
    args.out.write_text(json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    print(f"{len(catalog['trainers'])} trainer spawns, {len(catalog['offers'])} offer lists, "
          f"{len(catalog['spells'])} spells -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
