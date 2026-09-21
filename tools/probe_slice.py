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
It walks the chain in order and steps past a node whose postcondition holds - a quest in
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
from jev.clients.choose import ChooseListLine  # noqa: E402
from jev.clients.fight import Fight  # noqa: E402
from jev.clients.interact import GOSSIP_YARDS, Interact, Result  # noqa: E402
from jev.clients.loot import Loot  # noqa: E402
from jev.clients.recover import Recover, Recovered  # noqa: E402
from jev.clients.rest import Rest  # noqa: E402
from jev.guide import playhead  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.guide.tracker import Tracker  # noqa: E402
from jev.learn.episode import Recorder, SkillOutcome  # noqa: E402
from jev.perceive.radio_frame import list_lines, name_id  # noqa: E402
from jev.run.client import NotRunning, attach, with_travel  # noqa: E402
from jev.run.hunt import DEFAULT_HUNT_YARDS, Hunt  # noqa: E402
from jev.run.journal import Journal, outcome_of  # noqa: E402
from jev.world.state_v1 import StepKind  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", type=int, default=0,
                    help="debug only: stop after N nodes. 0 keeps playing")
    ap.add_argument("--run-for", type=float, default=3600.0,
                    help="seconds to keep playing before stopping")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts at the same node before calling it stuck")
    ap.add_argument("--runs-dir", default="runs",
                    help="where the flight recorder writes")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--hunt", type=float, default=600.0,
                    help="seconds to work an objective before reporting where it got to")
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
        that a quest was turned in - a finished quest and an untaken one are both simply
        absent - and without it the run after a hand-in walks back to the giver.
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

    # The flight recorder. A run can be repeated; the corpus of a run cannot be recovered
    # afterwards, so recording is not optional and not conditional.
    journal = Journal(Recorder(root=args.runs_dir), client_id="slice")
    print(f"  recording to {journal.recorder.dir}")

    # The only difference between accepting and turning in.
    GOALS = {StepKind.QUEST_ACCEPT: Goal.HELD, StepKind.QUEST_TURNIN: Goal.CLEARED}

    fight = Fight(hid=client.hid, read=client.read, read_frame=client.frame,
                  window_origin=client.origin,
                  window_centre_x=client.size[0] // 2)
    rest = Rest(hid=client.hid, read=client.read)
    loot = Loot(hid=client.hid, read=client.read, read_frame=client.frame,
                window_origin=client.origin)

    def walk_to(map_point) -> bool:
        """For the corpse run. The planner needs a height and a map fraction has none, so
        the nearest placed node's world z is used - the guide knows the terrain."""
        wx, wy = map_to_world(map_point[0], map_point[1], bounds)
        placed = [n for n in graph.nodes if n.world is not None and n.map_id == bounds.map_id]
        z = min(placed, key=lambda n: (n.world[0] - wx) ** 2 + (n.world[1] - wy) ** 2
                ).world[2] if placed else 0.0
        return client.approach((wx, wy, z), timeout_s=args.timeout)

    recover = Recover(hid=client.hid, read=client.read, window_origin=client.origin,
                      window_size=client.size, walk_to=walk_to)

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

    def _is_a_list(client) -> bool:
        """A frame with lines and no button to press is a list, whatever opened it."""
        reading = client.reading()
        if reading is None or not reading.values:
            return False
        painted = reading.values.get("ui.advance_x")
        return painted is None and bool(list_lines(reading))

    def do_objective(node) -> int:
        """Work the objective's **disk** until the server's counter says it is done.

        The node carries a point and a radius, and the radius is the part that matters: a
        spawn pin is one kobold out of a camp full of them, and standing on it is how a
        run reaches 1/10 and then reports "not visible" twenty times while the rest of the
        camp wanders about fifteen yards away.
        """
        wanted = name_id(node.notes) if node.notes else None
        print(f"  objective: {node.objectives[0]}")
        # `node.hunt_yards` only. Never `r` - see `DEFAULT_HUNT_YARDS`.
        radius = node.hunt_yards or DEFAULT_HUNT_YARDS
        print(f"  disk: {radius:.0f} yards around {node.pos}"
              + ("" if node.hunt_yards else "  (node carries none; using the default)"))
        hunt = Hunt(fight=fight, rest=rest, read=client.read,
                    approach=lambda world: client.approach(world, timeout_s=args.timeout),
                    progress=lambda: progress(node.quest_id),
                    loot=loot, journal=journal, observe=client.state,
                    step_id=node.id)
        outcome = hunt.run(node.world, radius, wanted, timeout_s=args.hunt)
        have, need = progress(node.quest_id)
        print(f"  {outcome.value}: {have}/{need} after {hunt.kills} kills over "
              f"{hunt.moves} stations"
              + (f" - {hunt.detail}" if hunt.detail else ""))
        return 0 if outcome.ok else 1

    memory = playhead.load(graph.graph_id)
    print(f"  completed so far: {sorted(memory.completed) or 'nothing remembered'}")

    # The playhead loop. Not `--steps 1` with a human between steps: that is why every
    # recovery tonight needed one. Resume, arm, record, next, until the clock runs out or
    # the same node fails often enough to mean something.
    rc = 0
    step = 0
    last_id: str | None = None
    last_node = None
    repeats = 0
    deadline = time.monotonic() + args.run_for

    def failed(node, why: str) -> bool:
        """Record a failed step. True when the same node has failed too often to retry."""
        nonlocal repeats, last_id
        journal.skill(node.kind.value.upper() if node else "STEP",
                      SkillOutcome.ABORTED, started_at=started, state=state,
                      step_id=node.id if node else None, detail=why)
        repeats = repeats + 1 if node is not None and node.id == last_id else 1
        last_id = node.id if node is not None else None
        if repeats >= args.retries:
            print(f"  {why} - {repeats} times on the same node; stopping")
            return True
        print(f"  {why} - trying again ({repeats}/{args.retries})")
        return False

    while time.monotonic() < deadline and (args.steps == 0 or step < args.steps):
        step += 1
        started = time.monotonic()
        state = client.state()
        journal.tick(state)

        # Dead is not a step problem, and every skill reports it as one. Recovery is a
        # skill that already exists, so the loop uses it rather than stopping and waiting
        # for a person - which is the whole difference between a probe and a runtime.
        if state is not None and (state.vitals.dead is True or state.vitals.ghost is True):
            print("\n--- dead; recovering ---")
            died_at = time.monotonic()
            # A ghost has already released and the body's position is gone for good.
            # But the loop knows which node it was working when it died, and that is
            # where the body is - the same guess a person makes, from data the runtime
            # already has. Without it an unattended loop stops at the first death.
            where = last_node or next_step()      # already a ghost before the first step
            corpse = (state.pos.mx, state.pos.my) if state.vitals.dead else (
                where.pos if where is not None and where.pos else None)
            if corpse is not None and not state.vitals.dead:
                print(f"  auto-released; guessing the corpse is at {where.id}")
            # More than one pass, because one is rarely enough: a corpse run wedges on
            # the way and the planner replans from wherever it stopped. Every recovery
            # done by hand tonight took two to four passes for exactly this reason.
            for attempt in range(args.retries):
                got_up = recover.run(corpse)
                print(f"  recover {attempt + 1}: {got_up.value}"
                      + (f" - {recover.detail}" if recover.detail else ""))
                if got_up.ok or got_up is Recovered.NO_CORPSE:
                    break
            journal.skill("RECOVER", outcome_of(got_up.ok), started_at=died_at,
                          state=state, detail=f"{got_up.value}: {recover.detail}"
                          if recover.detail else got_up.value)
            if not got_up.ok:
                print("  could not get back up; stopping")
                rc = 1
                break
            continue

        node = next_step()
        if node is None:
            print("\ncannot read the client; stopping")
            rc = 1
            break

        last_node = node
        print(f"\n--- step {step}: {node.id} ---")
        print(f"  {node.kind.value} quest {node.quest_id} at {node.notes} {node.pos}")
        journal.tick(state, skill=node.kind.value.upper(), intent=node.title or node.id)

        if node.world is None:
            print(f"  no spawn for this node ({node.notes}); the graph cannot place it")
            rc = 1
            break

        client.focused()

        if node.kind is StepKind.QUEST_OBJECTIVE:
            if do_objective(node) == 0:
                repeats, last_id = 0, node.id
                continue
            if failed(node, "the objective did not finish"):
                rc = 1
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
        print(f"  {result.value}" + (f" - {inter.detail}" if inter.detail else ""))
        journal.skill("INTERACT", outcome_of(result.opened), started_at=started,
                      state=state, step_id=node.id,
                      detail=f"{result.value}: {inter.detail}" if inter.detail
                      else result.value)
        if not result.opened:
            if failed(node, f"could not open {node.notes}"):
                rc = 1
                break
            continue

        # A list, not a button. Pick our own quest out of it by name; the NPC may have
        # several, and they are identical to a camera.
        #
        # Gossip is not the only list. An NPC with two or more quests and nothing else to
        # say opens the **quest greeting panel** instead - `ui.quest_frame` true,
        # `ui.gossip` false, no Accept button painted, and the same `ui.list_*` lines
        # underneath. Deputy Willem with two quests reported
        # `no_button - frame open but no advance button painted` while line 1 was
        # `Eagan Peltskinner` all along.
        if result is Result.GOSSIP or _is_a_list(client):
            chose_at = time.monotonic()
            chose = chooser.run(node.title)
            print(f"  chose {node.title!r}: {chose.value} at {chooser.clicked}"
                  + (f" - {chooser.detail}" if chooser.detail else ""))
            journal.skill("CHOOSE_LINE", outcome_of(chose.ok), started_at=chose_at,
                          state=state, step_id=node.id,
                          detail=f"{chose.value}: {chooser.detail}" if chooser.detail
                          else chose.value)
            if not chose.ok:
                if failed(node, f"could not pick {node.title!r} out of the list"):
                    rc = 1
                    break
                continue

        client.log.reset()
        advanced_at = time.monotonic()
        outcome = advance.run(node.quest_id, goal)
        print(f"  {goal.value}: pressed {advance.clicked} -> {outcome.value}"
              + (f" - {advance.detail}" if advance.detail else ""))
        journal.skill(f"ADVANCE_{goal.value.upper()}", outcome_of(outcome.ok),
                      started_at=advanced_at, state=state, step_id=node.id,
                      detail=f"{outcome.value}: {advance.detail}" if advance.detail
                      else outcome.value)
        if not outcome.ok:
            if failed(node, f"could not {goal.value} quest {node.quest_id}"):
                rc = 1
                break
            continue

        if node.kind is StepKind.QUEST_TURNIN and node.quest_id is not None:
            # The one fact 2.4.3 will not give back later. Written the moment it is
            # witnessed, because the log forgets a quest the instant it is handed in.
            memory = playhead.with_completed(memory, node.quest_id)
            playhead.save(graph.graph_id, node.id, memory.completed)
        print(f"  log now: {client.quest_ids()}")
        repeats, last_id = 0, node.id

    print(f"\nrecorded: {journal.summary()}")
    journal.close()
    client.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
