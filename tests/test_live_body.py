"""Exercise the live composition with fake devices and real skill contracts."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_runtime_records import seen

from jev.clients.advance import Advanced, Goal
from jev.clients.choose import Chose
from jev.clients.fight import Fought
from jev.clients.interact import Result as Interacted
from jev.clients.loot import Looted
from jev.clients.recover import Recover, Recovered
from jev.clients.repair import Repaired
from jev.clients.rest import Rested
from jev.coach.schema import Decision, Intent
from jev.guide.coords import ZoneBounds
from jev.guide.graph import Graph, Node
from jev.guide.objectives import progress
from jev.guide.tracker import Event, Tracker
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed
from jev.perceive.radio_frame import name_id
from jev.run.body import LiveBody
from jev.run.supervisor import BodyFailure, Cancelled, FocusLost, Result, Unsupported
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
    # These tests are about skill composition; levelling before a skill has its own.
    b.ready_camera = lambda state: None
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


def test_a_repairer_that_cannot_be_clicked_is_passed_over_for_the_next(tmp_path, monkeypatch):
    """Janos Hammerknuckle's awning took every probe, and Dermot Johns and Godric Rothgar
    stood twenty yards off; the guide named only him (26 September). Every repairer in the
    zone is ranked as merchants are, and a failure is remembered (V201)."""
    from jev.world.vendor import Merchant, load_merchant_failures

    b = body()
    b.merchant_memory = tmp_path / "merchant-memory.json"
    vendors = (Merchant(78, "Under The Awning", 0, (50, 51, 0), frozenset(), repairs=True),
               Merchant(1213, "In Plain View", 0, (52, 52, 0), frozenset(), repairs=True),
               Merchant(152, "Sells Only", 0, (50, 50, 0), frozenset()))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    answers = {"Under The Awning": Interacted.NOT_VISIBLE, "In Plain View": Interacted.VENDOR}
    visit = Mock(side_effect=lambda name, **kw: answers[name])
    b.interact = SimpleNamespace(open_on=visit, detail="hover: ground")
    assert b._visit_repairer()
    assert [c.args[0] for c in visit.call_args_list] == ["Under The Awning", "In Plain View"]
    assert load_merchant_failures(b.merchant_memory) == {78: 1}
    assert b._repairer_yards() == pytest.approx(1.0), "the smith a yard off, not the grocer"


def test_a_failed_repairer_is_followed_only_by_its_neighbours(tmp_path, monkeypatch):
    """From Sentinel Hill the next repairer after William MacGregor was the Defias
    Profiteer in Moonbrook, 625 yards among Defias, walked for as soon as MacGregor's walk
    ended in the hearthstone (session 160). A failure there is the repair's failure."""
    from jev.world.vendor import Merchant

    b = body()
    b.merchant_memory = tmp_path / "merchant-memory.json"
    vendors = (Merchant(1668, "In Town", 0, (50, 51, 0), frozenset(), repairs=True),
               Merchant(1669, "Another Town", 0, (95, 95, 0), frozenset(), repairs=True))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    visit = Mock(return_value=Interacted.APPROACH_FAILED)
    b.interact = SimpleNamespace(open_on=visit, detail="the planner could not stand us on the node")
    with pytest.raises(BodyFailure, match="In Town"):
        b._visit_repairer()
    assert [c.args[0] for c in visit.call_args_list] == ["In Town"]


def test_a_purse_lesson_outlives_the_session(tmp_path):
    """V206: what the purse could not pay is kept per character for its next session."""
    from jev.coach.policy import Context

    b = body()
    b.purse_memory = tmp_path / "character-1.purse.json"
    b.policy_context = Context()
    b.policy_context.repair_failed(45)
    later = body()
    later.purse_memory = b.purse_memory
    later.policy_context = Context()
    assert not later.policy_context.can_repair(45)
    later.policy_context.repaired()
    again = body()
    again.purse_memory = b.purse_memory
    again.policy_context = Context()
    assert again.policy_context.can_repair(0)


def test_a_leg_starts_in_combat_only_while_combat_is_paused():
    """V212: after fights that never engaged, the walk is the way out of reach; in session
    176 the pause met "combat before travel" sixteen times."""
    import time as clock

    from jev.coach.policy import Context

    b = body()
    b.client.read = lambda: {"vitals.hp": 1.0, "vitals.combat": True}
    b.policy_context = Context()
    with pytest.raises(Cancelled, match="combat before travel"):
        b._approach((50, 50, 0))
    b.policy_context.fight_paused_until = clock.time() + 30
    b._approach((50, 50, 0))
    b.client.approach.assert_called()


def test_a_leg_out_of_reach_does_not_stop_for_a_meal():
    """V218: in combat with combat paused, a hurt character walks on; session 188 asked for
    a meal six times running, "in combat; not a moment to eat", and stood still."""
    import time as clock

    from jev.coach.policy import Context

    b = body()
    b.client.read = lambda: {"vitals.hp": 0.3, "vitals.combat": True}
    b.policy_context = Context()
    b.policy_context.fight_paused_until = clock.time() + 30
    b.rest = SimpleNamespace(until=lambda *a, **k: pytest.fail("a meal in combat"), detail="")
    b.fight.top_up = lambda: pytest.fail("a heal before the way out")
    b._approach((50, 50, 0))
    b.client.approach.assert_called()


def test_a_leg_wedged_indoors_is_walked_again_after_backing_out():
    """V230: the retry plans from outside the way the character came in."""
    b = body()
    reads = {"vitals.hp": 1.0, "vitals.combat": False, "pos.indoors": True}
    b.client.read = lambda: dict(reads)
    b.client.approach.side_effect = [False, True]
    b.client.back_out = lambda: reads.update({"pos.indoors": False}) or True
    assert b._approach((50, 50, 0)) is True
    assert b.client.approach.call_count == 2
    outdoors = body()
    outdoors.client.read = lambda: {"vitals.hp": 1.0, "vitals.combat": False,
                                    "pos.indoors": False}
    outdoors.client.approach.return_value = False
    outdoors.client.back_out = lambda: pytest.fail("backed out of the open air")
    assert outdoors._approach((50, 50, 0)) is False
    assert outdoors.client.approach.call_count == 1


def test_unread_health_does_not_start_a_leg():
    b = body()
    b.client.read = lambda: {}
    with pytest.raises(Cancelled, match="health unread"):
        b._approach((50, 50, 0))
    b.client.approach.assert_not_called()


def test_merchant_failure_preserves_the_interaction_cause():
    b = body()
    b.interact = SimpleNamespace(open_on=lambda *args, **kw: Interacted.NOT_VISIBLE,
                                 detail="selected the right unit, but no ring and plate to aim at")
    with pytest.raises(BodyFailure) as caught:
        b._open_merchant("Repairer", (50, 50, 0), (0.5, 0.5))
    assert caught.value.result.outcome is SkillOutcome.ABORTED
    assert caught.value.result.code == "not_visible"
    assert "Repairer: selected the right unit" in caught.value.result.detail


def test_blind_merchant_interaction_preempts_without_consuming_a_failed_attempt():
    b = body()
    b.interact = SimpleNamespace(open_on=lambda *args, **kw: Interacted.BLIND,
                                 detail="no captured frame after selecting")
    with pytest.raises(BodyFailure) as caught:
        b._open_merchant("Repairer", (50, 50, 0), (0.5, 0.5))
    result = caught.value.result
    assert result.outcome is SkillOutcome.PREEMPTED
    assert result.code == "blind"
    assert result.detail == "Repairer: no captured frame after selecting"


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
    b.gather = SimpleNamespace(open=lambda wanted: False, detail="no hover named the object")
    result = b._quest(seen()) if kind is StepKind.QUEST_ACCEPT else b._hunt(seen())
    # A quest at an object looks for it by its tooltip; an objective without structured
    # targets still has no object to look for.
    assert result.code == ("not_visible" if kind is StepKind.QUEST_ACCEPT else "unsupported")
    b.interact.open_on.assert_not_called()


def test_an_arm_cannot_silently_target_a_different_quest():
    b = body()
    b.arm.decision = b.arm.decision.model_copy(update={"params": {"step_id": "different"}})
    b.interact = SimpleNamespace(open_on=Mock())
    result = b.execute(b.arm, seen(), lambda: None)
    assert result.code == "unsupported" and "step_id" in result.detail
    b.interact.open_on.assert_not_called()


def test_focus_loss_stops_the_body_before_another_physical_action():
    b = body()
    def quest(_):
        b.client.hid.ready = lambda: False
        b.client.hid.checkpoint()
        pytest.fail("the body continued after focus loss")
    b._quest = quest
    with pytest.raises(FocusLost):
        b.execute(b.arm, seen(), lambda: None)


def test_a_replacement_uses_focus_backoff_before_executing():
    b = body()
    b.client.hid.ready = lambda: False
    events = []
    def focus(*args, **kw):
        kw["checkpoint"]()
        events.append("focus")
        b.client.hid.ready = lambda: True
        return True
    b.client.focused = focus
    b._quest = lambda _: events.append("quest") or Result(SkillOutcome.SUCCEEDED)
    assert b.execute(b.arm, seen(), lambda: None).outcome is SkillOutcome.SUCCEEDED
    assert events == ["focus", "quest"]


def test_refused_focus_never_enters_the_skill():
    b = body()
    b.client.hid.ready = lambda: False
    b.client.focused = Mock(return_value=False)
    b._quest = Mock()
    assert b.execute(b.arm, seen(), lambda: None).code == "refused"
    b._quest.assert_not_called()


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


def test_all_unit_actions_share_one_targeting_owner_and_event_frame_writer():
    original = body()
    retain = Mock(return_value={"file": "observed.png"})
    composed = LiveBody(original.client, original.graph, record_frame=retain)
    assert composed.interact.targeting is composed.fight.targeting is composed.loot.targeting
    assert composed.targeting is composed.fight.targeting
    assert composed.targeting.hid is composed.client.hid
    assert composed.targeting.window_origin == composed.client.origin
    assert composed.targeting.record_frame is retain
    composed.checkpoint = Mock()
    composed.targeting.read()
    composed.targeting.read_frame()
    assert composed.checkpoint.call_count == 2


@pytest.mark.parametrize("outcome, status", [
    (Looted.REFUSED, SkillOutcome.ABORTED), (Looted.BLIND, SkillOutcome.PREEMPTED),
    (Looted.INTERRUPTED, SkillOutcome.PREEMPTED), (Looted.BAGS_FULL, SkillOutcome.PREEMPTED),
    (Looted.WINDOW_OPEN, SkillOutcome.ABORTED),
])
def test_direct_combat_does_not_hide_post_kill_loot_failure(outcome, status):
    b = body()
    plate = object()
    b.fight = SimpleNamespace(run=Mock(return_value=Fought.KILLED), detail="observed death",
                              last_plate=plate, killed_name_id=2864)
    b.loot = SimpleNamespace(run=Mock(return_value=outcome), detail="uncompleted corpse action")
    result = b._fight(seen())
    assert result.outcome is status and result.code == outcome.value
    assert result.detail == "post-kill loot: uncompleted corpse action"
    b.loot.run.assert_called_once_with(progress=b._progress, anchor=plate, name_id=2864,
                                       far=False)


@pytest.mark.parametrize("outcome", [Looted.TOOK, Looted.NOTHING, Looted.NO_CORPSE])
def test_direct_combat_keeps_success_after_observed_loot_outcome(outcome):
    b = body()
    b.fight = SimpleNamespace(run=lambda _: Fought.KILLED, detail="observed death",
                              last_plate=None, killed_name_id=None)
    b.loot = SimpleNamespace(run=lambda **_: outcome, detail="observed corpse outcome")
    result = b._fight(seen())
    assert result.outcome is SkillOutcome.SUCCEEDED and result.code == "killed"


@pytest.mark.parametrize("outcome", [Fought.REFUSED, Fought.BLIND, Fought.INTERRUPTED])
def test_direct_combat_failure_never_attempts_loot(outcome):
    b = body()
    b.fight = SimpleNamespace(run=lambda _: outcome, detail="fight stopped")
    b.loot = SimpleNamespace(run=Mock())
    result = b._fight(seen())
    assert result.outcome is not SkillOutcome.SUCCEEDED
    assert result.code == outcome.value and result.detail == "fight stopped"
    b.loot.run.assert_not_called()


@pytest.mark.parametrize("outcome, status", [
    (Interacted.BLIND, SkillOutcome.PREEMPTED), (Interacted.INTERRUPTED, SkillOutcome.PREEMPTED),
    (Interacted.REFUSED, SkillOutcome.ABORTED), (Interacted.WINDOW_OPEN, SkillOutcome.ABORTED),
])
def test_quest_interaction_uses_the_same_terminal_outcome_mapping_as_services(outcome, status):
    b = body()
    b.interact = SimpleNamespace(open_on=lambda *_, **__: outcome, detail="interaction stopped")
    b.advance = SimpleNamespace(run=Mock())
    result = b._quest(seen())
    assert result.outcome is status and result.code == outcome.value
    assert result.detail == "interaction stopped"
    b.advance.run.assert_not_called()


def calibration_devices(b, monkeypatch):
    """Keep the real Camera and its shared HID owner; replace only physical delivery."""
    monkeypatch.setattr("jev.clients.camera.time.sleep", lambda _: None)
    b.client.hid.move_to = Mock(return_value=True)
    b.client.hid.move_by = Mock(return_value=True)
    b.client.hid.button = Mock(return_value=True)
    return b.client.hid


@pytest.mark.parametrize(("combat", "levels"), [(False, True), (True, False), (None, False)])
def test_the_camera_is_levelled_before_a_skill_only_while_nothing_is_fighting(
        monkeypatch, combat, levels):
    """Levelled lazily, the first look of run 20260923T173347-590b06 was a panic fight at
    12% health, and the five-second drag outlasted the character."""
    from jev.world.state_v1 import Vitals

    b = body()
    del b.ready_camera                        # the body's own, not the fixture's
    hid = calibration_devices(b, monkeypatch)
    b._quest = lambda state: Result(SkillOutcome.SUCCEEDED, "ok", "ok")
    state = seen(vitals=Vitals(hp=1, combat=combat, dead=False, ghost=False))
    b.execute(b.arm, state, lambda: None)
    assert b.camera.calibrated is levels
    assert hid.move_by.call_count == (2 if levels else 0)
    b.execute(b.arm, state, lambda: None)
    assert hid.move_by.call_count == (2 if levels else 0), "levelled twice in one session"


def test_interact_fight_and_loot_reuse_one_camera_across_ordinary_skill_releases(monkeypatch):
    b = body()
    hid = calibration_devices(b, monkeypatch)
    assert b.interact.level.__self__ is b.fight.level.__self__ is b.loot.level.__self__ is b.camera
    assert b.camera.hid is b.client.hid
    assert b.interact.level() is True
    b.release()
    assert b.fight.level() is True
    b.release()
    assert b.loot.level() is True
    b.release()
    assert b.interact.level() is True
    assert hid.move_to.call_count == 1
    assert hid.move_by.call_count == hid.button.call_count == 2


def test_observed_focus_loss_between_workers_invalidates_the_shared_camera(monkeypatch):
    b = body()
    hid = calibration_devices(b, monkeypatch)
    assert b.fight.level() is True
    hid.ready = lambda: False
    assert b.has_focus() is False
    assert hid.move_by.call_count == 2, "invalidation itself sent camera input"
    hid.ready = lambda: True
    assert b.has_focus() is True
    assert b.loot.level() is True
    assert hid.move_by.call_count == 4
    assert b.interact.level() is True
    assert hid.move_by.call_count == 4


def test_active_worker_focus_checkpoint_invalidates_before_raising(monkeypatch):
    b = body()
    hid = calibration_devices(b, monkeypatch)
    assert b.interact.level() is True

    def quest(_):
        hid.ready = lambda: False
        hid.checkpoint()
        pytest.fail("worker continued after losing input ownership")

    b._quest = quest
    with pytest.raises(FocusLost):
        b.execute(b.arm, seen(), lambda: None)
    b.release()
    hid.ready = lambda: True
    assert b.loot.level() is True
    assert hid.move_by.call_count == 4


@pytest.mark.parametrize("outcome", ["restored", "failed", "cancelled"])
def test_reconnect_invalidates_camera_before_session_work_even_when_reconnect_fails(
        monkeypatch, outcome):
    b = body()
    hid = calibration_devices(b, monkeypatch)
    assert b.fight.level() is True
    checkpoint = Mock()
    response = Result(SkillOutcome.SUCCEEDED if outcome == "restored" else SkillOutcome.ABORTED,
                      "fixture session attempt", "reconnected" if outcome == "restored" else "error")
    observed = []

    def reconnect(client, supplied_checkpoint, *, env_file):
        assert client is b.client and supplied_checkpoint is checkpoint
        assert env_file == "fixture.env"
        observed.append(b.camera._calibrated_geometry)
        if outcome == "cancelled":
            raise Cancelled("stop requested")
        return response

    monkeypatch.setattr("jev.run.watchdog.reconnect_client", reconnect)
    if outcome == "cancelled":
        with pytest.raises(Cancelled, match="stop requested"):
            b.reconnect(checkpoint, env_file="fixture.env")
    else:
        assert b.reconnect(checkpoint, env_file="fixture.env") is response
    assert observed == [None], "session work began with an old calibration still valid"
    assert hid.move_by.call_count == 2, "reconnecting performed speculative calibration"
    assert b.loot.level() is True
    assert hid.move_by.call_count == 4


def test_a_fight_inside_an_objective_is_for_its_creature():
    """Delegated COMBAT_PROFILE inside the wolf objective asked for no name and could pick
    the nearest plate - a rabbit on the first live run."""
    b = body(StepKind.QUEST_OBJECTIVE)
    asked = []
    b.fight = SimpleNamespace(run=lambda name_id: asked.append(name_id) or Fought.NOT_VISIBLE,
                              detail="not visible", last_plate=None, killed_name_id=None)
    b._objective_name = lambda: 2864
    b._fight(seen())
    assert asked == [2864]


def test_the_objective_name_is_the_armed_steps_creature_or_none():
    from jev.perceive.radio_frame import name_id

    creature = body(StepKind.QUEST_OBJECTIVE)
    assert creature._objective_name() == name_id("NPC")
    service = body(StepKind.QUEST_OBJECTIVE)
    service.graph = Graph(graph_id="g", faction="alliance", entry="quest", nodes=(
        service.graph.nodes[0].model_copy(update={"target_kind": "gameobject"}),))
    assert service._objective_name() is None, "an object is not a creature to fight"
    service.arm = None
    assert service._objective_name() is None


def test_the_spirit_healer_raises_a_ghost_where_it_appeared(monkeypatch):
    """Getting up beside the level 6 wolf that had just killed it, four times running
    (run 20260923T181209-bc03ba). The Spirit Healer answers with the same painted button."""
    import jev.clients.recover
    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    states = iter([
        {"vitals.dead": True, "pos.mx": 0.45, "pos.my": 0.66},
        {"vitals.ghost": True, "vitals.dead": False, "pos.mx": 0.39, "pos.my": 0.60},
        {"vitals.ghost": True, "vitals.dead": False, "pos.mx": 0.39, "pos.my": 0.60},
        {"vitals.ghost": True, "vitals.dead": False, "ui.modal": True,
         "ui.advance_x": 0.5, "ui.advance_y": 0.2},
        {"vitals.ghost": False, "vitals.dead": False},
    ])
    talked, walked = [], Mock()
    recovery = Recover(hid=None, read=lambda: next(states), walk_to=walked,
                       interact=lambda name: talked.append(name) or "no_window")
    pressed = []
    recovery._press = lambda values: pressed.append(values.get("ui.advance_x")) or True
    assert recovery.run(release_only=True) is Recovered.RELEASED
    assert recovery.graveyard == (0.39, 0.60)
    assert recovery.run_spirit_healer() is Recovered.ALIVE
    assert talked == ["Spirit Healer"] and 0.5 in pressed
    walked.assert_not_called()                  # still beside it, no walk back


@pytest.mark.parametrize(("ghost_x", "stops"), [(0.30, True), (0.49, False)])
def test_a_ghost_the_walk_left_out_of_its_bodys_reach_stops_pressing(monkeypatch, ghost_x,
                                                                     stops):
    """V301: the server refuses a reclaim from beyond 39 yards whatever is pressed
    (`HandleReclaimCorpseOpcode`), and 13 of the hive's 171 corpse runs that ended still a
    ghost on 28 Sep pressed on from there for their 150 looks."""
    import jev.clients.recover
    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    ghost = {"vitals.dead": False, "vitals.ghost": True, "pos.mx": ghost_x, "pos.my": 0.5,
             "pos.corpse_mx": 0.5, "pos.corpse_my": 0.5}
    looks, pressed = [], []
    recovery = Recover(hid=None, read=lambda: looks.append(1) or dict(ghost),
                       walk_to=lambda point: False,
                       reach=lambda here, corpse: abs(here[0] - corpse[0]) <= 0.1)
    recovery._press = lambda values: pressed.append(1) or True
    assert recovery.run(tries=20) is Recovered.STILL_GHOST
    assert (pressed, len(looks)) == (([], 2) if stops else ([1] * 20, 21))
    assert ("out of the body's reach" in recovery.detail) is stops


def test_the_body_measures_the_reach_on_its_own_map():
    b = body()                                        # 100 yards a map fraction
    assert b._in_reclaim_reach((0.5, 0.5), (0.5, 0.88)) is True
    assert b._in_reclaim_reach((0.5, 0.5), (0.5, 0.9)) is False


def unreached_ghost(corpse=(0.5, 0.5)):
    from jev.world.state_v1 import Pos, Vitals

    return seen(pos=Pos(mx=0.5, my=0.5, corpse_mx=corpse[0], corpse_my=corpse[1], zone="zone"),
                vitals=Vitals(hp=0.0, dead=False, ghost=True))


def test_a_body_no_corpse_run_gets_up_at_is_left_for_the_spirit_healer(tmp_path, monkeypatch):
    """V301: two drowned orcs' bodies lay on the seabed off Ratchet, 60 to 70 yards under the
    ghosts walking the water over them, and every corpse run ended still a ghost, 29 and 34 of
    them in the hive's two hours to 11:11 on 28 Sep. Two in a row at one body, and the Spirit
    Healer raises the ghost; the next session remembers the body and where the ghost appeared."""
    from jev.coach.policy import Context
    from jev.world.state_v1 import Char

    b = body()
    b.purse_memory = tmp_path / "character-1.purse.json"
    b.policy_context = Context()
    b._over_body = lambda: None
    b._wait_out_sickness = lambda: 0.0
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE

    def release(release_only):
        b.recover.graveyard = (0.2, 0.5)             # the ghost appears at the graveyard
        return Recovered.RELEASED

    b.recover.run = release
    b._release(seen(char=Char(level=5)))
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.STILL_GHOST
    assert b._recover(unreached_ghost()).code == "still_ghost"
    assert b._recover(unreached_ghost((0.5, 0.52))).code == "still_ghost", "2 yards off: it"
    assert calls == ["corpse", "corpse"]

    later = body()                                   # the next session, begun as a ghost
    later.purse_memory = b.purse_memory
    later.policy_context = Context()
    later._wait_out_sickness = lambda: 0.0
    assert later.recover.graveyard is None
    seen_at = []
    later.recover.run_spirit_healer = lambda: (seen_at.append(later.recover.graveyard)
                                               or Recovered.ALIVE)
    later.recover.run = lambda corpse: pytest.fail("a third corpse run at the same body")
    result = later._recover(unreached_ghost())
    assert result.code == "alive" and "out of reach" in result.detail
    assert seen_at[0] == pytest.approx((0.2, 0.5)), "where the last session saw it appear"
    assert later._unreached is None and later._graveyard is None, "up: nothing to keep"


def test_a_ghost_twice_over_its_body_on_another_floor_finds_a_spirit_healer(monkeypatch):
    """V301: two orcs drowned off Ratchet, their bodies on the seabed under the ghosts, and
    released before V301 kept a graveyard: every session began as a ghost that had seen none.
    Two corpse runs "over the body and still a ghost: on another floor than the body" (V289),
    then the Spirit Healer, which refused at once, "no graveyard seen", and back to the body,
    run after run (the hive, 28 Sep 12:17-12:19). The healer is where the server sends a
    ghost of its side from where it stands: Ratchet's, the Barrens' own graveyard, not the
    Valley of Trials', nearer in a straight line."""
    import jev.clients.recover
    from jev.clients.recover import RETURN_TO_LIFE
    from jev.guide.coords import bounds_by_radio_id, map_to_world, world_to_map
    from jev.guide.path import Path, PathStatus
    from jev.world.state_v1 import Char, Pos, Vitals

    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: [])
    zones = bounds_by_radio_id("data/zones-tbc-243.json")
    b = body()
    b.client.bounds, b.client.coordinate_zones = zones[842], zones   # Durotar's map frame
    b._side, b._revived_at = "horde", None
    b._wait_out_sickness = lambda: 0.0
    body_at = (0.37330690473368977, 0.8115300286572622)             # Bildo's, in the Barrens
    world = {"at": body_at, "gossip": False, "modal": False, "alive": False}

    def read():
        if world["alive"]:
            return {"vitals.dead": False, "vitals.ghost": False, "pos.zone_id": 6112}
        values = {"vitals.dead": False, "vitals.ghost": True, "pos.zone_id": 6112,
                  "pos.mx": world["at"][0], "pos.my": world["at"][1],
                  "pos.corpse_mx": body_at[0], "pos.corpse_my": body_at[1]}
        if world["gossip"]:
            values["ui.gossip"] = True
        if world["modal"]:
            values.update({"ui.modal": True, "ui.advance_x": 0.5, "ui.advance_y": 0.3})
        return values

    b.client.read, b.client.position = read, lambda: world["at"]
    # Under the ghost, the water at 0.1 over a seabed the planner reaches from no floor (V289).
    b.client._ground = None
    b.client.query = SimpleNamespace(
        path=lambda m, start, end: Path(PathStatus.COMPLETE, ((start[0], start[1], 0.1),)))
    b._body_height = lambda world_point: -65.2
    floors, walked, talked, chosen = [], [], [], []
    b.client._next_floor = lambda around=None: floors.append(around)
    b._corpse_walk = lambda point: True                 # over the body: the walk arrives
    b.recover.walk_to = lambda point: walked.append(point) or world.update(at=point) or True
    b.recover.interact = lambda name: talked.append(name) or world.update(gossip=True) or "gossip"
    b.recover.choose = lambda title: (chosen.append(title), world.update(gossip=False, modal=True),
                                      SimpleNamespace(ok=True))[-1]
    b.recover._press = lambda values: bool(values.get("ui.modal")) and not world.update(alive=True)
    said = []
    b.say = said.append
    cx, cy = body_at
    ghost = seen(char=Char(level=5, faction="horde"), vitals=Vitals(hp=0.0, dead=False, ghost=True),
                 pos=Pos(mx=cx, my=cy, corpse_mx=cx, corpse_my=cy, zone="Barrens"))
    for run in (1, 2):
        assert b._recover(ghost).code == "still_ghost", f"corpse run {run}"
        assert "  over the body and still a ghost: on another floor than the body" in said
        assert len(floors) == run and not talked
    assert b.recover.graveyard is None, "a session begun as a ghost saw no graveyard"
    result = b._recover(ghost)
    assert (result.code, talked, chosen) == ("alive", ["Spirit Healer"], [RETURN_TO_LIFE])
    ratchet = map_to_world(*walked[0], b.client.bounds)
    assert ratchet == pytest.approx((-1081.4, -3478.7), abs=0.5), "the Barrens' graveyard"
    assert world_to_map(*ratchet, b.client.bounds) == pytest.approx(walked[0])
    assert b._unreached is None, "up: nothing left unreached"


def test_another_body_is_walked_to_afresh(monkeypatch):
    """V301: the count is the body's, not the character's: a new death is a new corpse run."""
    b = body()
    b._over_body = lambda: None
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.STILL_GHOST
    b._recover(unreached_ghost())
    b._recover(unreached_ghost((0.5, 0.7)))          # 20 yards off: another body
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.ALIVE
    assert b._recover(unreached_ghost((0.5, 0.7))).code == "alive"
    assert calls == ["corpse", "corpse", "corpse"]


@pytest.mark.parametrize(("since_revived", "healer"), [(60.0, True), (600.0, False), (None, False)])
def test_a_body_that_killed_the_character_again_is_left_for_the_spirit_healer(
        monkeypatch, since_revived, healer):
    from jev.clients.hearth import Hearthed
    from jev.run.body import DEATH_TRAP_S

    b = body()
    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.time", lambda: now)     # the wall's clock (V247)
    b._revived_at = None if since_revived is None else now - since_revived
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.ALIVE
    b.hearth.run = lambda: calls.append("hearth") or Hearthed.HOME
    result = b._recover(seen())
    assert result.code == "alive"
    assert calls == (["healer", "hearth"] if healer else ["corpse"])
    assert (b._revived_at is None) if healer else (b._revived_at == now)
    assert DEATH_TRAP_S > 60.0



@pytest.mark.parametrize(("home_yards", "hearths"), [(300.0, True), (3500.0, False)])
def test_up_at_the_spirit_healer_home_only_when_home_is_near_the_work(
        tmp_path, monkeypatch, home_yards, hearths):
    """V279: with its stone bound at Sentinel Hill and its grind on Elwynn's Prowlers (V276),
    a death trap would have hearthed the level 12 mage 3,500 yards from its work."""
    from jev.clients.hearth import Hearthed
    from jev.world.home import save_home

    b = body()
    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.time", lambda: now)
    b._revived_at = now - 60.0                              # a body that killed it again
    node = b.graph.get(seen().guide.step_id) or b.graph.nodes[0]
    b.home_memory = tmp_path / "home.json"
    save_home(b.home_memory, (node.world[0] + home_yards, node.world[1], 0.0), name="an inn")
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.hearth.run = lambda: calls.append("hearth") or Hearthed.HOME
    result = b._recover(seen())
    assert calls == (["healer", "hearth"] if hearths else ["healer"])
    assert ("walking on" in result.detail) is not hearths

def test_a_revival_is_remembered_by_the_next_session(tmp_path, monkeypatch):
    """V247: session 219 began with the get-up at the end of 218 forgotten, got up at the
    body again and died."""
    from jev.coach.policy import Context

    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.time", lambda: now)
    b = body()
    b.purse_memory = tmp_path / "character-1.purse.json"
    b.policy_context = Context()
    b._revived(now - 60.0)
    later = body()
    later.purse_memory = b.purse_memory
    later.policy_context = Context()
    assert later._revived_at == now - 60.0
    calls = []
    later.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    later.recover.run = lambda corpse: calls.append("corpse") or Recovered.ALIVE
    from jev.clients.hearth import Hearthed

    later.hearth.run = lambda: calls.append("hearth") or Hearthed.HOME
    later._wait_out_sickness = lambda: 0.0
    later._recover(seen())
    assert calls[0] == "healer", "died again inside the trap's minutes: not at the body"


def ghost_at(corpse_world, bounds):
    from jev.guide.coords import world_to_map
    from jev.world.state_v1 import Pos, Vitals

    cx, cy = world_to_map(*corpse_world, bounds)
    gx, gy = world_to_map(corpse_world[0] - 150.0, corpse_world[1], bounds)
    return seen(pos=Pos(mx=gx, my=gy, corpse_mx=cx, corpse_my=cy, zone="Elwynn"),
                vitals=Vitals(hp=0.0, dead=False, ghost=True))


@pytest.mark.parametrize(("camp", "expected"), [
    ([(0.0, 0.0), (20.0, 0.0), (-20.0, 0.0), (0.0, 20.0), (0.0, -20.0),
      (18.0, 18.0), (-18.0, 18.0), (18.0, -18.0), (-18.0, -18.0),
      (40.0, 0.0), (-40.0, 0.0), (0.0, 40.0), (0.0, -40.0)], "healer"),
    ([(0.0, 5.0)], "corpse"),                  # one wolf beside it: a spot 25 yards off is clear
    ([], "corpse"),                            # nothing hostile near
])
def test_a_body_in_a_camp_is_got_up_from_at_the_spirit_healer(monkeypatch, camp, expected):
    """V247: 15 of the mage's 22 get-ups at the body died again, a median of 39 s later,
    and 2 of its 12 at the Spirit Healer (sessions 195-219). No hearthstone for it: the stone
    is kept for a wedge."""
    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._revived_at = None
    b._side = "alliance"
    corpse = (-9000.0, 100.0)
    spawns = [(corpse[0] + dx, corpse[1] + dy, 60.0) for dx, dy in camp]
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: list(spawns))
    b._wait_out_sickness = lambda: 0.0
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.ALIVE
    b.hearth.run = lambda: calls.append("hearth") or None
    assert b._recover(ghost_at(corpse, b.client.bounds)).code == "alive"
    assert calls == [expected]


def test_a_ghost_gets_up_clear_of_hostile_spawns_on_a_step_with_none_of_its_own(monkeypatch):
    """V247: on a travel step the step names no spawns, and the mage got up 25 yards short
    of its body among the Mangy Wolves that had killed it (session 219)."""
    import math

    from jev.guide.coords import map_to_world, world_to_map

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._side = "alliance"
    b.hunt_spawns = {}
    corpse = (-9000.0, 100.0)
    wolves = [(-9020.0, 100.0, 60.0), (-9015.0, 110.0, 60.0)]    # on the graveyard's side
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: list(wolves))
    b.recover.graveyard = world_to_map(-9100.0, 100.0, b.client.bounds)
    walked = []
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True
    assert b._short_of_body(world_to_map(*corpse, b.client.bounds)) is True
    spot = walked[0]
    assert math.dist(spot[:2], corpse) == pytest.approx(25.0, abs=0.5)
    assert min(math.dist(spot[:2], w[:2]) for w in wolves) > 25.0, "not among the wolves"


def test_a_hearthstone_still_cooling_is_not_pressed(tmp_path, monkeypatch):
    """V253: 10 of the mage's 14 presses in sessions 205-217 met a stone still cooling,
    about 20 s each, and a wedge pressed it walk after walk."""
    from jev.clients.hearth import Hearthed
    from jev.coach.policy import Context
    from jev.run.body import HEARTH_COOLDOWN_S, HEARTH_RETRY_S

    now = [10_000.0]
    monkeypatch.setattr("jev.run.body.time.time", lambda: now[0])
    b = body()
    b.purse_memory = tmp_path / "character-1.purse.json"
    b.policy_context = Context()
    b.client.position = lambda: None
    pressed = []
    b.hearth.run = lambda: pressed.append(now[0]) or Hearthed.HOME
    assert b._go_home() is Hearthed.HOME
    now[0] += 600.0
    assert b._go_home() is Hearthed.NOT_READY and len(pressed) == 1, "cooling: not pressed"
    later = body()                                   # the next session knows it
    later.purse_memory = b.purse_memory
    later.policy_context = Context()
    later.hearth.run = lambda: pressed.append(now[0]) or Hearthed.HOME
    assert later._go_home() is Hearthed.NOT_READY and len(pressed) == 1
    now[0] += HEARTH_COOLDOWN_S
    later.client.position = lambda: None
    assert later._go_home() is Hearthed.HOME and len(pressed) == 2
    later.hearth.run = lambda: pressed.append(now[0]) or Hearthed.NOT_READY
    now[0] += HEARTH_COOLDOWN_S
    assert later._go_home() is Hearthed.NOT_READY and len(pressed) == 3
    now[0] += HEARTH_RETRY_S / 2
    assert later._go_home() is Hearthed.NOT_READY and len(pressed) == 3, "tried again later"


def test_a_service_blocked_for_the_step_does_not_stop_its_grind():
    """V194: session 156's repair walk timed out and repairs were blocked for the step
    (V185), but the grind asked for one from inside the hunt, with no step named, and
    stopped for "durability is low" seventeen times."""
    from jev.world.state_v1 import Bags

    b = body()
    worn = seen(bags=Bags(free=20, durability_min=0.2, money_copper=5000))
    b.client.state = lambda: worn
    assert b._service_needed() == "durability is low", "no block: the repair is wanted"
    b.policy_context.repair_unreachable(b.arm.step_id)
    assert b._service_needed() is None, "blocked for this step: the grind goes on"


@pytest.mark.parametrize(("killer", "healer"), [(6, True), (4, False), (None, False)])
def test_a_character_killed_by_a_far_stronger_unit_gets_up_at_the_spirit_healer(killer, healer):
    """V197: a level 3 mage stranded among level 5-6 Mangy Wolves got up beside its body,
    among them, and died five times in one session."""
    from jev.clients.hearth import Hearthed
    from jev.world.state_v1 import Char

    b = body()
    b._revived_at = None
    b.fight._target_level = killer
    b._wait_out_sickness = lambda: 0.0
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.recover.run = lambda corpse: calls.append("corpse") or Recovered.ALIVE
    b.hearth.run = lambda: calls.append("hearth") or Hearthed.HOME
    ghost = seen(char=Char(level=3))
    assert b._recover(ghost).code == "alive"
    assert calls == (["healer", "hearth"] if healer else ["corpse"])


def near_to(spawns):
    """`hostiles.near` over a fixed list of (x, y, z, extra) spawns: those inside the radius."""
    import math

    return lambda map_id, x, y, radius, **kw: [s for s in spawns
                                               if math.dist(s[:2], (x, y)) <= radius]


# Raven Hill's graveyard as the hive's level 7 human met it: its level 23-25 spawns 29 to 36
# yards off, each reaching a level 7 17 yards beyond the ordinary (V300).
RAVEN_HILL = ((29.0, -15.0), (36.0, -17.0), (42.0, -39.0))


@pytest.mark.parametrize(("why", "spawns", "expected"), [
    ("killed by a level 24 at the graveyard", RAVEN_HILL, ["corpse"]),
    ("killed by a level 24, the graveyard clear", (), ["healer", "hearth"]),
    ("a body that killed it again, at the graveyard", RAVEN_HILL, ["corpse"]),
])
def test_the_spirit_healer_is_no_way_out_from_a_graveyard_in_a_camp(monkeypatch, why, spawns,
                                                                     expected):
    """V300: a level 7 human died at Raven Hill's graveyard to a level 24, and every get-up at
    its Spirit Healer, chosen for a stronger killer or a body that killed it again, was
    attacked a median 2 s later: 67 deaths in two hours (the hive, 28 Sep). A spot 25 yards
    from the body, on the far side from the graveyard's units, was out of their reach."""
    import math

    from jev.clients.hearth import Hearthed
    from jev.guide.coords import map_to_world, world_to_map
    from jev.world.state_v1 import Char, Pos, Vitals

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._side = "alliance"
    b._wait_out_sickness = lambda: 0.0
    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.time", lambda: now)
    trapped = why.startswith("a body that killed it again")
    b._revived_at = now - 60.0 if trapped else None
    b.fight._target_level = None if trapped else 24
    graveyard = (-9000.0, 100.0)
    corpse = (graveyard[0] - 3.0, graveyard[1])              # it died beside the healer
    units = [(graveyard[0] + dx, graveyard[1] + dy, 30.0, 17.0) for dx, dy in spawns]
    monkeypatch.setattr("jev.run.body.hostiles.near", near_to(units))
    b.recover.graveyard = world_to_map(*graveyard, b.client.bounds)
    cx, cy = world_to_map(*corpse, b.client.bounds)
    ghost = seen(char=Char(level=7), vitals=Vitals(hp=0.0, dead=False, ghost=True),
                 pos=Pos(mx=b.recover.graveyard[0], my=b.recover.graveyard[1],
                         corpse_mx=cx, corpse_my=cy, zone="Duskwood"))
    calls, walked = [], []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.hearth.run = lambda: calls.append("hearth") or Hearthed.HOME

    def run(corpse_point):
        calls.append("corpse")
        b.recover.walk_to(corpse_point)
        return Recovered.ALIVE

    b.recover.run = run
    b.recover.corpse = (cx, cy)
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True
    assert b._recover(ghost).code == "alive"
    assert calls == expected
    assert bool(walked) is (expected == ["corpse"])
    if walked:
        spot = walked[0]
        assert min(math.dist(spot[:2], u[:2]) - u[3] for u in units) >= 18.0, \
            "got up out of the graveyard's units' reach"


def test_of_two_camps_the_ghost_gets_up_in_the_roomier(monkeypatch):
    """V300 beside V247: a body in a camp is left for the Spirit Healer only while the
    healer's graveyard has more room than the body's spot."""
    from jev.guide.coords import world_to_map

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._side = "alliance"
    b._revived_at = None
    b._wait_out_sickness = lambda: 0.0
    corpse = (-9000.0, 100.0)
    camp = [(corpse[0] + dx, corpse[1] + dy, 60.0, 0.0) for dx, dy in (
        (0.0, 0.0), (20.0, 0.0), (-20.0, 0.0), (0.0, 20.0), (0.0, -20.0), (18.0, 18.0),
        (-18.0, 18.0), (18.0, -18.0), (-18.0, -18.0), (40.0, 0.0), (-40.0, 0.0), (0.0, 40.0),
        (0.0, -40.0))]
    graveyard = (corpse[0] - 300.0, corpse[1])
    for crowded, expected in ((False, "healer"), (True, "corpse")):
        units = camp + ([(graveyard[0] + 4.0, graveyard[1], 60.0, 5.0)] if crowded else [])
        monkeypatch.setattr("jev.run.body.hostiles.near", near_to(units))
        b.recover.graveyard = world_to_map(*graveyard, b.client.bounds)
        calls = []
        b.recover.run_spirit_healer = lambda calls=calls: (calls.append("healer")
                                                           or Recovered.ALIVE)
        b.recover.run = lambda corpse_point, calls=calls: (calls.append("corpse")
                                                           or Recovered.ALIVE)
        b._corpse_walk = lambda point: True
        assert b._recover(ghost_at(corpse, b.client.bounds)).code == "alive"
        assert calls == [expected], "a graveyard 4 yards from a spawn is the smaller camp"


def test_resurrection_sickness_is_waited_out_before_going_on(monkeypatch):
    """V189: walking out under the sickness, a level 13 paladin met a Dust Devil 90 s
    after getting up at the Spirit Healer and died (session 150)."""
    from jev.run.body import SICKNESS_WAIT_MAX_S

    b = body()
    clock = {"t": 1000.0}
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("jev.run.body.time.sleep", lambda s: clock.update(t=clock["t"] + s))
    readings = {"char.level": 13, "vitals.combat": False}
    b._read = lambda: dict(readings)
    assert 180.0 <= b._wait_out_sickness() < 182.0, "a minute a level above ten"
    readings["char.level"] = 25
    assert b._wait_out_sickness() < SICKNESS_WAIT_MAX_S + 2.0, "inside the corpse run's time"
    readings["char.level"] = 9
    assert b._wait_out_sickness() == 0.0, "no sickness at level 10 and below"
    readings.update({"char.level": 13, "vitals.combat": True})
    assert b._wait_out_sickness() < 2.0, "an attack ends the wait"


def test_a_unit_with_no_nameplate_on_show_is_talked_to_where_a_hover_finds_it():
    """The Spirit Healer's plate was behind the strip (run 20260923T182125-9c54ea)."""
    from jev.clients.targeting import HoverCode, HoverResult
    from jev.perceive.radio_frame import name_id

    b = body()
    b.interact = SimpleNamespace(open_on=lambda name: Interacted.NOT_VISIBLE)
    hovered = []

    def probe(point, require_target=True):
        hovered.append(point)
        on = len(hovered) == 2
        return HoverResult(HoverCode.OTHER, point, None,
                           {"cursor.has": on, "cursor.name_id": name_id("Spirit Healer") if on else None},
                           "fixture")

    b.targeting.probe = probe
    b._read = lambda: {"ui.modal": True}
    clicks = []
    b.client.hid.click = lambda x, y, right=False: clicks.append((x, y, right)) or True
    assert b._talk_to("Spirit Healer") == "hovered"
    assert clicks == [(*hovered[1], True)], "right-clicked where the hover found it, once"


def test_a_unit_out_of_reach_is_stepped_toward_and_clicked_again():
    """Run 20260923T182544-7dad55: the right-click on the Spirit Healer answered "You are
    too far away!" and nothing else happened."""
    from jev.clients.targeting import HoverCode, HoverResult
    from jev.perceive.radio_frame import UI_ERROR_KEYS, name_id

    b = body()
    b.interact = SimpleNamespace(open_on=lambda name: Interacted.NOT_VISIBLE)
    healer = {"cursor.has": True, "cursor.name_id": name_id("Spirit Healer"), "ui.error_count": 4}
    b.targeting.probe = lambda point, require_target=True: HoverResult(
        HoverCode.OTHER, point, None, healer, "fixture")
    far = {"ui.error_last": UI_ERROR_KEYS.index("out_of_range"), "ui.error_count": 5}
    # Silence first: the error arrives after the server's reply, not with the first paint.
    answers = iter([{"ui.error_count": 4}, far, {"ui.modal": True, "ui.error_count": 5}])
    b._read = lambda: next(answers)
    clicks, steps = [], []
    b.client.hid.click = lambda x, y, right=False: clicks.append(right) or True
    b.client.hid.hold = lambda key, seconds, **_: steps.append(key) or True
    assert b._talk_to("Spirit Healer") == "hovered"
    assert clicks == [True, True] and steps == ["w"], "one step closer between two clicks"


def test_the_spirit_healer_is_asked_again_when_the_first_click_opens_nothing(monkeypatch):
    import jev.clients.recover
    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    ghost = {"vitals.ghost": True, "vitals.dead": False, "pos.mx": 0.39, "pos.my": 0.60}
    popup = {**ghost, "ui.modal": True, "ui.advance_x": 0.5, "ui.advance_y": 0.2}
    states = iter([ghost] + [ghost] * 4 + [popup, {"vitals.ghost": False, "vitals.dead": False}])
    talked = []
    recovery = Recover(hid=None, read=lambda: next(states),
                       interact=lambda name: talked.append(name) or "hovered")
    recovery.graveyard = (0.39, 0.60)
    recovery._press = lambda values: True
    assert recovery.run_spirit_healer() is Recovered.ALIVE
    assert talked == ["Spirit Healer", "Spirit Healer"], "asked again after a silent click"


def test_a_quest_at_a_body_is_opened_by_its_tooltip_then_advanced_as_ever():
    """Find the Lost Guards is handed in at A half-eaten body; Discover Rolf's Fate is
    taken from it."""
    b = body(StepKind.QUEST_TURNIN)
    node = b.graph.nodes[0].model_copy(update={"target_kind": "gameobject",
                                               "target_name": "A half-eaten body"})
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    events = []
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: pytest.fail("a body is not a unit"))
    b.gather = SimpleNamespace(open=lambda wanted: events.append(("open", wanted)) or True,
                               detail="")
    b.client.reading = lambda: SimpleNamespace(values={"ui.quest_frame": True,
                                                       "ui.advance_x": 0.4})
    b.advance = SimpleNamespace(run=lambda q, g: events.append(("advance", q, g)) or Advanced.DONE,
                                detail="confirmed")
    result = b.execute(b.arm, seen(), lambda: None)
    assert result.outcome.value == "succeeded"
    assert events == [("open", name_id("A half-eaten body")), ("advance", 1, Goal.CLEARED)]
    b.client.approach.assert_called_once()


def test_a_fight_says_the_corpse_is_looted_so_no_one_loots_it_again():
    """The tutor went on clicking corpses the fight had already emptied."""
    b = body(StepKind.QUEST_OBJECTIVE)
    b.fight = SimpleNamespace(run=lambda name: Fought.KILLED, last_plate=None, killed_name_id=7,
                              detail="selected plate on the centre line")
    b.loot = SimpleNamespace(run=lambda **kw: Looted.TOOK, detail="3 copper")
    result = b._fight(seen())
    assert result.outcome is SkillOutcome.SUCCEEDED
    assert "corpse looted: took - 3 copper" in result.detail


def test_a_kill_whose_corpse_is_not_found_is_still_a_kill():
    b = body(StepKind.QUEST_OBJECTIVE)
    b.fight = SimpleNamespace(run=lambda name: Fought.KILLED, last_plate=None, killed_name_id=7,
                              detail="selected plate on the centre line")
    b.loot = SimpleNamespace(run=lambda **kw: Looted.NO_CORPSE, detail="target observation: none")
    result = b._fight(seen())
    assert result.outcome is SkillOutcome.SUCCEEDED and "no_corpse" in result.detail
    b.loot = SimpleNamespace(run=lambda **kw: Looted.BAGS_FULL, detail="bags are full")
    assert b._fight(seen()).outcome is SkillOutcome.PREEMPTED, "full bags still call a vendor"


def test_a_trap_body_the_healer_will_not_raise_us_from_is_reclaimed_from_short_of_it(
        monkeypatch):
    """Up at the body among the Mangy Wolves three times running, the Spirit Healer
    answering nothing (run 20260924T041014-a9781c). A body is reclaimed from inside 39
    yards and the character stands where the ghost stood."""
    import math

    from jev.guide.coords import map_to_world, world_to_map
    from jev.run.body import TRAP_RECLAIM_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.time", lambda: now)
    b._revived_at = now - 30.0
    corpse = world_to_map(-9000.0, 100.0, b.client.bounds)
    b.recover.graveyard = world_to_map(-9100.0, 100.0, b.client.bounds)
    b.recover.corpse = corpse
    b.recover.run_spirit_healer = lambda: Recovered.STILL_GHOST
    walked = []
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True

    def run(corpse_point):
        b.recover.walk_to(corpse_point)
        return Recovered.ALIVE

    b.recover.run = run
    original = b.recover.walk_to
    assert b._recover(seen()).code == "alive"
    assert len(walked) == 1
    assert math.dist(walked[0], (-9000.0, 100.0)) == pytest.approx(TRAP_RECLAIM_YARDS, abs=0.5)
    assert walked[0][0] < -9000.0, "on the graveyard's side"
    assert b.recover.walk_to is original, "the normal walk is restored"


def test_every_body_is_reclaimed_short_of_it_from_where_the_ghost_stands(monkeypatch):
    """Up at the body itself, among the wolves that killed it, four times in two sessions
    (runs 20260924T045140-ec8686 and ...050644-f9f9fa); the second session started as a
    ghost and never saw the graveyard."""
    import math

    from jev.guide.coords import map_to_world, world_to_map
    from jev.run.body import TRAP_RECLAIM_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._revived_at = None
    b.recover.corpse = world_to_map(-9000.0, 100.0, b.client.bounds)
    b.client.position = lambda: world_to_map(-9000.0, 300.0, b.client.bounds)
    walked = []
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True

    def run(corpse_point):
        b.recover.walk_to(corpse_point)
        return Recovered.ALIVE

    b.recover.run = run
    assert b._recover(seen()).code == "alive"
    assert len(walked) == 1
    assert math.dist(walked[0], (-9000.0, 100.0)) == pytest.approx(TRAP_RECLAIM_YARDS, abs=0.5)
    assert walked[0][1] > 100.0, "on the side the ghost comes from"


def test_a_ghost_that_does_not_get_up_short_of_the_body_goes_closer(monkeypatch):
    """V213: on Sentinel Hill's slope a ghost 32 yards short (34 by the walk's slack) was out
    of the body's 39-yard reach with the height, and walked there again and again, "still
    a ghost" (session 178). Each get-up that does not come halves the next stop."""
    import math

    from jev.guide.coords import map_to_world, world_to_map
    from jev.run.body import TRAP_RECLAIM_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._revived_at = None
    b.recover.corpse = world_to_map(-9000.0, 100.0, b.client.bounds)
    b.client.position = lambda: world_to_map(-9000.0, 300.0, b.client.bounds)
    walked = []
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True
    outcomes = iter([Recovered.STILL_GHOST, Recovered.STILL_GHOST, Recovered.ALIVE])

    def run(corpse_point):
        b.recover.walk_to(corpse_point)
        return next(outcomes)

    b.recover.run = run
    for _ in range(3):
        b._recover(seen())
    shorts = [math.dist(w, (-9000.0, 100.0)) for w in walked]
    assert shorts == pytest.approx([TRAP_RECLAIM_YARDS, TRAP_RECLAIM_YARDS / 2,
                                    TRAP_RECLAIM_YARDS / 4], abs=0.5)
    assert b._reclaim_yards == TRAP_RECLAIM_YARDS, "up at last: the next death starts afresh"


def test_a_get_up_spot_on_another_floor_than_the_body_is_no_spot():
    """V289: the get-up spot 25 yards short of a body in Shadowthread Cave was on the hill
    over it, 110 yards up, and the ghost walked there and never got up (the hive, 28 Sep).
    A spot whose floor is further from the body's than the reach allows: to the body."""
    import math

    from jev.guide.coords import map_to_world, world_to_map
    from jev.guide.path import Path, PathStatus
    from jev.run.body import TRAP_RECLAIM_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._revived_at = None
    b.recover.corpse = world_to_map(-9000.0, 100.0, b.client.bounds)
    b.client.position = lambda: world_to_map(-9000.0, 300.0, b.client.bounds)
    b._body_height = lambda world: 55.0

    def mesh(floor_off_the_body):
        def path(map_id, start, end):
            x, y, _ = start
            near = math.dist((x, y), (-9000.0, 100.0)) < 10.0
            return Path(PathStatus.COMPLETE, ((x, y, 50.0 if near else floor_off_the_body),))
        return SimpleNamespace(path=path)

    walked = []
    b._corpse_walk = lambda point: walked.append(map_to_world(*point, b.client.bounds)) or True

    def run(corpse_point):
        b.recover.walk_to(corpse_point)
        return Recovered.ALIVE

    b.recover.run = run
    b.client.query = mesh(160.0)                         # the hill over the cave
    assert b._recover(seen()).code == "alive"
    assert math.dist(walked[-1], (-9000.0, 100.0)) < 0.5, "to the body itself"
    b.client.query = mesh(56.0)                          # a slope: the same floor
    b._recover(seen())
    assert math.dist(walked[-1], (-9000.0, 100.0)) == pytest.approx(TRAP_RECLAIM_YARDS, abs=0.5)


def test_a_ghost_over_its_body_that_does_not_get_up_walks_from_another_floor():
    """V289: on the hill over Shadowthread Cave the ghost stood 0.4 yards from its body on
    the map, 110 yards above it; every plan from there started on the cave's floor under it
    and arrived at once, forty minutes of "still a ghost" (the hive, 28 Sep). Over the body
    and not up: the next walk is to the body itself, from the next floor under the ghost,
    the floor the plans started on counted as tried."""
    from jev.guide.coords import world_to_map
    from jev.guide.path import Path, PathStatus
    from jev.run.body import TRAP_RECLAIM_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._revived_at = None
    b.recover.corpse = world_to_map(-9000.0, 100.0, b.client.bounds)
    b.client.position = lambda: world_to_map(-9000.3, 100.2, b.client.bounds)
    b.client._ground = None
    b.client.query = SimpleNamespace(
        path=lambda m, start, end: Path(PathStatus.COMPLETE, ((start[0], start[1], 50.0),)))
    b._body_height = lambda world: 55.0
    turned = []
    b.client._next_floor = lambda around=None: turned.append((around, b.client._ground[2]))
    b._corpse_walk = lambda point: True

    def run(corpse_point):
        b.recover.walk_to(corpse_point)
        return Recovered.STILL_GHOST

    b.recover.run = run
    assert b._recover(seen()).code == "still_ghost"
    assert b._reclaim_yards == 0.0, "the next walk is to the body itself"
    assert turned == [(55.0, 50.0)], "round the body's height, the floor tried counted"

    far = body()
    far.client.bounds = b.client.bounds
    far._revived_at = None
    far.recover.corpse = b.recover.corpse
    far.client.position = lambda: world_to_map(-9000.0, 300.0, far.client.bounds)
    far._corpse_walk = lambda point: True
    far.recover.run = lambda corpse_point: (far.recover.walk_to(corpse_point),
                                            Recovered.STILL_GHOST)[1]
    far._recover(seen())
    assert far._reclaim_yards == TRAP_RECLAIM_YARDS / 2, "short of it: V213's halving only"


def test_a_ghost_gets_up_out_of_the_camps_reach():
    """V233: the mage got up beside its body at Fargodeep with half its health and mana,
    among the Kobold Tunnelers that had killed it, and died again (sessions 196-197)."""
    import math

    from jev.run.body import REST_CLEAR_YARDS, reclaim_spot

    body_at, short = (0.0, 0.0), (0.0, 25.0)           # the graveyard lies north
    assert reclaim_spot(body_at, short, (), 25.0) == short, "no spawns known: as before"
    far = ((0.0, -60.0, 0.0),)
    assert reclaim_spot(body_at, short, far, 25.0) == short, "already clear of them"
    camp = ((0.0, 40.0, 0.0), (15.0, 35.0, 0.0), (-15.0, 35.0, 0.0))   # between it and the graveyard
    spot = reclaim_spot(body_at, short, camp, 25.0)
    assert math.dist(spot, body_at) == pytest.approx(25.0), "still within the body's reach"
    assert min(math.dist(spot, c[:2]) for c in camp) >= REST_CLEAR_YARDS
    assert spot[1] < 0, "the far side of the body from the camp"


def test_a_ghost_already_inside_the_short_ring_stays_where_it_is():
    from jev.guide.coords import world_to_map

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b.client.position = lambda: world_to_map(-9000.0, 120.0, b.client.bounds)
    b._corpse_walk = lambda point: pytest.fail("walked in to the body")
    assert b._short_of_body(world_to_map(-9000.0, 100.0, b.client.bounds)) is True


def test_a_right_click_nothing_answers_is_out_of_reach_too():
    """Six hover-proved clicks on the Spirit Healer opened nothing and raised no error: the
    server drops a right-click from beyond five yards without a word."""
    from jev.clients.targeting import HoverCode, HoverResult
    from jev.perceive.radio_frame import name_id

    b = body()
    b.interact = SimpleNamespace(open_on=lambda name: Interacted.NOT_VISIBLE)
    healer = {"cursor.has": True, "cursor.name_id": name_id("Spirit Healer"), "ui.error_count": 4}
    b.targeting.probe = lambda point, require_target=True: HoverResult(
        HoverCode.OTHER, point, None, healer, "fixture")
    clicks, steps = [], []
    b._read = lambda: {"ui.gossip": len(steps) >= 2, "ui.error_count": 4}
    b.client.hid.click = lambda x, y, right=False: clicks.append(right) or True
    b.client.hid.hold = lambda key, seconds, **_: steps.append(key) or True
    assert b._talk_to("Spirit Healer") == "hovered"
    assert clicks == [True, True, True] and steps == ["w", "w"], "stepped in until it answered"


def test_a_right_click_that_never_answers_is_not_reported_as_talked_to():
    from jev.clients.targeting import HoverCode, HoverResult
    from jev.perceive.radio_frame import name_id
    from jev.run.body import HOVER_STEPS

    b = body()
    b.interact = SimpleNamespace(open_on=lambda name: Interacted.NOT_VISIBLE)
    healer = {"cursor.has": True, "cursor.name_id": name_id("Spirit Healer")}
    b.targeting.probe = lambda point, require_target=True: HoverResult(
        HoverCode.OTHER, point, None, healer, "fixture")
    b._read = lambda: {}
    clicks = []
    b.client.hid.click = lambda x, y, right=False: clicks.append(right) or True
    b.client.hid.hold = lambda key, seconds, **_: True
    assert b._talk_to("Spirit Healer") is Interacted.NOT_VISIBLE
    assert len(clicks) == HOVER_STEPS + 1


def test_the_spirit_healers_gossip_line_is_chosen_to_raise_its_popup(monkeypatch):
    """Menu 83 in the world database: one line, "Return me to life.", and only choosing it
    raises the popup."""
    import jev.clients.recover
    from jev.clients.recover import RETURN_TO_LIFE
    monkeypatch.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
    ghost = {"vitals.ghost": True, "vitals.dead": False, "pos.mx": 0.39, "pos.my": 0.60}
    states = iter([ghost, {**ghost, "ui.gossip": True},
                   {**ghost, "ui.modal": True, "ui.advance_x": 0.5, "ui.advance_y": 0.2},
                   {"vitals.ghost": False, "vitals.dead": False}])
    chosen, pressed = [], []
    recovery = Recover(hid=None, read=lambda: next(states), interact=lambda name: "hovered",
                       choose=lambda title: chosen.append(title) or "chose")
    recovery.graveyard = (0.39, 0.60)
    recovery._press = lambda values: pressed.append(values.get("ui.advance_x")) or True
    assert recovery.run_spirit_healer() is Recovered.ALIVE
    assert chosen == [RETURN_TO_LIFE] == ["Return me to life."] and pressed == [0.5]


@pytest.mark.parametrize(("durability", "repairer_x", "hearths"), [
    (0.0, 400.0, True), (0.0, 60.0, False), (0.5, 400.0, False)])
def test_broken_gear_far_from_a_repairer_goes_home_by_hearthstone_first(
        durability, repairer_x, hearths):
    """A level 6 paladin at full health lost the first fight on its 310-yard walk to a
    repairer with broken gear (run 20260924T042040-e86c88)."""
    from jev.clients.hearth import Hearthed
    from jev.world.state_v1 import Bags

    b = body()
    b.client.bounds = ZoneBounds(1, 0, 1000, 0, 1000, 0)
    b.client.position = lambda: (0.5, 0.5)
    from jev.guide.coords import map_to_world

    here = map_to_world(0.5, 0.5, b.client.bounds)
    base = b.graph.nodes[0]
    repairer = base.model_copy(update={"id": "armourer", "kind": StepKind.REPAIR,
                                       "world": (here[0] + repairer_x, here[1], 0),
                                       "target_name": "Godric Rothgar"})
    b.graph = Graph(graph_id="g", faction="alliance", entry="quest", nodes=(base, repairer))
    calls = []
    b.hearth = SimpleNamespace(run=lambda: calls.append("hearth") or Hearthed.HOME, detail="")
    b.repair = SimpleNamespace(run=lambda: calls.append("repair") or Repaired.DONE, detail="")
    b._repair(seen(bags=Bags(durability_min=durability, free=5)))
    assert calls == (["hearth", "repair"] if hearths else ["repair"])



@pytest.mark.parametrize(("home_x", "by_home_x", "repairer_x", "hearths"), [
    (3500.0, None, 232.0, False),     # home far off where the guide was: walk to the repairer
    (300.0, 310.0, 600.0, True),      # home near, an armourer by it: home first
    (300.0, None, 600.0, False)])     # home near, no armourer by it: walk
def test_broken_gear_goes_home_only_when_home_is_the_way_to_a_repairer(
        tmp_path, home_x, by_home_x, repairer_x, hearths):
    """V278: back on Elwynn's Prowlers with its stone bound at Sentinel Hill, the level 12
    mage hearthed 3,500 yards from a repairer 232 yards off (session 266)."""
    from jev.clients.hearth import Hearthed
    from jev.guide.coords import map_to_world
    from jev.world.home import save_home
    from jev.world.state_v1 import Bags

    b = body()
    b.client.bounds = ZoneBounds(1, 0, 1000, 0, 1000, 0)     # no catalog repairer inside
    b.client.position = lambda: (0.5, 0.5)
    here = map_to_world(0.5, 0.5, b.client.bounds)
    b.home_memory = tmp_path / "home.json"
    save_home(b.home_memory, (here[0] + home_x, here[1], 0.0), name="an inn")
    base = b.graph.nodes[0]
    nodes = [base, base.model_copy(update={"id": "armourer", "kind": StepKind.REPAIR,
                                           "world": (here[0] + repairer_x, here[1], 0),
                                           "target_name": "Godric Rothgar"})]
    if by_home_x is not None:
        nodes.append(base.model_copy(update={"id": "home armourer", "kind": StepKind.REPAIR,
                                             "world": (here[0] + by_home_x, here[1], 0),
                                             "target_name": "Kirk Maxwell"}))
    b.graph = Graph(graph_id="g", faction="alliance", entry="quest", nodes=tuple(nodes))
    calls = []
    b.hearth = SimpleNamespace(run=lambda: calls.append("hearth") or Hearthed.HOME, detail="")
    b.repair = SimpleNamespace(run=lambda: calls.append("repair") or Repaired.DONE, detail="")
    b._repair(seen(bags=Bags(durability_min=0.0, free=5)))
    assert calls == (["hearth", "repair"] if hearths else ["repair"])

def test_a_meal_is_taken_out_of_reach_of_the_camps_spawns():
    """Eating in the middle of the wolf camp was bitten at 26% health (run
    20260924T053651-ac99b2)."""
    import math

    from jev.run.body import REST_CLEAR_YARDS, rest_spot

    camp = [(0.0, 0.0, 40.0), (10.0, 0.0, 40.0), (0.0, 10.0, 40.0), (40.0, 40.0, 41.0)]
    spot = rest_spot((3.0, 3.0), camp)
    assert spot is not None
    assert all(math.dist(spot[:2], s[:2]) >= REST_CLEAR_YARDS for s in camp)
    assert math.dist(spot[:2], (3.0, 3.0)) <= 30.0, "the nearest ring with room"
    assert rest_spot((-40.0, -40.0), camp) is None, "already clear of them"


def test_a_meal_on_a_step_with_no_spawns_walks_clear_of_hostile_ones(monkeypatch):
    """V247: all ten attacks on the resting or reviving mage began within 20 yards of a
    hostile spawn, and a travel or quest step names none of its own (sessions 195-219)."""
    import math

    from jev.guide.coords import world_to_map
    from jev.run.body import REST_CLEAR_YARDS

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b.client.position = lambda: world_to_map(-9000.0, 100.0, b.client.bounds)
    b._side = "alliance"
    b.hunt_spawns = {}
    wolves = [(-9005.0, 100.0, 60.0), (-9000.0, 108.0, 60.0)]
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: list(wolves))
    walked = []
    b._approach = lambda point: walked.append(point) or True
    b._clear_of_spawns()
    assert len(walked) == 1
    assert all(math.dist(walked[0][:2], w[:2]) >= REST_CLEAR_YARDS for w in wolves)
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: [])
    b._clear_of_spawns()
    assert len(walked) == 1, "nothing hostile near: eaten where it stands"


def test_the_rest_walks_clear_first_only_when_the_step_has_spawns():
    from jev.guide.coords import world_to_map

    b = body(StepKind.GRIND)
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b.client.position = lambda: world_to_map(-9000.0, 100.0, b.client.bounds)
    walked = []
    b._approach = lambda point: walked.append(point) or True
    b.hunt_spawns = {"quest": ((-9000.0, 105.0, 40.0), (-9005.0, 100.0, 40.0))}
    b._clear_of_spawns()
    assert len(walked) == 1
    b.hunt_spawns = {}
    b._clear_of_spawns()
    assert len(walked) == 1, "no spawns known, no walk"


def test_two_wedged_walks_running_go_home_by_hearthstone():
    """Against a barrel inside Northshire Abbey, walk after walk ended "could not free the
    character" (run 20260924T074713-f215ef)."""
    from jev.clients.hearth import Hearthed
    from jev.run.body import WEDGED_WALKS

    b = body()
    homes = []
    b.hearth = SimpleNamespace(run=lambda: homes.append(1) or Hearthed.HOME, detail="")
    wedged = SimpleNamespace(detail="leg 1 of 24: could not free the character")
    b.client.approach = lambda world, timeout_s=0: False
    b.client.last_travel = wedged
    for _ in range(WEDGED_WALKS - 1):
        assert b._approach((1.0, 2.0, 3.0)) is False
    assert homes == []
    assert b._approach((1.0, 2.0, 3.0)) is False
    assert homes == [1]
    b.client.last_travel = SimpleNamespace(detail="8 detours did not get around it")
    for _ in range(WEDGED_WALKS + 1):
        b._approach((1.0, 2.0, 3.0))
    assert homes == [1], "blocked is not wedged: the planner still has ways round"


def test_walks_that_get_nowhere_are_wedged_too():
    """Upstairs in the Lion's Pride Inn, walked on plans for the hall below, the character
    moved about the landing for two sessions and was never wedged by the unstick's measure
    (sessions 110 and 111)."""
    from jev.clients.hearth import Hearthed
    from jev.clients.travel import Outcome
    from jev.run.body import WEDGED_WALKS

    b = body()
    homes = []
    b.hearth = SimpleNamespace(run=lambda: homes.append(1) or Hearthed.HOME, detail="")
    b.client.approach = lambda world, timeout_s=0: False
    b.client.last_travel = SimpleNamespace(outcome=Outcome.TIMEOUT,
                                           detail="leg 3 of 12: ran out of time")
    b.client.last_headway, b.client.last_distance = 4.0, 12.0
    for _ in range(WEDGED_WALKS + 1):
        b._approach((1.0, 2.0, 3.0))
    assert homes == [], "a short walk's failure is never much headway"
    b.client.last_distance = 480.0
    for _ in range(WEDGED_WALKS):
        b._approach((1.0, 2.0, 3.0))
    assert homes == [1]
    b.client.last_headway = 60.0               # a good way along, then out of time
    for _ in range(WEDGED_WALKS + 1):
        b._approach((1.0, 2.0, 3.0))
    assert homes == [1]
    b.client.last_headway = 0.0
    b.client.last_travel = SimpleNamespace(outcome=Outcome.ABORTED, detail="caller aborted")
    for _ in range(WEDGED_WALKS + 1):
        b._approach((1.0, 2.0, 3.0))
    assert homes == [1], "a walk the caller cut short is not wedged"


def test_upgrades_in_the_bags_are_put_on_before_a_meal_and_remembered(tmp_path, monkeypatch):
    """A Militia Hammer and a Pikeman Shield rode in the bags all night beside a Worn Mace
    (run 20260924T090629-93a85b)."""
    import jev.run.body as module
    from jev.world.gear import load_worn

    b = body()
    b.gear_memory = tmp_path / "character.equipped.json"
    b._read = lambda: {"vitals.combat": False, "inventory.revision": 7, "char.class_id": 2,
                       "char.race_id": 1, "char.level": 8}
    worn = []

    class Wearer:
        detail = ""

        def __init__(self, *args, **kwargs):
            pass

        def bag_items(self):
            return {36, 5580, 6078, 2589}

        def equip_items(self, items):
            worn.append(set(items))
            return sorted(items)

    monkeypatch.setattr(module, "Vendor", Wearer)
    b._wear_upgrades()
    assert worn == [{5580, 6078}]
    assert set(load_worn(b.gear_memory)) == {"main_hand", "off_hand"}
    b._wear_upgrades()
    assert len(worn) == 1, "the same bags are not looked through twice"


def _census(bar, known):
    from jev.perceive.spellbook import SpellCensus

    census = SpellCensus()
    for slot in range(1, 13):
        census.observe({"bars.revision": 1, "bars.slot": slot, "bars.slot_spell": bar.get(slot)})
    for index, spell in enumerate(sorted(known), start=1):
        census.observe({"spells.revision": 1, "spells.total": len(known), "spells.index": index,
                        "spells.id": spell})
    return census


def test_training_visits_the_trainer_once_a_level_and_puts_the_spells_on_the_bar(monkeypatch):
    import jev.run.body as body_module
    from jev.clients.spellbook import Placed
    from jev.clients.trainer import Trained
    from jev.world.state_v1 import Bags, Char, Pos

    b = body()
    # Northshire, beside the Abbey: Brother Sammuel and Brother Wilhelm are both on map 0.
    b.client.bounds = ZoneBounds(12, 0, 1535.4166, -1935.4166, -7939.583, -10254.166)
    bar = {1: 6603, 2: 20154, 3: 635, **{s: 0 for s in range(4, 11)}, 11: None, 12: None}
    b.client.spells = _census(bar, {6603, 20154, 635})
    values = {"char.level": 8, "char.class_id": 2, "char.race_id": 1, "bags.money_copper": 626,
              "vitals.combat": False, "vitals.dead": False, "vitals.ghost": False,
              "bars.revision": 1, "spells.revision": 1}
    b.client.read = lambda: dict(values)
    here = (0.4789, 0.4115)
    b.client.position = lambda: here
    visits, plans, handed, said = [], [], {}, []
    b.say = said.append

    class Desk:
        def __init__(self, hid, read, visit, *args, **kwargs):
            self.visit, self.bought, self.spent, self.detail = visit, 0, 0, ""
            self.learned = []
            handed.update(kwargs)

        def run(self, *, timeout_s):
            self.visit()
            b.client.spells = _census(bar, {6603, 20154, 635, 639, 465, 20271, 19740, 498, 853})
            self.bought, self.spent = 6, 510
            self.learned = [465, 20271, 19740, 498, 639, 853]
            return Trained.DONE

    class Book:
        def __init__(self, *args):
            self.placed, self.detail = [], ""

        def place(self, plan):
            plans.append(plan)
            self.placed = list(plan)
            return Placed.DONE

    monkeypatch.setattr(body_module, "TrainerDesk", Desk)
    monkeypatch.setattr(body_module, "Spellbook", Book)
    monkeypatch.setattr(body_module, "CENSUS_S", 0.0)
    b._open_trainer = lambda trainer: visits.append(trainer.name) or True
    state = seen(char=Char(level=8, cls="paladin", race="human"), bags=Bags(money_copper=626),
                 pos=Pos(zone="Elwynn Forest", zone_id=12, mx=here[0], my=here[1]))
    assert b.trainable(state)
    result = b._train(state)
    assert result.outcome is SkillOutcome.SUCCEEDED, result.detail
    assert visits == ["Brother Wilhelm"]
    # V237: the desk is told what is worth buying there - the trainer's offers, the
    # spellbook and the bar - and says what it bought.
    assert handed["trainer"].name == "Brother Wilhelm" and handed["race_id"] == 1
    assert handed["known"] == {6603, 20154, 635} and handed["bar"] == bar
    assert any("Hammer of Justice 1" in line and "Judgement" in line for line in said)
    # The fight's lines before the blessing (V394).
    assert [(p.spell_id, p.slot) for p in plans[0]] == [
        (639, 3), (465, 4), (20271, 5), (498, 6), (853, 7), (19740, 8)]
    assert not b.policy_context.can_train(state), "a second visit at the same level"


def test_the_policy_is_told_what_the_purse_keeps_for_the_trainer():
    """V215: the least purse that makes a trainer visit due, from the census and the
    position, and nothing once the trainers in reach have nothing left to teach."""
    from jev.coach.policy import Context
    from jev.world.state_v1 import Bags, Char, Pos

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4166, -1935.4166, -7939.583, -10254.166)
    bar = {1: 6603, 2: 20154, 3: 635, **{s: 0 for s in range(4, 11)}, 11: None, 12: None}
    b.client.spells = _census(bar, {6603, 20154, 635})
    here = (0.4789, 0.4115)
    state = seen(char=Char(level=8, cls="paladin", race="human"), bags=Bags(money_copper=5),
                 pos=Pos(zone="Elwynn Forest", zone_id=12, mx=here[0], my=here[1]))
    context = Context()
    b.policy_context = context
    assert context.reserve(state) == 10, "Devotion Aura"
    # What a purchase keeps is the more of that and the repair reserve (V393): 2.6 x 8 x 8.
    assert context.kept(state) == 166
    b.client.spells = _census(bar, {6603, 20154, 635, 465, 20271, 19740, 498, 639, 21082,
                                    853, 1152, 3127})
    assert context.reserve(state) == 0 and context.kept(state) == 166
    b.client.spells = None
    assert context.reserve(state) == 0


def test_a_unit_not_found_on_the_floor_below_it_is_walked_up_to_again(monkeypatch):
    """V235: the walk to Zaldimar Wefhellt, upstairs in the Lion's Pride Inn, "arrived" in
    the hall under him, and training waited a session (session 197)."""
    import jev.run.body as body_module
    from jev.guide.coords import map_to_world, world_to_map

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4166, -1935.4166, -7939.583, -10254.166)
    zaldimar = (-9471.7, 34.5, 63.9)
    b.client.position = lambda: world_to_map(-9472.0, 33.0, b.client.bounds)
    b.client.query = object()
    monkeypatch.setattr(body_module, "surfaces_under", lambda q, m, x, y: [57.2, 63.9])
    answers = iter([Interacted.NO_TARGET, Interacted.TRAINER])
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: next(answers), detail="")
    assert b._open_on("Zaldimar Wefhellt", zaldimar, (0.43, 0.66)) is Interacted.TRAINER
    assert b.client._ground[2] == 57.2, "the next plan starts on the floor below"

    level = body()
    level.client.bounds = b.client.bounds
    level.client.position = b.client.position
    level.client.query = object()
    monkeypatch.setattr(body_module, "surfaces_under", lambda q, m, x, y: [57.2])
    level.interact = SimpleNamespace(open_on=lambda *a, **kw: Interacted.NO_TARGET, detail="")
    assert level._open_on("Zaldimar Wefhellt", zaldimar, (0.43, 0.66)) is Interacted.NO_TARGET


def test_a_spot_off_the_navmesh_reads_the_units_floors(monkeypatch):
    """V263: in the hall under Zaldimar Wefhellt the mage stood where the navmesh has only
    the roof (74.7), the hall's edge 3 yards off; under him are the hall and the roof
    (session 238)."""
    import jev.run.body as body_module
    from jev.guide.coords import world_to_map

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4166, -1935.4166, -7939.583, -10254.166)
    zaldimar = (-9471.7, 34.5, 63.9)
    b.client.position = lambda: world_to_map(-9471.1, 32.9, b.client.bounds)
    b.client.query = object()
    monkeypatch.setattr(body_module, "surfaces_under",
                        lambda q, m, x, y: [57.6, 74.8] if (x, y) == zaldimar[:2] else [74.7])
    answers = iter([Interacted.NO_TARGET, Interacted.TRAINER])
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: next(answers), detail="")
    assert b._open_on("Zaldimar Wefhellt", zaldimar, (0.43, 0.66)) is Interacted.TRAINER
    assert b.client._ground[2] == 57.6, "the unit's lowest floor: the hall"


def test_what_the_bar_conjures_is_remembered_while_its_census_is_read_again():
    """V243: after Frostbolt was placed the profile went blank, a restock of the food and
    water the mage conjures was asked for, and the walk wedged it for a session (215)."""
    from jev.world.combat import Ability, CombatProfile, Role

    b = body()
    b.fight.profile = CombatProfile(name="mage", abilities=(
        Ability(slot=5, role=Role.CONJURE, name="Conjure Water", spell_id=5504, creates=5350),
        Ability(slot=6, role=Role.CONJURE, name="Conjure Food", spell_id=587, creates=5349)))
    assert b.conjured_roles() == {"food", "drink"}
    b.fight.profile = None
    assert b.conjured_roles() == {"food", "drink"}, "the bar unread is not a bar without them"


def test_what_the_bar_conjured_outlives_the_session(tmp_path):
    """V244: each session begins with its bar unread, and session 217's first act was a
    restock of the food and water the mage conjures."""
    from jev.coach.policy import Context
    from jev.world.combat import Ability, CombatProfile, Role

    b = body()
    b.purse_memory = tmp_path / "character-1.purse.json"
    b.policy_context = Context()
    b.fight.profile = CombatProfile(name="mage", abilities=(
        Ability(slot=5, role=Role.CONJURE, name="Conjure Water", spell_id=5504, creates=5350),))
    assert b.conjured_roles() == {"drink"}
    later = body()
    later.purse_memory = b.purse_memory
    later.policy_context = Context()
    later.fight.profile = None
    assert later.conjured_roles() == {"drink"}, "the bar not yet read this session"


def test_the_trainer_s_gossip_line_is_chosen_by_its_text():
    from jev.world.training import trainers

    b = body()
    chosen = []
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: Interacted.GOSSIP, detail="")
    b.chooser = SimpleNamespace(run=lambda title: chosen.append(title) or Chose.CHOSE)
    sammuel = next(t for t in trainers(2, 1, 0) if t.name == "Brother Sammuel")
    assert b._open_trainer(sammuel) is True
    assert chosen == ["I would like to train further in the ways of the Light."]
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: Interacted.TRAINER, detail="")
    assert b._open_trainer(sammuel) is True


def test_a_skill_with_time_to_spare_first_puts_missing_spells_on_the_bar():
    b = body(StepKind.QUEST_ACCEPT)
    placed = []
    b._place_spells = lambda **kw: placed.append(b.arm.decision.skill) or "placed"
    b.interact = SimpleNamespace(open_on=lambda *a, **kw: Interacted.GOSSIP)
    b.chooser = SimpleNamespace(run=lambda title: Chose.CHOSE)
    b.advance = SimpleNamespace(run=lambda q, g: Advanced.DONE, detail="confirmed")
    b.execute(b.arm, seen(), lambda: None)
    assert placed == ["ACCEPT_QUEST"]
    fight = b.arm.decision.model_copy(update={"skill": "COMBAT_PROFILE", "intent": Intent.SERVICE})
    b.fight = SimpleNamespace(run=lambda name: Fought.LOST, detail="", profile=None)
    b.execute(Armed(fight, ArmedBy.POLICY, 0, "fight.rotation", "d", "quest"), seen(), lambda: None)
    assert placed == ["ACCEPT_QUEST"], "a fight stopped to put spells on the bar"



@pytest.mark.parametrize(("dead", "ghost", "kept"), [(True, False, 1), (False, True, 0)])
def test_where_the_character_died_is_remembered_for_walks_to_keep_clear_of(dead, ghost, kept):
    """Three deaths in twenty minutes at Jerod's Landing, each walked straight through
    (sessions 122 and 123). A ghost's position is the graveyard, not the body."""
    from jev.clients.recover import Recovered
    from jev.guide.route_memory import RouteMemory

    b = body()
    b.client.route_memory = RouteMemory()
    b._read = lambda: {"vitals.dead": dead, "vitals.ghost": ghost}
    b._position = lambda: (0.5, 0.5)
    b.recover = SimpleNamespace(run=lambda release_only: Recovered.RELEASED, detail="")
    b._release(None)
    dangers = b.client.route_memory.dangers
    assert len(dangers) == kept
    if kept:
        assert (dangers[0].x, dangers[0].y) == (50.0, 50.0)


def test_a_second_death_at_one_place_is_a_death_camp_left_once_up():
    """V307: Merany, a level 8 mage, died four times in 8 minutes at one spot by Raven Hill's
    graveyard, and after each of the first three got up by its body and walked to a repairer
    the same way (the hive, 28 Sep 12:15-12:23). The death is kept at the level it happened
    at, and a second within ten minutes at one place is a death camp, left once up."""
    from jev.clients.recover import Recovered
    from jev.coach.policy import Context
    from jev.guide.route_memory import RouteMemory

    b = body()
    b.client.route_memory = RouteMemory()
    b.policy_context = Context()
    b._read = lambda: {"vitals.dead": True, "vitals.ghost": False, "char.level": 8}
    b._position = lambda: (0.5, 0.5)
    b.recover = SimpleNamespace(run=lambda release_only: Recovered.RELEASED, detail="",
                                graveyard=None)
    b._release(None)
    assert b.policy_context.death_camp is None, "one death is no camp"
    assert b.client.route_memory.dangers[0].level == 8
    b._position = lambda: (0.5, 0.52)                  # two yards on
    b._release(None)
    assert b.policy_context.death_camp == (0, 48.0, 50.0)


def test_a_death_is_kept_as_the_characters_and_a_camp_another_made_is_left_too():
    """Review of 28 Sep (V307): a death camp is made of one character's own deaths, the hive
    sharing one route memory among its bots (`char.key` is who died); a character dying in a
    camp another made, that counts at its level, leaves it as well."""
    from jev.clients.recover import Recovered
    from jev.coach.policy import Context
    from jev.guide.route_memory import RouteMemory

    b = body()
    b.client.route_memory = RouteMemory()
    b.policy_context = Context()
    b.recover = SimpleNamespace(run=lambda release_only: Recovered.RELEASED, detail="",
                                graveyard=None)
    reading = {"vitals.dead": True, "vitals.ghost": False, "char.level": 8, "char.key": 11}
    b._read = lambda: reading
    b._position = lambda: (0.5, 0.5)
    b._release(None)
    reading["char.key"] = 22
    b._position = lambda: (0.5, 0.52)
    b._release(None)
    assert b.policy_context.death_camp is None, "two characters' deaths are no camp"
    assert [d.who for d in b.client.route_memory.dangers] == [11, 22]
    reading["char.key"] = 11
    b._release(None)
    assert b.policy_context.death_camp is not None, "the first character's second death"
    b.policy_context = Context()
    reading["char.key"] = 33
    b._position = lambda: (0.5, 0.51)
    b._release(None)
    assert b.policy_context.death_camp is not None, "a camp another made, left as well"


def test_the_release_is_pressed_before_the_death_is_saved_and_never_waits_for_the_save(tmp_path):
    """V327: in the hive's hour from 12:00 on 29 Sep a median of 2,061 s passed between a
    dead character's release being armed and its press. The death was kept first, and its
    save waited for the route memory's lock, which every bot of the four farm processes
    shares (`hive.shared`): hive-576 died at 12:07:41, the server released it itself at
    12:13:41, and its record was done at 12:27:38, when its release was cancelled, timed out.
    Now the release comes first and the death is in memory at once, a second one there a
    death camp as before (V307), and the save waits for the lock on the memory's writer; the
    session's end waits for it, and no longer than it is given."""
    import time

    from jev.coach.policy import Context
    from jev.guide.route_memory import RouteMemory
    from jev.persist import file_lock

    class Shared(RouteMemory):              # the hive's: a lock every farm process shares
        def _save(self):
            with file_lock(self.file.with_suffix(".lock")):
                order.append("saved")
                super()._save()

    order = []
    file = tmp_path / "route-memory.json"
    holding, freed = threading.Event(), threading.Event()

    def other_process():
        with file_lock(file.with_suffix(".lock")):
            holding.set()
            freed.wait(10.0)

    other = threading.Thread(target=other_process)
    other.start()
    holding.wait(5.0)
    b = body()
    b.client.route_memory = Shared(file)
    b.policy_context = Context()
    b._read = lambda: {"vitals.dead": True, "vitals.ghost": False, "char.level": 7,
                       "char.key": 576}
    b._position = lambda: (0.5, 0.5)
    b.recover = SimpleNamespace(run=lambda release_only: order.append("released")
                                or Recovered.RELEASED, detail="", graveyard=None)
    try:
        started = time.monotonic()
        assert b._release(None).outcome is SkillOutcome.SUCCEEDED
        b._position = lambda: (0.5, 0.52)          # two yards on: a death camp
        assert b._release(None).outcome is SkillOutcome.SUCCEEDED
        assert time.monotonic() - started < 2.0, "no release waited on the lock"
        assert order == ["released", "released"], "pressed before any save"
        assert b.policy_context.death_camp == (0, 48.0, 50.0), "the camp known at once"
        said = []
        b.say = said.append
        started = time.monotonic()
        b.end_session(timeout=0.3)
        assert time.monotonic() - started < 2.0 and any("still being saved" in s for s in said)
    finally:
        freed.set()
        other.join()
    b.end_session(timeout=5.0)
    assert "saved" in order
    assert [d.who for d in RouteMemory(file).dangers] == [576]


@pytest.mark.parametrize(("deaths", "graveyard_off", "expected"), [
    (1, 300.0, ["corpse"]),            # one death: a death spot, up by the body as before
    (2, 300.0, ["healer"]),            # a death camp: up at the Spirit Healer
    (2, 40.0, ["corpse"]),             # its graveyard in the camp too: by the body
])
def test_a_body_in_a_death_camp_is_got_up_from_at_the_spirit_healer(monkeypatch, deaths,
                                                                     graveyard_off, expected):
    """V307: of 143 get-ups within 35 yards of the body's spot in the hive's runs begun
    11:50-13:08 on 28 Sep, 80 died again before the next; the Spirit Healer's graveyard is no
    way out when it lies in the death camp too, as Raven Hill's did, 43 yards from Merany's."""
    from jev.guide.coords import world_to_map
    from jev.guide.route_memory import RouteMemory

    b = body()
    b.client.bounds = ZoneBounds(12, 0, 1535.4, -1935.4, -7939.6, -10254.2)
    b._side = "alliance"
    b._revived_at = None
    b._wait_out_sickness = lambda: 0.0
    monkeypatch.setattr("jev.run.body.hostiles.near", lambda *a, **k: [])
    corpse = (-9000.0, 100.0)
    b.client.route_memory = RouteMemory()
    for i in range(deaths):
        b.client.route_memory.died(0, (corpse[0] + 3.0 * i, corpse[1]))
    b.recover.graveyard = world_to_map(corpse[0] - graveyard_off, corpse[1], b.client.bounds)
    calls = []
    b.recover.run_spirit_healer = lambda: calls.append("healer") or Recovered.ALIVE
    b.recover.run = lambda corpse_point: calls.append("corpse") or Recovered.ALIVE
    b._corpse_walk = lambda point: True
    assert b._recover(ghost_at(corpse, b.client.bounds)).code == "alive"
    assert calls == expected


def test_a_caster_conjures_what_it_is_short_of_after_a_meal(monkeypatch):
    """V166: fewer than four Conjured Water in the bags, three casts; enough, none; and a
    paladin, with no conjure on its bar, never takes a census."""
    from dataclasses import replace

    from jev.run import body as module
    from jev.world.combat import Ability, Role, for_class

    conjure = Ability(slot=5, role=Role.CONJURE, name="Conjure Water", mana=60,
                      spell_id=5504, creates=5350)
    censuses = []

    class Counter:
        def __init__(self, *a, **k):
            pass

        def census(self):
            censuses.append(1)
            return {(0, 1): (5350, have[0]), (0, 2): (159, 0)}

    monkeypatch.setattr(module, "Vendor", Counter)
    for class_id, water, casts in ((8, 2, 3), (8, 6, 0), (2, 0, 0)):
        have = [water]
        b = body()
        taps = []
        b.client.hid.tap = lambda key: taps.append(key) or True
        b._await_cast = lambda: None
        values = {"vitals.combat": False, "vitals.power": 1.0, "vitals.power_max": 300,
                  "char.class_id": class_id, "char.race_id": 1, "inventory.revision": 7}
        b._read = lambda values=values: values
        base = for_class(class_id, 1)
        b.fight.profile = (replace(base, abilities=(*base.abilities, conjure))
                           if class_id == 8 else base)
        censuses.clear()
        b._conjure()
        assert taps == ["5"] * casts, (class_id, water)
        assert len(censuses) == (1 if class_id == 8 else 0)
        censuses.clear()
        b._conjure()
        assert not censuses, "the bags unchanged since: not looked at again"


def test_a_conjure_skipped_for_want_of_mana_is_tried_again_at_the_next_meal(monkeypatch):
    """Review, 25 September: the bags unchanged, the skipped casts were never tried again."""
    from dataclasses import replace

    from jev.run import body as module
    from jev.world.combat import Ability, Role, for_class

    conjure = Ability(slot=5, role=Role.CONJURE, name="Conjure Water", mana=60,
                      spell_id=5504, creates=5350)

    class Counter:
        def __init__(self, *a, **k):
            pass

        def census(self):
            return {(0, 1): (5350, 0)}

    monkeypatch.setattr(module, "Vendor", Counter)
    b = body()
    taps = []
    b.client.hid.tap = lambda key: taps.append(key) or True
    b._await_cast = lambda: None
    values = {"vitals.combat": False, "vitals.power": 0.1, "vitals.power_max": 300,
              "char.class_id": 8, "char.race_id": 1, "inventory.revision": 7}
    b._read = lambda: values
    base = for_class(8, 1)
    b.fight.profile = replace(base, abilities=(*base.abilities, conjure))
    b._conjure()
    assert taps == [], "30 mana cannot pay for a 60-mana conjure"
    values["vitals.power"] = 1.0
    b._conjure()
    assert taps == ["5"] * 3, "the same bags, looked at again"



def test_a_new_characters_home_is_where_it_began(tmp_path):
    """V176: with home unknown, a level-1 mage left Northshire for Goldshire's inn and died
    twice on the way. Its hearthstone is bound to where it began."""
    from jev.world.home import load_home
    from jev.world.state_v1 import Char

    b = body()
    b.home_memory = tmp_path / "home.json"
    b._inn = lambda state: SimpleNamespace(world=(-9460.0, 60.0, 57.0), name="Innkeeper Farley")
    fresh = seen(char=Char(level=1))
    assert b.bindable(fresh) is False
    assert load_home(b.home_memory) is not None, "where it stands, remembered"
    b.home_memory.unlink()
    assert b.bindable(seen(char=Char(level=5))) is True, "later, home unknown: bind"


def test_talent_points_are_spent_at_a_meal_on_a_schema_19_strip_once_a_level(monkeypatch):
    """V261: the paladin reached 15.87 with six talent points unspent."""
    from jev.clients.talents import Spent

    b = body()
    runs = []

    class Desk:
        def __init__(self, *a, **k):
            self.spent, self.learned, self.detail = 0, [], "never came round"

        def run(self, build):
            runs.append(len(build))
            return Spent.NOT_SEEN

    monkeypatch.setattr("jev.run.body.TalentDesk", Desk)
    reading = {"schema": 18, "char.talent_points": 1, "char.level": 10, "char.class_id": 8,
               "vitals.combat": False}
    b.client.read = lambda: dict(reading)
    b._spend_talents()
    assert runs == [], "an addon before schema 19 paints no talents"
    reading["schema"] = 19
    b._spend_talents()
    b._spend_talents()
    assert len(runs) == 1, "a visit that failed is not made again this level"
    reading["char.level"] = 11
    b._spend_talents()
    assert len(runs) == 2


def test_a_merchant_in_the_zone_the_character_stands_in_counts_too():
    """V281: at Sentinel Hill on the Elwynn guide the mage's repair went for Frederick Stover in
    Stormwind, 1,900 yards and no complete plan (session 266)."""
    from jev.world.vendor import Merchant

    b = body()
    elwynn = ZoneBounds(12, 0, 1535.4166, -1935.4166, -7939.583, -10254.166)
    westfall = ZoneBounds(40, 0, 2033.3333, -1500.0, -9616.6666, -11972.9166)
    b.client.bounds = elwynn
    b.client.coordinate_zones = {12080: elwynn, 40: westfall}
    stover = Merchant(entry=1298, name="Frederick Stover", map_id=0,
                      world=(-8795.5, 709.1, 102.4), items=frozenset(), repairs=True)
    macgregor = Merchant(entry=843, name="William MacGregor", map_id=0,
                         world=(-10653.0, 994.0, 32.0), items=frozenset(), repairs=True)
    b.client.read = lambda: {"pos.zone_id": 12080}
    assert b._in_zone([stover, macgregor]) == [stover], "in Elwynn: Elwynn's box"
    b.client.read = lambda: {"pos.zone_id": 40}
    assert b._in_zone([stover, macgregor]) == [stover, macgregor], "in Westfall: its box too"


def _grind_body(memory):
    node = Node(id="rib", kind=StepKind.GRIND, zone="zone", zone_id=1, pos=(0.5, 0.5),
                world=(50.0, 50.0, 0.0), map_id=0, level=(1, 3), target_name="Wolf",
                target_kind="creature", skills=("GRIND_UNTIL",))
    b = body()
    b.graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(node,))
    b.client.route_memory = memory
    d = Decision(goal="g", intent=Intent.ADVANCE, skill="GRIND_UNTIL", abort_if=["dead"],
                 confidence=1, why="fixture", params={"until_level": 3})
    b.arm = Armed(d, ArmedBy.POLICY, 0, "guide", "d", node.id)
    return b


def test_a_grind_whose_stations_lie_in_a_death_camp_waits_for_the_camp():
    """V334: no station of the rib is walked to, and its step waits until the camp ends."""
    import time as clock

    from jev.guide.route_memory import CAMP_S, RouteMemory

    memory = RouteMemory()
    now = clock.time()
    memory.died(0, (50.0, 50.0), now=now - 60.0, level=None)
    memory.died(0, (55.0, 50.0), now=now - 30.0, level=None)
    b = _grind_body(memory)
    result = b._hunt(seen())
    assert result.code == "camp" and result.outcome is SkillOutcome.ABORTED, result
    (walk,), _ = b.client.approach.call_args
    assert b.client.approach.call_count == 1, "no station walked to"
    assert memory.camp_at(0, walk[:2], now, None) is None, "only out of the camp's reach"
    left = b.policy_context.step_waiting("rib", clock.time())
    assert left is not None and abs(left - (CAMP_S - 30.0)) < 5.0


def test_a_walk_refused_through_a_death_camp_says_when_the_camp_ends():
    """V334: the hunt waits for the camp that refused its walk (`Path.camp`)."""
    import time as clock

    from jev.guide.path import Path, PathStatus
    from jev.guide.route_memory import CAMP_REFUSED, RouteMemory

    memory = RouteMemory()
    now = clock.time()
    memory.died(0, (50.0, 50.0), now=now - 60.0, level=8)
    memory.died(0, (55.0, 50.0), now=now - 30.0, level=8)
    b = _grind_body(memory)
    b.client.last_plan = Path(PathStatus.NOPATH, (), "mmap", CAMP_REFUSED, camp=(50.0, 50.0))
    planned, until = b._walk_note(8)
    assert planned is False and until == memory.camp_at(0, (50.0, 50.0), now, 8).camp_until
    b.client.last_plan = Path(PathStatus.NOPATH, (), "mmap", "no route")
    assert b._walk_note(8) == (False, None)
    b.client.last_plan = Path(PathStatus.COMPLETE, ((0, 0, 0), (1, 1, 0)), "mmap")
    assert b._walk_note(8) == (True, None)
    b.client.last_plan = None
    assert b._walk_note(8) == (False, None), "no plan asked for: the walk never began"


def test_a_hunt_or_walk_that_planned_no_route_waits_before_its_step_is_armed_again():
    """V335: a try that never moved the character waits a minute, then two, before the step
    is armed again; one that walks ends the run of waits."""
    import time as clock

    from jev.coach.policy import STEP_RETRY_MIN_S
    from jev.guide.path import Path, PathStatus
    from jev.guide.route_memory import RouteMemory

    b = _grind_body(RouteMemory())
    b.client.state = lambda: seen()
    b.client.approach = Mock(return_value=False)
    b.client.last_plan = Path(PathStatus.NOPATH, (), "mmap", "no path")
    assert b._hunt(seen()).code == "unreachable"
    left = b.policy_context.step_waiting("rib", clock.time())
    assert left is not None and abs(left - STEP_RETRY_MIN_S) < 2.0
    b.policy_context.step_wait_until.clear()
    assert b._travel(seen()).code == "unreachable"
    left = b.policy_context.step_waiting("rib", clock.time())
    assert left is not None and abs(left - 2 * STEP_RETRY_MIN_S) < 2.0, "twice as long"
    b.client.approach = Mock(return_value=True)
    b._travel(seen())
    assert "rib" not in b.policy_context.step_failures, "a walk that arrived ends the run"


def test_a_dry_ribs_wider_kinds_are_its_levels_none_grey_and_one_above_at_most(monkeypatch):
    """V337: the level 8 mage on Elwynn's 7-9 rib takes its kinds at 7 to 9; at level 7, 7 to
    8; a level 15 on a 1-3 rib, nothing (all grey)."""
    from jev.clients.fight import Kinds
    from jev.guide.route_memory import RouteMemory
    from jev.world import hostiles

    asked = []
    monkeypatch.setattr(hostiles, "kinds", lambda *a, **kw: asked.append(kw) or frozenset({7, 8}))
    b = _grind_body(RouteMemory())
    b._side = "alliance"
    node = Node(id="rib", kind=StepKind.GRIND, zone="zone", zone_id=1, pos=(0.5, 0.5),
                world=(50.0, 50.0, 0.0), map_id=0, level=(7, 9))
    wider = b._rib_kinds(node, node.world, 30.0, 7, 8)
    assert wider == Kinds(own=7, names=frozenset({8}), low=7, high=9)
    assert asked[-1]["low"] == 7 and asked[-1]["high"] == 9 and asked[-1]["side"] == "alliance"
    assert b._rib_kinds(node, node.world, 30.0, 7, 7).high == 8
    low = node.model_copy(update={"level": (1, 3)})
    assert b._rib_kinds(low, node.world, 30.0, 7, 15) is None, "all grey"
    assert b._rib_kinds(node, node.world, 30.0, 7, None) is None


def test_a_service_check_builds_on_the_reading_just_taken():
    """V338: the hunt asks whether a service is due after each pass's reading; the state is
    that reading's, not a capture of its own."""
    from jev.world.state_v1 import Bags

    b = body()
    worn = seen(bags=Bags(free=20, durability_min=0.2, money_copper=5000))
    b.client.state = lambda: pytest.fail("captured again")
    b.client.recent_state = lambda max_age_s: worn
    assert b._service_needed() == "durability is low"
    b.client.recent_state = lambda max_age_s: None
    b.client.state = lambda: worn
    assert b._service_needed() == "durability is low", "none recent: one of its own"
# hive-240, 28 Sep (V328): its last death in Tirisfal Glades at 12:12:34, as its session ended;
# 6 s later the next session began as a ghost at the graveyard by the Ruins of Lordaeron, in
# the server's zone Undercity, whose map does not hold the body, and no body was ever painted.
TIRISFAL_FELL = (0.6500566155033448, 0.5450090807148609)
TIRISFAL_ALIVE = (0.5623907763439764, 0.4942173763828313)
RUINS_GHOST = (0.6237705843644803, 0.6688431131379939)


def _tirisfal_body(tmp_path=None):
    from jev.coach.policy import Context
    from jev.guide.coords import bounds_by_radio_id

    b = body()
    b.client.bounds = bounds_by_radio_id("data/zones-tbc-243.json")[4049]   # the guide's frame
    if tmp_path is not None:
        b.purse_memory = tmp_path / "character-0b15a092.purse.json"
    b.policy_context = Context()
    b._wait_out_sickness = lambda: 0.0
    return b


def _undercity_ghost():
    from jev.world.state_v1 import Char, Pos, Vitals

    return seen(char=Char(level=2, faction="horde"), vitals=Vitals(hp=0.0, dead=False, ghost=True),
                pos=Pos(mx=RUINS_GHOST[0], my=RUINS_GHOST[1], zone="Undercity"))


def test_where_the_character_fell_is_kept_the_moment_its_death_is_read(tmp_path):
    """V328: hive-240's session ended at its death; the next began as a ghost the server had
    released beside a capital's Spirit Healer, with no body painted, and aborted its corpse
    run about 1,797 times a session for 27 hours. The death read, where it fell is in the
    purse file, and the next session's ghost walks there."""
    from jev.world.state_v1 import Pos, Vitals

    b = _tirisfal_body(tmp_path)
    b.observe(seen(pos=Pos(mx=TIRISFAL_ALIVE[0], my=TIRISFAL_ALIVE[1], zone="Tirisfal")))
    b.observe(seen(pos=Pos(mx=TIRISFAL_FELL[0], my=TIRISFAL_FELL[1], zone="Tirisfal"),
                   vitals=Vitals(hp=0.0, dead=True, ghost=False)))
    later = _tirisfal_body(tmp_path)                 # the next session, begun as a ghost
    later.recover.read = lambda: {"vitals.dead": False, "vitals.ghost": True,
                                  "pos.mx": RUINS_GHOST[0], "pos.my": RUINS_GHOST[1]}
    walked = []
    later._short_of_body = lambda point: walked.append(point) or True
    later.recover.reach = None
    later._in_reclaim_reach = lambda ghost, corpse: None
    later.recover.run_spirit_healer = lambda: pytest.fail("the body is known")
    import jev.clients.recover
    with pytest.MonkeyPatch.context() as m:
        m.setattr(jev.clients.recover.time, "sleep", lambda seconds: None)
        result = later._recover(_undercity_ghost())
    assert walked and walked[0] == pytest.approx(TIRISFAL_FELL)
    assert result.code == "still_ghost", "walked there; the popup is the next look's"


def test_a_ghost_nothing_knows_the_body_of_gets_up_at_the_spirit_healer(tmp_path):
    """V328: with no body painted, none kept and none told, the corpse run aborted at once,
    'a ghost with no corpse position', and was armed again half a second later: 858,365
    times on 29 Sep. Now it takes the Spirit Healer beside it, sickness and all; and one the
    healer does not raise either is an abort the policy waits on (`Context.death_waiting`)."""
    b = _tirisfal_body(tmp_path)
    b.recover.read = lambda: {"vitals.dead": False, "vitals.ghost": True,
                              "pos.mx": RUINS_GHOST[0], "pos.my": RUINS_GHOST[1]}
    b.recover.walk_to = lambda point: pytest.fail("no body to walk to")
    raised = []
    b.recover.run_spirit_healer = lambda: raised.append(b.recover.graveyard) or Recovered.ALIVE
    result = b._recover(_undercity_ghost())
    assert (result.outcome, result.code) == (SkillOutcome.SUCCEEDED, "alive")
    assert "where the body lies is unknown" in result.detail and len(raised) == 1
    b.recover.run_spirit_healer = lambda: Recovered.STILL_GHOST
    result = b._recover(_undercity_ghost())
    assert (result.outcome, result.code) == (SkillOutcome.ABORTED, "no_corpse")
    assert "Spirit Healer did not raise it" in result.detail


def test_the_servers_word_on_the_body_is_taken_where_the_client_has_it(tmp_path):
    """V328: the hive's bridge reports every dead or ghost bot's corpse (`hive.client`'s
    `corpse_world`); the live client has none, and walks by where it saw itself fall."""
    from jev.guide.coords import map_to_world

    b = _tirisfal_body(tmp_path)
    corpse = map_to_world(*TIRISFAL_FELL, b.client.bounds)
    b.client.corpse_world = lambda: (0, corpse[0], corpse[1])
    passed = []
    b.recover.run = lambda corpse_point: passed.append(corpse_point) or Recovered.ALIVE
    assert b._recover(_undercity_ghost()).code == "alive"
    assert passed[0] == pytest.approx(TIRISFAL_FELL)
    b.client.corpse_world = lambda: (1, corpse[0], corpse[1])          # another continent's
    assert b._known_body() is None


def test_a_ghost_read_with_no_death_seen_fell_where_it_last_stood_alive(tmp_path):
    """V328: a session that never read its character dead (released by the server or across
    a session's end) keeps where it last stood alive, which the session's end keeps too; up
    again, nothing is kept of the body."""
    from jev.world.state_v1 import Pos

    b = _tirisfal_body(tmp_path)
    b.observe(seen(pos=Pos(mx=TIRISFAL_ALIVE[0], my=TIRISFAL_ALIVE[1], zone="Tirisfal")))
    b.end_session(timeout=0.0)
    later = _tirisfal_body(tmp_path)
    later.observe(_undercity_ghost())
    assert later._known_body() == pytest.approx(TIRISFAL_ALIVE)
    later.recover.corpse = (0.1, 0.1)
    later.observe(seen(pos=Pos(mx=0.6, my=0.6, zone="Tirisfal")))
    assert later._fell is None and later.recover.corpse is None
    assert _tirisfal_body(tmp_path)._fell is None, "and the purse file forgets it"


def test_a_rib_wholly_in_a_death_camp_is_named_and_one_with_a_station_out_is_not():
    """V334: the runtime waits a step out on no rib whose every station lies in a camp."""
    import time as clock

    from jev.guide.route_memory import RouteMemory

    memory = RouteMemory()
    now = clock.time()
    memory.died(0, (50.0, 50.0), now=now - 60.0, level=5)
    memory.died(0, (55.0, 50.0), now=now - 30.0, level=5)
    b = _grind_body(memory)
    rib = b.graph.nodes[0].model_copy(update={"hunt_yards": 30.0})
    assert b.rib_camped(rib, 5), "rings of 30 yards round a camp's death"
    assert not b.rib_camped(rib, 9), "a camp of level 5 deaths holds no level 9"
    wide = rib.model_copy(update={"hunt_yards": 300.0})
    assert not b.rib_camped(wide, 5), "its outer ring is out of the camp"
    assert b.policy_context.camped == b.rib_camped


def test_a_step_waiting_on_a_camp_with_nowhere_else_is_waited_out_of_its_reach(monkeypatch):
    """V334: standing in the camp the character died in twice is where it dies a third time;
    the wait is stood out of its reach, at the nearest point clear of every camp."""
    import math as m
    import time as clock

    from jev.guide.route_memory import CAMP_YARDS, RouteMemory
    from jev.run.body import CAMP_CLEAR_YARDS

    memory = RouteMemory()
    now = clock.time()
    memory.died(0, (50.0, 50.0), now=now - 60.0, level=5)
    memory.died(0, (55.0, 50.0), now=now - 30.0, level=5)
    b = _grind_body(memory)
    walked = []
    b._approach = lambda world, stop_short=0.0: walked.append(world) or True
    b._position = lambda: (0.6, 0.5)                    # world (40, 60): in the camp
    assert b._out_of_camp(5) is True and len(walked) == 1
    spot = walked[0][:2]
    assert m.dist(spot, (50.0, 50.0)) >= CAMP_YARDS + CAMP_CLEAR_YARDS - 1e-6
    assert memory.camp_at(0, spot, now, 5) is None
    walked.clear()
    assert b._out_of_camp(9) is False and walked == [], "no camp at its level: stays"


def test_a_grinds_hunt_armed_again_after_a_fight_goes_on_from_its_place():
    """V343: every arm of GRIND_UNTIL built a new hunt, its tour from the head; 5,495 hunts
    the hive began again after a combat-cut walk (4 Oct 16:26-18:30) re-walked 5,917 stations
    already stood at, 25 hours. The body keeps the hunt's place for its step and objective,
    and a death, or another step's hunt, begins afresh."""
    from jev.guide.route_memory import RouteMemory
    from jev.run.hunt import Place

    b = _grind_body(RouteMemory())
    spawns = ((50.0, 50.0, 0.0), (90.0, 50.0, 0.0), (130.0, 50.0, 0.0))
    b.hunt_spawns = {"rib": spawns}
    b._hostiles = lambda *a, **k: ()
    b._stations = lambda *a, **k: None
    b.fight = SimpleNamespace(run=lambda *a, **k: Fought.NO_TARGET, pressed=[], closed=0,
                              heals_landed=0, heals_ignored=0, detail="", broken=False,
                              top_up=lambda *a, **k: True, top_ups=0, top_ups_landed=0)
    b._read = lambda: {"vitals.hp": 1.0, "char.level": 2}
    b._service_needed = lambda: None
    walked = []

    def cut(p, **kw):
        walked.append(tuple(p))
        if len(walked) == 2:
            raise Cancelled("combat interrupted the leg or service")
        return True
    b._approach = cut
    with pytest.raises(Cancelled):
        b._hunt(seen())
    place = b._place[1]
    assert isinstance(place, Place) and place.post == 1 and place.arrived == 1
    assert b._hunt_place(("rib", b._place[0][1])) is place, "the same step's hunt: kept"
    b._approach = lambda p, **kw: walked.append(tuple(p)) or True
    b._world_position = lambda: (88.0, 50.0)
    del walked[:]
    b._hunt(seen())
    assert walked[0] == spawns[1], "on from the walk cut short, not the tour's head"
    b._hunt_place(("rib", "kept"))
    from jev.world.state_v1 import Vitals

    b.observe(seen(vitals=Vitals(hp=0.0, dead=True, ghost=False)))
    assert b._place is None, "a death begins the hunt afresh"
    assert b._hunt_place(("other", "x")) is not place


def test_a_grinds_pulls_are_for_experience_and_a_quests_are_not():
    """V344: a grind's hunt and its defence ask the fight for a pull that pays (`Paying`);
    a quest objective's kind counts toward the quest at any level."""
    from jev.clients.fight import Paying
    from jev.guide.route_memory import RouteMemory

    b = _grind_body(RouteMemory())
    b.hunt_spawns = {"rib": ((50.0, 50.0, 0.0),)}
    b._hostiles = lambda *a, **k: ()
    b._stations = lambda *a, **k: None
    b._service_needed = lambda: None
    asked = []
    b.fight = SimpleNamespace(run=lambda name_id=None, **k: asked.append(name_id) or Fought.DIED,
                              pressed=[], closed=0, heals_landed=0, heals_ignored=0, detail="",
                              broken=False, top_up=lambda *a, **k: True, top_ups=0,
                              top_ups_landed=0)
    b._read = lambda: {"vitals.hp": 1.0, "char.level": 2}
    b._approach = lambda p, **kw: True
    b._hunt(seen())
    assert asked == [Paying(name_id("Wolf"))]
    assert b._objective_name() == Paying(name_id("Wolf"))
    quest = body(StepKind.QUEST_OBJECTIVE)
    assert quest._objective_name() == name_id("NPC"), "a quest's kind, at any level"
