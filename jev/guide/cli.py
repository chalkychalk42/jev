"""Generate a GuideGraph and report what actually came out.

    python -m jev.guide.cli human --out content/tbc/ally_human_1_12.json
    python -m jev.guide.cli human --min 12 --max 20 --out content/tbc/ally_human_12_20.json

Zone names are supplied here rather than read from the database, because the DBC mirror
keeps `AreaTable` as numeric columns only — the name strings live in the client's string
block and were not carried across. Naming them here is the honest version of that: the
list is short, it is visible, and it is wrong in a way you can see.
"""

from __future__ import annotations

import argparse
import pathlib

from jev.guide import spawns
from jev.guide.generate import generate
from jev.guide.graph import stats

DB = "data/knowledge/tbc-243.sqlite"

# Starting spines. Each is (faction, {zone_id: name}) for one race's 1-12.
SPINES: dict[str, tuple[str, dict[int, str]]] = {
    "human":   ("alliance", {9: "Northshire", 12: "Elwynn"}),
    "dwarf":   ("alliance", {132: "ColdridgeValley", 1: "DunMorogh"}),
    "nightelf": ("alliance", {188: "Shadowglen", 141: "Teldrassil"}),
    "draenei": ("alliance", {3526: "AmmenVale", 3524: "AzuremystIsle"}),
    "orc":     ("horde", {14: "Durotar"}),
    "tauren":  ("horde", {215: "Mulgore"}),
    "undead":  ("horde", {85: "Tirisfal"}),
    "bloodelf": ("horde", {3430: "EversongWoods"}),
}


# Where a race goes after its starting spine, by level band: (race, min, max) -> zones.
# The first zone is the guide's map frame; the client carries positions in the others
# across to it through world coordinates (`jev.run.client.Client._navigation_values`).
BANDS: dict[tuple[str, int, int], dict[int, str]] = {
    ("human", 12, 20): {40: "Westfall", 44: "Redridge"},
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("race", choices=sorted(SPINES))
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=None)
    ap.add_argument("--min", type=int, default=1)
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--max-quests", type=int, default=None)
    args = ap.parse_args()

    faction, zones = SPINES[args.race]
    zones = BANDS.get((args.race, args.min, args.max), zones)
    graph_id = f"{faction[:4]}_{args.race}_{args.min}_{args.max}"

    table: dict[str, list] = {}
    g = generate(
        args.db, graph_id=graph_id, faction=faction,
        zone_ids=tuple(zones), zone_names=zones,
        level_min=args.min, level_max=args.max, max_quests=args.max_quests,
        spawns=table,
    )
    s = stats(g)

    print(f"{s.graph_id}: {s.nodes} nodes, {s.quests} quests, levels {s.levels[0]}-{s.levels[1]}")
    for kind, n in s.by_kind.items():
        print(f"  {kind:18} {n}")
    print(f"  positioned         {s.with_position}/{s.nodes}")
    if s.unreachable:
        # Ribs are reached only on failure, so some unreachable nodes are expected.
        # A large number is not, and it means the spine did not wire.
        print(f"  unreachable        {s.unreachable}  (ribs and services are expected here)")

    missing = [n.id for n in g.nodes if n.pos is None]
    if missing:
        print(f"\n  {len(missing)} nodes have no map position; the recorder pass supplies these")
        for nid in missing[:5]:
            print(f"    {nid}")

    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        g.save(path)
        print(f"\nwrote {path}")
        print(f"wrote {spawns.save(path, table)} ({len(table)} hunts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
