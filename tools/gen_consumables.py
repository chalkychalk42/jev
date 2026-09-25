"""Generate the consumables table: every food and drink, what it restores, and from what level.

A caster conjures its water (`jev.world.training`, role `conjure`), and the bar's drink slot
holds the starting water, so a conjured drink is taken from the bags (`Vendor.use_item`,
DECISIONS V166). Which bag items are food or drink is read the way the combat profiles read
the starting bar's: an item of class 0, subclass 5, whose spell's aura is 84 (health: food)
or 85 (mana: drink), or both.

Each row: `role` ("food", "drink" or "both"), `level` (required to use it), `item_level`
(the better food of two is the higher) and `conjured` (the item vanishes on logout).

Usage: .venv/bin/python tools/gen_consumables.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data/knowledge/tbc-243.sqlite"
OUT = ROOT / "content/tbc/consumables.json"
AURA_FOOD, AURA_DRINK = 84, 85
ITEM_FLAG_CONJURED = 0x2


def generate(db: sqlite3.Connection) -> dict:
    auras = {row[0]: {row[1], row[2], row[3]} - {0, None} for row in db.execute(
        "SELECT Id, EffectApplyAuraName1, EffectApplyAuraName2, EffectApplyAuraName3 "
        "FROM world_spell_template")}
    out = {}
    for entry, spell, level, item_level, flags in db.execute(
            "SELECT entry, spellid_1, RequiredLevel, ItemLevel, Flags FROM world_item_template "
            "WHERE class = 0 AND subclass = 5 AND spellid_1 > 0 ORDER BY entry"):
        found = auras.get(spell, set())
        food, drink = AURA_FOOD in found, AURA_DRINK in found
        if not (food or drink):
            continue
        out[str(entry)] = {"role": "both" if food and drink else "food" if food else "drink",
                           "level": level or 0, "item_level": item_level or 0,
                           "conjured": bool((flags or 0) & ITEM_FLAG_CONJURED)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=DB)
    parser.add_argument("--out", type=pathlib.Path, default=OUT)
    args = parser.parse_args(argv)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    table = generate(db)
    args.out.write_text(json.dumps({"format": 1, "items": table}, indent=1) + "\n",
                        encoding="utf-8")
    print(f"{len(table)} foods and drinks -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
