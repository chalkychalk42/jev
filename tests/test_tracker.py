"""Whatever the tracker can settle must never reach the coach."""

from __future__ import annotations

from jev.guide.graph import FailEdge, FailWhen, Graph, Node
from jev.guide.tracker import Event, Tracker
from jev.world.state_v1 import Objective, Quest, State, StepKind, Vitals


def _graph() -> Graph:
    base = dict(zone="Elwynn", zone_id=12, pos=(0.5, 0.5))
    return Graph(
        graph_id="g", faction="alliance", entry="accept",
        nodes=(
            Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=7,
                 next=("do",), timeout_s=60.0,
                 on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=60, goto="rib"),), **base),
            Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=7, next=("turnin",),
                 on_fail=(FailEdge(when=FailWhen.DEATHS, value=3, goto="rib"),), **base),
            Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=7, **base),
            Node(id="rib", kind=StepKind.GRIND, level=(1, 10), **base),
        ),
    )


def _s(t=0.0, **kw) -> State:
    """A healthy character whose quest log has been read and is empty.

    `quests=()` rather than the default `None` matters: `None` means nobody looked, and
    a fail edge must not fire on an unread log. Tests about quests being *absent* have to
    say the log was actually read.
    """
    from jev.world.state_v1 import Pos
    base = dict(pos=Pos(zone="Elwynn", zone_id=12, mx=0.5, my=0.5),
                vitals=Vitals(hp=1.0, dead=False, ghost=False, combat=False),
                quests=())
    return State(t=t, client_id="c", **{**base, **kw})


def _with_quest(t=0.0, have=0, need=10) -> State:
    return _s(t, quests=(Quest(quest_id=7, title="q",
                               objectives=(Objective(text="kill", have=have, need=need),)),))


def test_accept_completes_when_the_quest_reaches_the_log():
    tr = Tracker(_graph(), "accept")
    tr.enter("accept", _s())
    assert tr.tick(_s()).event is not Event.ADVANCE
    v = tr.tick(_with_quest())
    assert v.event is Event.ADVANCE and v.goto == "do"


def test_a_skippable_quest_still_earns_completion_when_its_predicate_succeeds():
    graph = _graph()
    nodes = tuple(n.model_copy(update={"skippable": True}) for n in graph.nodes)
    tracker = Tracker(graph.model_copy(update={"nodes": nodes}), "accept")
    tracker.enter("accept", _s())
    assert tracker.tick(_with_quest()).completed


def test_an_objective_completes_on_counts_not_on_arrival():
    tr = Tracker(_graph(), "do")
    tr.enter("do", _with_quest())
    assert tr.tick(_with_quest(have=9)).event is not Event.ADVANCE
    assert tr.tick(_with_quest(have=10)).event is Event.ADVANCE


def test_a_turnin_needs_to_remember_the_quest_was_ever_taken():
    """Without the memory this reads as complete before the quest was accepted, and the
    playhead runs off the end of the chain in the first second of a run."""
    fresh = Tracker(_graph(), "turnin")
    fresh.enter("turnin", _s())                      # never had the quest
    assert fresh.tick(_s()).event is not Event.ADVANCE

    held = Tracker(_graph(), "turnin")
    held.enter("turnin", _with_quest(have=10))       # had it on entry
    assert held.tick(_s()).event is Event.ADVANCE


def test_a_dead_character_never_advances():
    """Advancing over a corpse discards the step that did the killing, which is the one
    worth knowing about."""
    tr = Tracker(_graph(), "do")
    tr.enter("do", _with_quest())
    dead = _with_quest(have=10).model_copy(update={"vitals": Vitals(dead=True)})
    v = tr.tick(dead)
    assert v.event is Event.DEATH, "the predicate was satisfiable and must still not fire"


def test_a_timeout_takes_the_edge_it_was_given():
    tr = Tracker(_graph(), "accept")
    tr.enter("accept", _s(t=0.0))
    v = tr.tick(_s(t=120.0))
    assert v.event is Event.FAIL and v.goto == "rib"


def test_an_expired_step_fails_by_its_own_timeout_edge_on_the_next_tick():
    """The watchdog's first answer to a stalled run (run 20260923T191946-2b79ed): the
    step fails into the grind it was built to take, instead of the run stopping."""
    tr = Tracker(_graph(), "accept")
    tr.enter("accept", _s(t=0.0))
    assert tr.tick(_s(t=1.0)).event is not Event.FAIL
    assert tr.expire()
    v = tr.tick(_s(t=2.0))
    assert v.event is Event.FAIL and v.goto == "rib"


def test_deaths_on_a_step_eventually_route_around_it():
    tr = Tracker(_graph(), "do")
    tr.enter("do", _with_quest())
    tr.memory.deaths = 3
    v = tr.tick(_with_quest())
    assert v.event is Event.FAIL and v.goto == "rib"


def test_a_blind_character_is_not_a_lost_one():
    """Unknown position must not read as off-route, or it fires the moment a loading
    screen blanks the readout."""
    from jev.world.state_v1 import Pos

    tr = Tracker(_graph(), "do")
    tr.enter("do", _with_quest())
    blind = _with_quest().model_copy(update={"pos": Pos(zone="Elwynn", zone_id=12)})
    for t in range(0, 120, 10):
        v = tr.tick(blind.model_copy(update={"t": float(t)}))
        assert v.event is not Event.OFF_ROUTE


def test_wandering_away_after_arriving_is_off_route():
    from jev.world.state_v1 import Pos

    tr = Tracker(_graph(), "do")
    tr.enter("do", _with_quest())
    tr.tick(_with_quest())                                   # arrive
    far = Pos(zone="Elwynn", zone_id=12, mx=0.9, my=0.9)
    tr.tick(_with_quest(t=1.0).model_copy(update={"pos": far}))
    v = tr.tick(_with_quest(t=60.0).model_copy(update={"pos": far}))
    assert v.event is Event.OFF_ROUTE and v.off_route_s > 20


def test_quest_missing_does_not_fire_from_across_the_zone():
    """Firing before arrival would skip every quest in the graph before the character
    walked to any of them."""
    from jev.world.state_v1 import Pos

    g = _graph()
    node = g.get("do").model_copy(
        update={"on_fail": (FailEdge(when=FailWhen.QUEST_MISSING, goto="rib"),)})
    g2 = Graph(graph_id="g", faction="alliance", entry="accept",
               nodes=tuple(node if n.id == "do" else n for n in g.nodes))

    tr = Tracker(g2, "do")
    far = Pos(zone="Elwynn", zone_id=12, mx=0.9, my=0.9)
    away = _s().model_copy(update={"pos": far})
    tr.enter("do", away)
    assert tr.tick(away).event is not Event.FAIL

    tr.tick(_s())                                            # arrive, quest not in log
    assert tr.tick(_s()).event is Event.FAIL


def test_a_vendor_step_does_not_complete_on_unread_bags():
    """Unknown is not success: a vendor step that 'completes' because nothing read the
    bags leaves a full inventory and an unfixed weapon."""
    from jev.world.state_v1 import Bags

    g = Graph(graph_id="g", faction="alliance", entry="v",
              nodes=(Node(id="v", kind=StepKind.VENDOR, zone="Elwynn", zone_id=12,
                          pos=(0.5, 0.5)),))
    tr = Tracker(g, "v")
    tr.enter("v", _s())
    assert tr.tick(_s()).event is not Event.ADVANCE
    ok = _s(bags=Bags(free=10, durability_min=0.9))
    assert tr.tick(ok).event is Event.ADVANCE


def test_a_step_past_its_timeout_with_no_edge_is_the_coachs_problem():
    g = Graph(graph_id="g", faction="alliance", entry="x",
              nodes=(Node(id="x", kind=StepKind.TRAIN, zone="Elwynn", zone_id=12,
                          pos=(0.5, 0.5), timeout_s=10.0),))
    tr = Tracker(g, "x")
    tr.enter("x", _s())
    assert tr.is_blocked(_s(t=999.0)), "nothing mechanical can resolve this"


# -- cold start: where is this character in the guide? ------------------------------

def _graph_1_12():
    from jev.guide.graph import Graph
    return Graph.load("content/tbc/ally_human_1_12.json")


def _state_with(quests, t=0.0):
    """A readable state whose only interesting content is the quest log."""
    from jev.world.state_v1 import Char, Pos, State, Vitals
    return State(t=t, client_id="c", char=Char(level=1),
                 pos=Pos(zone_id=12080, mx=0.48, my=0.43),
                 vitals=Vitals(dead=False, ghost=False), quests=quests)


def test_resume_puts_a_fresh_character_on_the_first_step():
    g = _graph_1_12()
    t = Tracker.resume(g, _state_with(()))
    assert t.step_id == g.entry
    assert g.get(t.step_id).kind is StepKind.QUEST_ACCEPT


def test_resume_walks_past_an_accept_the_log_already_satisfies():
    """The real case after the first live run: 783 is held, so the next thing to do is
    take it to McBride — not stand in front of Willem asking for it again."""
    from jev.world.state_v1 import Quest

    g = _graph_1_12()
    t = Tracker.resume(g, _state_with((Quest(quest_id=783),)))
    node = g.get(t.step_id)
    assert node.kind is StepKind.QUEST_TURNIN and node.quest_id == 783


def test_resume_does_not_walk_past_a_turnin_just_because_the_log_is_empty():
    """A turn-in's predicate needs the quest to have been *seen* in the log. Without that
    memory an empty log satisfies every turn-in in the guide and the playhead runs off the
    end of the chain on the first tick."""
    g = _graph_1_12()
    t = Tracker.resume(g, _state_with(()))
    assert g.get(t.step_id).kind is not StepKind.QUEST_TURNIN


def test_resume_stops_rather_than_guessing_when_the_log_was_never_read():
    """`None` is unread. Treating it as empty is how a live step gets skipped."""
    g = _graph_1_12()
    t = Tracker.resume(g, _state_with(None))
    assert t.step_id == g.entry


def test_resume_can_start_from_a_remembered_step_instead_of_the_entry():
    """After a hand-in the log is empty again, and a cold scan cannot tell a finished
    quest from an untaken one — so it stops at the accept of a quest already done and
    walks the character back to an NPC with nothing to give. The remembered step is a
    floor: the scan still runs forward from it."""
    g = _graph_1_12()
    turnin = "alli_human_1_12_783_a_threat_within_turnin"
    t = Tracker.resume(g, _state_with(()), start=turnin)
    assert t.step_id != g.entry, "an empty log sent the playhead back to the first accept"
    assert t.step_id != turnin, "783 is gone from the log; that turn-in is done"
    assert g.get(t.step_id).quest_id != 783, "still somewhere in the finished quest"


def test_only_the_start_step_is_credited_not_everything_behind_it():
    """The seed says "we had got this far", not "assume it all worked". A turn-in the
    scan walks *into* is judged against the live log like any other step."""
    g = _graph_1_12()
    t = Tracker.resume(g, _state_with(()), start=g.entry)
    node = g.get(t.step_id)
    assert node.kind is StepKind.QUEST_ACCEPT and node.quest_id == 783, (
        "an empty log at the first accept must stop there, seed or no seed")


def test_a_remembered_step_is_a_floor_not_an_answer():
    """Anything finished since is still skipped, so the file being slightly stale costs
    nothing."""
    from jev.world.state_v1 import Quest

    g = _graph_1_12()
    held = _state_with((Quest(quest_id=783),))
    assert Tracker.resume(g, held, start=g.entry).step_id == \
        Tracker.resume(g, held).step_id


def test_a_missing_or_corrupt_memory_is_no_memory_not_a_crash():
    import pathlib

    from jev.guide import playhead

    p = pathlib.Path("/tmp/jev-playhead-test.json")
    p.unlink(missing_ok=True)
    assert playhead.load("alli_human_1_12", p).step_id is None
    playhead.save("alli_human_1_12", "step_a", {783}, p)
    assert playhead.load("alli_human_1_12", p).step_id == "step_a"
    p.write_text("{not json", encoding="utf-8")
    assert playhead.load("alli_human_1_12", p).completed == frozenset()
    p.unlink(missing_ok=True)


def test_completed_quests_outlive_a_graph_that_was_regenerated():
    """A step id names a node and can stop existing — dropping two unplaceable quests did
    exactly that, and the lost position sent the character back to Deputy Willem for a
    quest handed in twenty minutes earlier. Quest ids are facts about the character, so
    they survive a different guide."""
    import pathlib

    from jev.guide import playhead

    p = pathlib.Path("/tmp/jev-playhead-graph.json")
    playhead.save("alli_human_1_12", "a_node_that_will_be_deleted", {783, 7}, p)
    other = playhead.load("some_other_guide", p)
    assert other.step_id is None, "a step id from a different guide is meaningless"
    assert other.completed == frozenset({783, 7}), "the character's quests went with it"
    p.unlink(missing_ok=True)


def test_a_completed_quest_is_behind_us_whatever_its_predicate_says():
    """The log cannot express this: a quest handed in and a quest never taken are both
    absent. Without carrying it, the scan stops at the accept of a finished quest."""
    g = _graph_1_12()
    t = Tracker.resume(g, _state_with(()), completed=frozenset({783}))
    node = g.get(t.step_id)
    assert node.quest_id != 783, "walked back into a quest that is already finished"
    assert node.kind is StepKind.QUEST_ACCEPT and node.quest_id == 7


def test_a_rib_remembers_its_way_back_across_runs(tmp_path):
    """Quest 15 sat complete in the log for a whole session: its hand-in had failed into a
    rib, and the playhead kept the rib but not where it led back to."""
    from jev.guide import playhead

    p = tmp_path / "playhead.json"
    playhead.save("g", "rib", {7}, p, rejoin_to="turnin")
    remembered = playhead.load("g", p)
    assert (remembered.step_id, remembered.rejoin_to) == ("rib", "turnin")
    assert playhead.with_completed(remembered, 8).rejoin_to == "turnin"
    p.write_text('{"graph_id": "g", "step_id": "rib", "completed": [7]}', encoding="utf-8")
    assert playhead.load("g", p).rejoin_to is None, "a file from before rejoin was saved"
    assert playhead.load("other", p).rejoin_to is None


def test_a_rib_resumes_with_its_way_back_or_not_at_all():
    g = _graph()
    with_way_back = Tracker.resume(g, _with_quest(have=10), start="rib", rejoin_to="turnin")
    assert with_way_back.step_id == "rib" and with_way_back.memory.rejoin_to == "turnin"
    # Without one it would grind until the guide ran out: scan from the entry instead,
    # which finds the hand-in of the quest the log says is complete.
    lost = Tracker.resume(g, _with_quest(have=10), start="rib")
    assert lost.step_id == "turnin"


def _walking(t, x, **kw):
    from jev.world.state_v1 import Pos

    return _s(t, pos=Pos(zone="Elwynn", zone_id=12, mx=x, my=0.5), **kw)


def test_a_steps_clock_stops_while_the_walk_to_it_is_getting_closer():
    """A hand-in failed four minutes in while the character was still walking to it, the
    minutes before spent dead, fighting and eating (run 20260923T174132-d01302)."""
    tr = Tracker(_graph(), "accept")               # at (0.5, 0.5), times out after 60 s
    tr.enter("accept", _walking(0, 0.1))
    for t in range(1, 180):                        # three minutes of steady walking
        assert tr.tick(_walking(t, 0.1 + 0.002 * t)).event is not Event.FAIL
    stalled = [tr.tick(_walking(t, 0.1 + 0.002 * 179)).event for t in range(180, 250)]
    assert Event.FAIL in stalled, "a walk that stops getting closer still times out"
    assert 60 <= stalled.index(Event.FAIL) <= 64


def test_a_steps_clock_stops_while_dead_or_fighting_and_runs_at_the_step():
    from jev.world.state_v1 import Vitals

    tr = Tracker(_graph(), "accept")
    tr.enter("accept", _s(0))
    for t in range(1, 101):
        assert tr.tick(_s(t, vitals=Vitals(hp=0.0, dead=True, ghost=False))).event is Event.DEATH
    for t in range(101, 201):
        fighting = Vitals(hp=0.5, dead=False, ghost=False, combat=True)
        assert tr.tick(_s(t, vitals=fighting)).event is not Event.FAIL
    assert tr.memory.working_s == 0.0
    events = [tr.tick(_s(t)).event for t in range(201, 270)]
    assert events.index(Event.FAIL) in (59, 60), "standing at the step counts"


def test_an_objectives_clock_starts_again_on_each_kill():
    """Twelve Kobold Laborers took longer than ten minutes of searching and eating between
    fights; the step failed over at 7/12, still killing (run 20260924T005824-740147)."""
    base = dict(zone="Elwynn", zone_id=12, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry="do", nodes=(
        Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=7, next=("rib",), timeout_s=60.0,
             on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=60, goto="rib"),), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(1, 10), **base),
    ))
    tr = Tracker(graph, "do")
    tr.enter("do", _with_quest(0.0, have=0))
    working = [tr.tick(_with_quest(float(t), have=t // 50)).event for t in range(1, 300)]
    assert Event.FAIL not in working, "a kill every fifty seconds is never a stall"
    stalled = [tr.tick(_with_quest(float(t), have=5)).event for t in range(300, 400)]
    assert Event.FAIL in stalled, "sixty seconds with no kill still fails over"
    assert stalled.index(Event.FAIL) <= 11


def test_a_rib_that_keeps_killing_the_character_is_left_for_its_way_back():
    """Mangy Wolves killed a level 6 paladin three times in one session, each time back at
    its body among them (run 20260924T035309-97796e)."""
    tr = Tracker(_graph(), "rib")
    tr.enter("rib", _s(0.0), rejoin_to="do")
    assert tr.tick(_s(1.0)).event is not Event.ADVANCE
    tr.memory.deaths = 1
    assert tr.tick(_s(2.0)).event is not Event.ADVANCE, "one death is bad luck"
    tr.memory.deaths = 2
    verdict = tr.tick(_s(3.0))
    assert verdict.event is Event.ADVANCE and verdict.goto == "do"
    assert tr.tick(_s(4.0, vitals=Vitals(hp=0.0, dead=True, ghost=False))).event is Event.DEATH


def test_a_steps_clock_stands_still_while_a_service_runs():
    """A hand-in's 240 s ran out 150 yards from the abbey on a sale and a meal (run
    20260924T043610)."""
    tr = Tracker(_graph(), "accept")            # times out after 60 s at the step
    tr.enter("accept", _s(0))
    tr.serving = True
    for t in range(1, 200):
        assert tr.tick(_s(t)).event is not Event.FAIL
    tr.serving = False
    events = [tr.tick(_s(t)).event for t in range(200, 300)]
    assert Event.FAIL in events and events.index(Event.FAIL) <= 61


def test_a_rib_whose_way_back_is_already_done_ends_at_once():
    """Garrick Padfoot was killed on the rib forty seconds after his hunt failed over into
    it, and the rib ground on with quest 6 complete in the log (run
    20260924T091648-c533a2)."""
    tr = Tracker(_graph(), "rib")
    tr.enter("rib", _with_quest(0.0, have=4), rejoin_to="do")
    assert tr.tick(_with_quest(1.0, have=4)).event is not Event.ADVANCE, "still short"
    verdict = tr.tick(_with_quest(2.0, have=10))
    assert verdict.event is Event.ADVANCE and verdict.goto == "do" and not verdict.completed
    tr.enter("do", _with_quest(2.0, have=10))
    assert tr.tick(_with_quest(3.0, have=10)).goto == "turnin", "and on to the hand-in"


def test_a_rib_back_to_a_hand_in_is_not_ended_by_an_empty_log():
    """A hand-in cannot tell done from never taken without having watched it."""
    tr = Tracker(_graph(), "rib")
    tr.enter("rib", _s(0.0), rejoin_to="turnin")
    assert tr.tick(_s(1.0)).event is not Event.ADVANCE


def test_a_short_rib_rejoins_when_its_time_is_up():
    """A level cures a step that kills the character, not one that could not find its NPC:
    a whole level of wolves at level 8 is an hour."""
    tr = Tracker(_graph(), "rib")
    tr.enter("rib", _s(0.0), rejoin_to="do")
    tr.memory.until = 300.0
    assert tr.tick(_s(299.0)).event is not Event.ADVANCE
    verdict = tr.tick(_s(300.0))
    assert verdict.event is Event.ADVANCE and verdict.goto == "do"


def test_a_finished_guide_is_remembered_for_that_guide_only(tmp_path):
    from jev.guide import playhead

    path = tmp_path / "p.json"
    playhead.save("g", "last", {1, 2}, path, finished=True)
    assert playhead.load("g", path).finished
    other = playhead.load("next", path)
    assert not other.finished and other.completed == {1, 2}, "the quests carry, the end does not"
    playhead.save("g", "last", {1, 2}, path)
    assert not playhead.load("g", path).finished
