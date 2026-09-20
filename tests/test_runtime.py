"""The whole brain, with no game, no capture and no Windows.

A bug that only appears after forty minutes of live play is unfixable if the only way to
reach it is forty minutes of live play. These run the real graph, the real tracker, the
real coach and the real recorder against synthesised states.
"""

from __future__ import annotations

from jev.clients.source import ScriptedSource, blind
from jev.guide.graph import Graph
from jev.guide.tracker import StepKind
from jev.learn.episode import Recorder, read
from jev.orch.runtime import ClientRuntime
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Objective,
    Pos,
    Quest,
    Sense,
    SenseFault,
    State,
    Ui,
    Vitals,
)

GRAPH = "content/tbc/ally_human_1_12.json"


def _at(node, t: float, **kw) -> State:
    """A healthy character standing on a node."""
    base = dict(
        pos=Pos(zone=node.zone, zone_id=node.zone_id,
                mx=node.pos[0] if node.pos else 0.5,
                my=node.pos[1] if node.pos else 0.5),
        vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
        bags=Bags(free=10, durability_min=1.0, money_copper=0),
        ui=Ui(loot=False, modal=False, gossip=False, vendor=False),
        sense=Sense(addon_ok=True, vision_conf=1.0),
    )
    return State(t=t, client_id="c01", **{**base, **kw})


def _runtime(states, tmp_path, **kw) -> ClientRuntime:
    graph = Graph.load(GRAPH)
    return ClientRuntime(
        client_id="c01", graph=graph, source=ScriptedSource(states),
        recorder=Recorder(root=tmp_path), **kw,
    )


def test_a_quest_is_accepted_worked_and_turned_in(tmp_path):
    """The vertical slice, driven entirely from the generated spine.

    This is the milestone in ARCHITECTURE.md §11, proven without the client: the playhead
    walks accept -> objective -> turn-in on quest ids the world DB supplied.
    """
    graph = Graph.load(GRAPH)
    by = graph.by_id()

    # Find a real accept/objective/turn-in triple in the generated graph.
    accept = next(n for n in graph.nodes
                  if n.kind is StepKind.QUEST_ACCEPT and n.next
                  and by[n.next[0]].kind is StepKind.QUEST_OBJECTIVE)
    do = by[accept.next[0]]
    turnin = by[do.next[0]]
    qid = accept.quest_id

    def with_quest(node, t, have, need=10, present=True):
        quests = ((Quest(quest_id=qid, title="q",
                         objectives=(Objective(text="kill", have=have, need=need),)),)
                  if present else ())
        return _at(node, t, quests=quests)

    states = [
        _at(accept, 0.0),                             # arrived, quest not yet taken
        with_quest(accept, 1.0, have=0),              # accepted
        with_quest(do, 2.0, have=4),                  # part way
        with_quest(do, 3.0, have=10),                 # objective complete
        with_quest(turnin, 4.0, have=10),             # at the turn-in, still in log
        _at(turnin, 5.0, quests=()),                  # handed in
    ]

    rt = _runtime(states, tmp_path)
    rt.tracker.enter(accept.id, states[0])
    rt._entered = True
    for _ in states:
        rt.tick()

    assert rt.counters.advances >= 3, "the playhead did not walk the chain"
    assert rt.tracker.step_id not in (accept.id, do.id), "still stuck on an earlier step"
    assert rt.counters.deaths == 0


def test_every_tick_is_recorded_with_an_author(tmp_path):
    """Recording only interesting ticks is exactly the sampling bias that ruins a
    training set."""
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i)) for i in range(8)], tmp_path)
    rt.run(ticks=8, period_s=0)

    rows = read(rt.recorder.dir / "ticks.jsonl")
    assert len(rows) == 8
    assert all(r["armed_by"] in {a.value for a in ArmedBy} for r in rows)
    assert all(r["situation_key"] for r in rows)


def test_a_shadow_prediction_is_written_on_every_tick_including_ones_it_did_not_drive(tmp_path):
    """Gate C's agreement number is only measurable if it was measured throughout."""
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i)) for i in range(4)], tmp_path,
                  shadow=lambda s: ("advance", "TRAVEL_TO", 0.42))
    rt.run(ticks=4, period_s=0)

    rows = read(rt.recorder.dir / "ticks.jsonl")
    assert all(r["shadow_intent"] == "advance" for r in rows)
    assert all(r["shadow_confidence"] == 0.42 for r in rows)


def test_the_loop_runs_with_no_teacher_at_all(tmp_path):
    """The invariant, at the level that matters: a full run with the teacher absent."""
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i)) for i in range(20)], tmp_path)
    assert rt.ask is None and rt.take is None

    c = rt.run(ticks=20, period_s=0)
    assert c.ticks == 20
    assert rt.armed is not None, "nothing was ever armed"
    assert c.escalated == 0


def test_escalation_never_blocks_the_tick(tmp_path):
    """The teacher is enqueued and abandoned; the tick continues on the scripted plan."""
    asked: list[str] = []
    rt = _runtime([State(t=float(i), client_id="c01") for i in range(6)], tmp_path,
                  ask=lambda state, key: asked.append(key))
    rt.run(ticks=6, period_s=0)

    assert asked, "a blind character should have wanted help"
    assert rt.armed is not None, "and should still have been given something to do"
    assert rt.armed.by is ArmedBy.POLICY


def test_a_teacher_answer_is_applied_when_one_happens_to_be_waiting(tmp_path):
    from jev.coach.schema import Decision, Intent

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="teacher says grind")
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i)) for i in range(3)], tmp_path,
                  take=lambda key: answer)
    rt.run(ticks=3, period_s=0)

    assert rt.counters.teacher_applied == 3
    assert rt.armed.by is ArmedBy.TEACHER


def test_an_unreadable_frame_does_not_stop_the_client(tmp_path):
    """'I could not see' is an observation the coach can act on. An exception is a client
    that stops."""
    states = [blind(float(i), "c01", SenseFault.NOT_FOUND) for i in range(5)]
    rt = _runtime(states, tmp_path)
    c = rt.run(ticks=5, period_s=0)

    assert c.ticks == 5 and c.blind_ticks == 5
    assert rt.armed is not None


def test_unresolved_counts_ticks_no_rule_could_settle(tmp_path):
    """The headline metric. A blind character is all guesswork; a healthy one on a known
    step is not."""
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)

    blind_rt = _runtime([blind(float(i), "c01", SenseFault.NOT_FOUND) for i in range(10)],
                        tmp_path / "a")
    blind_rt.run(ticks=10, period_s=0)

    seeing_rt = _runtime([_at(node, float(i)) for i in range(10)], tmp_path / "b")
    seeing_rt.run(ticks=10, period_s=0)

    assert blind_rt.counters.unresolved > seeing_rt.counters.unresolved


def test_a_long_corpse_run_is_one_death_not_a_hundred(tmp_path):
    """Counting per tick spent dead makes a corpse run read as a catastrophe."""
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    dead = [_at(node, float(i), vitals=Vitals(dead=True, ghost=False)) for i in range(30)]
    rt = _runtime(dead, tmp_path)
    rt.run(ticks=30, period_s=0)
    assert rt.counters.deaths == 1


def test_a_healthy_run_only_escalates_when_it_cannot_see(tmp_path):
    """`unresolved/h` is the headline metric and it has to mean something.

    In a simulated run with working senses the only ticks no rule can settle should be
    the ones where perception failed. This test exists because it caught a real design
    flaw: "in combat with nothing selected" was marked uncertain, which sent 42% of a run
    to the teacher to be told to pick a target. Acquiring a target is mechanical, and a
    rate-limited teacher cannot absorb that kind of waste.
    """
    from jev.clients.sim import Pretend

    graph = Graph.load(GRAPH)
    pretend = Pretend(graph, seed=1, trouble=0.08)
    rt = ClientRuntime(client_id="sim", graph=graph, source=pretend,
                       recorder=Recorder(root=tmp_path))
    for _ in range(400):
        rt.tick()
        pretend.follow(rt.tracker.step_id)

    c = rt.counters
    assert c.unresolved <= c.blind_ticks, (
        f"{c.unresolved} unresolved ticks against {c.blind_ticks} blind ones — "
        "something resolvable by rule is escalating"
    )
    assert c.unresolved / c.ticks < 0.15, "a perception problem wearing an intelligence costume"
    assert c.advances > 10, "the run has to actually progress for the ratio to mean anything"


def test_a_grind_rib_rejoins_the_spine(tmp_path):
    """A rib is a detour, not a destination.

    It is shared by every step in its zone, so the graph cannot name the way back and the
    tracker has to remember it. Without that the first simulated run spent four hundred
    ticks on a boar.

    The property is *leaving* a rib, not where the run happens to stop: ending on one is
    legitimate if the character only just failed into it.
    """
    from jev.clients.sim import Pretend

    graph = Graph.load(GRAPH)
    pretend = Pretend(graph, seed=3, trouble=0.25)   # trouble enough to fail into ribs
    rt = ClientRuntime(client_id="sim", graph=graph, source=pretend,
                       recorder=Recorder(root=tmp_path))

    def is_rib(step_id):
        node = graph.get(step_id)
        return node is not None and node.kind is StepKind.GRIND

    was_on_rib = False
    rejoined = 0
    for _ in range(900):
        rt.tick()
        pretend.follow(rt.tracker.step_id)
        now = is_rib(rt.tracker.step_id)
        if was_on_rib and not now:
            rejoined += 1
        was_on_rib = now

    assert rt.counters.fails > 0, "this seed should have failed into a rib"
    assert rejoined > 0, "entered a rib and never came back out"


def test_an_entry_fact_read_blind_is_backfilled_not_lost(tmp_path):
    """A step entered during a perception outage must not keep a degraded exit condition.

    A grind rib entered blind recorded no entry level, which silently changed its exit
    from "gain one level" to "reach the top of the band" — the rest of the game.
    """
    from jev.clients.source import ScriptedSource
    from jev.guide.tracker import Tracker
    from jev.world.state_v1 import Char, SenseFault

    graph = Graph.load(GRAPH)
    tracker = Tracker(graph, graph.entry)
    node = graph.get(graph.entry)

    tracker.enter(graph.entry, blind(0.0, "c", SenseFault.CHECKSUM))
    assert tracker.memory.level_at_entry is None

    seeing = _at(node, 1.0).model_copy(update={"char": Char(level=7, xp_pct=0.3)})
    tracker.tick(seeing)
    assert tracker.memory.level_at_entry == 7

    later = _at(node, 2.0).model_copy(update={"char": Char(level=9, xp_pct=0.1)})
    tracker.tick(later)
    assert tracker.memory.level_at_entry == 7, "a later reading is not the entry state"
    assert ScriptedSource is not None  # import used
