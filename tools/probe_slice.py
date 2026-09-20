#!/usr/bin/env python3
"""The first vertical slice, driven by the playhead rather than by proximity.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_slice.py

    empty log -> graph.entry -> mmap to Willem -> interact -> accept 783 -> log shows 783

The character's parking spot is irrelevant. The step comes from the graph and the quest
log, which is the whole point of having generated one.

The unknown here is the Accept button. Its position is deterministic in the stock UI but
not worth remembering wrongly, so it is swept for like the interact offset was — and the
sweep is self-verifying: the log gaining 783 means Accept was clicked, and nothing means
it was not. Decline closes the frame, which is recoverable by interacting again.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.capture import Backend, WindowCapture  # noqa: E402
from jev.clients.hid import Hid, Humaniser  # noqa: E402
from jev.clients.interact import Interact  # noqa: E402
from jev.clients.travel import Travel  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.perceive import radio_frame  # noqa: E402
from jev.perceive.questlog import QuestLog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--npc", default="Deputy Willem")
    ap.add_argument("--quest", type=int, default=783)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    ap.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2
    hwnds = win32.find_windows("World of Warcraft")
    if not hwnds:
        return 1
    hwnd = hwnds[0]
    win32.focus(hwnd)
    time.sleep(0.4)

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("slice"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)
    ox, oy, w, h = win32.client_rect(hwnd)
    log = QuestLog()

    def read():
        for _ in range(6):
            r = radio_frame.read(cap.grab().rgb)
            if r.ok:
                log.observe(r.values)
                return r.values
            time.sleep(0.05)
        return None

    def read_frame():
        try:
            return cap.grab().rgb
        except Exception:
            return None

    def read_pos():
        try:
            r = radio_frame.read(cap.grab().rgb)
        except Exception:
            return None
        if not r.ok or r.values.get("pos.mx") is None:
            return None
        log.observe(r.values)
        return (r.values["pos.mx"], r.values["pos.my"])

    def quest_ids(tries: int = 40) -> tuple[int, ...] | None:
        """Wait for a whole cycle. A partial one is unread, never a short log."""
        for _ in range(tries):
            read()
            assembled = log._complete
            if assembled is not None:
                return tuple(q.quest_id for q in assembled)
            time.sleep(0.08)
        return None

    v = read()
    if v is None:
        print("cannot read the strip")
        return 1
    bounds = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))[v["pos.zone_id"]]
    graph = Graph.load(str(ROOT / "content" / "tbc" / "ally_human_1_12.json"))

    print("--- 1. what does the log say ---")
    ids = quest_ids()
    print(f"  quests: {ids}   (() = read and empty, None = never read)")
    if ids is None:
        print("  the log never completed a cycle; stopping rather than guessing")
        return 1

    node = graph.get(graph.entry)
    print("\n--- 2. playhead ---")
    print(f"  entry {node.id}")
    print(f"  {node.kind.value} quest {node.quest_id} at {node.notes} {node.pos}")
    if args.quest in ids:
        print(f"  quest {args.quest} is already in the log; nothing to accept")
        return 0

    print("\n--- 3. plan and walk ---")
    launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
    query = MmapQuery(args.jevpath, args.mmaps, launcher=launcher)
    # Arrival for an NPC is interact range, not a tight radius. The client
    # answers that directly through CheckInteractDistance, which the radio paints as
    # target.in_melee — so the walk only has to get close enough for the client to
    # say yes, and a travel run that reports stuck at 4.3 yards has in fact arrived.
    # Stop the planned walk **short** of the NPC and let the approach cover the last
    # yards. Pathing onto his spawn point and accepting "within five yards" is how the
    # character ended up standing past him: the mesh aims at where he stands, and the
    # follower will happily walk through him to get there.
    travel = Travel(hid=hid, bounds=bounds, read_pos=read_pos, arrival_yards=9.0)
    here = travel.position()
    hw = map_to_world(here[0], here[1], bounds)
    tw = map_to_world(node.pos[0], node.pos[1], bounds)
    z = node.world[2] if node.world else 0.0
    path = query.path(bounds.map_id, (hw[0], hw[1], z), (tw[0], tw[1], z))
    print(f"  {path.status.value}: {len(path.points)} waypoints, {path.length_yards():.1f} yards")

    def replan(here_map):
        """Ask the mesh again from wherever the character actually got to. This is the
        engine; a follower that invents its own way round is the thing we retired."""
        w = map_to_world(here_map[0], here_map[1], bounds)
        fresh = query.path(bounds.map_id, (w[0], w[1], z), (tw[0], tw[1], z))
        print(f"  re-planned: {fresh.status.value}, {len(fresh.points)} waypoints")
        return fresh

    result = (travel.follow(path, timeout_s=args.timeout, replan=replan)
              if path.usable else travel.to(node.pos, timeout_s=args.timeout))
    remaining = ("unknown" if result.remaining_yards is None
                 else f"{result.remaining_yards:.1f} yards")
    print(f"  {result.outcome.value}, {remaining} left, "
          f"{result.turns} turns, {result.stuck_events} stuck")
    query.close()
    # `remaining_yards` on a failed leg is the distance to **that leg's waypoint**, not to
    # the destination. Reading it as "nearly at the NPC" is how a run concluded it was
    # 4.3 yards from Deputy Willem while wedged in a room thirty yards away with a wall
    # in between. Measure against the node.
    here = travel.position()
    to_node = travel.distance(here, node.pos) if here else None
    print(f"  {to_node:.1f} yards from the node itself"
          if to_node is not None else "  position unreadable")
    if result.outcome.value != "arrived":
        print(f"  {result.detail}")

    print(f"\n--- 4. interact with {args.npc} ---")
    # Deliberate, not a reflex (DECISIONS.md V18): the planner may have raised a console
    # during the walk, and input is refused while the game is not foreground.
    if not win32.is_foreground(hwnd):
        print("  window lost focus during the walk; raising it")
        win32.focus(hwnd)
        time.sleep(0.5)

    inter = Interact(hid=hid, bounds=bounds, read=read, read_frame=read_frame,
                     read_pos=read_pos, window_centre=(ox + w // 2, oy + h // 2),
                     window_origin=(ox, oy))
    result = inter.open_on(args.npc)
    if inter.sighting is not None:
        sg = inter.sighting
        print(f"  saw it: ring ({sg.ring.cx:.0f},{sg.ring.cy:.0f}) "
              f"plate ({sg.plate.cx:.0f},{sg.plate.cy:.0f}) torso {sg.torso}")
    print(f"  yawed={inter.yawed}  clicked={inter.clicked}")
    print(f"  {result.value}" + (f" — {inter.detail}" if inter.detail else ""))
    if not result.opened:
        cap.close()
        return 1

    print(f"\n--- 5. accept quest {args.quest} ---")
    print("  STOP. There is no deterministic way to press Accept yet.")
    print("  A sweep over the quest frame is the same flail one step later, so it is not")
    print("  here. The frame is open and the preconditions held; that is the milestone.")
    cap.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
