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
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.advance import AdvanceQuestFrame, Goal  # noqa: E402
from jev.clients.camera import Camera  # noqa: E402
from jev.clients.choose import ChooseListLine  # noqa: E402
from jev.clients.fight import Fight  # noqa: E402
from jev.clients.interact import GOSSIP_YARDS, Interact, Result  # noqa: E402
from jev.clients.loot import Loot  # noqa: E402
from jev.clients.recover import Recover, Recovered  # noqa: E402
from jev.clients.repair import Repair, Repaired  # noqa: E402
from jev.clients.rest import Rest  # noqa: E402
from jev.guide import playhead  # noqa: E402
from jev.guide.coords import bounds_by_radio_id, map_to_world  # noqa: E402
from jev.guide.graph import Graph  # noqa: E402
from jev.guide.path import MmapQuery  # noqa: E402
from jev.guide.tracker import Tracker  # noqa: E402
from jev.learn.episode import Recorder, SkillOutcome  # noqa: E402
from jev.perceive.radio_frame import list_lines, name_id  # noqa: E402
from jev.run.client import NotRunning, attach, with_travel  # noqa: E402
from jev.run.heartbeat import Heartbeat  # noqa: E402
from jev.run.hunt import DEFAULT_HUNT_YARDS, Hunt  # noqa: E402
from jev.run.journal import Journal, outcome_of  # noqa: E402
from jev.world.combat import HEAL_OUT_OF_COMBAT  # noqa: E402
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
    # 2 Hz on its own thread, per ARCH SS4. The loop only ever ticked where it decided
    # something, and a wedge is the stretch where it decides nothing - so the corpus had
    # every decision the bot has made and almost nothing about the thing it is worst at.
    heartbeat = Heartbeat(journal=journal, observe=client.state,
                          keys_down=client.hid.keys_down)

    # The only difference between accepting and turning in.
    GOALS = {StepKind.QUEST_ACCEPT: Goal.HELD, StepKind.QUEST_TURNIN: Goal.CLEARED}

    fight = Fight(hid=client.hid, read=client.read, read_frame=client.frame,
                  window_origin=client.origin,
                  window_centre_x=client.size[0] // 2)
    rest = Rest(hid=client.hid, read=client.read)
    loot = Loot(hid=client.hid, read=client.read, read_frame=client.frame,
                window_origin=client.origin)
    repair = Repair(hid=client.hid, read=client.read, visit=lambda: _visit_repairer(),
                    window_origin=(ox, oy), window_size=(w, h))
    camera = Camera(hid=client.hid, window_origin=(ox, oy), window_size=(w, h))

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
    def _nearest_repairer():
        """The closest merchant in the graph who repairs. The guide lists the places and
        the runtime picks: one repairer per zone was a lottery that sent a character in
        Northshire Abbey to Goldshire, 556 yards away, past three that would have done."""
        here = client.position()
        placed = [n for n in graph.nodes
                  if n.kind is StepKind.REPAIR and n.world is not None]
        if here is None or not placed:
            return None
        return min(placed, key=lambda n: math.dist(n.world[:2], here[:2]))

    def _visit_repairer() -> bool:
        node = _nearest_repairer()
        if node is None:
            print("  no repair merchant in the graph; nothing to walk to")
            return False
        # `notes` is "<name>; route not recorded" on a service node, and the name is the
        # half `Interact` matches a nameplate against.
        name = node.notes.split(";")[0].strip()
        print(f"  nearest repairer: {name} ({node.zone})")
        result = inter.open_on(name, node_world=node.world, node_map=node.pos)
        print(f"  {result.value}" + (f" - {inter.detail}" if inter.detail else ""))
        return result.opened

    def _settle_combat(state) -> bool:
        """Fight what is on us. Returns False if the pass was spent doing it.

        First of all the between-step rules, and absolute. A character that resurrects at
        its corpse resurrects in the middle of whatever killed it, and turning your back
        on melee is free hits taken and none dealt. Ordering this after the repair check
        exempted the walk to the merchant from the rule, and the walk to the merchant is
        the one that crosses the camp that did the killing: it died 80 yards along and
        reported `could not free the character`, which was true, because by then it was a
        corpse.
        """
        if state is None or state.vitals.combat is not True:
            return True
        print("\n--- in combat; nothing travels with something on it ---")
        outcome = fight.run(None)
        print(f"  {outcome.value} pressed {fight.pressed} closed {fight.closed}"
              + (f" - {fight.detail}" if fight.detail else ""))
        return False

    def _fit_to_travel(state) -> bool:
        """Heal and eat before starting a step. Returns False if the pass was spent on it.

        Not a nicety. A corpse run gets the character up **where it died**, at half
        health, next to whatever killed it - and the next thing the loop does is start a
        two-hundred-yard walk. The wolves that killed it killed it again on the way, and
        the run reported `could not free the character` because it was being eaten while
        it walked. `Hunt` already refuses to pull below the line; travelling below it is
        the same bet with the same odds, so it is the same rule.
        """
        if state is None:
            return True
        hp = state.vitals.hp
        if hp is None or float(hp) >= HEAL_OUT_OF_COMBAT:
            return True
        heartbeat.arm("REST", "top up before travelling")
        print(f"\n--- {float(hp):.0%} health; not starting a step on that ---")
        if fight.top_up():
            print(f"  topped up: {fight.top_ups_landed}/{fight.top_ups} landed")
            return True
        outcome = rest.until(0.9)
        print(f"  rest: {outcome.value}" + (f" - {rest.detail}" if rest.detail else ""))
        return False

    last_node = None
    # What the purse held the last time a repair could not be paid for.
    #
    # A `too_poor` does not get better by walking back, so the gate is money rather than a
    # try count: after one, nothing goes to a merchant again until there is more money
    # than there was when it failed. Between those, the loop fights with what it has -
    # at zero durability a death costs no further durability, so refusing to fight
    # protects nothing and only builds the deadlock it was meant to avoid.
    #
    # `money_copper` is quantised to 100 (the strip paints silver), so a rise has to be
    # at least a silver to be seen. That is the right order of magnitude for a repair.
    broke_at: int | None = None
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

    heartbeat.start()
    try:
        while time.monotonic() < deadline and (args.steps == 0 or step < args.steps):
            step += 1
            started = time.monotonic()
            state = client.state()
            journal.tick(state, keys=client.hid.keys_down())

            # Dead is not a step problem, and every skill reports it as one. Recovery is a
            # skill that already exists, so the loop uses it rather than stopping and waiting
            # for a person - which is the whole difference between a probe and a runtime.
            if state is not None and (state.vitals.dead is True or state.vitals.ghost is True):
                print("\n--- dead; recovering ---")
                heartbeat.arm("RECOVER", "get back up")
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
                # The guess is only a fallback now. `Recover` reads the body's real position
                # off the strip and will ignore this the moment the game offers one - which
                # matters, because the last death was **240 yards** from the node the run was
                # working, and thirteen passes at the node found nothing to get up from.
                for attempt in range(args.retries):
                    got_up = recover.run(corpse)
                    went = recover.corpse
                    where_txt = "nowhere" if went is None else f"({went[0]:.4f}, {went[1]:.4f})"
                    print(f"  recover {attempt + 1} at {where_txt}: {got_up.value}"
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

            # Before anything that looks. A camera pitched at the ground makes every skill
            # report the truth about an empty screen - `not_visible`, "no ring and nameplate
            # to click" - while the character stands 4.6 yards from a targeted merchant whose
            # nameplate is on screen. A second a pass, and it cannot make a good camera worse.
            camera.level()

            if not _settle_combat(state):
                continue          # something is hitting us; that is the whole pass

            purse = state.bags.money_copper if state is not None else None
            richer = broke_at is None or (purse is not None and purse > broke_at)
            if repair.needed() and richer:
                # Ahead of health, because a broken weapon loses the fight the health was
                # being saved for. This is the other half of dying: every death costs 10%
                # durability, and a character that never repairs eventually punches wolves.
                worst = repair.before
                heartbeat.arm("REPAIR", "worn gear")
                print("\n--- gear is worn; repairing before anything else ---")
                started = time.monotonic()
                outcome = repair.run()
                print(f"  repair: {outcome.value}"
                      + (f" - {repair.detail}" if repair.detail else "")
                      + (f" ({worst:.0%} -> {repair.after:.0%})"
                         if repair.after is not None and worst is not None else ""))
                journal.skill("REPAIR", outcome_of(outcome.ok), started_at=started,
                              state=state, detail=f"{outcome.value}: {repair.detail}"
                              if repair.detail else outcome.value)
                if outcome is Repaired.TOO_POOR:
                    broke_at = purse if purse is not None else 0
                    print(f"  not going back to a merchant until there is more than "
                          f"{broke_at} copper")
                continue

            if not _fit_to_travel(state):
                continue          # spent the pass getting well; re-read before deciding

            node = next_step()
            if node is None:
                print("\ncannot read the client; stopping")
                rc = 1
                break

            last_node = node
            heartbeat.arm(node.kind.value.upper(), node.title or node.id)
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

    finally:
        heartbeat.stop()

    print(f"\nrecorded: {journal.summary()}"
          + (f" ({heartbeat.ticks} from the heartbeat)" if heartbeat.ticks else ""))
    journal.close()
    client.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
