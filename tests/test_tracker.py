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
