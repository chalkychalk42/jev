"""Rules, not a model, decide whether a plan may be armed (PLAN §9.2).

Everything here is cheap, deterministic and explainable. The verifier exists because the
thing proposing plans is remote, occasionally wrong and sometimes not there at all, while
the thing executing them holds a live character. Refusals are named so a rejection reason
in the log points at a rule rather than at a vibe.

A refused plan is **not** a reason to stop. It is recorded with `status="rejected"` and the
caller falls back to the scripted default, which is always valid and always free.
"""

from __future__ import annotations

from collections.abc import Callable

from jev.coach.schema import Decision, Intent, Verdict
from jev.world.state_v1 import State

# Skills whose entire job is to be somewhere else, so a zone mismatch is expected
# rather than wrong.
TRAVELLING = frozenset({"TRAVEL_TO", "FOLLOW_PATH", "HEARTH", "FLIGHT_PATH", "BOAT_OR_ZEP"})

# Skills that talk to an NPC and therefore need a living character in front of one.
SOCIAL = frozenset({"VENDOR_REPAIR", "TRAIN_CLASS", "ACCEPT_QUEST", "TURNIN_QUEST",
                    "GOSSIP_PICK", "BUY_AMMO_REAGENT_FOOD", "BAG_MAKE_SPACE", "BIND_HEARTH"})

Rule = Callable[[Decision, State, frozenset[str]], Verdict]


def _skill_exists(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    if d.intent is Intent.WAIT and d.skill is None:
        return Verdict.accept()
    if not d.skill:
        return Verdict.refuse("skill_exists", f"intent {d.intent} needs a skill")
    if d.skill not in catalog:
        return Verdict.refuse("skill_exists", f"{d.skill!r} is not in the catalog")
    return Verdict.accept()


def _zone_matches(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    coordinate_zone = d.params.get("coord_zone_id")
    if coordinate_zone is not None:
        if type(coordinate_zone) is not int or coordinate_zone <= 0:
            return Verdict.refuse("coordinate_frame", "coordinate frame must be an area ID")
        if s.pos.coord_zone_id is not None and coordinate_zone != s.pos.coord_zone_id:
            return Verdict.refuse("coordinate_frame", "plan and observation use different map coordinate frames")
    want = d.params.get("zone")
    if want is None or d.skill in TRAVELLING:
        return Verdict.accept()
    if s.pos.zone is None:
        # Unknown is not a mismatch. Refusing here would ground the character every time
        # the zone reader blinked.
        return Verdict.accept()
    if want != s.pos.zone:
        return Verdict.refuse("zone_matches", f"plan targets {want!r}, character is in {s.pos.zone!r}")
    return Verdict.accept()


def _not_social_while_dead(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    if d.skill in SOCIAL and (s.vitals.dead is True or s.vitals.ghost is True):
        return Verdict.refuse("not_social_while_dead", f"{d.skill} with a dead character")
    return Verdict.accept()


def _not_social_in_combat(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    """Talking to an NPC mid-fight fails, and fails slowly.

    Allowed only when the plan already says it will abort on combat — which is the author
    telling us they know, rather than us guessing that they do.
    """
    if (
        d.skill in SOCIAL
        and s.vitals.combat is True
        and not any("combat" in c for c in d.abort_if)
    ):
        return Verdict.refuse(
            "not_social_in_combat",
            f"{d.skill} in combat without a combat abort condition",
        )
    return Verdict.accept()


def _abort_conditions_present(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    if d.intent is not Intent.WAIT and not d.abort_if:
        return Verdict.refuse("abort_conditions_present", "a skill with no abort condition")
    return Verdict.accept()


def _advance_needs_a_step(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    """Pursuing or skipping a step requires there to be one.

    The step may be named by the plan rather than by the state. State is sampled at 2 Hz
    and the playhead moves on tracker ticks, so a plan built from the current node can
    legitimately run ahead of the last state that was read. Requiring the *state* to
    already know would reject correct plans for being early.
    """
    if d.intent not in (Intent.ADVANCE, Intent.SKIP):
        return Verdict.accept()
    if s.guide.step_id is None and not d.params.get("step_id"):
        return Verdict.refuse("advance_needs_a_step", f"{d.intent} with no playhead")
    return Verdict.accept()


def _no_service_loop(d: Decision, s: State, catalog: frozenset[str]) -> Verdict:
    """Refuse a service run for a need the state does not report.

    Vendoring with empty bags is the shape of a loop: the skill succeeds, changes nothing,
    the same situation recurs and it is proposed again. Unknown is not a need — if nothing
    measured the bags, that is not evidence they are full.
    """
    if d.intent is not Intent.SERVICE:
        return Verdict.accept()
    kind = d.params.get("service")
    free, dur = s.bags.free, s.bags.durability_min
    if kind == "vendor" and free is not None and free > 6:
        return Verdict.refuse("no_service_loop", f"vendor run with {free} free slots")
    if kind == "repair" and dur is not None and dur > 0.7:
        return Verdict.refuse("no_service_loop", f"repair run at durability {dur:.2f}")
    return Verdict.accept()


RULES: tuple[Rule, ...] = (
    _skill_exists,
    _abort_conditions_present,
    _advance_needs_a_step,
    _zone_matches,
    _not_social_while_dead,
    _not_social_in_combat,
    _no_service_loop,
)


def verify(decision: Decision, state: State, catalog: frozenset[str]) -> Verdict:
    """First refusal wins, so the reason names the most fundamental problem."""
    for rule in RULES:
        verdict = rule(decision, state, catalog)
        if not verdict.ok:
            return verdict
    return Verdict.accept()
