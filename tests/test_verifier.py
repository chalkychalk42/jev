"""Rules, not a model, decide what may be armed — and a refusal is never a reason to stop."""

from __future__ import annotations

import pydantic
import pytest

from jev.coach.schema import Decision, Intent, TeacherReply
from jev.coach.verifier import verify
from jev.world.state_v1 import Bags, State, Vitals

CATALOG = frozenset({"GRIND_UNTIL", "TRAVEL_TO", "VENDOR_REPAIR", "ACCEPT_QUEST", "LOOT"})


def _d(**kw) -> Decision:
    base = dict(goal="g", intent=Intent.GRIND_RIB, skill="GRIND_UNTIL",
                abort_if=["dead"], confidence=0.7, why="because")
    return Decision(**{**base, **kw})


def test_an_unknown_skill_is_refused(state: State):
    v = verify(_d(skill="TELEPORT_TO_STORMWIND"), state, CATALOG)
    assert not v.ok and v.rule == "skill_exists"


def test_a_skill_with_no_abort_condition_cannot_be_built():
    """Caught by the schema, before the verifier ever sees it."""
    with pytest.raises(pydantic.ValidationError):
        _d(abort_if=[])


def test_travel_may_target_another_zone(state: State):
    assert verify(_d(skill="TRAVEL_TO", params={"zone": "Westfall"}), state, CATALOG).ok


def test_a_non_travel_skill_may_not(state: State):
    v = verify(_d(skill="GRIND_UNTIL", params={"zone": "Westfall"}), state, CATALOG)
    assert not v.ok and v.rule == "zone_matches"


def test_an_unreadable_zone_does_not_ground_the_character(state: State):
    """Refusing on unknown would stop the run every time the zone reader blinked."""
    blind = state.model_copy(update={"pos": state.pos.model_copy(update={"zone": None})})
    assert verify(_d(skill="GRIND_UNTIL", params={"zone": "Westfall"}), blind, CATALOG).ok


def test_a_corpse_cannot_shop(state: State):
    dead = state.model_copy(update={"vitals": Vitals(dead=True)})
    v = verify(_d(intent=Intent.SERVICE, skill="VENDOR_REPAIR",
                  params={"service": "vendor"}), dead, CATALOG)
    assert not v.ok and v.rule == "not_social_while_dead"


def test_talking_in_combat_needs_the_author_to_say_they_know(state: State):
    fighting = state.model_copy(update={"vitals": Vitals(combat=True)})
    assert not verify(_d(skill="ACCEPT_QUEST"), fighting, CATALOG).ok
    assert verify(_d(skill="ACCEPT_QUEST", abort_if=["combat"]), fighting, CATALOG).ok


def test_a_service_run_for_a_need_that_does_not_exist_is_refused(state: State):
    """Vendoring with empty bags succeeds, changes nothing, and recurs. That is a loop."""
    v = verify(_d(intent=Intent.SERVICE, skill="VENDOR_REPAIR",
                  params={"service": "vendor"}), state, CATALOG)
    assert not v.ok and v.rule == "no_service_loop"


def test_unknown_bags_are_not_evidence_of_a_need(state: State):
    """Unknown must not block a service run, only measured sufficiency may."""
    blind = state.model_copy(update={"bags": Bags(free=None)})
    assert verify(_d(intent=Intent.SERVICE, skill="VENDOR_REPAIR",
                     params={"service": "vendor"}), blind, CATALOG).ok


def test_advancing_with_no_playhead_is_refused(state: State):
    nowhere = state.model_copy(update={"guide": state.guide.model_copy(update={"step_id": None})})
    v = verify(_d(intent=Intent.ADVANCE, skill="LOOT"), nowhere, CATALOG)
    assert not v.ok and v.rule == "advance_needs_a_step"


def test_wait_needs_no_skill(state: State):
    assert verify(_d(intent=Intent.WAIT, skill=None), state, CATALOG).ok


def test_an_empty_teacher_reply_is_an_abstention_not_an_instruction():
    assert TeacherReply().is_empty()
    assert not TeacherReply(decision=_d()).is_empty()
