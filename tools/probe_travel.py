#!/usr/bin/env python3
"""Walk to a node and stop.

    python tools/probe_travel.py --to-npc 197          # Marshal McBride
    python tools/probe_travel.py --to 0.4817 0.4294

The travel half of the 1-12 slice. Reports what it did rather than asserting, because the
useful output of a first walk is a description: how far, how many turns, what the turn
rate converged to, and whether anything got stuck.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jev.clients import win32
from jev.clients.capture import Backend, WindowCapture
from jev.clients.hid import Hid, Humaniser
from jev.clients.travel import Travel
from jev.guide.coords import bounds_by_radio_id
from jev.guide.graph import Graph
from jev.perceive import radio_frame

# Resolved against the repo, not the cwd: these tools are run from wherever the Windows
# side happens to be, and a relative path then silently points at nothing.
ROOT = pathlib.Path(__file__).resolve().parent.parent
ZONES = str(ROOT / "data" / "zones-tbc-243.json")
GRAPH = str(ROOT / "content" / "tbc" / "ally_human_1_12.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", nargs=2, type=float, metavar=("MX", "MY"))
    ap.add_argument("--to-npc", type=int, help="walk to the first node with this npc_id")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--arrival", type=float, default=5.0)
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2
    hwnds = win32.find_windows("World of Warcraft")
    if not hwnds:
        print("no game window")
        return 1
    hwnd = hwnds[0]
    win32.focus(hwnd)
    time.sleep(0.4)
    if not win32.is_foreground(hwnd):
        print("could not raise the window; click it and re-run")
        return 1

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("travel"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)

    def read_pos():
        try:
            r = radio_frame.read(cap.grab().rgb)
        except Exception:
            return None
        if not r.ok or r.values.get("pos.mx") is None:
            return None
        return (r.values["pos.mx"], r.values["pos.my"])

    def read_zone():
        try:
            r = radio_frame.read(cap.grab().rgb)
        except Exception:
            return None
        return r.values.get("pos.zone_id") if r.ok else None

    zone = read_zone()
    if zone is None:
        print("cannot read the strip; is the addon loaded and on screen?")
        return 1
    try:
        bounds = bounds_by_radio_id(ZONES)[zone]
    except KeyError:
        print(f"zone id {zone} is not in {ZONES}")
        return 1

    label = "target"
    if args.to_npc is not None:
        graph = Graph.load(GRAPH)
        node = next((n for n in graph.nodes if n.npc_id == args.to_npc and n.pos), None)
        if node is None:
            print(f"no positioned node with npc_id {args.to_npc}")
            return 1
        target, label = node.pos, f"{node.notes or node.id}"
    elif args.to:
        target = (args.to[0], args.to[1])
    else:
        print("give --to MX MY or --to-npc ID")
        return 1

    travel = Travel(hid=hid, bounds=bounds, read_pos=read_pos,
                    arrival_yards=args.arrival)
    here = travel.position()
    if here is None:
        print("cannot read a position even with retries")
        return 1
    print(f"zone {bounds.area_id}, at {here}, walking to {target} ({label})")
    print(f"  {travel.distance(here, target):.1f} yards, bearing "
          f"{math.degrees(travel.bearing(here, target) or 0):.1f} deg")

    try:
        result = travel.to(target, timeout_s=args.timeout)
    finally:
        hid.release_all()
        cap.close()

    print()
    print(f"outcome        {result.outcome.value}")
    print(f"elapsed        {result.elapsed_s:.1f}s")
    print(f"remaining      {result.remaining_yards:.1f} yards"
          if result.remaining_yards is not None else "remaining      unknown")
    print(f"turns          {result.turns}")
    print(f"stuck events   {result.stuck_events}"
          + (f"  (last freed by: {travel.last_unstick})" if travel.last_unstick else ""))
    print(f"detours        {result.detours}")
    if travel.closest_yards is not None:
        print(f"closest        {travel.closest_yards:.1f} yards")
    print(f"turn rate      {result.turn_rate_deg_s:.1f} deg/s "
          f"(seeded at 134.0, corrected from observation)")
    if result.detail:
        print(f"detail         {result.detail}")
    return 0 if result.outcome.value == "arrived" else 1


if __name__ == "__main__":
    raise SystemExit(main())
