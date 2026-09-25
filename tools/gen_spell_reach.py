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


def _float(bits: int) -> float:
    """A DBC float column, stored as its 32 bits."""
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


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
    for spell_id, range_index, cast_index, attributes_ex in db.execute(
            f"SELECT Id, RangeIndex, CastingTimeIndex, AttributesEx FROM world_spell_template "
            f"WHERE Id IN ({','.join('?' * len(ids))})", ids):
        low, high = ranges.get(range_index, (0.0, 0.0))
        out[str(spell_id)] = {"min_yd": round(low, 1), "max_yd": round(high, 1),
                              "cast_s": round(max(0, casts.get(cast_index, 0)) / 1000, 2)}
        if (attributes_ex or 0) & CHANNELED:
            out[str(spell_id)]["channel"] = True
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
