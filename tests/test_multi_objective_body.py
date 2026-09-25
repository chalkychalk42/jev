"""Shared body switches proven Hunt composition between DB-backed objective targets."""

from types import SimpleNamespace

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


def _explore_body(log):
    b = body(StepKind.QUEST_OBJECTIVE, log=log)
    target = ObjectiveTarget(kind="explore", required_id=88, world=(-9843.5, 127.5, 5.4),
                             map_id=0, pos=(0.41, 0.82))
    node = b.graph.nodes[0].model_copy(update={"objective_targets": (target,)})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    return b, target


def test_an_exploration_is_walked_into_and_credited_by_the_quest_flag(monkeypatch):
    """The Fargodeep Mine: a ten-yard trigger round a point inside the mine's mouth."""
    b, target = _explore_body((Quest(quest_id=1, complete=False),))
    monkeypatch.setattr("jev.run.body.Hunt", lambda **kw: (_ for _ in ()).throw(
        AssertionError("an exploration is not a hunt")))

    def walked(world, **kw):
        b.client.log.complete = (Quest(quest_id=1, complete=True),)
        return True

    b.client.approach.side_effect = walked
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.outcome is SkillOutcome.SUCCEEDED and result.code == "done"
    assert b.client.approach.call_args.args[0] == target.world


def test_arriving_without_the_credit_is_not_exploring(monkeypatch):
    b, _ = _explore_body((Quest(quest_id=1, complete=False),))
    monkeypatch.setattr("jev.run.body.EXPLORE_CREDIT_S", 0.0)
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.outcome is SkillOutcome.ABORTED and result.code == "nothing"
    b.client.approach.return_value = False
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.outcome is SkillOutcome.ABORTED and result.code == "unreachable"


def test_a_quest_object_is_gathered_at_its_spawn_points_nearest_first(monkeypatch):
    """Milly's Harvest: crates in the vineyard, eight to bring back."""
    from jev.clients.gather import Gathered

    quest = Quest(quest_id=1, complete=False, objectives=(
        Objective(text="crates", have=6, need=8, counter_index=0),))
    b = body(StepKind.QUEST_OBJECTIVE, log=(quest,))
    target = ObjectiveTarget(kind="loot", required_id=11119, required_count=8, counter_index=0,
                             target_kind="gameobject", target_name="Milly's Harvest",
                             target_id=161557, world=(50, 50, 0), map_id=0, pos=(0.5, 0.5))
    node = b.graph.nodes[0].model_copy(update={"objective_targets": (target,)})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    b.hunt_spawns = {f"{node.id}#161557": ((90.0, 90.0, 0.0), (52.0, 52.0, 0.0), (60.0, 60.0, 0.0))}
    b.client.position = lambda: (0.5, 0.5)            # world (50, 50)
    monkeypatch.setattr("jev.run.body.Hunt", lambda **kw: (_ for _ in ()).throw(
        AssertionError("an object is not a creature to hunt")))
    walked = []
    b.client.approach.side_effect = lambda world, **kw: walked.append(world) or True
    picked = []

    def pick(wanted, progress):
        picked.append(wanted)
        current = b.client.log.complete[0]
        have = current.objectives[0].have + 1
        b.client.log.complete = (current.model_copy(update={
            "objectives": (Objective(text="crates", have=have, need=8, counter_index=0),),
            "complete": have >= 8}),)
        return Gathered.TOOK

    b.gather = SimpleNamespace(pick=pick, detail="objective")
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.outcome is SkillOutcome.SUCCEEDED
    assert walked == [(52.0, 52.0, 0.0), (60.0, 60.0, 0.0)], "nearest first, and no further"
    assert picked == [name_id("Milly's Harvest")] * 2


def test_a_spawn_point_that_shows_nothing_is_looked_at_again_a_step_back(monkeypatch):
    """Stood on the spawn point, the character hides what lies underfoot."""
    from jev.clients.gather import Gathered

    quest = Quest(quest_id=1, complete=False, objectives=(
        Objective(text="crates", have=7, need=8, counter_index=0),))
    b = body(StepKind.QUEST_OBJECTIVE, log=(quest,))
    target = ObjectiveTarget(kind="loot", required_id=11119, required_count=8, counter_index=0,
                             target_kind="gameobject", target_name="Milly's Harvest",
                             target_id=161557, world=(50, 50, 0), map_id=0, pos=(0.5, 0.5))
    node = b.graph.nodes[0].model_copy(update={"objective_targets": (target,)})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    b.hunt_spawns = {f"{node.id}#161557": ((52.0, 52.0, 0.0),)}
    holds = []
    b.client.hid.hold = lambda key, seconds, **kw: holds.append(key) or True
    answers = iter([Gathered.NOT_HERE, Gathered.TOOK])

    def pick(wanted, progress):
        got = next(answers)
        if got is Gathered.TOOK:
            b.client.log.complete = (Quest(quest_id=1, complete=True, objectives=(
                Objective(text="crates", have=8, need=8, counter_index=0),)),)
        return got

    b.gather = SimpleNamespace(pick=pick, detail="")
    assert b._hunt(seen(quests=b.client.log.complete)).outcome is SkillOutcome.SUCCEEDED
    assert holds == ["s"]


def test_a_gather_tries_first_where_the_object_has_been_found_and_learns_the_visit(monkeypatch):
    """V158: at the Eastvale Logging Camp nine spawn points of the wood bundles in a row
    showed nothing; the points that have given something are tried first."""
    import random

    from jev.clients.gather import Gathered
    from jev.learn.choices import ChoiceMemory, station_key

    quest = Quest(quest_id=1, complete=False, objectives=(
        Objective(text="crates", have=7, need=8, counter_index=0),))
    b = body(StepKind.QUEST_OBJECTIVE, log=(quest,))
    target = ObjectiveTarget(kind="loot", required_id=11119, required_count=8, counter_index=0,
                             target_kind="gameobject", target_name="Milly's Harvest",
                             target_id=161557, world=(50, 50, 0), map_id=0, pos=(0.5, 0.5))
    node = b.graph.nodes[0].model_copy(update={"objective_targets": (target,)})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    far, near = (90.0, 90.0, 0.0), (52.0, 52.0, 0.0)
    b.hunt_spawns = {f"{node.id}#161557": (far, near)}
    b.client.position = lambda: (0.5, 0.5)
    b.choice_memory = ChoiceMemory()
    for _ in range(6):
        b.choice_memory.record("gather.station", station_key("object:161557", far), True, 5.0)
        b.choice_memory.record("gather.station", station_key("object:161557", near), False, 5.0)
    b.choice_rng = random.Random(5)
    walked = []
    b.client.approach.side_effect = lambda world, **kw: walked.append(world) or True

    def pick(wanted, progress):
        current = b.client.log.complete[0]
        b.client.log.complete = (current.model_copy(update={
            "objectives": (Objective(text="crates", have=8, need=8, counter_index=0),),
            "complete": True}),)
        return Gathered.TOOK

    b.gather = SimpleNamespace(pick=pick, detail="objective")
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.outcome is SkillOutcome.SUCCEEDED
    assert walked == [far], "the point that has given crates before, though it is further"
    arm = b.choice_memory.arms("gather.station")[station_key("object:161557", far)]
    assert (arm.tries, arm.wins) == (7, 7)
