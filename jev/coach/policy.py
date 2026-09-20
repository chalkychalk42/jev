"""The scripted coach — the day-0 policy, and the reason a subscription is survivable.

This is the floor under everything. It is a costed priority list over world flags
(PLAN §8.2), it is free, it is always available, and it **always returns a valid plan**.

That last property is the whole point, and `ARCHITECTURE.md` §0 states it as the invariant
this project depends on:

    The system makes forward progress with zero teacher calls.

The teacher is an improvement engine, never a dependency. If Claude is rate-limited, down,
or simply not configured, the character keeps levelling — more slowly and more stupidly,
but it keeps levelling. Any design where that is not true has made a remote service part
of the hot loop of a game client, and it will spend its demo apologising.

Two tiers, borrowed from the plan:

  * **Hard preempts** — safety. These do not wait for anybody and cannot be overridden by
    a plan that arrived three seconds ago about a situation that has since changed.
  * **Soft priority** — what to do when nothing is on fire:
    `safety > corpse > service > eat > loot > combat > the armed guide skill > grind`

`decide()` returns `(Decision, confident)`. When it is not confident, the caller may
escalate — but escalation is an *upgrade*, never a prerequisite. The decision returned is
always usable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from jev.coach.schema import Decision, Intent
from jev.guide.graph import Node
from jev.skills.catalog import NAMES
from jev.world.state_v1 import State

# Below this, a decision is worth a teacher call if one is affordable. Above it, asking
# would be spending a rate-limited resource on something already known.
CONFIDENT = 0.6


@dataclass(frozen=True)
class Plan:
    decision: Decision
    confident: bool
    rule: str          # which priority fired, so the log says why rather than what


def _d(intent: Intent, skill: str | None, why: str, confidence: float,
       abort_if: tuple[str, ...] = ("dead",), goal: str = "", **params) -> Decision:
    assert skill is None or skill in NAMES, f"{skill} is not in the catalog"
    return Decision(
        goal=goal or f"{intent.value}:{skill or 'none'}",
        intent=intent, skill=skill, params=dict(params),
        abort_if=list(abort_if), confidence=confidence, why=why,
    )


# --------------------------------------------------------------------------- preempts


def preempt(state: State) -> Plan | None:
    """Safety. Ordered by how quickly ignoring it ends the run.

    Every test here is `is True`, never truthiness: unknown is not an emergency, and
    treating a blank reading as "dead" would have the character releasing its spirit every
    time a loading screen blanked the radio.
    """
    v, f = state.vitals, state.flags

    if v.ghost is True:
        return Plan(_d(Intent.SERVICE, "CORPSE_RUN", "ghost; walk back to the body", 0.95,
                       ("alive",)), True, "preempt.ghost")

    if v.dead is True:
        return Plan(_d(Intent.SERVICE, "RELEASE_SPIRIT", "dead; release", 0.95,
                       ("ghost",)), True, "preempt.dead")

    if state.ui.modal is True:
        # The world is obstructed. Moving while a dialog covers it is how a character
        # walks into a lake.
        return Plan(_d(Intent.WAIT, "ABORT_WAIT", "a dialog is covering the world", 0.9,
                       ("no_modal",)), True, "preempt.modal")

    if f.falling is True:
        return Plan(_d(Intent.WAIT, "IDLE", "falling; do not steer", 0.8,
                       ("landed",)), True, "preempt.falling")

    if v.hp is not None and v.hp < 0.20 and v.combat is True:
        return Plan(_d(Intent.SERVICE, "COMBAT_PROFILE", "critical in combat; panic branch",
                       0.8, ("dead", "hp>0.6"), profile="panic"), True, "preempt.critical")

    if state.ui.loot is True:
        # Short, and above combat: an open loot window blocks other interaction, so
        # leaving it open stalls everything behind it.
        return Plan(_d(Intent.SERVICE, "LOOT", "loot window is open", 0.9,
                       ("dead", "no_loot_window")), True, "preempt.loot")

    return None


# --------------------------------------------------------------------------- soft tier


def _service(state: State) -> Plan | None:
    b = state.bags

    # `is not None` throughout: unknown bags are not full bags, and a service loop on an
    # unread number is the failure the verifier's `no_service_loop` rule also guards.
    if b.durability_min is not None and b.durability_min <= 0.05:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "equipment is broken", 0.85,
                       ("dead", "combat"), service="repair"), True, "service.broken")

    if b.free is not None and b.free == 0:
        return Plan(_d(Intent.SERVICE, "BAG_MAKE_SPACE", "bags are full; nothing can drop",
                       0.75, ("dead", "combat"), service="bags"), True, "service.bags_full")

    if b.durability_min is not None and b.durability_min < 0.35:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "durability is low", 0.65,
                       ("dead", "combat"), service="repair"), True, "service.durability")

    return None


def _recover(state: State) -> Plan | None:
    v = state.vitals
    if v.combat is True:
        return None
    hp, power = v.hp, v.power
    if (hp is not None and hp < 0.55) or (power is not None and power < 0.35):
        return Plan(_d(Intent.SERVICE, "EAT_DRINK", "out of combat and low; recover first",
                       0.8, ("dead", "combat")), True, "recover.eat")
    return None


def _fight(state: State) -> Plan | None:
    if state.vitals.combat is not True:
        return None
    if state.target.has is not True:
        # In combat with nothing selected. This is mechanical — name-target the step's
        # mobs, or take the nearest hostile plate — so it is armed with confidence rather
        # than escalated. Marking it uncertain sent 42% of a simulated run to the teacher
        # to be told "pick a target", which is the precise waste a rate-limited teacher
        # cannot absorb.
        return Plan(_d(Intent.ADVANCE, "ACQUIRE_TARGET", "in combat with nothing selected",
                       0.8, ("dead", "no_combat", "has_target")), True,
                    "fight.acquire")
    if state.target.in_melee is False:
        return Plan(_d(Intent.SERVICE, "APPROACH_TARGET", "target is out of reach", 0.8,
                       ("dead", "no_combat")), True, "fight.approach")
    return Plan(_d(Intent.SERVICE, "COMBAT_PROFILE", "target in reach; run the rotation",
                   0.85, ("dead", "no_combat"), profile="default"), True, "fight.rotation")


def _guide(state: State, node: Node | None) -> Plan | None:
    """Do what the step says. This is the happy path and should be almost every tick."""
    if node is None:
        return None

    skill = next((s for s in node.skills if s in NAMES), None)
    if skill is None:
        return None

    # Not yet in position: the step's own travel leg comes first.
    if node.pos is not None and state.pos.mx is not None and state.pos.my is not None:
        dx, dy = state.pos.mx - node.pos[0], state.pos.my - node.pos[1]
        if (dx * dx + dy * dy) ** 0.5 > node.r and "TRAVEL_TO" in node.skills:
            return Plan(
                _d(Intent.REJOIN, "TRAVEL_TO", f"not yet at {node.id}", 0.85,
                   ("dead", "stuck_s>8"), goal=f"travel:{node.id}",
                   zone=node.zone, x=node.pos[0], y=node.pos[1], r=node.r),
                True, "guide.travel")

    return Plan(
        _d(Intent.ADVANCE, skill, f"service step {node.id}", 0.8,
           ("dead", "stuck_s>8"), goal=f"advance:{node.id}",
           zone=node.zone, step_id=node.id),
        True, "guide.step")


def _fallback(state: State) -> Plan:
    """There is always an answer, even when nothing else applied.

    This is the floor of the floor. Grinding where we stand is rarely optimal and is never
    wrong enough to justify stopping — and stopping is what a system with no fallback does
    the first time its remote brain is unreachable.
    """
    return Plan(
        _d(Intent.GRIND_RIB, "GRIND_UNTIL", "no step and nothing urgent; grind here",
           0.35, ("dead", "stuck_s>8")),
        False, "fallback.grind")


# --------------------------------------------------------------------------- entry point


def _blind(state: State) -> bool:
    """Nothing trustworthy is being read right now."""
    return not state.sense.addon_ok and (state.sense.vision_conf or 0.0) < 0.5


def _derate(plan: Plan) -> Plan:
    """Mark a plan made without working senses as the guess it is.

    A step can still be attempted blind — the scripted tier has to return *something* —
    but claiming 0.8 confidence in a position nothing confirmed would hide the exact
    ticks worth spending a teacher call on, and would make `unresolved/h` report a
    perception outage as a quiet, confident run.
    """
    d = plan.decision
    return Plan(d.model_copy(update={"confidence": min(d.confidence, 0.4)}),
                False, plan.rule + "+blind")


def decide(state: State, node: Node | None = None) -> Plan:
    """Always returns a usable plan. Never raises, never returns None.

    `confident=False` marks a tick worth a teacher call *if one is affordable*. It does
    not mark a tick that needs one — the decision handed back is valid either way, and
    that distinction is the difference between an improvement engine and a dependency.
    """
    # Safety first, and safety is not derated: a preempt fires on a positive observation
    # (`is True`), so if one matched, something was read.
    for tier in (preempt, _service, _recover, _fight):
        plan = tier(state)
        if plan is not None:
            return plan

    plan = _guide(state, node) or _fallback(state)
    return _derate(plan) if _blind(state) else plan


def wants_teacher(plan: Plan) -> bool:
    """Would a teacher call improve this tick? Advisory, never blocking."""
    return not plan.confident or plan.decision.confidence < CONFIDENT
