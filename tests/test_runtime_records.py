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
                 start_step="turnin", on_progress=lambda step, done: saved.append((step, done)))
    rt.run(4, 0)
    assert rt.finished
    assert rt.completed == {1}
    assert rt.counters.advances == 1
    assert saved[-1] == ("turnin", {1})
    assert rt.armed.decision.intent is Intent.WAIT


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
