#!/usr/bin/env python3
"""Generate the starting combat profiles from the world database.

    .venv/bin/python tools/gen_combat_profiles.py

Writes `content/tbc/combat-profiles.json`: for every race and class, what the game itself
puts on a fresh character's action bar, and **what each of those buttons is for**.

The roles are derived, not typed in. A spell's first effect says what it does — 10 is a
heal, 6 applies an aura, 78 is the melee attack — and a consumable's aura says what it
restores: 84 is health and 85 is mana, which is the only honest way to tell food from
drink when both are item class 0, subclass 5 and neither says so in its name.

    Darnassian Bleu          aura 84   food    slot 12
    Refreshing Spring Water  aura 85   drink   slot 11

That distinction is not academic: `Rest` was pressing slot 11 and reporting that the
character was out of food while a wheel of cheese sat in slot 12.

`every_s` for a buff comes from the spell's own duration — Seal of Righteousness is
`DurationIndex` 9, which is 30 seconds — minus a margin, so a seal is refreshed before it
lapses rather than on a number somebody liked.

What is *not* here is policy. When to heal, when to eat, how low is low: those belong to
the engine that reads this, because they are decisions rather than facts about the game.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
ROOT = pathlib.Path(__file__).resolve().parent.parent

# `playercreateinfo_action.type`
TYPE_SPELL = 0
TYPE_ITEM = 128

# First-effect codes that matter. Everything else a class starts with is something it
# presses at an enemy, which is what `attack` means here.
EFFECT_HEAL = 10
EFFECT_APPLY_AURA = 6
EFFECT_ATTACK = 78

# Consumable aura types: what the thing actually restores.
AURA_MOD_REGEN = 84          # health -> food
AURA_MOD_POWER_REGEN = 85    # mana   -> drink

# How long before a buff lapses to re-press it.
BUFF_MARGIN_S = 5.0

CLASS_NAMES = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest",
               7: "shaman", 8: "mage", 9: "warlock", 11: "druid"}


def spell_row(db, slot: int, action: int) -> dict | None:
    row = db.execute(
        "select SpellName, Effect1, ManaCost, DurationIndex "
        "from world_spell_template where Id=?", (action,)).fetchone()
    if row is None:
        return None
    name, effect, mana, duration_index = row
    out = {"slot": slot, "spell": action, "name": name, "mana": mana or 0}
    if effect == EFFECT_HEAL:
        out["role"] = "heal"
    elif effect == EFFECT_ATTACK:
        # Spell 6603 is melee auto-attack, and it is a **toggle**: pressing it while
        # already swinging stops the swing. A rotation that treats it as a filler turns
        # the character's attack on and off all fight, which is what the first live
        # rotation did — `pressed [1, 2, 2, ..., 1, 2, ...]`.
        out["role"] = "attack"
        out["toggle"] = True
    elif effect == EFFECT_APPLY_AURA:
        out["role"] = "buff"
        ms = db.execute("select c1 from dbc_SpellDuration where id=?",
                        (duration_index,)).fetchone()
        if ms and ms[0]:
            out["every_s"] = max(1.0, ms[0] / 1000.0 - BUFF_MARGIN_S)
    else:
        out["role"] = "attack"
    return out


def item_row(db, slot: int, action: int) -> dict | None:
    row = db.execute("select name, spellid_1 from world_item_template where entry=?",
                     (action,)).fetchone()
    if row is None:
        return None
    name, spell = row
    aura = db.execute("select EffectApplyAuraName1 from world_spell_template where Id=?",
                      (spell,)).fetchone()
    code = aura[0] if aura else None
    role = {AURA_MOD_REGEN: "food", AURA_MOD_POWER_REGEN: "drink"}.get(code)
    if role is None:
        return None
    return {"slot": slot, "item": action, "name": name, "role": role}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "knowledge" / "tbc-243.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "content" / "tbc" / "combat-profiles.json"))
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    profiles: dict[str, dict] = {}
    for race, klass in db.execute(
            "select distinct race, class from world_playercreateinfo_action order by race, class"):
        rows = []
        for button, action, kind in db.execute(
                "select button, action, type from world_playercreateinfo_action "
                "where race=? and class=? order by button", (race, klass)):
            slot = button + 1
            row = (spell_row(db, slot, action) if kind == TYPE_SPELL
                   else item_row(db, slot, action) if kind == TYPE_ITEM else None)
            if row is not None:
                rows.append(row)
        if rows:
            profiles[f"{race}:{klass}"] = {
                "race": race, "class": klass,
                "name": CLASS_NAMES.get(klass, str(klass)), "rows": rows,
            }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profiles, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    roles = sorted({r["role"] for p in profiles.values() for r in p["rows"]})
    print(f"wrote {out} — {len(profiles)} race/class profiles, roles {roles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
