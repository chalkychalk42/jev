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
        pos=Pos(zone=node.zone, zone_id=node.zone_id, coord_zone_id=node.coord_zone_id,
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
        quests = ((Quest(quest_id=qid, title="q", complete=have >= need,
                         objectives=(Objective(text="kill", have=have, need=need,
                                               counter_index=0),)),)
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


def test_an_unresolved_tick_reaches_the_corpus_not_just_the_counter(tmp_path):
    """The in-process counter is this client's dashboard; the corpus is what the eval
    board and every later analysis read. An unresolved tick that exists only in memory
    is a headline metric that reports zero for a run full of them."""
    from jev.learn.episode import read

    rt = _runtime([State(t=float(i), client_id="c01") for i in range(6)], tmp_path)
    rt.run(ticks=6, period_s=0)

    assert rt.counters.unresolved > 0
    decisions = [d for d in read(rt.recorder.dir / "decisions.jsonl")
                 if d["intent"] == "escalate"]
    assert len(decisions) == rt.counters.unresolved
    assert all(d["intent"] == "escalate" for d in decisions)
    assert all(d["situation_key"] for d in decisions)


def test_a_teacher_answer_is_linked_to_the_escalation_that_asked_for_it(tmp_path):
    """`escalated_from` lets the board count questions rather than reconstruct them from
    timestamps and buckets, which over-counts the moment one client escalates the same
    bucket twice on a tick."""
    from jev.coach.schema import Decision, Intent
    from jev.learn.episode import read

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="teacher says grind")

    # Answer only after the client has actually asked about this situation, which is the
    # order real operation has: escalate, then hear back some ticks later.
    ready: list[bool] = [False]
    rt = _runtime([State(t=float(i), client_id="c01") for i in range(6)], tmp_path,
                  ask=lambda state, key: ready.__setitem__(0, True),
                  take=lambda key: answer if ready[0] else None)
    rt.run(ticks=6, period_s=0)

    rows = read(rt.recorder.dir / "decisions.jsonl")
    applied = [r for r in rows if r["author"] == "teacher"]
    assert applied, "no teacher answer was recorded"
    assert any(r["escalated_from"] for r in applied), "no answer linked to its question"


def test_an_unsolicited_answer_does_not_invent_a_link(tmp_path):
    """An answer for a bucket this client never asked about — a cache hit, or another
    client's question — has no escalation of its own, and saying so beats claiming one."""
    from jev.coach.schema import Decision, Intent
    from jev.learn.episode import read

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="from another client")
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i)) for i in range(3)], tmp_path, take=lambda key: answer)
    rt.run(ticks=3, period_s=0)

    applied = [r for r in read(rt.recorder.dir / "decisions.jsonl")
               if r["author"] == "teacher"]
    assert applied
    assert all(r["escalated_from"] is None for r in applied)


def test_a_healthy_run_writes_few_decision_rows(tmp_path):
    """One row per unresolved tick is bounded by construction — and when it is not
    bounded, that is the signal rather than the cost."""
    from jev.clients.sim import Pretend
    from jev.learn.episode import read

    graph = Graph.load(GRAPH)
    pretend = Pretend(graph, seed=5, trouble=0.08)
    rt = ClientRuntime(client_id="sim", graph=graph, source=pretend,
                       recorder=Recorder(root=tmp_path))
    for _ in range(400):
        rt.tick()
        pretend.follow(rt.tracker.step_id)

    decisions = read(rt.recorder.dir / "decisions.jsonl")
    ticks = read(rt.recorder.dir / "ticks.jsonl")
    escalations = [d for d in decisions if d["intent"] == "escalate"]
    assert len(escalations) / len(ticks) < 0.2, "escalating on a fifth of ticks is the signal"
    assert any(d["intent"] != "escalate" for d in decisions), "ordinary choices need labels too"


def test_a_skill_armed_mechanically_still_gets_an_outcome(tmp_path):
    """Skills the tracker or a System 1 preempt armed write no decision row, so joining
    decisions to grades left them permanently ungraded — and PLAN §10's retirement rule,
    "success rate below 0.4 over 20", had no rate to compute."""
    from jev.clients.sim import Pretend
    from jev.learn.episode import read

    graph = Graph.load(GRAPH)
    pretend = Pretend(graph, seed=5, trouble=0.12)
    rt = ClientRuntime(client_id="sim", graph=graph, source=pretend,
                       recorder=Recorder(root=tmp_path))
    for _ in range(300):
        rt.tick()
        pretend.follow(rt.tracker.step_id)

    rows = read(rt.recorder.dir / "skills.jsonl")
    assert rows, "no skill outcome was ever recorded"
    assert len(rows) == rt.counters.skills_closed
    assert {r["skill"] for r in rows} - set(), "skills should be named"
    assert any(r["outcome"] == "succeeded" for r in rows)
    assert all(r["duration_s"] >= 0 for r in rows)


def test_an_interrupted_skill_is_not_a_failed_one(tmp_path):
    """Counting a preemption as failure retires exactly the skills that run in dangerous
    places — the ones most worth keeping."""
    from jev.learn.episode import SkillOutcome, SkillResultRow

    row = SkillResultRow(
        run_id="r", client_id="c", t=1.0, tick_id=1, skill="TRAVEL_TO",
        armed_by=ArmedBy.POLICY, outcome=SkillOutcome.PREEMPTED,
        duration_s=3.0, situation_key="k",
    )
    assert not row.counts_toward_rate
    assert not row.succeeded

    ok = SkillResultRow(
        run_id="r", client_id="c", t=1.0, tick_id=1, skill="LOOT",
        armed_by=ArmedBy.S1_PREEMPT, outcome=SkillOutcome.SUCCEEDED,
        duration_s=1.0, situation_key="k",
    )
    assert ok.counts_toward_rate and ok.succeeded


def test_a_run_that_ended_mid_skill_is_not_evidence(tmp_path):
    """Absence of an outcome is not a bad outcome — the same rule grading already uses."""
    from jev.learn.episode import SkillOutcome, SkillResultRow

    row = SkillResultRow(
        run_id="r", client_id="c", t=1.0, tick_id=1, skill="GRIND_UNTIL",
        armed_by=ArmedBy.POLICY, outcome=SkillOutcome.UNKNOWN,
        duration_s=9.0, situation_key="k",
    )
    assert not row.counts_toward_rate


class FakeQueue:
    """A teacher queue with latency, answering only under the key it was asked about.

    Both halves matter and both were got wrong by a simpler fake. Answering any key hides
    the fact that `situation_key` re-bins step age at sixty seconds, so the key moves
    underneath a round trip. Answering instantly hides staleness entirely, which is the
    common case at a measured ~52s.
    """

    def __init__(self, answer, latency_s: float) -> None:
        self.answer = answer
        self.latency_s = latency_s
        self.asked: dict[str, float] = {}
        self.delivered: set[str] = set()
        self.now = 0.0

    def ask(self, state, key: str) -> None:
        self.now = state.t
        self.asked.setdefault(key, state.t)

    def take(self, key: str):
        if key in self.delivered or key not in self.asked:
            return None
        if self.now - self.asked[key] < self.latency_s:
            return None
        self.delivered.add(key)
        return self.answer


def test_a_late_answer_keeps_its_artifacts_and_loses_its_action(tmp_path):
    """The measured teacher round trip is ~52s against a 60s situation bin, so a late
    answer is the common case rather than an edge one.

    Discarding the whole reply to avoid acting on a stale instruction would throw away
    the durable half — the combat profile, the on_fail edge — which is about the step and
    does not go stale at all (DECISIONS.md V11).
    """
    from jev.coach.schema import Decision, Intent
    from jev.learn.episode import read

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="grind a while")
    q = FakeQueue(answer, latency_s=90.0)

    states = [State(t=float(i) * 20.0, client_id="c01") for i in range(14)]
    rt = _runtime(states, tmp_path, ask=q.ask, take=q.take)
    for state in states:
        q.now = state.t
        rt.tick()

    assert rt.counters.teacher_stale > 0, "nothing aged past the bound"
    rows = [r for r in read(rt.recorder.dir / "decisions.jsonl") if r["author"] == "teacher"]
    assert rows, "a stale answer must still be recorded, not dropped"
    assert any(r["status"] == "rejected" for r in rows)
    assert any("artifacts kept" in (r["why"] or "") for r in rows)


def test_an_answer_inside_the_window_still_needs_current_perception(tmp_path):
    from jev.coach.schema import Decision, Intent

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="grind a while")
    q = FakeQueue(answer, latency_s=2.0)

    states = [State(t=float(i), client_id="c01") for i in range(8)]
    rt = _runtime(states, tmp_path, ask=q.ask, take=q.take)
    for state in states:
        q.now = state.t
        rt.tick()

    assert rt.counters.teacher_applied == 0
    replies = [r for r in read(rt.recorder.dir / "decisions.jsonl") if r["author"] == "teacher"]
    assert replies and all(r["verifier_verdict"] == "sense.blind" for r in replies)
    assert rt.counters.teacher_stale == 0


def test_an_answer_is_found_even_though_the_bucket_moved(tmp_path):
    """`situation_key` bins step age, so a step crossing from "fresh" into "slow" changes
    its own key mid-flight. A lookup on only the *current* key would miss every slow
    answer, re-ask, and never once hit the cache the key exists to make possible."""
    from jev.coach.schema import Decision, Intent

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="grind a while")
    q = FakeQueue(answer, latency_s=50.0)

    states = [State(t=float(i) * 20.0, client_id="c01") for i in range(12)]
    rt = _runtime(states, tmp_path, ask=q.ask, take=q.take)
    keys = []
    for state in states:
        q.now = state.t
        keys.append(rt.tick().situation_key)

    assert len(set(keys)) > 1, "the key should re-bin as the step ages"
    assert q.delivered, "no answer was ever collected"
    replies = [r for r in read(rt.recorder.dir / "decisions.jsonl") if r["author"] == "teacher"]
    assert len(replies) == len(q.delivered)


def test_an_unsolicited_answer_is_never_stale(tmp_path):
    """A cached or shared answer has no age of its own. Inventing one would silently drop
    every answer the farm reuses, which is the saving `situation_key` exists for."""
    from jev.coach.schema import Decision, Intent

    answer = Decision(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                      abort_if=["dead"], confidence=0.9, why="from another client")
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    rt = _runtime([_at(node, float(i) * 500.0) for i in range(4)], tmp_path,
                  take=lambda key: answer)
    rt.run(ticks=4, period_s=0)

    assert rt.counters.teacher_stale == 0
    assert rt.counters.teacher_applied == 4
