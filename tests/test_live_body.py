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
    b.loot.run.assert_called_once_with(progress=b._progress, anchor=plate, name_id=2864)


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


@pytest.mark.parametrize(("since_revived", "healer"), [(60.0, True), (600.0, False), (None, False)])
def test_a_body_that_killed_the_character_again_is_left_for_the_spirit_healer(
        monkeypatch, since_revived, healer):
    from jev.clients.hearth import Hearthed
    from jev.run.body import DEATH_TRAP_S

    b = body()
    now = 10_000.0
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: now)
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
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: now)
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
    b.client.last_headway = 4.0
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
    visits, plans = [], []

    class Desk:
        def __init__(self, hid, read, visit, *args):
            self.visit, self.bought, self.spent, self.detail = visit, 0, 0, ""

        def run(self, *, timeout_s):
            self.visit()
            b.client.spells = _census(bar, {6603, 20154, 635, 639, 465, 20271, 19740, 498, 853})
            self.bought, self.spent = 6, 510
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
    assert [(p.spell_id, p.slot) for p in plans[0]] == [
        (639, 3), (465, 4), (19740, 5), (20271, 6), (498, 7), (853, 8)]
    assert not b.policy_context.can_train(state), "a second visit at the same level"


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
