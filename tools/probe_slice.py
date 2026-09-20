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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.advance import AdvanceQuestFrame, Goal  # noqa: E402
from jev.clients.choose import ChooseListLine  # noqa: E402
from jev.clients.fight import Fight, Fought  # noqa: E402
from jev.clients.interact import GOSSIP_YARDS, Interact, Result  # noqa: E402
from jev.guide import playhead  # noqa: E402
from jev.guide.coords import bounds_by_radio_id  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.guide.tracker import Tracker  # noqa: E402
from jev.perceive.radio_frame import name_id  # noqa: E402
from jev.run.client import NotRunning, attach, with_travel  # noqa: E402
from jev.world.state_v1 import StepKind  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", type=int, default=1,
                    help="how many graph nodes to attempt; one at a time by default")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--kills", type=int, default=12,
                    help="attempts at an objective before reporting where it got to")
    ap.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    ap.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    args = ap.parse_args()

    try:
        client = attach("slice")
    except NotRunning as e:
        print(e)
        return 2
    if not client.focused():
        print("could not bring the client to the foreground")
        return 1

    v = client.read()
    if v is None:
        print("cannot read the strip")
        client.close()
        return 1
    bounds = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))[v["pos.zone_id"]]
    graph = Graph.load(str(ROOT / "content" / "tbc" / "ally_human_1_12.json"))

    print("--- 1. what does the log say ---")
    ids = client.quest_ids()
    print(f"  quests: {ids}   (() = read and empty, None = never read)")
    if ids is None:
        print("  the log never completed a cycle; stopping rather than guessing")
        client.close()
        return 1

    launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
    with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher),
                arrival_yards=GOSSIP_YARDS, say=print)

    def next_step():
        """Which step, according to the graph, the log, and where we got to last time.

        The remembered position is a **floor**, not an answer: the scan still runs from
        it, so anything since finished is skipped. It exists because the log cannot say
        that a quest was turned in — a finished quest and an untaken one are both simply
        absent — and without it the run after a hand-in walks back to the giver.
        """
        nonlocal memory
        st = client.state()
        if st is None:
            return None
        memory = playhead.load(graph.graph_id)
        start = memory.step_id if graph.get(memory.step_id or "") is not None else None
        tracker = Tracker.resume(graph, st, start=start, completed=memory.completed)
        playhead.save(graph.graph_id, tracker.step_id, memory.completed)
        return graph.get(tracker.step_id)

    ox, oy = client.origin
    w, h = client.size
    inter = Interact(hid=client.hid, bounds=bounds, read=client.read,
                     read_frame=client.frame, read_pos=client.position,
                     window_centre=(ox + w // 2, oy + h // 2), window_origin=(ox, oy),
                     approach=lambda world: client.approach(world, timeout_s=args.timeout))
    advance = AdvanceQuestFrame(hid=client.hid, read=client.read,
                                quest_ids=lambda: client.quest_ids(tries=1),
                                window_origin=(ox, oy), window_size=(w, h))
    chooser = ChooseListLine(hid=client.hid, read=client.reading,
                             window_origin=(ox, oy), window_size=(w, h))

    # The only difference between accepting and turning in.
    GOALS = {StepKind.QUEST_ACCEPT: Goal.HELD, StepKind.QUEST_TURNIN: Goal.CLEARED}

    fight = Fight(hid=client.hid, read=client.read, read_frame=client.frame,
                  window_origin=client.origin,
                  window_centre_x=client.size[0] // 2)

    def progress(quest_id):
        """Objective counts for one quest, from the **assembled** log.

        The server's own tally, not an inference from kills we think we landed.
        """
        done = client.log.complete
        if done is None:
            client.quest_ids()
            done = client.log.complete
        for q in done or ():
            if q.quest_id == quest_id and q.objectives:
                o = q.objectives[0]
                return (o.have, o.need)
        return (None, None)

    def do_objective(node) -> int:
        """Stand in the camp and kill the thing the step names, until the count is in."""
        wanted = name_id(node.notes) if node.notes else None
        print(f"  objective: {node.objectives[0]}")
        if not client.approach(node.world):
            print("  could not get to the camp")
            return 1

        for attempt in range(args.kills):
            have, need = progress(node.quest_id)
            print(f"  progress: {have}/{need}")
            if need is not None and have is not None and have >= need:
                print("  objective complete")
                return 0
            outcome = fight.run(wanted)
            print(f"    kill {attempt + 1}: {outcome.value} "
                  f"(pressed {fight.pressed}, closed {fight.closed}, "
                  f"last hp {fight.last_hp})"
                  + (f" — {fight.detail}" if fight.detail else ""))
            if outcome is Fought.DIED:
                return 1
            if outcome in (Fought.NO_TARGET, Fought.NOT_VISIBLE):
                # Nothing in reach. Standing still and Tabbing harder will not change that.
                print("  nothing attackable from here")
                return 1

        have, need = progress(node.quest_id)
        print(f"  progress: {have}/{need} after {args.kills} attempts")
        return 0 if (need is not None and have is not None and have >= need) else 1

    memory = playhead.load(graph.graph_id)
    print(f"  completed so far: {sorted(memory.completed) or 'nothing remembered'}")

    rc = 0
    for step in range(args.steps):
        node = next_step()
        if node is None:
            print("\ncannot read the client; stopping")
            rc = 1
            break

        print(f"\n--- step {step + 1}: {node.id} ---")
        print(f"  {node.kind.value} quest {node.quest_id} at {node.notes} {node.pos}")

        if node.world is None:
            print(f"  no spawn for this node ({node.notes}); the graph cannot place it")
            rc = 1
            break

        client.focused()

        if node.kind is StepKind.QUEST_OBJECTIVE:
            rc = do_objective(node)
            if rc:
                break
            continue

        goal = GOALS.get(node.kind)
        if goal is None:
            print(f"  {node.kind.value} is not built yet; stopping rather than "
                  f"pretending the step is done")
            rc = 1
            break
        if node.npc_id is None:
            print(f"  no NPC for this node ({node.notes})")
            rc = 1
            break

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

        client.log.reset()
        outcome = advance.run(node.quest_id, goal)
        print(f"  {goal.value}: pressed {advance.clicked} -> {outcome.value}"
              + (f" — {advance.detail}" if advance.detail else ""))
        if not outcome.ok:
            rc = 1
            break
        if node.kind is StepKind.QUEST_TURNIN and node.quest_id is not None:
            # The one fact 2.4.3 will not give back later. Written the moment it is
            # witnessed, because the log forgets a quest the instant it is handed in.
            memory = playhead.with_completed(memory, node.quest_id)
            playhead.save(graph.graph_id, node.id, memory.completed)
        print(f"  log now: {client.quest_ids()}")

    client.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
