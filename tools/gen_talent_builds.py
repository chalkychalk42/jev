#!/usr/bin/env python3
"""Generate the talent builds: which talent each point goes to, in order, per class (V261).

A character gains a talent point a level from 10, and the bot spent none: the paladin reached
15.87 with six unspent, and the mage reaches 10 with its first. A build is named by talent and
points (`BUILDS`, passive talents only - nothing here adds a spell the bot does not press),
and resolved against the client's own talent tables (`dbc_TalentTab` for the tree and its
place among the class's three tabs, `dbc_Talent` for each talent's tier, column and ranks,
their first rank's spell for its name). A build that asks for a tier before its tree holds
the five points a tier the server requires is refused here, not at the trainer.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Class ids as the game numbers them, and the builds: (talent name, points) in the order the
# points go. A caster that fights in reach keeps its casts (Burning Soul) and reaches further
# (Flame Throwing); a paladin that seals, judges and swings spends less mana on it
# (Benediction), judges more often and parries.
BUILDS = {
    8: (("Improved Fireball", 5), ("Ignite", 5), ("Burning Soul", 2), ("Flame Throwing", 2),
        ("Improved Fire Blast", 3), ("Impact", 5), ("Incineration", 2),
        ("Master of Elements", 3), ("Critical Mass", 3)),
    2: (("Benediction", 5), ("Improved Judgement", 2), ("Deflection", 3),
        ("Seal of Command", 1), ("Conviction", 5), ("Improved Blessing of Might", 5),
        ("Vindication", 3), ("Deflection", 5), ("Pursuit of Justice", 3)),
}
POINTS_PER_TIER = 5


def trees(db: sqlite3.Connection, class_id: int) -> dict[str, dict]:
    """Every talent of the class by name: its tab (1-3 as the frame orders them), tier,
    column and ranks."""
    mask = 1 << (class_id - 1)
    found: dict[str, dict] = {}
    for tab_id, row in ((r[0], r) for r in db.execute("select * from dbc_TalentTab")):
        class_mask, order = row[-3], row[-2]
        if class_mask != mask:
            continue
        for _id, tier, column, *ranks in db.execute(
                "select id, c2, c3, c4, c5, c6, c7, c8 from dbc_Talent where c1=? "
                "order by c2, c3", (tab_id,)):
            ranks = [r for r in ranks if r]
            if not ranks:
                continue
            name = db.execute("select SpellName from world_spell_template where Id=?",
                              (ranks[0],)).fetchone()
            if name and name[0]:
                found[name[0]] = {"tab": int(order) + 1, "tier": int(tier),
                                  "column": int(column), "ranks": len(ranks)}
    return found


def resolve(db: sqlite3.Connection, class_id: int, build) -> list[dict]:
    """The build point by point: each point's talent, where it stands, and the rank it
    reaches."""
    talents = trees(db, class_id)
    spent: dict[int, int] = {}
    rank: dict[str, int] = {}
    points = []
    for name, target in build:
        talent = talents.get(name)
        if talent is None:
            raise ValueError(f"class {class_id}: no talent named {name!r}")
        if target > talent["ranks"]:
            raise ValueError(f"{name} has {talent['ranks']} ranks, not {target}")
        while rank.get(name, 0) < target:
            tab, tier = talent["tab"], talent["tier"]
            if spent.get(tab, 0) < POINTS_PER_TIER * tier:
                raise ValueError(f"{name} (tier {tier}) with {spent.get(tab, 0)} points in "
                                 f"tab {tab}: the tier asks {POINTS_PER_TIER * tier}")
            rank[name] = rank.get(name, 0) + 1
            spent[tab] = spent.get(tab, 0) + 1
            points.append({"name": name, "tab": tab, "tier": tier, "column": talent["column"],
                           "rank": rank[name]})
    return points


def generate(db: sqlite3.Connection, builds=BUILDS) -> dict:
    return {"format": 1,
            "classes": {str(c): resolve(db, c, build) for c, build in sorted(builds.items())}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path,
                        default=ROOT / "data/knowledge/tbc-243.sqlite")
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "content/tbc/talent-builds.json")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
        builds = generate(db)
    args.out.write_text(json.dumps(builds, indent=1) + "\n")
    print(", ".join(f"class {c}: {len(points)} points" for c, points in builds["classes"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
