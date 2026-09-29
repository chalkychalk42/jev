"""Count a danger map afresh from the finished runs (`jev.learn.danger.rebuild`, V326).

    python tools/rebuild_danger.py --out /tmp/danger.json /home/ash/JevHive/var/runs

Before V326 the hive counted others' runs while they were being played, and never again; its
map is rebuilt once from its runs. Every process that holds the map saves it whole, so the
farm is stopped while the rebuilt file replaces the old one. Writes `--out` only.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev.guide.coords import bounds_by_radio_id  # noqa: E402
from jev.learn.danger import DangerMap, rebuild, runs_in  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", type=Path, nargs="+", help="directories of runs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--zones", type=Path, default=ROOT / "data" / "zones-tbc-243.json")
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f"{args.out} exists: write a new file, then move it into place")
    zones = bounds_by_radio_id(str(args.zones))
    by_area = {zone.area_id: zone for zone in zones.values()}
    runs = [run for directory in args.runs for run in runs_in(directory, DangerMap())]
    started = time.monotonic()
    danger = rebuild(runs, by_area.get, args.out)
    print(f"{args.out}: {len(danger.cells)} cells from {len(runs)} runs, settled through "
          f"{danger.through or '-'}, {len(danger.counted)} named, "
          f"{time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
