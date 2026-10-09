"""Generate the potions table: every healing and mana potion, what it restores, from what level.

A fight about to be lost drinks a healing potion, and a caster out of mana a mana potion
(`jev.clients.fight`, DECISIONS V555): what characters carry from quests and loot. A potion is
an item of class 0, subclass 1 whose spell is of the potions' shared cooldown (category 4, two
minutes) and heals (effect 10) or gives mana (effect 30, power 0).

Each row: `kind` ("heal" or "mana"), `level` (required to use it), `item_level`, and `min` and
`max` (what it restores).

Usage: .venv/bin/python tools/gen_potions.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data/knowledge/tbc-243.sqlite"
OUT = ROOT / "content/tbc/potions.json"
POTION_CATEGORY = 4                  # the potions' shared cooldown
EFFECT_HEAL, EFFECT_ENERGIZE, POWER_MANA = 10, 30, 0


def generate(db: sqlite3.Connection) -> dict:
    spells = {row[0]: row[1:] for row in db.execute(
        "SELECT Id, Effect1, EffectMiscValue1, EffectBasePoints1, EffectDieSides1 "
        "FROM world_spell_template")}
    out = {}
    for entry, spell, category, level, item_level in db.execute(
            "SELECT entry, spellid_1, spellcategory_1, RequiredLevel, ItemLevel "
            "FROM world_item_template WHERE class = 0 AND subclass = 1 AND spellid_1 > 0 "
            "ORDER BY entry"):
        facts = spells.get(spell)
        if facts is None or category != POTION_CATEGORY:
            continue
        effect, misc, base, sides = facts
        if effect == EFFECT_HEAL:
            kind = "heal"
        elif effect == EFFECT_ENERGIZE and misc == POWER_MANA:
            kind = "mana"
        else:
            continue
        low = (base or 0) + 1
        out[str(entry)] = {"kind": kind, "level": level or 0, "item_level": item_level or 0,
                           "min": low, "max": low + max(0, (sides or 1) - 1)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DB)
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    args = parser.parse_args(argv)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    table = generate(db)
    args.out.write_text(json.dumps({"format": 1, "items": table}, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"{len(table)} potions -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
