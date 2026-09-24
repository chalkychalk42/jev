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


def test_optional_queue_and_shadow_failures_do_not_remove_the_floor(tmp_path):
    def unavailable(*args):
        raise RuntimeError("offline")
    rt = runtime(tmp_path, [State(t=0, client_id="c")],
                 take=unavailable, ask=unavailable, shadow=unavailable)
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
    richer = state.model_copy(update={"bags": Bags(durability_min=0, money_copper=11)})
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
                 on_progress=lambda *args, finished=False: saved.append(finished))
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
        Node(id="rib", kind=StepKind.GRIND, level=(1, 10), skills=("TRAVEL_TO", "GRIND_UNTIL"),
             **base),
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


def test_a_short_ribs_end_outlives_the_session(tmp_path):
    from jev.guide import playhead

    path = tmp_path / "character.json"
    playhead.save("g", "rib", {1}, path, rejoin_to="turnin", rib_until=1234.5)
    assert playhead.load("g", path).rib_until == 1234.5
    rt = ClientRuntime("c", rib_graph(), ScriptedSource([held(0, 3)]), Recorder(tmp_path),
                       start_step="rib", start_rejoin="turnin", start_rib_until=1234.5)
    rt.tick(choose=False)
    assert rt.tracker.memory.until == 1234.5


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
