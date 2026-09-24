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

from collections.abc import Callable
from dataclasses import dataclass

from jev.coach.schema import Decision, Intent
from jev.guide.graph import Node
from jev.skills.catalog import NAMES
from jev.world.combat import HEAL_OUT_OF_COMBAT
from jev.world.state_v1 import PowerType, State, StepKind

# Below this, a decision is worth a teacher call if one is affordable. Above it, asking
# would be spending a rate-limited resource on something already known.
CONFIDENT = 0.6

# Reflexes: the rules for a fight the character is already in, and for death. Their
# routine runs at once and is never put to a tutor (a thirty-second deadline is a death in
# a fight - run 20260923T172056-54f4d5), and losing one is a death, which the tracker
# counts against its step, not a failed attempt at that step.
REFLEX_RULES = ("fight.", "preempt.dead", "preempt.ghost", "preempt.critical")


def reflex(rule: str) -> bool:
    return rule.startswith(REFLEX_RULES)


# Services only a routine can do, never put to the tutor: training ends in drags from the
# spellbook onto the bar, which the tutor has no control for.
ROUTINE_RULES = ("service.train",)


def routine_only(rule: str) -> bool:
    return rule.startswith(ROUTINE_RULES)


@dataclass(frozen=True)
class Plan:
    decision: Decision
    confident: bool
    rule: str          # which priority fired, so the log says why rather than what


@dataclass
class Context:
    """Measured service outcomes that remain true between observations."""

    repair_blocked: bool = False
    repair_money: int | None = None
    supplies_blocked: bool = False
    supplies_money: int | None = None

    def repair_failed(self, money: int | None) -> None:
        self.repair_blocked, self.repair_money = True, money

    def can_repair(self, money: int | None) -> bool:
        # An unknown purse cannot establish that an unaffordable repair became payable.
        return not self.repair_blocked or (
            money is not None and self.repair_money is not None and money > self.repair_money
        )

    def supplies_failed(self, money: int | None) -> None:
        self.supplies_blocked, self.supplies_money = True, money

    def can_restock(self, money: int | None) -> bool:
        return not self.supplies_blocked or (
            money is not None and self.supplies_money is not None and money > self.supplies_money)

    bags_blocked: bool = False

    def bags_failed(self) -> None:
        self.bags_blocked = True

    def can_make_space(self, free: int | None) -> bool:
        # Full bags with nothing a merchant may buy: another visit changes nothing until a
        # slot frees (food eaten, an item used), and a run asking again stops on it.
        if free is not None and free > 0:
            self.bags_blocked = False
        return not self.bags_blocked

    # Whether a class trainer has something to teach that the purse can pay for, from
    # the spellbook census and the trainer catalog (`LiveBody.trainable`). Absent, no
    # training is ever asked for.
    trainable: Callable[[State], bool] | None = None
    train_blocked_level: int | None = None

    def train_failed(self, level: int | None) -> None:
        # One visit a level: the next level brings new spells, and a trainer that could not
        # be reached, or would not teach what it lists, may from there.
        self.train_blocked_level = level

    def can_train(self, state: State) -> bool:
        level = state.char.level
        if self.trainable is None or level is None or level == self.train_blocked_level:
            return False
        return self.trainable(state)


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


def service(state: State, *, context: Context | None = None) -> Plan | None:
    """The service priority, shared by idle selection and long-skill handoff."""
    if state.vitals.combat is not False:
        return None
    b = state.bags
    can_repair = context is None or context.can_repair(b.money_copper)
    can_sell = context is None or context.can_make_space(b.free)

    # `is not None` throughout: unknown bags are not full bags, and a service loop on an
    # unread number is the failure the verifier's `no_service_loop` rule also guards.
    if can_repair and b.durability_min is not None and b.durability_min <= 0.05:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "equipment is broken", 0.85,
                       ("dead", "combat"), service="repair"), True, "service.broken")

    if can_sell and b.free is not None and b.free == 0:
        return Plan(_d(Intent.SERVICE, "BAG_MAKE_SPACE", "bags are full; nothing can drop",
                       0.75, ("dead", "combat"), service="bags"), True, "service.bags_full")

    if can_repair and b.durability_min is not None and b.durability_min < 0.35:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "durability is low", 0.65,
                       ("dead", "combat"), service="repair"), True, "service.durability")

    if ((context is None or context.can_restock(b.money_copper))
            and ((b.food_id is not None and b.food_count == 0)
                 or (b.drink_id is not None and b.drink_count == 0))):
        return Plan(_d(Intent.SERVICE, "BUY_AMMO_REAGENT_FOOD", "confirmed food or drink is empty",
                       0.8, ("dead", "combat"), service="supplies"), True, "service.supplies")

    # Last: spells a trainer would teach now. A paladin that never trained fought to level
    # 8 on Seal of Righteousness and Holy Light rank 1, losing to two wolves at once. It can
    # wait for a meal: the walk to Goldshire's trainer is 640 yards of Elwynn.
    if context is not None and _recover(state) is None and context.can_train(state):
        return Plan(_d(Intent.SERVICE, "TRAIN_CLASS", "the class trainer has spells to teach",
                       0.6, ("dead", "combat"), service="train"), True, "service.train")

    return None


def _recover(state: State) -> Plan | None:
    v = state.vitals
    if v.combat is True:
        return None
    hp, power = v.hp, v.power
    low_mana = v.power_type is PowerType.MANA and power is not None and power < 0.35
    if (hp is not None and hp < HEAL_OUT_OF_COMBAT) or low_mana:
        return Plan(_d(Intent.SERVICE, "EAT_DRINK", "out of combat and low; recover first",
                       0.8, ("dead", "combat")), True, "recover.eat")
    return None


def _fight(state: State) -> Plan | None:
    if state.vitals.combat is not True:
        return None
    # Fight already owns acquisition, closing, facing and the rotation. Splitting its
    # phases into separate arms would put the old duel-range heuristic back in charge
    # and interrupt a proven engagement whenever the target moves.
    return Plan(_d(Intent.SERVICE, "COMBAT_PROFILE", "in combat; defend with the full rotation",
                   0.85, ("dead", "no_combat"), profile="default"), True, "fight.rotation")


def _guide(state: State, node: Node | None) -> Plan | None:
    """Do what the step says. This is the happy path and should be almost every tick."""
    if node is None:
        return None

    skill = next((s for s in node.skills if s in NAMES and s != "TRAVEL_TO"), None)
    if skill is None and "TRAVEL_TO" in node.skills:
        skill = "TRAVEL_TO"
    if node.kind in (StepKind.QUEST_OBJECTIVE, StepKind.GRIND):
        skill = "GRIND_UNTIL"
    if skill is None:
        return None
    frame = ({"coord_zone_id": node.coord_zone_id} if node.coord_zone_id is not None
             else {"zone": node.zone})

    # Not yet in position: the step's own travel leg comes first.
    if (node.kind is not StepKind.QUEST_OBJECTIVE and node.pos is not None
            and state.pos.mx is not None and state.pos.my is not None):
        dx, dy = state.pos.mx - node.pos[0], state.pos.my - node.pos[1]
        if (dx * dx + dy * dy) ** 0.5 > node.r and "TRAVEL_TO" in node.skills:
            return Plan(
                _d(Intent.REJOIN, "TRAVEL_TO", f"not yet at {node.id}", 0.85,
                   ("dead", "stuck_s>8"), goal=f"travel:{node.id}",
                   **frame, x=node.pos[0], y=node.pos[1], r=node.r),
                True, "guide.travel")

    return Plan(
        _d(Intent.ADVANCE, skill, f"service step {node.id}", 0.8,
           ("dead", "stuck_s>8"), goal=f"advance:{node.id}",
           **frame, step_id=node.id),
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


def decide(state: State, node: Node | None = None, *, context: Context | None = None) -> Plan:
    """Always returns a usable plan. Never raises, never returns None.

    `confident=False` marks a tick worth a teacher call *if one is affordable*. It does
    not mark a tick that needs one — the decision handed back is valid either way, and
    that distinction is the difference between an improvement engine and a dependency.
    """
    # Safety first, and safety is not derated: a preempt fires on a positive observation
    # (`is True`), so if one matched, something was read.
    for plan in (preempt(state), _fight(state), service(state, context=context), _recover(state)):
        if plan is not None:
            return plan

    plan = _guide(state, node) or _fallback(state)
    return _derate(plan) if _blind(state) else plan


def wants_teacher(plan: Plan) -> bool:
    """Would a teacher call improve this tick? Advisory, never blocking."""
    return not plan.confident or plan.decision.confidence < CONFIDENT
