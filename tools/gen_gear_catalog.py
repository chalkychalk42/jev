"""Generate the gear catalog: what each wearable item is, and who can wear it.

The bot equips upgrades it finds in its bags (`jev.world.gear`). That needs three facts per
item the strip cannot give: the slot it goes in, whether this character can use it, and how
good it is. They come from this server's world database, like the vendor catalog:

- `items`: every armour piece and one-handed weapon up to level 30 in a slot the bot fills,
  with its armour type or weapon type, required level, class and race masks, and a score -
  armour plus weighted primary stats for armour, damage per second for a weapon;
- `proficiencies`: per `race:class`, the armour and weapon types its starting spells teach
  (Mail, One-Handed Maces, Shield...). A fresh character can use nothing else, and a weapon
  skill it lacks makes the client refuse the item.

Usage: .venv/bin/python tools/gen_gear_catalog.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[1]

# InventoryType -> the slot the bot fills. Rings and trinkets have two slots each, and
# two-handers would take the shield off: left out.
SLOTS = {1: "head", 2: "neck", 3: "shoulders", 5: "chest", 20: "chest", 6: "waist",
         7: "legs", 8: "feet", 9: "wrists", 10: "hands", 16: "back",
         13: "main_hand", 21: "main_hand", 14: "off_hand"}

# Proficiency spells -> (item class, subclass). Armour is class 4, weapons class 2.
PROFICIENCIES = {
    9078: (4, 1), 9077: (4, 2), 8737: (4, 3), 750: (4, 4), 9116: (4, 6),
    196: (2, 0), 198: (2, 4), 201: (2, 7), 1180: (2, 15), 15590: (2, 13),
}
# Armour subclass 0 (cloaks, necks, rings) is miscellaneous and needs no proficiency.
MISC_ARMOUR = (4, 0)

# Item stat types: agility 3, strength 4, intellect 5, spirit 6, stamina 7.
STAT_WEIGHT = {3: 6.0, 4: 10.0, 5: 6.0, 6: 3.0, 7: 10.0}

MAX_LEVEL = 30


def score(row: dict) -> float:
    stats = sum(STAT_WEIGHT.get(row[f"stat_type{i}"], 0.0) * (row[f"stat_value{i}"] or 0)
                for i in range(1, 7))
    if row["class"] == 2:
        delay = (row["delay"] or 0) / 1000.0
        dps = ((row["dmg_min1"] or 0) + (row["dmg_max1"] or 0)) / 2.0 / delay if delay else 0.0
        return round(dps * 10.0 + stats / 10.0, 3)
    return round((row["armor"] or 0) + stats, 3)


def generate(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    items = {}
    wanted = ",".join(str(t) for t in SLOTS)
    for row in db.execute(
            f"select * from world_item_template where RequiredLevel <= ? and class in (2, 4) "
            f"and InventoryType in ({wanted})", (MAX_LEVEL,)):
        row = dict(row)
        items[str(row["entry"])] = {
            "slot": SLOTS[row["InventoryType"]], "kind": [row["class"], row["subclass"]],
            "level": row["RequiredLevel"], "classes": row["AllowableClass"],
            "races": row["AllowableRace"], "quality": row["Quality"], "score": score(row)}
    proficiencies: dict[str, list] = {}
    for race, cls, spell in db.execute(
            "select race, class, Spell from world_playercreateinfo_spell"):
        if spell in PROFICIENCIES:
            proficiencies.setdefault(f"{race}:{cls}", []).append(list(PROFICIENCIES[spell]))
    for key in proficiencies:
        proficiencies[key] = sorted({tuple(k) for k in proficiencies[key]} | {MISC_ARMOUR})
    return {"format": 1, "items": items,
            "proficiencies": {k: [list(v) for v in vs] for k, vs in sorted(proficiencies.items())}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=ROOT / "data/knowledge/tbc-243.sqlite")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "content/tbc/gear-catalog.json")
    args = parser.parse_args(argv)
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
        catalog = generate(db)
    args.out.write_text(json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    print(f"{len(catalog['items'])} items, {len(catalog['proficiencies'])} race:class profiles "
          f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
