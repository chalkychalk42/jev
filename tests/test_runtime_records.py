"""Decisions, execution lifetimes and attribution, using only observed fixture states."""

import pytest

from jev.clients.source import ScriptedSource
from jev.coach.policy import Context, decide
from jev.coach.schema import Artifact, ArtifactKind, Decision, Intent, TeacherReply
from jev.guide.graph import Graph, Node
from jev.learn.episode import Recorder, SkillOutcome, read
from jev.orch.runtime import ClientRuntime
from jev.world.state_v1 import ArmedBy, Bags, Pos, Quest, Sense, State, StepKind, Ui, Vitals


def graph():
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, zone="zone", zone_id=1,
             pos=(0.5, 0.5), quest_id=1, skills=("TRAVEL_TO", "ACCEPT_QUEST"), next=("turnin",)),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, zone="zone", zone_id=1,
             pos=(0.6, 0.5), quest_id=1, skills=("TRAVEL_TO", "TURNIN_QUEST")),
    ))


def seen(t=0, **kw):
    base = State(t=t, client_id="c", pos=Pos(zone="zone", mx=0.5, my=0.5),
                 quests=(), sense=Sense(addon_ok=True), ui=Ui(modal=False),
                 vitals=Vitals(hp=1, power=1, combat=False, dead=False, ghost=False))
    return base.model_copy(update=kw)


def runtime(tmp_path, states, **kwargs):
    return ClientRuntime("c", graph(), ScriptedSource(states), Recorder(tmp_path), **kwargs)


def answer(skill="GRIND_UNTIL"):
    return Decision(goal="teacher", intent=Intent.GRIND_RIB, skill=skill,
                    abort_if=["dead"], confidence=0.9, why="a proposed choice")


def test_normal_choices_are_recorded_once_and_link_every_tick(tmp_path):
    rt = runtime(tmp_path, [seen(t) for t in (0, 0.5, 1, 1.5)], keys_down=lambda: ["w"])
    rt.run(4, 0)
    decisions = read(rt.recorder.dir / "decisions.jsonl")
    ticks = read(rt.recorder.dir / "ticks.jsonl")
    assert len(decisions) == 1
    assert decisions[0]["skill"] == "ACCEPT_QUEST", "arrival must move beyond the travel phase"
    assert {t["decision_id"] for t in ticks} == {decisions[0]["decision_id"]}
    assert {t["state"]["control"]["armed_at"] for t in ticks} == {0}
    assert all(t["keys"] == ["w"] for t in ticks)
    assert all(t["shadow_confidence"] == 0 and t["shadow_intent"] is None for t in ticks)
    assert all(t["state"]["guide"]["step_id"] == "accept" for t in ticks)
    assert all(t["state"]["situation_key"] == t["situation_key"] for t in ticks)


def test_body_result_retains_original_step_and_full_duration(tmp_path):
    rt = runtime(tmp_path, [seen(0), seen(5), seen(9, quests=(Quest(quest_id=1),))])
    rt.tick()
    original = rt.armed
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin"
    rt.finish(SkillOutcome.SUCCEEDED, "accepted confirmed")
    result = read(rt.recorder.dir / "skills.jsonl")[0]
    assert result["duration_s"] == 9
    assert result["step_id"] == "accept"
    assert result["situation_key"] == original.situation_key
    rt.finish(SkillOutcome.SUCCEEDED)
    assert len(read(rt.recorder.dir / "skills.jsonl")) == 1


def test_dead_to_ghost_and_unread_ticks_count_one_death(tmp_path):
    states = [seen(0), seen(1, vitals=Vitals(dead=True)), seen(2, vitals=Vitals(ghost=True)),
              seen(3, vitals=Vitals()), seen(4, vitals=Vitals(ghost=True)), seen(5),
              seen(6, vitals=Vitals(dead=True))]
    rt = runtime(tmp_path, states)
    rt.run(len(states), 0)
    assert rt.counters.deaths == 2
    assert rt.tracker.memory.deaths == 2


def test_rejected_teacher_is_never_credited_for_the_fallback(tmp_path):
    rt = runtime(tmp_path, [seen()], take=lambda key: answer("DOES_NOT_EXIST"))
    rt.tick()
    decisions = read(rt.recorder.dir / "decisions.jsonl")
    teacher, applied = decisions
    assert teacher["status"] == "rejected"
    assert teacher["verifier_verdict"] == "skill_exists"
    assert applied["author"] == "policy"
    assert rt.counters.teacher_applied == 0
    assert read(rt.recorder.dir / "ticks.jsonl")[0]["decision_id"] == applied["decision_id"]


def test_teacher_cannot_override_a_current_modal(tmp_path):
    rt = runtime(tmp_path, [seen(ui=Ui(modal=True))], take=lambda key: answer())
    rt.tick()
    assert rt.armed.by is ArmedBy.S1_PREEMPT
    assert rt.armed.decision.skill == "ABORT_WAIT"
    teacher = read(rt.recorder.dir / "decisions.jsonl")[0]
    assert teacher["status"] == "rejected" and teacher["verifier_verdict"] == "preempt"


def test_artifact_only_reply_is_kept_without_inventing_an_action(tmp_path):
    reply = TeacherReply(artifacts=[Artifact(kind=ArtifactKind.ON_FAIL_EDGE, target="accept",
                                             payload={"goto": "turnin"}, rationale="candidate")])
    rt = runtime(tmp_path, [seen()], take=lambda key: reply)
    rt.tick()
    teacher = read(rt.recorder.dir / "decisions.jsonl")[0]
    assert teacher["artifacts"][0]["payload"] == {"goto": "turnin"}
    assert teacher["intent"] is None
    assert rt.armed.by is ArmedBy.POLICY


def test_optional_queue_failures_do_not_remove_the_floor(tmp_path):
    def unavailable(*args):
        raise RuntimeError("offline")
    rt = runtime(tmp_path, [State(t=0, client_id="c")], take=unavailable, ask=unavailable)
    rt.tick()
    assert rt.armed is not None
    assert read(rt.recorder.dir / "ticks.jsonl")[0]["shadow_confidence"] == 0


def test_client_identity_mismatch_is_not_silently_recorded(tmp_path):
    rt = runtime(tmp_path, [seen().model_copy(update={"client_id": "other"})])
    with pytest.raises(ValueError, match="source client"):
        rt.tick()
    assert not (rt.recorder.dir / "ticks.jsonl").exists()


def test_broke_repair_waits_for_observed_money_growth_and_combat_wins():
    ctx = Context()
    state = seen(bags=Bags(durability_min=0, money_copper=10))
    assert decide(state, graph().nodes[0], context=ctx).decision.skill == "VENDOR_REPAIR"
    ctx.repair_failed(10)
    assert decide(state, graph().nodes[0], context=ctx).decision.skill == "ACCEPT_QUEST"
    # A copper more is not a purse that grew (V196): a silver more is.
    copper = state.model_copy(update={"bags": Bags(durability_min=0, money_copper=11)})
    assert decide(copper, graph().nodes[0], context=ctx).decision.skill == "ACCEPT_QUEST"
    richer = state.model_copy(update={"bags": Bags(durability_min=0, money_copper=110)})
    assert decide(richer, graph().nodes[0], context=ctx).decision.skill == "VENDOR_REPAIR"
    combat = richer.model_copy(update={"vitals": Vitals(hp=0.8, combat=True)})
    assert decide(combat, graph().nodes[0], context=ctx).rule.startswith("fight.")


def test_an_unaffordable_repair_does_not_hide_full_bags():
    context = Context()
    context.repair_failed(10)
    state = seen(bags=Bags(free=0, durability_min=0, money_copper=10))
    assert decide(state, context=context).rule == "service.bags_full"


def test_full_bags_with_nothing_to_sell_wait_for_a_slot_to_free():
    """Asked again, a sale that found nothing sellable stops the run on the same bags."""
    context = Context()
    full = seen(bags=Bags(free=0, durability_min=1.0))
    assert decide(full, context=context).rule == "service.bags_full"
    context.bags_failed()
    assert decide(full, context=context).rule != "service.bags_full"
    decide(seen(bags=Bags(free=1, durability_min=1.0)), context=context)
    assert decide(full, context=context).rule == "service.bags_full", \
        "a slot freed and filled again is worth another visit"


def test_nearly_full_bags_visit_the_merchant_before_the_loot_stops():
    """Session 81 left four kills unlooted with full bags on its way to a merchant."""
    context = Context()
    tight = seen(bags=Bags(free=2, durability_min=1.0))
    assert decide(tight, context=context).rule == "service.bags_full"
    context.bags_failed(2)
    assert decide(tight, context=context).rule != "service.bags_full"
    assert decide(seen(bags=Bags(free=1, durability_min=1.0)),
                  context=context).rule != "service.bags_full", "fuller, but nothing new sold"
    decide(seen(bags=Bags(free=3, durability_min=1.0)), context=context)
    assert decide(tight, context=context).rule == "service.bags_full", "room, then tight again"
    context.bags_failed(2)
    for free in (1, 0, 1):                          # fuller, full, a meal eaten
        assert decide(seen(bags=Bags(free=free, durability_min=1.0)),
                      context=context).rule != "service.bags_full"
    assert decide(seen(bags=Bags(free=0, durability_min=1.0)),
                  context=context).rule == "service.bags_full", "freed and filled again"


def test_service_waits_for_observed_out_of_combat_state():
    from jev.coach.policy import service

    state = seen(vitals=Vitals(combat=None), bags=Bags(free=0, durability_min=0))
    assert service(state) is None


def test_changed_params_are_a_new_arm_even_with_same_skill(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(2)], take=lambda key: answer())
    rt.tick()
    old = rt.armed
    rt.take = lambda key: answer().model_copy(update={"params": {"new": True}})
    rt.tick()
    assert rt.armed is not old
    assert rt.armed.at == 2


def test_unimplemented_skill_fails_verification_before_execution(tmp_path):
    rt = runtime(tmp_path, [seen()], available_skills=frozenset({"IDLE"}))
    rt.tick()
    assert rt.armed.decision.skill is None
    assert rt.armed.rule == "unavailable"
    assert read(rt.recorder.dir / "decisions.jsonl")[0]["status"] == "rejected"


@pytest.mark.parametrize("combat", [False, True])
def test_a_grind_step_does_not_attach_level_parameters_to_travel_or_defence(tmp_path, combat):
    state = seen(vitals=Vitals(hp=1, combat=combat))
    rt = runtime(tmp_path, [state])
    node = Node(id="rib", kind=StepKind.GRIND, zone="zone", zone_id=1,
                pos=(0.9, 0.9), skills=("TRAVEL_TO", "GRIND_UNTIL"), level=(1, 10))
    rt.graph = Graph(graph_id="rib", faction="alliance", entry=node.id, nodes=(node,))
    rt.tick()
    assert rt.armed.decision.skill == ("COMBAT_PROFILE" if combat else "TRAVEL_TO")
    assert "until_level" not in rt.armed.decision.params


def test_terminal_step_is_completed_and_persisted_once(tmp_path):
    saved = []
    rt = runtime(tmp_path, [seen(0, quests=(Quest(quest_id=1),)), seen(1), seen(2), seen(3)],
                 start_step="turnin",
                 on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_: saved.append((step, done)))
    rt.run(4, 0)
    assert rt.finished
    assert rt.completed == {1}
    assert rt.counters.advances == 1
    assert saved[-1] == ("turnin", {1})
    assert rt.armed.decision.intent is Intent.WAIT


def test_a_finished_guide_is_saved_as_finished(tmp_path):
    """So the next session can take the guide that follows it (`jev.run.cli.NEXT_GUIDE`)."""
    saved = []
    rt = runtime(tmp_path, [seen(0, quests=(Quest(quest_id=1),)), seen(1), seen(2), seen(3)],
                 start_step="turnin",
                 on_progress=lambda *args, finished=False, **_: saved.append(finished))
    rt.run(4, 0)
    assert rt.finished and saved[-1] is True and saved[0] is False


def test_a_saved_step_removed_by_regeneration_resumes_from_observed_predicates(tmp_path):
    rt = runtime(tmp_path, [seen(quests=(Quest(quest_id=1),))], start_step="removed_step")
    rt.tick()
    assert rt.tracker.step_id == "turnin"
    assert rt.armed.decision.skill == "TRAVEL_TO"


def test_a_teacher_answer_cannot_arm_movement_on_a_blind_state(tmp_path):
    rt = runtime(tmp_path, [State(t=0, client_id="c")], take=lambda key: answer())
    rt.tick()
    assert rt.armed.decision.skill is None
    assert rt.counters.teacher_applied == 0


def test_turnin_is_not_completed_when_the_log_is_mid_cycle(tmp_path):
    rt = runtime(tmp_path, [seen(0, quests=(Quest(quest_id=1),)), seen(1, quests=None)],
                 start_step="turnin")
    rt.run(2, 0)
    assert not rt.finished
    assert rt.completed == set()


def rib_graph():
    from jev.guide.graph import FailEdge, FailWhen

    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), timeout_s=10.0,
             on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=10, goto="rib"),), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=2,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        # Its creatures suit levels 3 to 5 (`rib_fits`): a rib above the character is none
        # to fail into (V329).
        Node(id="rib", kind=StepKind.GRIND, level=(1, 10), skills=("TRAVEL_TO", "GRIND_UNTIL"),
             mob_levels=(3, 4), **base),
    ))


def held(t, level):
    from jev.world.state_v1 import Char

    return seen(t, char=Char(level=level), quests=(Quest(quest_id=1, complete=True),))


def test_a_step_that_failed_into_a_rib_is_retried_once_then_passed_over(tmp_path):
    """A hand-in that timed out and was passed over left quest 15 complete in the log for
    good, with quest 21 behind it. Retried once after its rib; a second failure moves on,
    so a step that cannot succeed costs two ribs, not the run."""
    saved = []
    states = [held(0, 3), held(12, 3), held(13, 4), held(25, 4), held(26, 5)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                       on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_: saved.append((step, rejoin)))
    visited = []
    for _ in states:
        rt.tick(choose=False)
        visited.append((rt.tracker.step_id, rt.tracker.memory.rejoin_to))
    assert visited == [("turnin", None), ("rib", "turnin"), ("turnin", None),
                       ("rib", "after"), ("after", None)]
    assert ("rib", "turnin") in saved and ("rib", "after") in saved, "the way back was not saved"


def test_a_rib_with_no_way_back_rejoins_the_first_step_not_done(tmp_path):
    states = [held(0, 3), held(1, 4)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path))
    rt.tick(choose=False)
    rt.tracker.enter("rib", states[0])          # a rib entered with no rejoin point
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin" and not rt.finished


def test_a_step_fails_into_the_rib_for_the_characters_own_level(tmp_path):
    """The guide names a rib for the step's quest level; the character may not be there."""
    from jev.guide.graph import FailEdge, FailWhen

    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, timeout_s=10.0,
             skills=("TRAVEL_TO", "TURNIN_QUEST"), level=(5, 8),
             on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=10, goto="boars"),), **base),
        Node(id="wolves", kind=StepKind.GRIND, level=(1, 3), skills=("GRIND_UNTIL",), **base),
        Node(id="boars", kind=StepKind.GRIND, level=(5, 7), skills=("GRIND_UNTIL",), **base),
    ))
    rt = ClientRuntime("c", graph, ScriptedSource([held(0, 3), held(12, 3)]), Recorder(tmp_path))
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("wolves", "turnin")


def test_deaths_on_a_step_outlive_the_session_that_counted_them(tmp_path):
    """Counted in memory alone, a rib's four deaths in two sessions never added up."""
    saved = []
    states = [held(0, 6), held(1, 6)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="rib", start_rejoin="turnin", start_deaths=2,
                       on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_: saved.append((step, deaths)))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin", "a rib that killed twice is left for its way back"

    from jev.guide import playhead
    path = tmp_path / "character.json"
    playhead.save("g", "rib", {1}, path, rejoin_to="turnin", deaths=3)
    assert playhead.load("g", path).deaths == 3
    playhead.save("g", "rib", {1}, path, rejoin_to="turnin")
    assert playhead.load("g", path).deaths == 0


def test_a_step_retried_in_an_earlier_session_is_passed_over_on_its_next_failure(tmp_path):
    """Kept in memory alone, every fifteen-minute session gave quest 3905's hand-in its
    first failure afresh, and it cycled between Brother Neals' stair and the wolves."""
    saved = []
    states = [held(0, 3), held(12, 3)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_retried=frozenset({"turnin"}),
                       on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_:
                       saved.append((step, rejoin, retried)))
    for _ in states:
        rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib", "after")
    assert saved[-1][2] == frozenset({"turnin"})

    from jev.guide import playhead
    path = tmp_path / "character.json"
    playhead.save("g", "rib", {1}, path, rejoin_to="after", retried={"turnin"})
    assert playhead.load("g", path).retried == frozenset({"turnin"})
    assert playhead.load("other", path).retried == frozenset(), "another guide's steps"
    playhead.save("g", "rib", {1}, path)
    assert playhead.load("g", path).retried == frozenset()


def test_a_steps_own_skill_out_of_attempts_takes_the_steps_fail_edge(tmp_path):
    """With one attempt a session, a hand-in the Abbey's stair defeats stopped every
    session, and each new one tried the step afresh with its timeout never reached (run
    20260924T081531-4249a4)."""
    saved = []
    states = [held(0, 3)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="turnin",
                       on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_:
                       saved.append((step, rejoin, retried)))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin"
    assert rt.fail_over("VENDOR_REPAIR", "no merchant") is False, "not the step failing"
    assert rt.fail_over("TURNIN_QUEST", "no observed nameplate") is True
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib", "turnin")
    assert saved[-1] == ("rib", "turnin", frozenset({"turnin"}))

    rt.tracker.enter("turnin", states[0])            # back from the rib
    assert rt.fail_over("TURNIN_QUEST", "no observed nameplate") is True
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib", "after"), \
        "retried once, then passed over"


def test_only_a_step_that_killed_the_character_earns_a_whole_rib(tmp_path):
    from jev.guide.tracker import SHORT_RIB_S

    saved = []
    states = [held(0, 3), held(12, 3)]
    rt = ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                       on_progress=lambda *args, **_: saved.append(args))
    rt.tick(choose=False)
    rt.tick(choose=False)                            # the hand-in times out
    assert rt.tracker.step_id == "rib"
    assert rt.tracker.memory.until == pytest.approx(12 + SHORT_RIB_S)
    assert saved[-1][5] == pytest.approx(12 + SHORT_RIB_S), "the rib's end is saved"

    from jev.guide.tracker import Event
    from jev.guide.tracker import Verdict as TrackVerdict

    rt2 = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                        start_step="turnin")
    rt2.tick(choose=False)
    rt2._apply(TrackVerdict(Event.FAIL, goto="rib", reason="deaths_on_step=3.0"), held(1, 3))
    assert rt2.tracker.step_id == "rib" and rt2.tracker.memory.until is None


def above_graph():
    """`rib_graph` with its one rib's creatures three levels above a level 3 (V329)."""
    g = rib_graph()
    return g.model_copy(update={"nodes": tuple(
        n.model_copy(update={"mob_levels": (6, 7)}) if n.id == "rib" else n for n in g.nodes)})


def test_with_no_grind_for_the_level_a_failed_step_is_tried_again_at_once(tmp_path):
    """V329: with every rib that suited it barred, the lowest of the rest was taken, and
    hive-200, a level 8 dwarf hunter, climbed Dun Morogh's and Loch Modan's ribs to their 17-19
    and 19-20, dying on most (29 Sep, 11:42 to 15:02). A rib above the character or grey to it is
    none: a step that failed without dying is tried again at once, then passed over, as after a
    rib, and the run goes on."""
    saved = []
    states = [held(0, 3), held(12, 3), held(13, 3), held(25, 3)]
    rt = ClientRuntime("c", above_graph(), ScriptedSource(states), Recorder(tmp_path),
                       on_progress=lambda step, done, rejoin, deaths, retried=frozenset(),
                       until=None, **_: saved.append((step, rejoin, retried)))
    rt.tick(choose=False)
    rt.tick(choose=False)                            # the hand-in times out
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("turnin", None)
    assert rt.tracker.memory.until is None and "turnin" in rt._retried
    assert saved[-1] == ("turnin", None, frozenset({"turnin"}))
    rt.tick(choose=False)
    rt.tick(choose=False)                            # and again: passed over
    assert rt.tracker.step_id == "after" and not rt.finished

    out = ClientRuntime("c", above_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                        start_step="turnin")
    out.tick(choose=False)
    assert out.fail_over("TURNIN_QUEST", "no observed nameplate") is True, "the run goes on"
    assert out.tracker.step_id == "turnin" and out.tracker.memory.attempts == 0


def test_with_no_grind_for_the_level_a_step_that_kills_is_passed_over(tmp_path):
    """V329: a level cures a step that kills the character; with no grind for its level, the
    step is passed over, and the last step failing ends the guide."""
    from jev.guide.tracker import Event
    from jev.guide.tracker import Verdict as TrackVerdict

    rt = ClientRuntime("c", above_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="turnin")
    rt.tick(choose=False)
    rt._apply(TrackVerdict(Event.FAIL, goto="rib", reason="deaths_on_step=3.0"), held(1, 3))
    assert rt.tracker.step_id == "after" and "turnin" in rt._retried

    g = above_graph()
    last = g.model_copy(update={"nodes": tuple(
        n.model_copy(update={"next": ()}) if n.id == "turnin" else n for n in g.nodes)})
    end = ClientRuntime("c", last, ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                        start_step="turnin")
    end.tick(choose=False)
    end._apply(TrackVerdict(Event.FAIL, goto="rib", reason="deaths_on_step=3.0"), held(1, 3))
    assert end.finished, "nothing after it and no grind: the guide is done"


def test_a_barred_rib_that_suits_comes_before_an_unbarred_one_above(tmp_path):
    """V329: the bar keeps a failure off a rib while another suits the character; with none, the
    barred one again, never one above."""
    g = rib_graph()
    nodes = (*g.nodes, Node(id="high", kind=StepKind.GRIND, zone="zone", zone_id=1, level=(7, 9),
                            mob_levels=(7, 8), pos=(0.5, 0.5), skills=("GRIND_UNTIL",)))
    rt = ClientRuntime("c", g.model_copy(update={"nodes": nodes}),
                       ScriptedSource([held(0, 3), held(12, 3)]), Recorder(tmp_path),
                       start_retried=frozenset({"rib-bar:rib@3"}))
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib", "turnin")


def test_a_rib_the_route_leaves_out_is_never_ground(tmp_path):
    """V331: a rib in a capital's box, marked where the guide is played, is no grind: no failure
    goes to it, and a playhead on it starts from the guide's entry."""
    g = rib_graph()
    nodes = (*g.nodes, Node(id="city", kind=StepKind.GRIND, zone="zone", zone_id=1, level=(1, 10),
                            mob_levels=(3, 4), pos=(0.5, 0.5), skills=("GRIND_UNTIL",),
                            route_blocked_reason="a grind in a capital's map box"))
    blocked = g.model_copy(update={"nodes": (nodes[-1], *nodes[:-1])})
    rt = ClientRuntime("c", blocked, ScriptedSource([held(0, 3), held(12, 3)]), Recorder(tmp_path))
    assert [r.id for r in rt._ribs_all] == ["rib"]
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib"
    on = ClientRuntime("c", blocked, ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="city", start_rejoin="turnin")
    on.tick(choose=False)
    assert on.tracker.step_id == "turnin", "from the entry: the accept is done, the hand-in next"


def test_characters_failing_the_same_step_spread_over_its_ribs(tmp_path):
    """V332: the character's key (`char.key`, `character_key`) spreads the ribs as good for its
    level: the hive's 40 bots on one rib (29 Sep). The same character, the same rib."""
    from jev.guide.graph import spread_rank
    from jev.guide.tracker import Event
    from jev.guide.tracker import Verdict as TrackVerdict

    g = rib_graph()
    twin = Node(id="rib_two", kind=StepKind.GRIND, zone="zone", zone_id=1, level=(1, 10),
                mob_levels=(3, 4), pos=(0.5, 0.5), skills=("GRIND_UNTIL",))
    graph = g.model_copy(update={"nodes": (*g.nodes, twin)})
    first = {k: max(("rib", "rib_two"), key=lambda r: spread_rank(k, r)) for k in range(20)}
    keys = (next(k for k, r in first.items() if r == "rib"),
            next(k for k, r in first.items() if r == "rib_two"))
    went = []
    for key in (*keys, keys[0]):
        rt = ClientRuntime("c", graph, ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                           start_step="turnin", character_key=key)
        rt.tick(choose=False)
        rt._apply(TrackVerdict(Event.FAIL, goto="rib", reason="deaths_on_step=3.0"), held(1, 3))
        went.append(rt.tracker.step_id)
    assert went == ["rib", "rib_two", "rib"]


def test_a_short_ribs_end_outlives_the_session(tmp_path):
    from jev.guide import playhead

    path = tmp_path / "character.json"
    playhead.save("g", "rib", {1}, path, rejoin_to="turnin", rib_until=1234.5)
    assert playhead.load("g", path).rib_until == 1234.5
    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="rib", start_rejoin="turnin", start_rib_until=1234.5)
    rt.tick(choose=False)
    assert rt.tracker.memory.until == 1234.5


def test_a_steps_entry_level_outlives_the_session(tmp_path):
    """V214: each session entered the rib afresh, and a level 15 paladin failed over at 14
    was still asked for 16 (26 September)."""
    from jev.guide import playhead

    path = tmp_path / "character.json"
    playhead.save("g", "rib", {1}, path, rejoin_to="turnin", entry_level=14)
    assert playhead.load("g", path).entry_level == 14
    saved = []
    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="rib", start_rejoin="turnin", start_entry_level=14,
                       on_progress=lambda *args, entry_level=None, **_: saved.append(entry_level))
    rt.tick(choose=False)
    assert rt.tracker.memory.level_at_entry == 14
    assert saved and saved[-1] == 14, "and saved again"
    playhead.save("g", "rib", {1}, path, entry_level=True)
    assert playhead.load("g", path).entry_level is None, "only a level"


def test_the_rest_of_a_quest_whose_accept_was_passed_over_is_skipped(tmp_path):
    """Quest 16's accept failed twice and was passed over; its objective then stopped a
    session on the quest's absence and would have cost two ribs and a walk to Gerard."""
    from jev.guide.tracker import Event
    from jev.guide.tracker import Verdict as TrackVerdict

    guide = Graph.load("content/tbc/ally_human_1_12.json")
    rt = ClientRuntime("c", guide, ScriptedSource([seen(0)]), Recorder(tmp_path))
    accept = "alli_human_1_12_16_give_gerard_a_drink_accept"
    rt.tracker.enter("alli_human_1_12_16_give_gerard_a_drink_do", seen(0))
    missing = TrackVerdict(Event.FAIL, goto="alli_human_1_12_grind_elwynn_1_3",
                           reason="quest_missing=None")
    assert rt._past_abandoned_quest(missing) is None, "its accept was never passed over"
    rt._retried.add(accept)
    beyond = rt._past_abandoned_quest(missing)
    assert beyond is not None and guide.get(beyond).quest_id != 16
    rt._apply(missing, seen(1))
    assert rt.tracker.step_id == beyond, "went to a rib instead of on"
    out_of_attempts = TrackVerdict(Event.FAIL, goto="alli_human_1_12_grind_elwynn_1_3",
                                   reason="GRIND_UNTIL out of attempts: quest absent from readable log")
    rt.tracker.enter("alli_human_1_12_16_give_gerard_a_drink_turnin", seen(2))
    assert rt._past_abandoned_quest(out_of_attempts) == beyond
    timeout = TrackVerdict(Event.FAIL, goto="alli_human_1_12_grind_elwynn_1_3",
                           reason="timeout_s=240.0")
    assert rt._past_abandoned_quest(timeout) is None, "only the quest's absence skips it"


def test_a_step_of_an_abandoned_quest_is_skipped_before_it_is_walked_to(tmp_path):
    guide = Graph.load("content/tbc/ally_human_1_12.json")
    turnin = "alli_human_1_12_16_give_gerard_a_drink_turnin"
    log_without_16 = seen(0, quests=(Quest(quest_id=783),))
    rt = ClientRuntime("c", guide, ScriptedSource([log_without_16]), Recorder(tmp_path))
    rt.tracker.enter(turnin, log_without_16)
    assert rt._abandoned_now(log_without_16) is None, "its accept was never passed over"
    rt._retried.add("alli_human_1_12_16_give_gerard_a_drink_accept")
    beyond = rt._abandoned_now(log_without_16)
    assert beyond is not None and guide.get(beyond).quest_id != 16
    assert rt._abandoned_now(seen(0, quests=None)) is None, "an unread log skips nothing"
    assert rt._abandoned_now(seen(0, quests=(Quest(quest_id=16),))) is None


def test_a_fight_on_the_way_stops_the_step_s_clock_like_a_meal():
    """A dozen kobolds on the way back from Fargodeep Mine ran quest 60's hand-in out of
    its four minutes, the quest complete: the walk to each and its corpse were counted."""
    from jev.orch.runtime import SERVICING_SKILLS

    assert {"COMBAT_PROFILE", "EAT_DRINK", "LOOT"} <= SERVICING_SKILLS
    assert "ABORT_WAIT" not in SERVICING_SKILLS


def test_a_fight_on_the_way_is_not_an_attempt_at_the_step(tmp_path):
    """A fight whose target vanished made a quest accept's first timeout its second
    attempt, and "not offered" sent the character to a grind 1,558 yards away and back for
    a quest that was there all along (session 90)."""
    from dataclasses import replace

    rt = runtime(tmp_path, [seen(t / 2) for t in range(4)])
    rt.tick(choose=True)
    step_work = rt.armed
    assert step_work.rule.startswith("guide.") and step_work.step_id == "accept"
    rt.armed = replace(step_work, rule="fight.rotation")
    rt.finish(SkillOutcome.ABORTED, "target disappeared without observed death")
    assert rt.tracker.memory.attempts == 0, "a fight on the way is not the step failing"
    rt.armed = step_work
    rt.finish(SkillOutcome.TIMED_OUT, "skill timeout")
    assert rt.tracker.memory.attempts == 1


def detour_runtime(tmp_path, states):
    """At the step after quest 1's hand-in, which failed twice and was passed over."""
    return ClientRuntime("c", rib_graph(), ScriptedSource(states), Recorder(tmp_path),
                         start_step="after", start_retried=frozenset({"turnin"}))


def test_a_passed_over_hand_in_within_reach_is_made_on_the_way(tmp_path):
    """Kobold Candles sat complete in the log after its hand-in was passed over, with
    William Pestle twenty yards from the NPC the guide kept returning to (25 September)."""
    from jev.orch.runtime import DETOUR

    states = [held(0, 5), held(1, 5), seen(2)]          # the last: quest 1 handed in
    rt = detour_runtime(tmp_path, states)
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("turnin", "after")
    assert DETOUR + "turnin@5" in rt._retried, "spent at the level it was tried at"
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin", "still in the log: still handing it in"
    rt.tick(choose=False)
    assert rt.tracker.step_id == "after" and 1 in rt.completed, "handed in and back"


def test_a_quest_finished_after_its_hand_in_was_left_is_handed_in_on_the_way(tmp_path):
    """V234: the mage finished Kobold Candles after its objective was passed over and its
    hand-in passed by, and walked on with it complete in the log."""
    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 5), held(1, 5)]),
                       Recorder(tmp_path), start_step="after")
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("turnin", "after")


def test_a_hand_in_on_the_way_that_fails_goes_straight_back_and_is_not_tried_again(tmp_path):
    states = [held(t, 5) for t in (0, 1, 13, 14, 15)]
    rt = detour_runtime(tmp_path, states)
    visited = []
    for _ in states:
        rt.tick(choose=False)
        visited.append((rt.tracker.step_id, rt.tracker.memory.rejoin_to))
    assert visited[1] == ("turnin", "after")
    assert ("rib", "after") not in visited, "a failed detour is not worth a rib"
    assert visited[2:] == [("after", None)] * 3, "back, and once only"


def test_a_level_is_another_try_at_a_hand_in_on_the_way(tmp_path):
    """Kobold Candles, Collecting Kelp and the Grape Manifest spent their one detour on an
    inn and an abbey whose stairs the walk could not yet climb, and sat complete in the log
    after the walk was fixed (25 September)."""
    states = [held(0, 5), held(1, 5), held(13, 5), held(14, 5), held(15, 6), held(16, 6)]
    rt = detour_runtime(tmp_path, states)
    visited = []
    for _ in states:
        rt.tick(choose=False)
        visited.append((rt.tracker.step_id, rt.tracker.memory.rejoin_to))
    assert visited[2:4] == [("after", None)] * 2, "failed at level 5: back"
    assert visited[4] == ("turnin", "after"), "at level 6, on the way again"


def test_a_detour_spent_before_levels_were_kept_counts_for_the_level_first_read(tmp_path):
    from jev.orch.runtime import DETOUR

    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 5), held(1, 6)]),
                       Recorder(tmp_path), start_step="after",
                       start_retried=frozenset({"turnin", DETOUR + "turnin"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "after" and DETOUR + "turnin@5" in rt._retried
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin", "a level later, another try"


def chain_graph():
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("next_accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="next_accept", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("next_turnin",),
             quest_prerequisites=((1,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="next_turnin", kind=StepKind.QUEST_TURNIN, quest_id=2, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=3,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
    ))


@pytest.mark.parametrize(("retried", "expected"), [
    (frozenset({"turnin", "detour:turnin"}), "after"),   # passed over, its detour spent
    (frozenset({"turnin"}), "turnin"),                    # its one detour comes first
])
def test_an_accept_waiting_on_a_lost_hand_in_is_passed_by(tmp_path, retried, expected):
    """The Escape waits on Collecting Kelp's hand-in, lost in the Lion's Pride Inn
    (session 109); trying it anyway costs a rib, a retry and a pass-over."""
    rt = ClientRuntime("c", chain_graph(), ScriptedSource([held(0, 11), held(1, 11)]),
                       Recorder(tmp_path), start_step="next_accept", start_retried=retried)
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert rt.tracker.step_id == expected


def blocked_chain_graph():
    """Quest 1 under way; quest 2 needs it, and quest 3 needs quest 2."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="do", nodes=(
        Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("next_accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="next_accept", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("next_turnin",),
             quest_prerequisites=((1,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="next_turnin", kind=StepKind.QUEST_TURNIN, quest_id=2, next=("chain_accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="chain_accept", kind=StepKind.QUEST_ACCEPT, quest_id=3, next=("chain_turnin",),
             quest_prerequisites=((2,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="chain_turnin", kind=StepKind.QUEST_TURNIN, quest_id=3, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=4,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
    ))


@pytest.mark.parametrize(("complete", "retried", "expected"), [
    (False, frozenset({"do"}), "after"),            # never finished: the chain on it goes too
    (True, frozenset({"do"}), "next_accept"),       # finished after all: it can be offered
    (False, frozenset(), "next_accept"),            # not passed over: the accept is tried
])
def test_an_accept_needing_a_quest_whose_objective_was_passed_over_is_passed_by(
        tmp_path, complete, retried, expected):
    """V219: Wolves Across the Border sat at 4 of 8, its objective passed over, and the
    accept of Milly Osworth, which needs it, took Deputy Willem's other quest instead and
    failed over to a rib (session 189). Milly's Harvest and the Grape Manifest need it in
    turn."""
    states = [seen(t, quests=(Quest(quest_id=1, complete=complete),)) for t in range(3)]
    rt = ClientRuntime("c", blocked_chain_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="next_accept", start_retried=retried)
    for _ in states:
        rt.tick(choose=False)
    assert rt.tracker.step_id == expected


def test_a_rib_waiting_to_retry_an_accept_that_cannot_happen_ends(tmp_path):
    """V220: the mage ground level 1-3 kobolds on a rib to retry Milly's Harvest and the Grape
    Manifest, which need Milly Osworth, itself waiting on a quest given up (sessions 190-191)."""
    from jev.guide.graph import Graph as G

    graph = blocked_chain_graph()
    rib = Node(id="rib", kind=StepKind.GRIND, level=(1, 10), skills=("TRAVEL_TO", "GRIND_UNTIL"),
               zone="zone", zone_id=1, pos=(0.5, 0.5))
    graph = G(graph_id=graph.graph_id, faction=graph.faction, entry=graph.entry,
              nodes=graph.nodes + (rib,))
    states = [seen(t, quests=(Quest(quest_id=1, complete=False),)) for t in range(3)]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                       start_step="rib", start_rejoin="chain_accept",
                       start_retried=frozenset({"do"}))
    for _ in states:
        rt.tick(choose=False)
    assert rt.tracker.step_id == "after"
    still = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path / "b"),
                          start_step="rib", start_rejoin="chain_accept")
    for _ in states:
        still.tick(choose=False)
    assert still.tracker.step_id == "rib", "a retry that can happen waits for the rib"


def accept_chain_graph():
    """Quest 1's accept; quest 2 needs quest 1, and quest 3 needs quest 2."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("next_accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="next_accept", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("next_turnin",),
             quest_prerequisites=((1,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="next_turnin", kind=StepKind.QUEST_TURNIN, quest_id=2, next=("chain_accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="chain_accept", kind=StepKind.QUEST_ACCEPT, quest_id=3, next=("chain_turnin",),
             quest_prerequisites=((2,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="chain_turnin", kind=StepKind.QUEST_TURNIN, quest_id=3, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=4, next=("rib",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(1, 60),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
    ))


@pytest.mark.parametrize(("held", "retried", "start", "rejoin", "expected"), [
    ((), frozenset({"accept"}), "next_accept", None, "after"),     # never offered: the chain goes
    ((1,), frozenset({"accept"}), "next_accept", None, "next_accept"),  # taken after all
    ((), frozenset(), "next_accept", None, "next_accept"),         # never tried: it may be offered
    ((), frozenset({"accept"}), "rib", "accept", "rib"),           # a rib waits to try it again
])
def test_an_accept_needing_a_quest_whose_accept_was_passed_over_is_passed_by(
        tmp_path, held, retried, start, rejoin, expected):
    """V245: A Fishy Peril's accept was passed over in the Lion's Pride Inn, and Further
    Concerns, which needs it, was walked to twice from Westbrook through Mangy Wolves; the
    Marshal offered something else, and three deaths came on the road (sessions 218-219)."""
    states = [seen(t, quests=tuple(Quest(quest_id=q, complete=False) for q in held))
              for t in range(3)]
    rt = ClientRuntime("c", accept_chain_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step=start, start_rejoin=rejoin, start_retried=retried)
    for _ in states:
        rt.tick(choose=False)
    assert rt.tracker.step_id == expected


def objective_graph():
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("do",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=2,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
    ))


@pytest.mark.parametrize(("complete", "retried", "expected"), [
    (False, frozenset({"do"}), "after"),       # passed over, never finished: nothing to hand in
    (True, frozenset({"do"}), "turnin"),       # finished after all: hand it in
    (False, frozenset(), "turnin"),            # not passed over: the hand-in waits on the log
])
def test_a_hand_in_whose_objective_was_passed_over_is_passed_by(tmp_path, complete, retried,
                                                                  expected):
    """Goldtooth's objective was passed over at the bottom of Fargodeep Mine, and its
    hand-in came next with the necklace never taken (session 116)."""
    from jev.world.state_v1 import Char

    states = [seen(t, char=Char(level=12), quests=(Quest(quest_id=1, complete=complete),))
              for t in (0, 1)]
    rt = ClientRuntime("c", objective_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="turnin", start_retried=retried)
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert rt.tracker.step_id == expected


def band_graph():
    """Quest 1 under way, then an accept; quests 5 and 6 handed in near here, quest 7 in a
    city and quest 8 across the zone."""
    here = dict(zone="zone", zone_id=1, coord_zone_id=12)
    return Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("do",), pos=(0.5, 0.5),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **here),
        Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, next=("turnin",),
             pos=(0.5, 0.5), skills=("TRAVEL_TO", "GRIND_UNTIL"), **here),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("next_accept",),
             pos=(0.5, 0.5), skills=("TRAVEL_TO", "TURNIN_QUEST"), **here),
        Node(id="next_accept", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("far_turnin",),
             pos=(0.5, 0.5), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **here),
        Node(id="far_turnin", kind=StepKind.QUEST_TURNIN, quest_id=5, next=("near_turnin",),
             pos=(0.7, 0.6), skills=("TRAVEL_TO", "TURNIN_QUEST"), **here),
        Node(id="near_turnin", kind=StepKind.QUEST_TURNIN, quest_id=6, next=("city_turnin",),
             pos=(0.55, 0.5), skills=("TRAVEL_TO", "TURNIN_QUEST"), **here),
        Node(id="city_turnin", kind=StepKind.QUEST_TURNIN, quest_id=7, zone="city", zone_id=2,
             coord_zone_id=1519, pos=(0.5, 0.5), next=("across_turnin",),
             skills=("TRAVEL_TO", "TURNIN_QUEST")),
        Node(id="across_turnin", kind=StepKind.QUEST_TURNIN, quest_id=8, pos=(0.95, 0.95),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **here),
    ))


def at_level(t, level, *complete, doing=()):
    from jev.world.state_v1 import Char

    quests = tuple(Quest(quest_id=q, complete=True) for q in complete) + tuple(
        Quest(quest_id=q, complete=False) for q in doing)
    return seen(t, char=Char(level=level), quests=quests,
                pos=Pos(zone="zone", coord_zone_id=12, mx=0.5, my=0.5))


def test_an_outgrown_guide_hands_in_what_is_done_nearby_then_ends(tmp_path):
    """V162: at 13, Testvvi had 26 steps of the 1-12 guide left, its mobs three to five
    levels below it. Hand-ins within reach come first, nearest first; the city's and one
    across the zone are not a trip back; then the guide is done, for the next."""
    saved = []
    states = [at_level(0, 13, 5, 6, 7, 8), at_level(1, 13, 5, 7, 8), at_level(2, 13, 7, 8)]
    rt = ClientRuntime("c", band_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="next_accept", outgrown_at=13,
                       on_progress=lambda *a, finished=False, **_: saved.append(finished))
    visited = []
    for _ in states:
        rt.tick(choose=False)
        visited.append(rt.tracker.step_id)
    assert visited[:2] == ["near_turnin", "far_turnin"], "nearest first; the city's passed by"
    assert rt.finished and {5, 6} <= rt.completed and not {7, 8} & rt.completed
    assert saved[-1] is True, "the playhead says the guide is done"


@pytest.mark.parametrize(("start", "state", "expected"), [
    ("next_accept", at_level(0, 12, 5), "next_accept"),     # below the band: the guide goes on
    ("do", at_level(0, 13, doing=(1,)), "do"),               # a quest under way is finished
    ("turnin", at_level(0, 13, 1), "turnin"),                # and handed in
])
def test_the_guide_goes_on_below_the_band_and_while_a_quest_is_under_way(tmp_path, start,
                                                                         state, expected):
    rt = ClientRuntime("c", band_graph(), ScriptedSource([state]), Recorder(tmp_path),
                       start_step=start, outgrown_at=13)
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.finished) == (expected, False)


def test_an_outgrown_guide_does_not_retry_a_hand_in_passed_over(tmp_path):
    rt = ClientRuntime("c", band_graph(), ScriptedSource([at_level(0, 13, 6)]),
                       Recorder(tmp_path), start_step="next_accept", outgrown_at=13,
                       start_retried=frozenset({"near_turnin"}))
    rt.tick(choose=False)
    assert rt.finished


def test_only_a_guide_with_a_next_is_outgrown():
    from jev.run.cli import NEXT_GUIDE, OUTGROWN_AT

    assert set(OUTGROWN_AT) <= set(NEXT_GUIDE)


def test_the_quest_under_way_when_the_band_applies_is_handed_in_however_far():
    """V177: the Riverpaw bounty came to 8 of 8 at the camp, and its hand-in across Elwynn
    was left for the next guide, which never makes it (session 137)."""
    from jev.world.state_v1 import Char

    far = band_graph().model_copy(update={"nodes": tuple(
        n.model_copy(update={"pos": (0.95, 0.95)}) if n.id == "turnin" else n
        for n in band_graph().nodes)})
    doing = seen(0, char=Char(level=13), quests=(Quest(quest_id=1, complete=False),),
                 pos=Pos(zone="zone", coord_zone_id=12, mx=0.5, my=0.5))
    done = doing.model_copy(update={"t": 1, "quests": (Quest(quest_id=1, complete=True),)})
    rt = ClientRuntime("c", far, ScriptedSource([doing, done, done]), Recorder("/tmp"),
                       start_step="do", outgrown_at=13)
    for _ in range(3):
        rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin" and not rt.finished, "its hand-in, 0.63 away"


def finished_guide_graph():
    """Two quest steps behind the character, and a grind of its level."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="old_accept", nodes=(
        Node(id="old_accept", kind=StepKind.QUEST_ACCEPT, quest_id=40, next=("last",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="last", kind=StepKind.QUEST_TURNIN, quest_id=59,
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="rib_9_11", kind=StepKind.GRIND, level=(9, 11),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
    ))


@pytest.mark.parametrize(("flag", "levels", "expected_step", "finished"), [
    (True, (10, 10, 10), "rib_9_11", False),     # grinding on for its level
    (True, (10, 11, 12), "rib_9_11", True),      # the level reached: the guide done again
    (False, (10, 10, 10), "old_accept", False),  # before V262: scanned from the entry
])
def test_a_finished_guides_grind_is_resumed_and_finishes_it(tmp_path, flag, levels,
                                                            expected_step, finished):
    """V262: a playhead put on a finished guide's grind was scanned from the entry, and the
    level 9 mage walked 2,000 yards back to Milly Osworth's quest, passed over long before
    (session 237)."""
    from jev.world.state_v1 import Char

    states = [seen(t, char=Char(level=lvl), quests=()) for t, lvl in enumerate(levels)]
    rt = ClientRuntime("c", finished_guide_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="rib_9_11", start_entry_level=11,
                       start_grind_then_finish=flag, completed={59})
    for _ in states:
        rt.tick(choose=False)
    assert rt.tracker.step_id == expected_step
    assert rt.finished is finished


def leave_graph():
    """A step, a grind beside the death camp by (5, 0) and one 500 yards from it."""
    base = dict(zone="zone", zone_id=1, map_id=0)
    return Graph(graph_id="g", faction="alliance", entry="step", nodes=(
        Node(id="step", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("after",), pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=2, pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="camp_rib", kind=StepKind.GRIND, level=(1, 10), pos=(0.51, 0.5),
             world=(40.0, 0.0, 0.0), skills=("GRIND_UNTIL",), **base),
        Node(id="far_rib", kind=StepKind.GRIND, level=(1, 10), pos=(0.9, 0.9),
             world=(400.0, 300.0, 0.0), skills=("GRIND_UNTIL",), **base),
    ))


def test_after_a_death_in_a_death_camp_the_grind_of_its_level_comes_before_any_service(
        tmp_path):
    """V307: after each of Merany's first three deaths at one spot by Raven Hill's graveyard
    came a meal and its walk to a repairer, back through the spot (the hive, 28 Sep
    12:15-12:23). Up again, the character walks to the grind of its level out of the camp,
    the step it was on kept for after, and no service is armed until it is there."""
    from jev.world.state_v1 import Char

    broken = dict(char=Char(level=5), bags=Bags(free=10, durability_min=0.0, money_copper=500))
    states = [seen(0, **broken),
              seen(1, vitals=Vitals(hp=0, power=0, dead=True, ghost=False, combat=False),
                   **broken),
              seen(2, **broken), seen(3, **broken),
              seen(4, pos=Pos(zone="zone", mx=0.9, my=0.9), **broken)]
    rt = ClientRuntime("c", leave_graph(), ScriptedSource(states), Recorder(tmp_path))
    rt.tick(choose=False)
    rt.policy_context.camp_left(0, 5.0, 0.0)              # the body, at the release
    rt.tick(choose=False)
    assert rt.tracker.step_id == "step", "not while dead"
    rt.tick()
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("far_rib", "step")
    assert rt.armed.decision.skill == "GRIND_UNTIL", "the grind before the repair"
    rt.tick()
    assert rt.armed.decision.skill == "GRIND_UNTIL"
    rt.tick()                                               # there: the leave is made
    assert rt.tracker.step_id == "far_rib"
    assert rt.armed.decision.skill == "VENDOR_REPAIR", "then the services, as before"
    assert rt.policy_context.death_camp is None and not rt.policy_context.leaving(4)


def test_the_way_out_of_a_death_camp_is_no_wait_for_its_step(tmp_path):
    """V307 with V220: a grind that waits to retry a step ends when the step cannot happen,
    but the grind a death camp is left for is no wait for the step: the leave is made first."""
    from jev.world.state_v1 import Char

    base = dict(zone="zone", zone_id=1, map_id=0)
    graph = Graph(graph_id="g", faction="alliance", entry="a7", nodes=(
        Node(id="a7", kind=StepKind.QUEST_ACCEPT, quest_id=7, next=("step",), pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="step", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("after",), pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), quest_prerequisites=((7,),),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=2, pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="far_rib", kind=StepKind.GRIND, level=(1, 10), pos=(0.9, 0.9),
             world=(400.0, 300.0, 0.0), skills=("GRIND_UNTIL",), **base),
    ))
    there = Pos(zone="zone", mx=0.9, my=0.9)
    states = [seen(0, char=Char(level=5)), seen(1, char=Char(level=5)),
              seen(2, char=Char(level=5), pos=there), seen(3, char=Char(level=5), pos=there)]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                       start_step="step", start_retried=frozenset({"a7"}))
    rt.policy_context.camp_left(0, 5.0, 0.0)
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("far_rib", "step")
    rt.tick(choose=False)
    assert rt.tracker.step_id == "far_rib", "the leave abandoned for a step that cannot happen"
    rt.tick(choose=False)                                  # there
    rt.tick(choose=False)
    assert rt.tracker.step_id == "after", "then the grind's way back that cannot happen ends"


def level_graph():
    """A hand-in, then an accept whose quest asks level 5 (its band's floor, MinLevel)."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    return Graph(graph_id="g", faction="alliance", entry="before", nodes=(
        Node(id="before", kind=StepKind.QUEST_TURNIN, quest_id=9, next=("accept",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, level=(5, 12),
             next=("turnin",), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, level=(5, 12),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="low", kind=StepKind.GRIND, level=(2, 4), skills=("GRIND_UNTIL",), **base),
        Node(id="high", kind=StepKind.GRIND, level=(6, 8), skills=("GRIND_UNTIL",), **base),
    ))


def test_an_accept_below_its_quests_level_waits_on_the_grind_of_the_characters_level(tmp_path):
    """V308: of 117 accepts failed over to a grind in the hive from 12:00 to 13:08 on 28 Sep,
    56 were below the quest's MinLevel, each walked to, refused, walked from to a grind and
    back, and refused again: Hattheas, a level 2 blood elf, at Major Malfunction (MinLevel 4),
    12:24 and 12:31. The accept reached at level 3 arms the grind for level 3, no walk to the
    giver, until the quest's level (V317: one level on, an accept two up was two ribs); at 4
    it waits on, and at 5 the giver is walked to."""
    from jev.world.state_v1 import Char

    def at(t, level, *log):
        return seen(t, char=Char(level=level), quests=tuple(Quest(quest_id=q) for q in log))

    states = [at(0, 3, 9), at(1, 3), at(2, 3), at(3, 4), at(4, 4), at(5, 5), at(6, 5)]
    rt = ClientRuntime("c", level_graph(), ScriptedSource(states), Recorder(tmp_path),
                       start_step="before")
    rt.tick()
    assert rt.tracker.step_id == "before"
    rt.tick()                                             # handed in: the accept is reached
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("low", "accept")
    assert rt.armed.decision.skill == "GRIND_UNTIL", "no walk to a giver who will refuse"
    assert rt.armed.decision.params["until_level"] == 5, "the accept's level, not one on"
    assert rt.tracker.memory.until is None and "accept" not in rt._retried, "not a failure"
    rt.tick()
    rt.tick()                                             # level 4: still below
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("low", "accept")
    rt.tick()
    rt.tick()                                             # level 5
    assert rt.tracker.step_id == "accept"
    assert rt.armed.decision.skill == "ACCEPT_QUEST"


def test_the_rest_of_a_quest_handed_in_on_the_way_is_passed(tmp_path):
    """V308: a hand-in on the way (V234) leaves the quest's objective on the route, and of 83
    objectives failed over with their quest absent in the hive from 12:00 to 13:08 on 28 Sep,
    40 were of quests handed in: a troll warrior's Simple Tablet, handed in at 12:15:54, its
    objective armed 21 s later and failed over, "quest absent"."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("other",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="other", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("do",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="do", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=3,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
    ))
    states = [seen(0), seen(1, quests=(Quest(quest_id=2),))]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                       start_step="other", completed={1})
    rt.tick(choose=False)
    assert rt.tracker.step_id == "other"
    rt.tick()                                             # quest 2 taken: on to quest 1's
    assert rt.tracker.step_id == "after", "the objective of a quest handed in"
    assert rt.armed.decision.skill == "ACCEPT_QUEST"


def test_an_accept_passed_by_for_a_lost_prerequisite_takes_the_rest_of_its_quest(tmp_path):
    """V308: a tauren druid's Rite of Strength was passed by for a lost Rites of the
    Earthmother, and its objective, further on, was walked to and failed twice, "quest
    absent" (the hive, 28 Sep 12:05 and 12:11)."""
    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry="do1", nodes=(
        Node(id="do1", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, next=("turnin1",),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="turnin1", kind=StepKind.QUEST_TURNIN, quest_id=1, next=("accept2",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="accept2", kind=StepKind.QUEST_ACCEPT, quest_id=2, next=("accept3",),
             quest_prerequisites=((1,),), skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="accept3", kind=StepKind.QUEST_ACCEPT, quest_id=3, next=("do2",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="do2", kind=StepKind.QUEST_OBJECTIVE, quest_id=2, next=("turnin2",),
             skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="turnin2", kind=StepKind.QUEST_TURNIN, quest_id=2, next=("after",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="after", kind=StepKind.QUEST_ACCEPT, quest_id=4,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
    ))
    unfinished = Quest(quest_id=1, complete=False)          # its objective passed over (V219)
    states = [seen(0, quests=(unfinished,)), seen(1, quests=(unfinished, Quest(quest_id=3)))]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                       start_step="accept2", start_retried=frozenset({"do1"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "accept3" and "accept2" in rt._retried
    rt.tick()                                             # quest 3 taken
    assert rt.tracker.step_id == "after", "the objective of a quest never taken"
    assert rt.armed.decision.skill == "ACCEPT_QUEST"


def test_a_quest_its_giver_will_not_give_is_passed_over_at_once(tmp_path):
    """V308: Botanist Taerix, a breadcrumb every draenei route takes after the quest it leads
    to, was refused 14 times on 8 bots from 12:00 to 13:08 on 28 Sep, a walk to a grind and
    back between each first refusal and its second. Refused, the quest is passed over at once,
    accept to hand-in."""
    rt = ClientRuntime("c", chain_graph(), ScriptedSource([seen(0)]), Recorder(tmp_path))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "accept"
    assert rt.not_offered("turnin") is False, "not the step the playhead is on"
    assert rt.not_offered("accept") is True
    assert rt.tracker.step_id == "next_accept" and "accept" in rt._retried
    assert rt.tracker.memory.rejoin_to is None, "no grind and no second walk"


def test_a_service_barred_on_a_step_is_barred_there_alone(tmp_path):
    """Review of 28 Sep (V309): a repair, a restock or a bag service that could not be done
    on a step is not asked again on it; the playhead gone from the step, the bar is lifted,
    as a grind rib comes back later and one timed-out sale must not bar it for good."""
    full = Bags(free=0, durability_min=1.0, money_copper=500)
    rt = runtime(tmp_path, [seen(0, bags=full), seen(1, bags=full),
                            seen(2, bags=full, quests=(Quest(quest_id=1),))])
    context = rt.policy_context
    context.bags_unreachable("accept", 0)
    context.repair_unreachable("accept", 0)
    context.supplies_unreachable("elsewhere", 0)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "accept"
    assert context.bags_unreachable_step == context.repair_unreachable_step == "accept"
    assert context.supplies_unreachable_step is None, "not the playhead's step"
    assert not context.can_make_space(0, "accept", 1)
    rt.tick(choose=False)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "turnin"
    assert context.bags_unreachable_step is None and context.repair_unreachable_step is None
    assert context.can_make_space(0, "accept", 3), "back on the step later, asked again"


def wait_graph(ahead: bool = True):
    """V317: a spine whose next accept asks level 8 (Lost But Not Forgotten, MinLevel 8, in
    Durotar), then one asking 9, then, when `ahead`, one a level 6 character can take; and
    three ribs of level 6, `rib_a` where the character stands, `rib_b` 110 yards off, and
    `rib_c` 1,200 yards off."""
    base = dict(zone="zone", zone_id=1, map_id=0)
    accept = ("TRAVEL_TO", "ACCEPT_QUEST")
    return Graph(graph_id="g", faction="horde", entry="gated", nodes=(
        Node(id="gated", kind=StepKind.QUEST_ACCEPT, quest_id=1, level=(8, 14),
             next=("gated_do",), pos=(0.3, 0.3), world=(-500.0, 0.0, 0.0), skills=accept, **base),
        Node(id="gated_do", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, level=(8, 14),
             next=("higher",), pos=(0.3, 0.3), skills=("TRAVEL_TO", "GRIND_UNTIL"), **base),
        Node(id="higher", kind=StepKind.QUEST_ACCEPT, quest_id=2, level=(9, 14), next=("open",),
             pos=(0.3, 0.3), skills=accept, **base),
        Node(id="open", kind=StepKind.QUEST_ACCEPT, quest_id=3, level=(5 if ahead else 9, 12),
             next=("open_in",), pos=(0.3, 0.3), skills=accept, **base),
        Node(id="open_in", kind=StepKind.QUEST_TURNIN, quest_id=3, level=(5, 12),
             pos=(0.3, 0.3), skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="rib_a", kind=StepKind.GRIND, level=(4, 6), pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("GRIND_UNTIL",), **base),
        Node(id="rib_b", kind=StepKind.GRIND, level=(4, 6), pos=(0.53, 0.5),
             world=(110.0, 0.0, 0.0), skills=("GRIND_UNTIL",), **base),
        Node(id="rib_c", kind=StepKind.GRIND, level=(4, 6), pos=(0.8, 0.8),
             world=(900.0, 800.0, 0.0), skills=("GRIND_UNTIL",), **base),
    ))


def level6(t, **kw):
    from jev.world.state_v1 import Char

    return seen(t, char=Char(level=kw.pop("level", 6)), **kw)


def test_an_accept_above_the_level_goes_on_to_the_next_step_it_can_do(tmp_path):
    """V317: at 09:30 on 29 Sep, 100 of the 125 hive bots with under a minute on a quest step
    in 4 h stood on a rib waiting for an accept 1 to 5 levels above them, and 96 of them had
    one they could take further along their spine: Wuhson, a level 7 troll mage, from 04:15
    for Lost But Not Forgotten (MinLevel 8). The accept is passed over, as a refused one is,
    with the accepts above the level on the way, and the next one it can take is walked to."""
    rt = ClientRuntime("c", wait_graph(), ScriptedSource([level6(0), level6(1)]),
                       Recorder(tmp_path), start_step="gated")
    rt.tick()
    assert rt.tracker.step_id == "open", "a grind of hours where a quest was minutes"
    assert {"gated", "higher"} <= rt._retried
    assert rt.armed.decision.skill == "TRAVEL_TO", "the walk to its giver, not a grind"
    rt.tick(choose=False)
    assert rt.tracker.step_id == "open"


def test_a_rib_already_waiting_on_an_accept_goes_on_to_the_step_it_can_do(tmp_path):
    """V317: the hive's 100 bots waiting on a rib resume there, their way back the accept and
    the level they entered at kept (V308); the first tick takes them to the step they can do."""
    saved = []
    rt = ClientRuntime("c", wait_graph(), ScriptedSource([level6(0)]), Recorder(tmp_path),
                       start_step="rib_a", start_rejoin="gated", start_entry_level=6,
                       on_progress=lambda step, *a, **k: saved.append(step))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "open" and saved[-1] == "open"


def test_with_nothing_to_do_ahead_the_wait_lasts_until_the_accepts_level(tmp_path):
    """V317: with no step ahead it can do, the character waits on a grind, to the quest's
    level, not one past the level it came at, and not for the rib's own clock: the giver
    still refuses, and the timed-out rib was entered again the same tick."""
    states = [level6(0), level6(1), level6(2), level6(3, level=7), level6(4, level=8)]
    rt = ClientRuntime("c", wait_graph(ahead=False), ScriptedSource(states), Recorder(tmp_path),
                       start_step="gated")
    rt.tick()
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib_a", "gated")
    assert rt.armed.decision.params["until_level"] == 8
    assert "gated" not in rt._retried, "a wait, not a failure"
    rt.tracker.memory.working_s = 10_000.0                # past the rib's 900 s
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_a"
    rt.tick(choose=False)
    rt.tick(choose=False)                                 # level 7
    assert rt.tracker.step_id == "rib_a"
    rt.tick(choose=False)                                 # level 8: the giver
    assert rt.tracker.step_id == "gated"


def test_a_rib_that_earns_nothing_goes_back_to_the_spine_not_round_again(tmp_path):
    """V317: in the hive's 8 h to 09:30 on 29 Sep a grind ended 5,036 times, 479 h, as "no
    quest or experience progress; step failed over", and the rib went on: its clock, kept in
    memory, began again with each 15-minute session and never reached its 900 s. Failed, it
    is barred at the level and left for its way back: the accept, which goes on to a step
    ahead the character can do or, with none, waits on another rib."""
    rt = ClientRuntime("c", wait_graph(ahead=False), ScriptedSource([level6(t) for t in range(4)]),
                       Recorder(tmp_path), start_step="rib_a", start_rejoin="gated",
                       start_entry_level=7)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_a"
    assert rt.expire_step()                               # the watchdog's window
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib_b", "gated")
    assert rt.tracker.memory.level_at_entry == 7, "still the accept's level"
    assert "rib-bar:rib_a@6" in rt._retried
    assert rt.expire_step()
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_c", "never back to the rib that failed"

    ahead = ClientRuntime("c", wait_graph(), ScriptedSource([level6(t) for t in range(3)]),
                          Recorder(tmp_path), start_step="rib_a", start_rejoin="gated",
                          start_entry_level=7, start_retried=frozenset({"open"}))
    ahead.tick(choose=False)
    assert ahead.tracker.step_id == "rib_a", "nothing ahead yet: the open accept was passed"
    ahead._retried.discard("open")                        # say a level made it doable
    ahead.expire_step()
    ahead.tick(choose=False)
    assert ahead.tracker.step_id == "open"


def test_a_ribs_grind_out_of_attempts_rejoins_the_spine_rather_than_stopping_the_run(tmp_path):
    """V317: "walked the whole disk and found nothing to fight" ended a rib's grind 1,296
    times in the hive's 8 h to 09:30 on 29 Sep; a rib had no fail edge, the run stopped, and
    the next session resumed the same rib. Cordianna's Undercity rib did it at 09:29:47."""
    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="rib", start_rejoin="turnin")
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib"
    assert rt.fail_over("GRIND_UNTIL", "walked the whole disk and found nothing to fight")
    assert rt.tracker.step_id == "turnin" and "rib-bar:rib@3" in rt._retried


def test_a_death_camp_on_a_rib_never_sends_the_next_death_back_to_it(tmp_path):
    """V317: the leave from a death camp (V307) took the nearest rib out of it, and the next
    death there the nearest out of that one: Wuhson went Durotar 5-7, 7-9, 5-7, 7-9 from
    04:35 to 09:30 on 29 Sep, and 990 of the hive's 1,112 rib-to-rib moves in 8 h came within
    10 minutes of a death. The rib holding the camp is barred at the level, and the level the
    wait is for goes with the character."""
    states = [level6(t) for t in range(3)]
    rt = ClientRuntime("c", wait_graph(ahead=False), ScriptedSource(states), Recorder(tmp_path),
                       start_step="rib_a", start_rejoin="gated", start_entry_level=7)
    rt.policy_context.camp_left(0, 5.0, 0.0)              # died at rib_a
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib_b", "gated")
    assert rt.tracker.memory.level_at_entry == 7
    rt.tracker.memory.arrived = True                      # there: the leave is made
    rt.tick(choose=False)
    rt.policy_context.camp_left(0, 112.0, 0.0)            # and died at rib_b
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_c", "rib_a again: the ping-pong"
    assert rt.tracker.memory.level_at_entry == 7


def test_a_finished_guides_grind_that_earns_nothing_goes_to_another(tmp_path):
    """V317: a guide run out below the next one's first level grinds its own until then (V262);
    a rib of it that earns nothing had no end but that level, and 20 of the 125 bots stuck
    in the hive on 29 Sep were on one. It goes to another rib for the level, the level kept."""
    rt = ClientRuntime("c", wait_graph(), ScriptedSource([level6(t) for t in range(3)]),
                       Recorder(tmp_path), start_step="rib_a", start_entry_level=9,
                       start_grind_then_finish=True)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_a"
    assert rt.expire_step()
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_b" and not rt.finished
    assert rt.tracker.memory.level_at_entry == 9


def test_a_ribs_bar_is_lifted_when_the_level_rises(tmp_path):
    rt = ClientRuntime("c", wait_graph(ahead=False),
                       ScriptedSource([level6(0), level6(1), level6(2, level=7)]),
                       Recorder(tmp_path), start_step="rib_a", start_rejoin="gated",
                       start_entry_level=7)
    rt.tick(choose=False)
    rt.expire_step()
    rt.tick(choose=False)
    assert "rib-bar:rib_a@6" in rt._retried
    rt.tick(choose=False)
    assert not any(r.startswith("rib-bar:") for r in rt._retried)


def test_with_nothing_ahead_a_quest_in_the_log_behind_the_accept_is_done_first(tmp_path):
    """V317: hive-386, a level 6 orc hunter, waited on Lost But Not Forgotten (MinLevel 8) with
    nothing it could take before level 7 ahead, and Sting of the Scorpid and Vile Familiars
    complete in its log, their hand-ins passed over behind it (29 Sep). A quest in the log
    behind the accept is done on the way, once a level, and the accept waited on after."""
    base = dict(zone="zone", zone_id=1, map_id=0, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="horde", entry="old_in", nodes=(
        Node(id="old_in", kind=StepKind.QUEST_TURNIN, quest_id=5, next=("gated",),
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="gated", kind=StepKind.QUEST_ACCEPT, quest_id=1, level=(8, 14),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(4, 6), world=(0.0, 0.0, 0.0),
             skills=("GRIND_UNTIL",), **base),
    ))
    done = (Quest(quest_id=5, complete=True),)
    states = [level6(0, quests=done), level6(1, quests=()), level6(2, quests=())]
    rt = ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                       start_step="rib", start_rejoin="gated", start_entry_level=7,
                       start_retried=frozenset({"old_in"}))
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("old_in", "gated")
    rt.tick(choose=False)                                 # handed in
    assert 5 in rt.completed
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib", "gated"), "then waits"


def elsewhere_graph():
    """A quest objective with a fail edge, and three ribs of levels 4-6 for a level 5."""
    from jev.guide.graph import FailEdge, FailWhen

    base = dict(zone="zone", zone_id=1, map_id=0)
    ribs = tuple(Node(id=f"rib_{name}", kind=StepKind.GRIND, level=(4, 6), pos=pos,
                      world=(x, 0.0, 0.0), skills=("GRIND_UNTIL",), **base)
                 for name, pos, x in (("near", (0.52, 0.5), 50.0), ("mid", (0.6, 0.5), 300.0),
                                      ("far", (0.9, 0.5), 900.0)))
    high = Node(id="rib_high", kind=StepKind.GRIND, level=(9, 11), pos=(0.5, 0.51),
                world=(5.0, 0.0, 0.0), skills=("GRIND_UNTIL",), **base)
    return Graph(graph_id="g", faction="alliance", entry="kill", nodes=(
        Node(id="kill", kind=StepKind.QUEST_OBJECTIVE, quest_id=1, pos=(0.5, 0.5),
             world=(0.0, 0.0, 0.0), skills=("GRIND_UNTIL",), timeout_s=600.0,
             on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=600, goto="rib_near"),), **base),
        *ribs, high))


def _elsewhere(tmp_path, n=8, **kw):
    from jev.world.state_v1 import Char

    states = [seen(1000.0 + t, char=Char(level=5)) for t in range(n)]
    return ClientRuntime("c", elsewhere_graph(), ScriptedSource(states), Recorder(tmp_path),
                         start_step="kill", **kw)


def test_a_step_that_waits_on_a_camp_is_waited_out_on_the_nearest_free_rib(tmp_path):
    """V334 with the coordinator's review: a step that waits on a death camp left the
    character standing until the camp ended; replayed, the playheads came back to such a
    step 7,067 times in the hive's 03:00-09:30 of 29 Sep, 413 h before the camps ended. It
    grinds the nearest rib fit for its level that waits on nothing and has a station out of
    every camp, and comes back to the step when its wait ends; never a rib above its level."""
    rt = _elsewhere(tmp_path)
    rt.policy_context.camped = lambda rib, level: rib.id == "rib_near"
    rt.policy_context.step_waits("kill", 1000.0 + 3000.0, "camp", 1000.0)
    rt.tick()
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("rib_mid", "kill")
    assert rt.tracker.memory.until == 4000.0, "back when the wait ends"
    assert rt.armed.decision.skill == "GRIND_UNTIL" and rt.armed.step_id == "rib_mid"


def test_a_short_wait_is_stood_and_a_meal_finishes_before_the_detour(tmp_path):
    from jev.orch.runtime import WAIT_ELSEWHERE_S

    rt = _elsewhere(tmp_path)
    rt.policy_context.step_waits("kill", 1000.0 + WAIT_ELSEWHERE_S - 10.0, "no route", 1000.0)
    rt.tick()
    assert rt.tracker.step_id == "kill" and rt.armed.rule == "wait.step"
    rt = _elsewhere(tmp_path)
    rt.tick()
    rt.armed = rt.armed.__class__(**{**rt.armed.__dict__, "rule": "recover.eat"})
    rt.policy_context.step_waits("kill", 4000.0, "camp", 1000.0)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "kill", "the meal first"


def test_a_rib_that_waits_hands_its_way_back_to_the_rib_that_replaces_it(tmp_path):
    rt = _elsewhere(tmp_path, start_rejoin="kill", start_entry_level=5)
    rt.start_step = "rib_near"
    rt.start_rib_until = 1500.0
    rt.policy_context.step_waits("rib_near", 4000.0, "camp", 1000.0)
    rt.tick(choose=False)
    memory = rt.tracker.memory
    assert (rt.tracker.step_id, memory.rejoin_to) == ("rib_mid", "kill")
    assert (memory.level_at_entry, memory.until) == (5, 1500.0)


def test_a_wait_elsewhere_takes_its_rib_as_every_other_rib_choice_does(tmp_path):
    """Merged with V329-V332: a step's wait out of a death camp (V334) chooses through the
    runtime's one rib choice, of the ribs that suit the level (`rib_fits`): a rib the route
    leaves out (V331) is none, and a barred rib comes after the others."""
    g = elsewhere_graph()
    blocked = g.model_copy(update={"nodes": tuple(
        n.model_copy(update={"route_blocked_reason": "a grind in a capital's map box"})
        if n.id == "rib_near" else n for n in g.nodes)})
    from jev.world.state_v1 import Char

    states = [seen(1000.0 + t, char=Char(level=5)) for t in range(3)]
    rt = ClientRuntime("c", blocked, ScriptedSource(states), Recorder(tmp_path), start_step="kill")
    rt.policy_context.step_waits("kill", 4000.0, "camp", 1000.0)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "rib_mid"
    barred = _elsewhere(tmp_path, start_retried=frozenset({"rib-bar:rib_near@5"}))
    barred.policy_context.step_waits("kill", 4000.0, "camp", 1000.0)
    barred.tick(choose=False)
    assert barred.tracker.step_id == "rib_mid", "the barred rib after the others"


def test_waiting_elsewhere_cannot_loop_and_stands_when_every_rib_waits(tmp_path):
    """Each rib sent to that waits in its turn is not chosen again: the detours are fewer
    than the ribs, then the step is stood out, looked at again every `WAIT_LOOK_S`."""
    from jev.orch.runtime import WAIT_LOOK_S

    rt = _elsewhere(tmp_path, n=40)
    rt.policy_context.step_waits("kill", 4000.0, "camp", 1000.0)
    visited = []
    for _ in range(12):
        rt.tick(choose=False)
        step = rt.tracker.step_id
        if step != "kill":
            visited.append(step)
            # Its hunt ends `camp` too: it waits, and its fail-over takes it back.
            rt.policy_context.step_waits(step, 4000.0, "camp", 1000.0)
            rt.tracker.enter("kill", rt.last_state)
    assert visited == ["rib_near", "rib_mid", "rib_far"], "each once, none above the level"
    looked = rt._elsewhere_looked
    assert looked[0] == "kill" and rt.tracker.step_id == "kill"
    rt.policy_context.step_wait_until.pop("rib_mid")       # its camp ended
    rt.tick(choose=False)
    assert rt.tracker.step_id == "kill", f"not looked at again within {WAIT_LOOK_S:.0f}s"
