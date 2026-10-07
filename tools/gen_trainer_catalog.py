"""Generate the trainer catalog: who trains each class, what they teach, what each spell is for.

A character trains at its class trainer and puts what it learned on its bar
(`jev.world.training`). That needs facts the strip cannot give, from this server's world
database, like the vendor and gear catalogs:

- `trainers`: every class trainer with a spawn, its class, which sides it serves (by its
  faction template's masks), where it stands, and the gossip line that opens its training
  window ("I would like to train further in the ways of the Light.") where it has one;
- `offers`: per trainer, the spells it teaches - the trainer's own spell, which is what its
  window lists and the server sells (a trainer spell whose effect is "learn spell" teaches
  its trigger spells: Judgement is sold as 10321, which teaches 20271 and a Seal of
  Righteousness), the level and the price;
- `spells`: for every spell taught (all of a trainer spell's "learn spell" effects), every
  spell a class starts with on its bar, a wand's shot a class starts knowing (V397), and
  every spell that replaces one of those on the bar when learned, what it is: its name and
  rank, and a **role** read from what it does;
  and for a trainer spell that teaches others, the facts of the first it teaches with
  `teaches`, the spells it teaches (V359).

Roles, from the spell's own data, never from its name:

    heal         first effect heals (10)
    last_resort  heals to full (67), or a heal on a cooldown of ten minutes or more
    aura         an area aura on the party that never lapses (35, infinite duration), or one
                 on the caster alone that never lapses and raises what it fights with
                 (dodge, attack power, ranged attack power: Aspect of the Monkey, V361)
    long_buff    an aura on a friend, the caster or the party round it (a shout, V361)
                 lasting a minute or more (not threat)
    short_buff   an aura on the caster lasting under a minute (a seal)
    save         the caster or a friend made immune to damage (aura 39 or 40, any effect),
                 or shielded from damage of every school (aura 69: Power Word: Shield, V361)
    cc           one enemy held out of the fight (V395): its first effect confuses (aura 5:
                 Polymorph), fears (7: Fear, Scare Beast) or roots (26: Entangling Roots) it,
                 or transforms it (56), or any effect stuns it with a stun that damage breaks
                 (aura 12 and the damage interrupt flag: Gouge, Hibernate, Shackle Undead,
                 Sap); with how long it holds (`holds_s`), the creature types it takes
                 (`creatures`, the spell's TargetCreatureType mask: Hibernate's beasts and
                 dragonkin) and whether it only pins the unit where it stands (`pins`, a
                 root: one already at hand strikes on)
    guard        a crowd's blows held off for a few seconds (V396): the enemies round the
                 caster feared (aura 7 round it: Psychic Scream, Howl of Terror), or a short
                 aura on the caster that turns blows aside - dodge, parry, block, damage
                 taken made less (auras 49, 47, 51, 87: Evasion, Shield Block, Shield Wall) -
                 or a short dummy aura on it that answers the melee blows it takes
                 (Retaliation);
                 with how long it lasts (`holds_s`) and whether it reaches round the caster
                 (`around`)

and, whatever the role, `form` for a spell cast only in a form or stance the character does
not take (V394: a druid's Claw, a rogue's Sap, a warrior's Shield Block).
    stun         the enemy stunned (aura 12)
    wand         a wand's shot, repeating (`Shoot`, V397): a weapon blow that asks for a wand;
                 known from the start by every priest, mage and warlock
    strike       damage, a weapon blow or a drain on the enemy target, on a cooldown or not;
                 or an aura on the enemy with damage beside it (Frostbolt's slow) or a
                 periodic missile (Arcane Missiles); or, cast with mana, damage to the enemy
                 beside a first effect of another kind (Earth Shock's interrupt, V361)
    dot          damage over time on the enemy (aura 3), not channelled, no combo point
                 given or spent: Corruption, Shadow Word: Pain, Serpent Sting, Rend (V361)
    mark         an aura on the enemy that raises the attack power of whoever attacks it
                 (auras 127 and 165: Hunter's Mark, V404), put on a unit once as a fight opens
    slow         the enemy slowed (aura 33) as the first effect, with no damage beside it and
                 nothing that holds it, cast with mana and not channelled (Concussive Shot,
                 V404): pressed at a unit coming for the character
    root         everything round the caster held in place (aura 26: Frost Nova)
    conjure      an item made for the caster (effect 24: Conjure Water, Conjure Food), and
                 which item (`creates`)
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
AURA_PERIODIC_TRIGGER = 4
AURA_ROOT = 26
AURA_SLOW = 33
AURA_TRANSFORM = 56
AURA_PERIODIC_DAMAGE = 3
AURA_SCHOOL_ABSORB = 69
SCHOOLS_ALL = 127
EFFECT_ADD_COMBO_POINTS = 80
POWER_MANA = 0
# Auras on the caster alone, never lapsing, that raise what it fights with (V361): dodge,
# melee and ranged attack power, damage done, armour and resistances. Not a form or stance
# (36), stealth (16), speed (31, 129) or tracking (44, 45).
AURAS_KEPT = (13, 22, 49, 99, 124)
# Every party member round the caster: Battle Shout (V361).
TARGET_PARTY_AROUND = 20
EFFECT_CREATE_ITEM = 24
# Threat (Righteous Fury): a tank's buff, of no use to a character fighting alone.
AURA_THREAT = 10
AURAS_IMMUNE = (39, 40)
TARGET_SELF = 1
TARGET_ENEMY = 6
# Round the caster (A), every enemy in the area (B): Arcane Explosion, Thunder Clap, Frost Nova.
TARGET_CASTER_AREA = 22
TARGET_ENEMIES_IN_AREA = 15
CAST_INSTANT = 1
TARGETS_FRIEND = (21, 57)
DURATION_INFINITE = 21
ATTR_PASSIVE = 0x40
# What holds one enemy out of the fight (V395): confused, feared, rooted, transformed, and a
# stun that damage breaks (`AURA_INTERRUPT_FLAG_DAMAGE`).
AURA_CONFUSE = 5
AURA_FEAR = 7
AURAS_HOLD = (AURA_CONFUSE, AURA_FEAR, AURA_ROOT, AURA_TRANSFORM)
INTERRUPT_ON_DAMAGE = 0x2
# Auras on the caster that turn a crowd's blows aside for a few seconds (V396): parry, dodge,
# block, and damage taken made less (not Recklessness's more); and a dummy aura that answers
# the melee blows it takes, whose effect the server scripts (Retaliation's counterstrike; a
# seal's answers blows dealt).
AURAS_GUARD = (47, 49, 51)
AURA_DAMAGE_TAKEN = 87
AURA_DUMMY = 4
PROC_TAKEN_MELEE = 0x8 | 0x20
# An enemy marked: whoever attacks it hits harder (V404), the ranged and melee attack power
# its attackers gain (`SPELL_AURA_RANGED_ATTACK_POWER_ATTACKER_BONUS`, `..._MELEE_...`).
AURAS_MARK = (127, 165)
# A wand's shot (V397): a weapon blow that asks for a wand (item class 2, subclass 19) and
# repeats until stopped (the server's `SPELL_ATTR_EX2_AUTOREPEAT_FLAG`).
ITEM_CLASS_WEAPON = 2
WAND_SUBCLASS_MASK = 1 << 19
AUTO_REPEAT = 0x20
# A spell cast only in a form or stance the character does not take (V394): its `Stances`
# mask set, without the attribute that lets it be cast unshifted - a druid's Claw and Maul, a
# rogue's Sap, a warrior's Revenge and Shield Block (Defensive Stance). A warrior lives in
# Battle Stance, the stance it starts in and the only one taken, and what that allows is
# its own (Overpower, Retaliation).
NOT_NEED_SHAPESHIFT = 0x80000
BATTLE_STANCE = 1 << 16

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
        "EffectApplyAuraName2, EffectApplyAuraName3, Effect2, Effect3, EffectItemType1, "
        "EffectImplicitTargetB1, CastingTimeIndex, ChannelInterruptFlags, PowerType, "
        "EffectMiscValue1, EffectPointsPerComboPoint1, EffectImplicitTargetA2, "
        "EffectImplicitTargetA3, AuraInterruptFlags, EquippedItemClass, "
        "EquippedItemSubClassMask, AttributesEx2, EffectBasePoints1, EffectBasePoints2, "
        "EffectBasePoints3, ProcFlags, Stances "
        "from world_spell_template where Id=?", (spell_id,)).fetchone()
    if row is None:
        return None
    (name, rank, attributes, effect, aura, target, duration_index, recovery, category,
     mana, mana_pct, caster_state, creature_type, aura2, aura3, effect2, effect3,
     item, target_b, cast_index, channel, power_type, misc, per_combo, target2, target3,
     interrupts, item_class, item_subclasses, attributes_ex2, points, points2, points3,
     procs, stances) = row
    combo = bool(per_combo) or EFFECT_ADD_COMBO_POINTS in (effect, effect2, effect3)
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
    if (stances and not (attributes_ex2 or 0) & NOT_NEED_SHAPESHIFT
            and not stances & BATTLE_STANCE):
        facts["form"] = True
    if caster_state:
        # Usable only in a state another spell sets, and spent by it: Judgement needs a
        # seal and releases it.
        facts["spends"] = True
    # Each effect's aura and its target, for what holds an enemy or guards the caster (V395).
    effects = ((effect, aura, target), (effect2, aura2, target2), (effect3, aura3, target3))
    lessened = any(a == AURA_DAMAGE_TAKEN and (p or 0) < 0
                   for a, p in ((aura, points), (aura2, points2), (aura3, points3)))
    holds = ((effect == EFFECT_APPLY_AURA and aura in AURAS_HOLD and target == TARGET_ENEMY)
             or (bool((interrupts or 0) & INTERRUPT_ON_DAMAGE)
                 and any(e == EFFECT_APPLY_AURA and a == AURA_STUN and t == TARGET_ENEMY
                         for e, a, t in effects)))
    scatters = any(e == EFFECT_APPLY_AURA and a == AURA_FEAR and t == TARGET_CASTER_AREA
                   for e, a, t in effects)
    short_on_self = (effect == EFFECT_APPLY_AURA and target == TARGET_SELF and duration_s
                     and duration_s < LONG_BUFF_S)
    if attributes & ATTR_PASSIVE:
        facts["role"] = "passive"
    elif effect == EFFECT_ATTACK:
        facts["role"] = "attack"
    elif (item_class == ITEM_CLASS_WEAPON and item_subclasses == WAND_SUBCLASS_MASK
          and (attributes_ex2 or 0) & AUTO_REPEAT and effect in EFFECTS_WEAPON):
        # A wand's shot (V397): no mana, no global cooldown, a caster's damage when its mana
        # is spent or its unit nearly dead.
        facts["role"] = "wand"
    elif holds and not auras & set(AURAS_IMMUNE):
        # One enemy out of the fight while it lasts (V395), before a stun and a strike: Gouge
        # does a little damage first, Hibernate is a stun that damage breaks. Not Banish,
        # which makes its unit immune too (a save, below).
        facts["role"] = "cc"
        facts["holds_s"] = duration_s or 0.0
        if creature_type:
            facts["creatures"] = creature_type
        if effect == EFFECT_APPLY_AURA and aura == AURA_ROOT:
            facts["pins"] = True
    elif scatters or (short_on_self and (auras & set(AURAS_GUARD) or lessened
                                         or (auras == {AURA_DUMMY}
                                             and (procs or 0) & PROC_TAKEN_MELEE))):
        # A crowd held off (V396): before the buffs, which these were (Evasion, Shield Block).
        facts["role"] = "guard"
        facts["holds_s"] = duration_s or 0.0
        if scatters:
            facts["around"] = True
    elif effect == EFFECT_HEAL_MAX_HEALTH or (effect == EFFECT_HEAL
                                              and cooldown_s >= LAST_RESORT_COOLDOWN_S):
        facts["role"] = "last_resort"
    elif effect == EFFECT_HEAL:
        facts["role"] = "heal"
    elif effect == EFFECT_AREA_AURA_PARTY and lasting and target == TARGET_SELF:
        facts["role"] = "aura"
        facts["aura"] = aura
    elif (effect == EFFECT_APPLY_AURA and lasting and target == TARGET_SELF
          and aura in AURAS_KEPT):
        facts["role"] = "aura"
        facts["aura"] = aura
    elif (effect == EFFECT_APPLY_AURA and aura == AURA_SCHOOL_ABSORB and misc == SCHOOLS_ALL
          and (target == TARGET_SELF or target in TARGETS_FRIEND) and duration_s):
        # Shielded from every school (Power Word: Shield): pressed before a heal as a save
        # is, not again while it lasts (V361). Fire Ward's one school is not.
        facts["role"] = "save"
        facts["every_s"] = max(1.0, duration_s - BUFF_MARGIN_S)
    elif effect == EFFECT_APPLY_AURA and auras & set(AURAS_IMMUNE):
        facts["role"] = "save"
    elif effect == EFFECT_APPLY_AURA and aura == AURA_STUN and target == TARGET_ENEMY:
        facts["role"] = "stun"
    elif (effect in (EFFECT_APPLY_AURA, EFFECT_AREA_AURA_PARTY) and duration_s
          and (target in (TARGET_SELF, TARGET_PARTY_AROUND) or target in TARGETS_FRIEND)
          and aura != AURA_THREAT):
        facts["role"] = "long_buff" if duration_s >= LONG_BUFF_S else "short_buff"
        facts["aura"] = aura
        facts["every_s"] = max(1.0, duration_s - BUFF_MARGIN_S)
    elif (effect in (EFFECT_SCHOOL_DAMAGE, EFFECT_DUMMY, EFFECT_SCRIPT, *EFFECTS_WEAPON)
          and target == TARGET_ENEMY and not creature_type):
        # Not a power drain (Mana Tap, Drain Mana): it takes mana and hurts nothing, and
        # against a unit without mana every press is refused (V288).
        facts["role"] = "strike"
    elif (effect == EFFECT_APPLY_AURA and target == TARGET_ENEMY and not creature_type
          and (EFFECT_SCHOOL_DAMAGE in (effect2, effect3) or aura == AURA_PERIODIC_TRIGGER)):
        # Damage whose first effect is its rider: Frostbolt's slow, Arcane Missiles'
        # periodic missile (V165).
        facts["role"] = "strike"
    elif (effect == EFFECT_APPLY_AURA and aura == AURA_PERIODIC_DAMAGE and target == TARGET_ENEMY
          and not creature_type and not channel and not combo):
        # Damage over time: put on a unit once and not again while it lasts (V361, V360).
        facts["role"] = "dot"
    elif (target == TARGET_ENEMY and not creature_type and power_type == POWER_MANA
          and EFFECT_SCHOOL_DAMAGE in (effect2, effect3) and not channel and not combo):
        # Damage beside a first effect of another kind, cast with mana: Earth Shock (V361).
        # A rage or energy interrupt (Kick, Shield Bash) is the interrupt's, not damage's.
        facts["role"] = "strike"
    elif any(e == EFFECT_APPLY_AURA and a in AURAS_MARK and t == TARGET_ENEMY
             for e, a, t in effects):
        # A unit marked as the fight opens, not again while it lasts (V404): Hunter's Mark.
        facts["role"] = "mark"
        facts["every_s"] = duration_s or 0.0
    elif (effect == EFFECT_APPLY_AURA and aura == AURA_SLOW and target == TARGET_ENEMY
          and not creature_type and not channel and power_type == POWER_MANA
          and not auras & set(AURAS_HOLD)
          and EFFECT_SCHOOL_DAMAGE not in (effect2, effect3)
          and not set(EFFECTS_WEAPON) & {effect2, effect3}):
        # A unit coming for the character slowed, nothing else (V404): Concussive Shot.
        facts["role"] = "slow"
        facts["holds_s"] = duration_s or 0.0
    elif AURA_ROOT in auras and target != TARGET_ENEMY:
        facts["role"] = "root"
    elif (effect == EFFECT_SCHOOL_DAMAGE and target == TARGET_CASTER_AREA
          and target_b == TARGET_ENEMIES_IN_AREA and cast_index == CAST_INSTANT):
        # Damage to every enemy round the caster, at once (V277): Arcane Explosion.
        facts["role"] = "area"
    elif AURA_TRANSFORM in auras and target == TARGET_ENEMY:
        facts["role"] = "cc"
    elif effect == EFFECT_CREATE_ITEM and target == TARGET_SELF:
        facts["role"] = "conjure"
        facts["creates"] = item
    else:
        facts["role"] = "utility"
    if AURA_SLOW in auras and target == TARGET_ENEMY:
        facts["slows"] = True
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
    wrappers: dict[int, list[int]] = {}
    for entry, name, faction, klass, template, menu in db.execute(
            "select Entry, Name, Faction, TrainerClass, TrainerTemplateId, GossipMenuId "
            "from world_creature_template where TrainerType=0 and TrainerClass>0 "
            "order by Entry"):
        if not name or name.startswith("["):
            continue
        # Not a spawn only a game event puts there (see tools/gen_vendor_catalog.py).
        spawns = db.execute("select map, position_x, position_y, position_z from "
                            "world_creature where id=? and guid not in (select guid from "
                            "world_game_event_creature where event > 0) order by guid",
                            (entry,)).fetchall()
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
                # Sold by its own id (V359): the server's CMSG_TRAINER_BUY_SPELL looks the
                # trainer's list up by it, and the hive's paladins asked for 20271, "not
                # trained", at every visit, and never had Judgement.
                rows.append({"spell": spell, "level": level, "cost": cost})
                if learned != [spell]:
                    wrappers[spell] = learned
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
    # A wand's shot, which a priest, a mage and a warlock know from the start and nothing puts
    # on a bar (V397): Shoot, 5019.
    for (spell_id,) in db.execute("select distinct Spell from world_playercreateinfo_spell "
                                  "order by Spell"):
        facts = spell_facts(db, spell_id) if str(spell_id) not in spells else None
        if facts is not None and facts["role"] == "wand":
            spells[str(spell_id)] = facts
    for spell_id, learned in sorted(wrappers.items()):
        facts = spells.get(str(learned[0]))
        if facts is not None:
            spells[str(spell_id)] = {**facts, "teaches": learned}
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
