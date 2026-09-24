#!/usr/bin/env python3
"""Generate conservative vendor facts from the server DB and starting bar profiles.

No prices are used to author transactions: the open merchant's observed offer wins.
The sell allowlist deliberately excludes every item mentioned as a requirement/source/
reward by any quest, even when its quality is poor. Poor weapons and armour are sold:
nothing here equips gear, and a first-levels backpack of sixteen slots filled with them
(Frayed Shoes, Unkempt Pants) until the character could loot nothing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent


def generate(db: sqlite3.Connection, profiles: dict) -> dict:
    quest_cols = [r[1] for r in db.execute("pragma table_info(world_quest_template)")
                  if r[1].startswith(("ReqItemId", "ReqSourceId", "RewItemId",
                                      "RewChoiceItemId")) or r[1] == "SrcItemId"]
    protected = {int(i) for row in db.execute(
        "select " + ",".join(quest_cols) + " from world_quest_template") for i in row if i}
    junk = [int(row[0]) for row in db.execute(
        "select entry from world_item_template where Quality=0 and SellPrice>0 "
        "and class not in (0,1,2,4,6,11,12,13,15,16) "
        "and InventoryType=0 and startquest=0 order by entry") if row[0] not in protected]
    supplies = {key: {r["role"]: {"item_id": r["item"], "name": r["name"],
                                 "slot": r["slot"]}
                      for r in profile["rows"] if r["role"] in ("food", "drink")}
                for key, profile in profiles.items()}
    # Poor misc (class 15) drops are the usual vendor trash. Permit them only after
    # protecting all quest references, consumables and equipment above.
    junk += [int(row[0]) for row in db.execute(
        "select entry from world_item_template where Quality=0 and SellPrice>0 "
        "and class=15 and InventoryType=0 and startquest=0 order by entry")
             if row[0] not in protected]
    junk += [int(row[0]) for row in db.execute(
        "select entry from world_item_template where Quality=0 and SellPrice>0 "
        "and class in (2,4) and startquest=0 order by entry") if row[0] not in protected]
    vendors = []
    # A spawn a game event adds (a positive event in game_event_creature) stands there only
    # while the event runs: with full bags the character walked to the Darkmoon Faire's
    # empty grounds by Goldshire for Stamp Thunderhorn, Sylannia and Professor Thaddeus
    # Paleo, found none of them, and the session stopped (session 63).
    for row in db.execute(
        "select distinct t.Entry,t.Name,c.map,c.position_x,c.position_y,c.position_z "
        "from world_creature_template t join world_creature c on c.id=t.Entry "
        "where (t.NpcFlags & 128)!=0 and c.guid not in "
        "(select guid from world_game_event_creature where event > 0) "
        "order by t.Entry,c.map,c.position_x,c.position_y"):
        entry, name, map_id, x, y, z = row
        if not name or name.startswith("["):
            # "[DND] TAR Pedestal - Trainer, Druid" and 779 others: developer placeholders the
            # server flags as vendors. The nearest one was walked to on a sale.
            continue
        sold = {int(r[0]) for r in db.execute(
            "select item from world_npc_vendor where entry=? and ExtendedCost=0 "
            "and condition_id=0 union select v.item from world_npc_vendor_template v "
            "join world_creature_template t on t.VendorTemplateId=v.entry "
            "where t.Entry=? and v.ExtendedCost=0 and v.condition_id=0", (entry, entry))}
        vendors.append({"entry": entry, "name": name, "map_id": map_id,
                        # The mirrored world DB stores coordinates as TEXT. Emit
                        # numeric yards, matching the guide generator's contract.
                        "world": [float(x), float(y), float(z)], "items": sorted(sold)})
    prices = {str(row[0]): int(row[1]) for row in db.execute(
        "select entry,SellPrice from world_item_template") if row[0] in set(junk)}
    # General bags (any item fits) and their slots: one found in the bags goes on the belt.
    bags = {str(row[0]): int(row[1]) for row in db.execute(
        "select entry,ContainerSlots from world_item_template where class=1 and subclass=0 "
        "and InventoryType=18 and ContainerSlots>0 and BagFamily=0 order by entry")}
    return {"schema": 1, "junk": sorted(set(junk)), "junk_prices": prices, "supplies": supplies,
            "vendors": vendors, "bags": bags}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path,
                        default=ROOT / "data/knowledge/tbc-243.sqlite")
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "content/tbc/vendor-catalog.json")
    parser.add_argument("--lua", type=pathlib.Path,
                        default=ROOT / "addons/JevRadio/Supplies.lua")
    args = parser.parse_args()
    profiles = json.loads((ROOT / "content/tbc/combat-profiles.json").read_text())
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
        catalog = generate(db, profiles)
    args.out.write_text(json.dumps(catalog, separators=(",", ":"), sort_keys=True) + "\n")
    lines = ["-- Generated by tools/gen_vendor_catalog.py; server starting item identities.",
             "return {"]
    for key, roles in sorted(catalog["supplies"].items()):
        fields = ", ".join(f'{role} = {value["item_id"]}'
                           for role, value in sorted(roles.items()))
        lines.append(f'    ["{key}"] = {{ {fields} }},')
    args.lua.write_text("\n".join([*lines, "}", ""]))
    print(f"{len(catalog['junk'])} safe junk IDs, {len(catalog['vendors'])} vendor spawns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
