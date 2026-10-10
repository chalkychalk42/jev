"""A recorded full inventory must not rearm the same quest gather without a retry limit."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_body import body

from jev.clients.gather import Gathered
from jev.clients.source import ScriptedSource
from jev.guide.graph import FailEdge, FailWhen, Graph, Node, ObjectiveTarget
from jev.learn.episode import Recorder, SkillOutcome, read
from jev.orch.runtime import ClientRuntime
from jev.run.supervisor import Result, Supervisor
from jev.world.state_v1 import State, StepKind

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gather-full-bags.json").read_text())


def setup_gather():
    state = State.model_validate(FIXTURE["state"])
    b = body(StepKind.QUEST_OBJECTIVE, log=state.quests)
    # Replay the recorded inventory and quest; motion and input remain fixture devices.
    target = ObjectiveTarget(kind="loot", required_id=4864, required_count=1, counter_index=0,
                             target_kind="gameobject", target_name="Imprisoned Darkspear",
                             target_id=3237, world=(50, 50, 0), map_id=0, pos=(0.5, 0.5))
    node = b.graph.nodes[0].model_copy(update={"quest_id": 808, "objective_targets": (target,),
                                             "skills": ("TRAVEL_TO", "GRIND_UNTIL")})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    values = {"vitals.hp": state.vitals.hp, "vitals.combat": False,
              "vitals.dead": False, "vitals.ghost": False, "bags.free": state.bags.free}
    b.client.read = lambda: values
    b.gather = SimpleNamespace(pick=Mock(return_value=Gathered.BAGS_FULL), detail="full")
    return b, state, values


@pytest.mark.parametrize("blocked", ["no_junk", "unreachable"])
def test_full_bags_with_no_service_fail_before_walking_to_the_object(blocked):
    b, state, _ = setup_gather()
    if blocked == "no_junk":
        b.policy_context.bags_failed(0)
    else:
        b.policy_context.bags_unreachable("quest", time.time())
    result = b._hunt(state)
    assert result.outcome is SkillOutcome.ABORTED and result.code == "bags_full"
    b.client.approach.assert_not_called()
    b.gather.pick.assert_not_called()


def test_a_bag_service_that_can_help_keeps_its_turn():
    b, state, _ = setup_gather()
    result = b._hunt(state)
    assert result.outcome is SkillOutcome.PREEMPTED and result.code == "bags_full"
    b.client.approach.assert_not_called()
    b.gather.pick.assert_not_called()


def test_bags_filling_during_the_walk_also_spend_an_attempt_when_service_is_blocked():
    b, state, values = setup_gather()
    values["bags.free"] = 1
    b.policy_context.bags_failed(0)
    result = b._hunt(state)
    assert b.client.approach.call_count == b.gather.pick.call_count == 1
    assert result.outcome is SkillOutcome.ABORTED and result.code == "bags_full"


def test_a_freed_slot_allows_the_held_quest_to_be_gathered():
    b, state, values = setup_gather()
    b.policy_context.bags_failed(0)
    values["bags.free"] = 1

    def pick(*args):
        b.client.log.complete = (state.quests[0].model_copy(update={"complete": True}),)
        return Gathered.TOOK

    b.gather.pick.side_effect = pick
    assert b._hunt(state).outcome is SkillOutcome.SUCCEEDED
    assert b.gather.pick.call_count == 1


@pytest.mark.parametrize(("change", "code"), [
    ({"vitals.combat": True}, "interrupted"), ({"ui.modal": True}, "interrupted"),
    ({"vitals.dead": True}, "died"), ({"vitals.ghost": True}, "died"), (None, "blind"),
])
def test_an_interruption_with_full_bags_does_not_spend_a_quest_attempt(change, code):
    b, state, values = setup_gather()
    b.policy_context.bags_failed(0)
    if change is None:
        b.client.read = lambda: None
    else:
        values.update(change)
    result = b._hunt(state)
    assert result.outcome is SkillOutcome.PREEMPTED and result.code == code
    b.client.approach.assert_not_called()
    b.gather.pick.assert_not_called()


def test_supervisor_leaves_the_blocked_gather_after_three_attempts(tmp_path):
    """The actual worker/runtime take the existing failure route, retaining the held quest."""
    b, state, _ = setup_gather()
    node = b.graph.nodes[0].model_copy(update={
        "next": ("turnin",),
        "on_fail": (FailEdge(when=FailWhen.TIMEOUT, value=600, goto="rib"),),
    })
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(
        node, Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=808,
                   skills=("TURNIN_QUEST",), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(15, 17), **base),
    ))
    b.graph = graph
    states = [state.model_copy(update={"t": float(t), "client_id": "c",
                                      "pos": state.pos.model_copy(update={"mx": .5, "my": .5,
                                                                         "zone": "zone",
                                                                         "zone_id": 1,
                                                                         "coord_zone_id": 1})})
              for t in range(12)]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path))
    b.policy_context = rt.policy_context
    b.policy_context.bags_failed(0)
    # Restocking is also unavailable in the recorded run; it must not replace the gather.
    b.policy_context.supplies_failed(state.bags.money_copper)
    calls = []

    def execute(arm, observed, checkpoint):
        b.arm = arm
        calls.append((arm.step_id, arm.decision.skill))
        return (b._hunt(observed) if arm.step_id == node.id else
                Result(SkillOutcome.SUCCEEDED, "fixture fallback", "done"))

    adapter = SimpleNamespace(available=b.available, execute=execute, travelling=False,
                              release=lambda: None)
    supervisor = Supervisor(rt, adapter, say=lambda line: None, max_failures=3)
    try:
        for t in range(8):
            supervisor.step(float(t))
            if rt.tracker.step_id != node.id:
                break
            if supervisor.worker is not None:
                assert supervisor.worker.done.wait(1)
        assert rt.tracker.step_id == "rib", (supervisor.failure, calls, rt.tracker.memory)
        assert calls.count((node.id, "GRIND_UNTIL")) == 3
        assert not supervisor.stopped.is_set()
        assert 808 not in rt.completed and b.client.log.complete == state.quests
        failed = [r for r in read(rt.recorder.dir / "skills.jsonl") if r["step_id"] == node.id]
        assert len(failed) == 3 and all(r["outcome"] == "aborted" for r in failed)
    finally:
        supervisor.close()
