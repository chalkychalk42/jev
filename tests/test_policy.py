"""The floor under everything: a valid plan, always, with no teacher and no network."""

from __future__ import annotations

import pytest

from jev.coach.policy import decide, preempt, wants_teacher
from jev.coach.schema import Intent
from jev.coach.verifier import verify
from jev.guide.graph import Graph
from jev.skills.catalog import NAMES
from jev.world.state_v1 import Bags, Pos, Sense, State, Target, Ui, Vitals


def _s(**kw) -> State:
    base = dict(vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                bags=Bags(free=10, durability_min=1.0),
                pos=Pos(zone="Elwynn", zone_id=12, mx=0.5, my=0.5),
                ui=Ui(loot=False, modal=False),
                sense=Sense(addon_ok=True, vision_conf=1.0))
    return State(t=0.0, client_id="c", **{**base, **kw})


# --- the invariant ----------------------------------------------------------

def test_the_system_makes_progress_with_zero_teacher_calls():
    """ARCHITECTURE.md §0. The teacher is an improvement engine, never a dependency.

    If this fails, a remote service has become part of the hot loop of a game client and
    the project will spend its demo apologising.
    """
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = graph.get(graph.entry)

    states = [
        _s(),
        _s(vitals=Vitals(dead=True)),
        _s(vitals=Vitals(ghost=True)),
        _s(vitals=Vitals(hp=0.1, combat=True), target=Target(has=True, in_melee=True)),
        _s(ui=Ui(loot=True)),
        _s(ui=Ui(modal=True)),
        _s(bags=Bags(free=0, durability_min=0.5)),
        _s(bags=Bags(free=5, durability_min=0.0)),
        _s(vitals=Vitals(hp=0.3, combat=False, dead=False)),
        _s(vitals=Vitals(combat=True), target=Target(has=True, in_melee=False)),
        _s(vitals=Vitals(combat=True), target=Target(has=False)),
        State(t=0.0, client_id="c"),                       # nothing observed at all
    ]

    for state in states:
        plan = decide(state, node)
        assert plan.decision is not None, f"no plan for {plan.rule}"
        assert plan.decision.skill is None or plan.decision.skill in NAMES
        assert plan.decision.abort_if, "a skill with no abort condition runs until a corpse"


def test_every_scripted_plan_passes_the_verifier():
    """The floor must not propose things the gate above it rejects, or the fallback
    fallback is 'do nothing'."""
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = graph.get(graph.entry)
    for state in (_s(), _s(vitals=Vitals(dead=True)), _s(ui=Ui(loot=True)),
                  _s(bags=Bags(free=0)), State(t=0.0, client_id="c")):
        plan = decide(state, node)
        v = verify(plan.decision, state, NAMES)
        assert v.ok, f"{plan.rule} produced a plan the verifier refuses: {v.reason}"


def test_a_state_where_nothing_is_known_still_gets_a_plan():
    plan = decide(State(t=0.0, client_id="c"))
    assert plan.decision.intent is Intent.GRIND_RIB
    assert not plan.confident, "guessing in the dark should be marked as guessing"


def test_a_plan_made_blind_is_marked_as_a_guess():
    """Claiming 0.8 confidence in a position nothing confirmed would make a perception
    outage report as a quiet, confident run and hide the ticks worth escalating."""
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = graph.get(graph.entry)
    seeing = _s(pos=Pos(zone=node.zone, zone_id=node.zone_id,
                        mx=node.pos[0], my=node.pos[1]))
    blind = seeing.model_copy(update={"sense": Sense(addon_ok=False, vision_conf=0.0)})

    assert decide(seeing, node).confident
    guess = decide(blind, node)
    assert not guess.confident
    assert guess.decision.confidence <= 0.4
    assert guess.rule.endswith("+blind")


# --- preempts ---------------------------------------------------------------

def test_unknown_is_never_an_emergency():
    """Every preempt tests `is True`. Treating a blank reading as 'dead' has the
    character releasing its spirit each time a loading screen blanks the radio."""
    assert preempt(State(t=0.0, client_id="c")) is None


def test_a_ghost_walks_back_before_anything_else():
    plan = decide(_s(vitals=Vitals(ghost=True), bags=Bags(free=0, durability_min=0.0)))
    assert plan.decision.skill == "CORPSE_RUN"


def test_a_covering_dialog_stops_movement():
    """Moving while a dialog covers the world is how a character walks into a lake."""
    assert decide(_s(ui=Ui(modal=True))).decision.skill == "ABORT_WAIT"


def test_an_open_loot_window_outranks_combat():
    plan = decide(_s(ui=Ui(loot=True), vitals=Vitals(combat=True),
                     target=Target(has=True, in_melee=True)))
    assert plan.decision.skill == "LOOT"


# --- soft tier --------------------------------------------------------------

@pytest.mark.parametrize(
    ("state", "skill"),
    [
        (_s(bags=Bags(free=5, durability_min=0.0)), "VENDOR_REPAIR"),
        (_s(bags=Bags(free=0, durability_min=1.0)), "BAG_MAKE_SPACE"),
        (_s(vitals=Vitals(hp=0.3, combat=False)), "EAT_DRINK"),
        (_s(vitals=Vitals(combat=True), target=Target(has=True, in_melee=False)),
         "APPROACH_TARGET"),
        (_s(vitals=Vitals(combat=True), target=Target(has=True, in_melee=True)),
         "COMBAT_PROFILE"),
    ],
)
def test_the_priority_order_holds(state, skill):
    assert decide(state).decision.skill == skill


def test_unknown_bags_do_not_trigger_a_service_run():
    """A service loop on an unread number: the skill succeeds, changes nothing, and the
    same situation recurs forever."""
    plan = decide(_s(bags=Bags(free=None, durability_min=None)))
    assert plan.decision.skill != "VENDOR_REPAIR"


def test_the_guide_step_is_used_once_nothing_is_urgent():
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = graph.get(graph.entry)
    at_node = _s(pos=Pos(zone=node.zone, zone_id=node.zone_id,
                         mx=node.pos[0], my=node.pos[1]))
    plan = decide(at_node, node)
    assert plan.rule == "guide.step"
    assert plan.decision.params["step_id"] == node.id


def test_being_far_from_the_step_produces_a_travel_leg_first():
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = graph.get(graph.entry)
    away = _s(pos=Pos(zone=node.zone, zone_id=node.zone_id, mx=0.95, my=0.95))
    plan = decide(away, node)
    assert plan.decision.skill == "TRAVEL_TO" and plan.decision.intent is Intent.REJOIN


def test_wanting_the_teacher_is_advisory_not_blocking():
    """A tick that wants a teacher still carries a usable plan — that distinction is the
    difference between an improvement engine and a dependency."""
    plan = decide(State(t=0.0, client_id="c"))
    assert wants_teacher(plan)
    assert plan.decision.skill in NAMES
