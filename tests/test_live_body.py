"""Exercise the live composition with fake devices and real skill contracts."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_runtime_records import seen

from jev.clients.advance import Advanced, Goal
from jev.clients.choose import Chose
from jev.clients.interact import Result as Interacted
from jev.clients.recover import Recover, Recovered
from jev.clients.rest import Rested
from jev.coach.schema import Decision, Intent
from jev.guide.coords import ZoneBounds
from jev.guide.graph import Graph, Node
from jev.guide.objectives import progress
from jev.guide.tracker import Event, Tracker
from jev.orch.runtime import Armed
from jev.run.body import LiveBody
from jev.run.supervisor import BodyFailure, Cancelled, Unsupported
from jev.world.state_v1 import ArmedBy, Objective, Quest, StepKind


def body(kind=StepKind.QUEST_ACCEPT, *, log=()):
    node = Node(id="quest", kind=kind, zone="zone", zone_id=1, pos=(0.5, 0.5),
                world=(50, 50, 0), map_id=0, quest_id=1, npc_id=123, title="A quest", notes="NPC",
                target_name="NPC", target_kind="creature")
    graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(node,))
    hid = SimpleNamespace(ready=lambda: True, checkpoint=None, release_all=lambda: None,
                          keys_down=lambda: [], held_buttons=set())
    client = SimpleNamespace(bounds=ZoneBounds(1, 0, 100, 0, 100, 0),
                             travel=SimpleNamespace(read_pos=None), hid=hid,
                             origin=(0, 0), size=(1600, 900), _capturing=threading.RLock(),
                             log=SimpleNamespace(complete=log, reset=Mock()),
                             quest_ids=lambda **kw: (), position=lambda: (0.5, 0.5),
                             reading=lambda: None, read=lambda: {"vitals.hp": 1.0},
                             frame=lambda: None, approach=Mock(return_value=True))
    b = LiveBody(client, graph, say=lambda line: None)
    skill = "ACCEPT_QUEST" if kind is StepKind.QUEST_ACCEPT else "TURNIN_QUEST"
    d = Decision(goal="g", intent=Intent.ADVANCE, skill=skill, abort_if=["dead"],
                 confidence=1, why="fixture")
    b.arm = Armed(d, ArmedBy.POLICY, 0, "guide", "d", node.id)
    return b


@pytest.mark.parametrize(("kind", "goal"), [(StepKind.QUEST_ACCEPT, Goal.HELD),
                                          (StepKind.QUEST_TURNIN, Goal.CLEARED)])
def test_accept_and_turnin_keep_the_confirmed_interact_choose_advance_composition(kind, goal):
    b = body(kind)
    events = []
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: events.append("interact") or Interacted.GOSSIP)
    b.chooser = SimpleNamespace(run=lambda title: events.append(("choose", title)) or Chose.CHOSE)
    # Stub only physical actions; goals and composition are the production ones.
    b.advance = SimpleNamespace(run=lambda q, g: events.append(("advance", q, g)) or Advanced.DONE,
                                detail="confirmed")
    result = b.execute(b.arm, seen(), lambda: None)
    assert result.outcome.value == "succeeded"
    assert events == ["interact", ("choose", "A quest"), ("advance", 1, goal)]
    b.client.log.reset.assert_called_once()


def test_nearest_repairer_compares_world_yards_and_filters_maps():
    b = body()
    base = b.graph.nodes[0]
    repairers = [base.model_copy(update={"id": "far", "kind": StepKind.REPAIR, "world": (1, 1, 0), "target_name": "Far"}),
                 base.model_copy(update={"id": "near", "kind": StepKind.REPAIR, "world": (51, 50, 0), "target_name": "Near"}),
                 base.model_copy(update={"id": "wrong-map", "kind": StepKind.REPAIR, "world": (50, 50, 0), "map_id": 1})]
    b.graph = Graph(graph_id="g", faction="alliance", entry="quest", nodes=(base, *repairers))
    visit = Mock(return_value=Interacted.VENDOR)
    b.interact = SimpleNamespace(open_on=visit)
    assert b._visit_repairer()
    assert visit.call_args.args[0] == "Near"


def test_unread_health_does_not_start_a_leg():
    b = body()
    b.client.read = lambda: {}
    with pytest.raises(Cancelled, match="health unread"):
        b._approach((50, 50, 0))
    b.client.approach.assert_not_called()


def test_no_food_during_travel_is_a_failure_not_an_endless_preemption():
    b = body()
    b.client.read = lambda: {"vitals.hp": 0.3}
    b.fight = SimpleNamespace(top_up=lambda: False)
    b.rest = SimpleNamespace(until=lambda _: Rested.NO_FOOD, detail="empty food slot")
    with pytest.raises(BodyFailure) as caught:
        b._approach((50, 50, 0))
    assert caught.value.result.code == "no_food"
    b.client.approach.assert_not_called()


@pytest.mark.parametrize("kind", [StepKind.QUEST_ACCEPT, StepKind.QUEST_OBJECTIVE])
def test_gameobjects_never_enter_the_creature_locator(kind):
    b = body(kind)
    node = b.graph.nodes[0].model_copy(update={"target_kind": "gameobject"})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    b.interact = SimpleNamespace(open_on=Mock())
    result = b._quest(seen()) if kind is StepKind.QUEST_ACCEPT else b._hunt(seen())
    assert result.code == "unsupported"
    b.interact.open_on.assert_not_called()
    b.client.approach.assert_not_called()


def test_an_arm_cannot_silently_target_a_different_quest():
    b = body()
    b.arm.decision = b.arm.decision.model_copy(update={"params": {"step_id": "different"}})
    b.interact = SimpleNamespace(open_on=Mock())
    result = b.execute(b.arm, seen(), lambda: None)
    assert result.code == "unsupported" and "step_id" in result.detail
    b.interact.open_on.assert_not_called()


def test_a_later_objective_is_not_hunted_at_the_first_objectives_spawn():
    q = Quest(quest_id=1, objectives=(Objective(text="a", have=8, need=8),
                                     Objective(text="b", have=2, need=6)))
    b = body(StepKind.QUEST_OBJECTIVE, log=(q,))
    with pytest.raises(Unsupported, match="own generated target"):
        b._quest_progress()


def test_progress_includes_every_counter_and_respects_incomplete_flag():
    q = Quest(quest_id=1, objectives=(Objective(text="a", have=8, need=8),
                                     Objective(text="b", have=2, need=6)))
    value = progress((q,), 1)
    assert (value.have, value.need, value.first_incomplete) == (10, 14, 1)
    assert value.complete is False
    assert progress(None, 1).complete is None
    done_counts = q.model_copy(update={"objectives": (Objective(text="a", have=8, need=8),),
                                      "complete": False})
    assert progress((done_counts,), 1).complete is False


def test_a_partial_log_never_confirms_a_turnin():
    node = body(StepKind.QUEST_TURNIN).graph.nodes[0]
    graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(node,))
    tracker = Tracker(graph, node.id)
    tracker.enter(node.id, seen(quests=(Quest(quest_id=1),)))
    assert tracker.tick(seen(1, quests=None)).event is not Event.ADVANCE
    assert tracker.tick(seen(2, quests=())).event is Event.ADVANCE


def test_release_is_separate_from_the_corpse_walk_and_requires_observed_ghost(monkeypatch):
    import jev.clients.recover
    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    states = iter([{"vitals.dead": True, "pos.mx": 0.2, "pos.my": 0.3},
                   {"vitals.ghost": True, "pos.corpse_mx": 0.2, "pos.corpse_my": 0.3}])
    walk = Mock()
    recovery = Recover(hid=None, read=lambda: next(states), walk_to=walk)
    recovery._press = lambda values: True
    assert recovery.run(release_only=True) is Recovered.RELEASED
    assert recovery.corpse == (0.2, 0.3)
    walk.assert_not_called()


def test_unknown_life_state_is_not_successful_recovery():
    recovery = Recover(hid=None, read=lambda: {})
    assert recovery.run() is Recovered.BLIND
