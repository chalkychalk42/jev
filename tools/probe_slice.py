#!/usr/bin/env python3
"""The vertical slice, driven by the playhead rather than by flags.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_slice.py --steps 1

The character's parking spot is irrelevant, and so is which quest is next: the step comes
from the graph and the quest log. Naming the NPC on the command line was scaffolding for
the first accept, and it hid the fact that nothing was reading the chain.

Three pieces, none of which know about each other:

    the graph     which step        content/tbc/ally_human_1_12.json
    the mesh      how to stand there jev.guide.path + Travel.follow
    the locator   where to click     jev.perceive.units.find

and one skill per node kind, both of which are `Interact` followed by `AdvanceQuestFrame`
with a different goal. Accepting and turning in differ by `Goal.HELD` vs `Goal.CLEARED`
and by nothing else.

Where the playhead is still naive
---------------------------------
It walks the chain in order and steps past a node whose postcondition holds — a quest in
the log for accept, out of it for turn-in. That is right while the chain is being walked
forwards for the first time and wrong after a restart, because a quest turned in last
session is indistinguishable from one never accepted: both are simply absent. Fixing it
needs completed-quest state on the strip, which is a field, not a workaround. Until then
a stale start re-offers a finished quest and fails honestly at the NPC, which is the
failure mode worth having.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.advance import AdvanceQuestFrame, Goal  # noqa: E402
from jev.clients.capture import Backend, WindowCapture  # noqa: E402
from jev.clients.choose import ChooseListLine  # noqa: E402
from jev.clients.hid import Hid, Humaniser  # noqa: E402
from jev.clients.interact import GOSSIP_YARDS, Interact, Result  # noqa: E402
from jev.clients.travel import Travel  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.guide.tracker import Tracker  # noqa: E402
from jev.perceive import radio_frame  # noqa: E402
from jev.perceive.questlog import QuestLog  # noqa: E402
from jev.world.state_v1 import StepKind  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", type=int, default=1,
                    help="how many graph nodes to attempt; one at a time by default")
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

    def read_reading():
        """The whole reading, not just its values: list lines are a property of the
        frame, not of the character, so they never entered `state_v1`."""
        for _ in range(6):
            r = radio_frame.read(cap.grab().rgb)
            if r.ok:
                log.observe(r.values)
                return r
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

    def state():
        """A `State` carrying the **assembled** log, which is what the tracker needs.

        `to_state` will not call one frame a log and is right not to, so the accumulated
        one is handed in. Without this the tracker sees an empty log on every tick and
        every accept step looks unfinished.
        """
        for _ in range(6):
            r = radio_frame.read(cap.grab().rgb)
            if r.ok:
                log.observe(r.values)
                return radio_frame.to_state(r, t=time.time(), client_id="slice",
                                            quests=log.complete)
            time.sleep(0.05)
        return None

    v = read()
    if v is None:
        print("cannot read the strip")
        return 1
    zones = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))
    bounds = zones[v["pos.zone_id"]]
    graph = Graph.load(str(ROOT / "content" / "tbc" / "ally_human_1_12.json"))

    print("--- 1. what does the log say ---")
    ids = quest_ids()
    print(f"  quests: {ids}   (() = read and empty, None = never read)")
    if ids is None:
        print("  the log never completed a cycle; stopping rather than guessing")
        return 1

    launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
    query = MmapQuery(args.jevpath, args.mmaps, launcher=launcher)
    def playhead():
        """Which step, according to the graph and the log. The tracker owns the rule."""
        st = state()
        return None if st is None else graph.get(Tracker.resume(graph, st).step_id)

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
              f"{result.stuck_events} stuck"
              + (f" — {result.detail}" if result.detail else ""))
        return result.outcome.value == "arrived"

    travel = Travel(hid=hid, bounds=bounds, read_pos=read_pos,
                    arrival_yards=GOSSIP_YARDS)
    inter = Interact(hid=hid, bounds=bounds, read=read, read_frame=read_frame,
                     read_pos=read_pos, window_centre=(ox + w // 2, oy + h // 2),
                     window_origin=(ox, oy), approach=approach)
    advance = AdvanceQuestFrame(hid=hid, read=read, quest_ids=lambda: quest_ids(tries=1),
                                window_origin=(ox, oy), window_size=(w, h))
    chooser = ChooseListLine(hid=hid, read=read_reading,
                             window_origin=(ox, oy), window_size=(w, h))

    # The only difference between accepting and turning in.
    GOALS = {StepKind.QUEST_ACCEPT: Goal.HELD, StepKind.QUEST_TURNIN: Goal.CLEARED}

    rc = 0
    for step in range(args.steps):
        node = playhead()
        if node is None:
            print("\ncannot read the client; stopping")
            rc = 1
            break

        print(f"\n--- step {step + 1}: {node.id} ---")
        print(f"  {node.kind.value} quest {node.quest_id} at {node.notes} {node.pos}")

        goal = GOALS.get(node.kind)
        if goal is None:
            print(f"  {node.kind.value} is not built yet; stopping rather than "
                  f"pretending the step is done")
            rc = 1
            break
        if node.world is None or node.npc_id is None:
            print(f"  no spawn for this node ({node.notes}); the graph cannot place it")
            rc = 1
            break

        if not win32.is_foreground(hwnd):
            win32.focus(hwnd)
            time.sleep(0.5)

        result = inter.open_on(node.notes, node_world=node.world, node_map=node.pos)
        if inter.sighting is not None:
            sg = inter.sighting
            print(f"  saw it: ring ({sg.ring.cx:.0f},{sg.ring.cy:.0f}) torso {sg.torso}")
        print(f"  {result.value}" + (f" — {inter.detail}" if inter.detail else ""))
        if not result.opened:
            rc = 1
            break

        # A list, not a button. Pick our own quest out of it by name; the NPC may have
        # several, and they are identical to a camera.
        if result is Result.GOSSIP:
            chose = chooser.run(node.title)
            print(f"  chose {node.title!r}: {chose.value} at {chooser.clicked}"
                  + (f" — {chooser.detail}" if chooser.detail else ""))
            if not chose.ok:
                rc = 1
                break

        log.reset()
        outcome = advance.run(node.quest_id, goal)
        print(f"  {goal.value}: pressed {advance.clicked} -> {outcome.value}"
              + (f" — {advance.detail}" if advance.detail else ""))
        if not outcome.ok:
            rc = 1
            break
        print(f"  log now: {quest_ids()}")

    query.close()
    cap.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
