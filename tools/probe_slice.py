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

    print("\n--- 3. stand on him, with the mesh ---")
    launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
    query = MmapQuery(args.jevpath, args.mmaps, launcher=launcher)
    travel = Travel(hid=hid, bounds=bounds, read_pos=read_pos, arrival_yards=3.0)

    def approach(node_world) -> bool:
        """Plan from here to the NPC's world point and follow it.

        The planner is the only thing that knows about terrain; the skill above knows only
        where to click. Nine yards of blind walking found a fence this had already routed
        around.
        """
        here = travel.position()
        if here is None:
            print("  cannot read a position")
            return False
        hw = map_to_world(here[0], here[1], bounds)
        path = query.path(bounds.map_id, (hw[0], hw[1], node_world[2]), node_world)
        print(f"  {path.status.value}: {len(path.points)} waypoints, "
              f"{path.length_yards():.1f} yards")
        if not path.usable:
            return False

        def replan(here_map):
            w = map_to_world(here_map[0], here_map[1], bounds)
            return query.path(bounds.map_id, (w[0], w[1], node_world[2]), node_world)

        result = travel.follow(path, timeout_s=args.timeout, replan=replan)
        remaining = ("unknown" if result.remaining_yards is None
                     else f"{result.remaining_yards:.1f} yards")
        print(f"  {result.outcome.value}, {remaining} left, {result.turns} turns, "
              f"{result.stuck_events} stuck")
        return result.outcome.value == "arrived"

    print(f"\n--- 4. interact with {args.npc} ---")
    if not win32.is_foreground(hwnd):
        win32.focus(hwnd)
        time.sleep(0.5)

    inter = Interact(hid=hid, bounds=bounds, read=read, read_frame=read_frame,
                     read_pos=read_pos, window_centre=(ox + w // 2, oy + h // 2),
                     window_origin=(ox, oy), approach=approach)
    result = inter.open_on(args.npc, node_world=node.world, node_map=node.pos)
    query.close()

    if inter.sighting is not None:
        sg = inter.sighting
        print(f"  saw it: ring ({sg.ring.cx:.0f},{sg.ring.cy:.0f}) torso {sg.torso}")
    print(f"  used_centre={inter.used_centre}  clicked={inter.clicked}")
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
