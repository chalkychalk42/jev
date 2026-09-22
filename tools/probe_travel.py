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
from jev.guide.coords import bounds_by_radio_id, map_to_world, world_to_map
from jev.guide.graph import Graph
from jev.guide.path import MmapQuery
from jev.perceive import radio_frame

# Resolved against the repo, not the cwd: these tools are run from wherever the Windows
# side happens to be, and a relative path then silently points at nothing.
ROOT = pathlib.Path(__file__).resolve().parent.parent
ZONES = str(ROOT / "data" / "zones-tbc-243.json")
GRAPH = str(ROOT / "content" / "tbc" / "ally_human_1_12.json")


def world_to_map_safe(pt, bounds):
    return world_to_map(pt[0], pt[1], bounds)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", nargs=2, type=float, metavar=("MX", "MY"))
    ap.add_argument("--to-npc", type=int, help="walk to the first node with this npc_id")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--arrival", type=float, default=5.0)
    ap.add_argument("--straight", action="store_true",
                    help="skip the planner and walk at the point (the old behaviour)")
    ap.add_argument("--wsl-distro", default="Ubuntu-24.04",
                    help="run the navmesh sidecar over here")
    ap.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    ap.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath",
                    help="path to the sidecar AS SEEN BY THE MACHINE THAT RUNS IT; on "
                         "Windows the repo is a UNC share and wsl.exe needs the Linux path")
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2
    try:
        hwnd = win32.game_window()
    except win32.GameWindowError as exc:
        print(exc)
        return 1
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

    query = None
    path = None
    if not args.straight:
        # The sidecar, its tiles and the compiler all live on the Linux side, so it is
        # reached through wsl.exe rather than cross-compiled. Planning happens once per
        # leg, so the boundary costs nothing that matters.
        launcher = () if win32.IS_WINDOWS is False else ("wsl.exe", "-d", args.wsl_distro, "-e")
        query = MmapQuery(args.jevpath, args.mmaps, launcher=launcher)
        here_world = map_to_world(here[0], here[1], bounds)
        target_world = map_to_world(target[0], target[1], bounds)
        z = 0.0
        if args.to_npc is not None and node.world:
            z = node.world[2]
        print("planning...")
        path = query.path(bounds.map_id, (here_world[0], here_world[1], z),
                          (target_world[0], target_world[1], z))
        print(f"  {path.status.value} via {path.source}: {len(path.points)} waypoints, "
              f"{path.length_yards():.1f} yards"
              + (f" - {path.detail}" if path.detail else ""))
        for pt in path.points:
            frac = world_to_map_safe(pt, bounds)
            print(f"    ({pt[0]:9.1f},{pt[1]:9.1f},{pt[2]:7.1f})"
                  + (f"  map ({frac[0]:.4f},{frac[1]:.4f})" if frac else ""))

    try:
        if path is not None and path.usable:
            def replan(here_map):
                """Ask the mesh again from wherever the character actually got to."""
                w = map_to_world(here_map[0], here_map[1], bounds)
                fresh = query.path(bounds.map_id, (w[0], w[1], z),
                                   (target_world[0], target_world[1], z))
                print(f"  re-planned: {fresh.status.value}, {len(fresh.points)} waypoints")
                return fresh

            result = travel.follow(path, timeout_s=args.timeout, replan=replan)
        else:
            if path is not None:
                print("  no usable route; walking straight at it instead")
            result = travel.to(target, timeout_s=args.timeout)
    finally:
        hid.release_all()
        cap.close()
        if query is not None:
            query.close()

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
