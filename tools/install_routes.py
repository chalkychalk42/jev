"""Install the hive's race and class routes as live guides (V412), and say what each needs.

    python tools/install_routes.py --check            the readiness table, nothing copied
    python tools/install_routes.py                    copy them into data/routes, then the table
    python tools/install_routes.py --from DIR --to DIR

The routes are JevHive's (`hive.routes`, built by `hive.convert` from the Guidelime routes
players wrote, on this server's world database): `<race>_<class>.json` and its spawns. They
are their authors' and are never committed: `data/` is not tracked. The live runner plays one
with `--route <race>_<class>`, or `--route auto` for the logged-in character's race and class
(`jev.run.cli`). A route a check finds a problem in is not copied.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev.guide import live_routes  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", type=Path, default=live_routes.HIVE_ROUTES)
    parser.add_argument("--to", dest="target", type=Path, default=live_routes.ROUTES)
    parser.add_argument("--check", action="store_true", help="the table only; nothing copied")
    parser.add_argument("--world-db", type=Path, default=ROOT / "data/knowledge/tbc-243.sqlite")
    args = parser.parse_args(argv)
    failed = 0
    print("| route | loads | supported steps (of) | quests left out | frame | maps | needs |")
    print("|---|---|---|---|---|---|---|")
    for name in live_routes.NAMES:
        path = args.source / f"{name}.json"
        if not path.is_file():
            print(f"| {name} | missing | | | | | |")
            failed += 1
            continue
        r = live_routes.readiness(name, path, world_db=args.world_db)
        needs = "; ".join(f"{k}: {v} steps" for k, v in sorted(r.needs.items())) or "-"
        print(f"| {name} | {'yes' if r.ok else 'NO: ' + '; '.join(r.problems[:2])} | "
              f"{r.supported_steps} ({r.steps}) | {r.excluded_quests} | {r.frame} | "
              f"{','.join(map(str, r.maps))} | {needs} |")
        if not r.ok:
            failed += 1
            continue
        if not args.check:
            args.target.mkdir(parents=True, exist_ok=True)
            for suffix in (".json", ".spawns.json"):
                found = args.source / f"{name}{suffix}"
                if found.is_file():
                    shutil.copy2(found, args.target / found.name)
    if not args.check:
        print(f"installed into {args.target}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
