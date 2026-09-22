"""Shared body switches proven Hunt composition between DB-backed objective targets."""

from test_live_body import body
from test_runtime_records import seen

from jev.guide.graph import ObjectiveTarget
from jev.guide.tracker import Event, Tracker, route_destination
from jev.learn.episode import SkillOutcome
from jev.perceive.radio_frame import name_id
from jev.run.hunt import Hunted
from jev.world.state_v1 import Objective, Quest, StepKind


def test_two_hunts_use_separate_destinations_and_only_quest_flag_advances(monkeypatch):
    quest = Quest(quest_id=1, complete=False, objectives=(
        Objective(text="Prowler", have=7, need=8, counter_index=0),
        Objective(text="Young Forest Bear", have=0, need=5, counter_index=1),
    ))
    b = body(StepKind.QUEST_OBJECTIVE, log=(quest,))
    targets = (
        ObjectiveTarget(kind="kill", required_id=118, required_count=8, counter_index=0,
                        target_kind="creature", target_name="Prowler", world=(11, 12, 13),
                        map_id=0, hunt_yards=31),
        ObjectiveTarget(kind="kill", required_id=822, required_count=5, counter_index=1,
                        target_kind="creature", target_name="Young Forest Bear",
                        world=(91, 92, 93), map_id=0, hunt_yards=54),
    )
    node = b.graph.nodes[0].model_copy(update={"objective_targets": targets})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    tracker = Tracker(b.graph, node.id)
    tracker.enter(node.id, seen(quests=(quest,)))
    visits = []

    class MeasuredHunt:
        detail = "fixture observed selected requirement complete"

        def __init__(self, **callbacks):
            self.callbacks = callbacks

        def run(self, destination, radius, target_hash, **kwargs):
            index = len(visits)
            expected = targets[index]
            assert (destination, radius, target_hash) == (
                expected.world, expected.hunt_yards, name_id(expected.target_name))
            assert self.callbacks["progress"]() == ((7, 8) if index == 0 else (0, 5))
            assert self.callbacks["is_complete"]() is False
            current = b.client.log.complete[0]
            updated = tuple(o.model_copy(update={"have": o.need}) if o.counter_index == index else o
                            for o in current.objectives)
            b.client.log.complete = (current.model_copy(update={"objectives": updated,
                                                               "complete": index == 1}),)
            assert self.callbacks["is_complete"]() is True
            visits.append(destination)
            return Hunted.DONE

    monkeypatch.setattr("jev.run.body.Hunt", MeasuredHunt)
    first = b._hunt(seen(quests=b.client.log.complete))
    assert first.outcome is SkillOutcome.SUCCEEDED
    assert tracker.tick(seen(1, quests=b.client.log.complete)).event is not Event.ADVANCE
    second = b._hunt(seen(2, quests=b.client.log.complete))
    assert second.outcome is SkillOutcome.SUCCEEDED
    assert tracker.tick(seen(3, quests=b.client.log.complete)).event is Event.ADVANCE
    assert visits == [t.world for t in targets]


def test_complete_delivery_never_constructs_hunt_or_walks(monkeypatch):
    b = body(StepKind.QUEST_OBJECTIVE, log=(Quest(quest_id=1, complete=True),))
    target = ObjectiveTarget(kind="delivery", required_id=745, required_count=1, counter_index=0)
    node = b.graph.nodes[0].model_copy(update={"objective_targets": (target,)})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    monkeypatch.setattr("jev.run.body.Hunt", lambda **kw: (_ for _ in ()).throw(
        AssertionError("delivery attempted a hunt")))
    assert b._hunt(seen(quests=b.client.log.complete)).outcome is SkillOutcome.SUCCEEDED
    b.client.approach.assert_not_called()


def test_tracker_changes_route_pin_without_penalizing_the_second_hunt():
    targets = tuple(ObjectiveTarget(kind="kill", required_id=i + 1, required_count=5,
                                    counter_index=i, pos=pos)
                    for i, pos in enumerate(((0.1, 0.1), (0.8, 0.8))))
    b = body(StepKind.QUEST_OBJECTIVE)
    node = b.graph.nodes[0].model_copy(update={"objective_targets": targets})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    tracker = Tracker(b.graph, node.id)

    def observed(t, pos, first_done=False):
        q = Quest(quest_id=1, complete=False, objectives=(
            Objective(text="first", have=5 if first_done else 1, need=5, counter_index=0),
            Objective(text="second", have=1, need=5, counter_index=1)))
        state = seen(t, quests=(q,))
        return state.model_copy(update={"pos": state.pos.model_copy(update={"mx": pos[0], "my": pos[1]})})

    first = observed(0, (0.1, 0.1))
    tracker.enter(node.id, first)
    assert tracker.tick(first).event is Event.ARRIVED
    transit = observed(1, (0.4, 0.4), first_done=True)
    assert route_destination(transit, node) == (0.8, 0.8)
    assert tracker.tick(transit).event is Event.NONE
    assert tracker.tick(observed(30, (0.4, 0.4), first_done=True)).event is not Event.OFF_ROUTE
    assert tracker.tick(observed(31, (0.8, 0.8), first_done=True)).event is Event.ARRIVED
    tracker.tick(observed(32, (0.4, 0.4), first_done=True))
    assert tracker.tick(observed(54, (0.4, 0.4), first_done=True)).event is Event.OFF_ROUTE


def test_tracker_cannot_complete_an_unpainted_fourth_requirement_when_flag_unread():
    targets = tuple(ObjectiveTarget(kind="kill", required_id=i + 1, required_count=5,
                                    counter_index=i) for i in range(4))
    visible = Quest(quest_id=1, complete=None, objectives=tuple(
        Objective(text="", have=5, need=5, counter_index=i) for i in range(3)))
    b = body(StepKind.QUEST_OBJECTIVE, log=(visible,))
    node = b.graph.nodes[0].model_copy(update={"objective_targets": targets})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    tracker = Tracker(b.graph, node.id)
    state = seen(quests=(visible,))
    tracker.enter(node.id, state)
    assert tracker.tick(state).event is not Event.ADVANCE
    done = seen(1, quests=(visible.model_copy(update={"complete": True}),))
    assert tracker.tick(done).event is Event.ADVANCE
