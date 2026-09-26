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
from jev.world.combat import HEAL_OUT_OF_COMBAT, is_caster, rest_mana
from jev.world.state_v1 import PowerType, State, StepKind
from jev.world.vendor import supply_prices

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
# spellbook onto the bar, which the tutor has no control for, and binding the hearthstone
# answers a confirmation the tutor has no row for either. A meal between fights is the
# routine's too: it was a third of the tutor's decisions in teach mode (38 of 112 over five
# sessions), its choices do not separate in any state a student sees (it ate at a median
# 57% health and walked about at 71-80%), and walking about took it off its route - session
# 93's walk back from a mine grew from 1,070 yards to 1,169 and its hand-in failed over.
ROUTINE_RULES = ("service.train", "service.bind", "service.discover", "recover.eat")


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

    # A repairer out of reach is not walked to again on this step, as a merchant for
    # supplies is not (V175, V185): the walk out of Sentinel Hill's inn to William MacGregor
    # stuck in its doorway, and one failed repair stopped session 144.
    repair_unreachable_step: str | None = None

    def repair_unreachable(self, step_id: str | None) -> None:
        self.repair_unreachable_step = step_id

    def can_repair(self, money: int | None, step_id: str | None = None) -> bool:
        if step_id is not None and step_id == self.repair_unreachable_step:
            return False
        # An unknown purse cannot establish that an unaffordable repair became payable. The
        # repair's price is not read before it is asked for: the purse must have doubled, or
        # grown by `REPAIR_RETRY_COPPER`, whichever is more (V196). Any copper more walked a
        # broke level 2 mage to Northshire's smith after every kill.
        return not self.repair_blocked or (
            money is not None and self.repair_money is not None
            and money >= max(2 * self.repair_money, self.repair_money + REPAIR_RETRY_COPPER)
        )

    def supplies_failed(self, money: int | None) -> None:
        self.supplies_blocked, self.supplies_money = True, money

    # What the purchase that could not be paid needed, when the merchant said (V186): a
    # level 1 mage out of water, too poor to buy more, walked 100 to 150 yards to Northshire's
    # merchant and back after every kill, each copper looted making the purse "more" than
    # at the failure (the mage's check, 26 September).
    supplies_needed: int | None = None

    def supplies_need(self, copper: int | None) -> None:
        self.supplies_needed = copper

    # A merchant out of reach (the walk or the talk timed out) is not walked to again on
    # this step, as a trainer is not (V175): Goldshire's innkeeper, upstairs of whom the
    # walk kept ending, stopped two sessions at T-0.
    supplies_unreachable_step: str | None = None

    def supplies_unreachable(self, step_id: str | None) -> None:
        self.supplies_unreachable_step = step_id

    def can_restock(self, money: int | None, step_id: str | None = None) -> bool:
        if step_id is not None and step_id == self.supplies_unreachable_step:
            return False
        if not self.supplies_blocked:
            return True
        if money is not None and self.supplies_needed is not None:
            return money >= self.supplies_needed
        return money is not None and self.supplies_money is not None and money > self.supplies_money

    bags_blocked: bool = False
    # The fewest free slots since a merchant visit found nothing to sell, and whether one
    # has freed since: a slot freed and filled again may hold something a merchant buys.
    bags_blocked_at: int = 0
    bags_freed: bool = False

    def bags_failed(self, free: int | None = None) -> None:
        self.bags_blocked, self.bags_blocked_at, self.bags_freed = True, free or 0, False

    def can_make_space(self, free: int | None) -> bool:
        # Bags with nothing a merchant may buy: another visit changes nothing until something
        # new is in them, and a run asking again stops on it.
        if self.bags_blocked and free is not None:
            if free > BAGS_LOW or (self.bags_freed and free <= self.bags_blocked_at):
                self.bags_blocked = False
            elif free > self.bags_blocked_at:
                self.bags_freed = True
            elif not self.bags_freed:
                self.bags_blocked_at = free
        return not self.bags_blocked

    # Whether a class trainer has something to teach that the purse can pay for, from
    # the spellbook census and the trainer catalog (`LiveBody.trainable`). Absent, no
    # training is ever asked for.
    trainable: Callable[[State], bool] | None = None
    # What the character makes for itself ("drink", "food"): a caster's conjures, which
    # a restock need not buy (`LiveBody.conjured_roles`, V166).
    conjures: Callable[[], frozenset[str]] | None = None
    # A caster's measured mana line (`Fight.mana_line`, V170): `None` keeps the fixed one.
    mana_line: Callable[[], float | None] | None = None

    def conjured(self) -> frozenset[str]:
        try:
            return self.conjures() if self.conjures is not None else frozenset()
        except Exception:
            return frozenset()
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

    # Whether an inn stands near the guide's work while home is far or unknown
    # (`LiveBody.bindable`). Absent, the hearthstone is never bound.
    bindable: Callable[[State], bool] | None = None
    bind_blocked: tuple[str | None] | None = None      # (step,) once a bind failed there

    def bind_failed(self, step_id: str | None) -> None:
        # One try a step: the next step may be near another inn, or this one reachable.
        self.bind_blocked = (step_id,)

    def can_bind(self, state: State) -> bool:
        if self.bindable is None or self.bind_blocked == (state.guide.step_id,):
            return False
        return self.bindable(state)

    # Whether an unvisited flight master stands near the character (`LiveBody.discoverable`).
    discoverable: Callable[[State], bool] | None = None
    discover_blocked: tuple[str | None] | None = None  # (step,) once a visit failed there

    def discover_failed(self, step_id: str | None) -> None:
        self.discover_blocked = (step_id,)

    def can_discover(self, state: State) -> bool:
        if self.discoverable is None or self.discover_blocked == (state.guide.step_id,):
            return False
        return self.discoverable(state)


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


# Free bag slots at which the merchant is visited. Not none: every fight on the way there
# drops loot, and with full bags session 81 left four kills unlooted on its way to one.
BAGS_LOW = 2
# After a repair the purse could not pay, how much more it must hold before the next (V196).
REPAIR_RETRY_COPPER = 100

# --------------------------------------------------------------------------- soft tier


def _affordable(item_id: int, money: int | None) -> bool:
    """Is one purchase of this food or drink in the purse? A purse or price not known is not
    a reason to stay (V195). A level 2 mage with 10 copper and water at 25 walked from
    Northshire to Goldshire's merchants and on for it, 2,000 yards (the mage's third check)."""
    price = supply_prices().get(item_id)
    return money is None or not price or money >= price


def service(state: State, *, context: Context | None = None) -> Plan | None:
    """The service priority, shared by idle selection and long-skill handoff."""
    if state.vitals.combat is not False:
        return None
    b = state.bags
    can_repair = context is None or context.can_repair(b.money_copper, state.guide.step_id)
    can_sell = context is None or context.can_make_space(b.free)

    # `is not None` throughout: unknown bags are not full bags, and a service loop on an
    # unread number is the failure the verifier's `no_service_loop` rule also guards.
    if can_repair and b.durability_min is not None and b.durability_min <= 0.05:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "equipment is broken", 0.85,
                       ("dead", "combat"), service="repair"), True, "service.broken")

    if can_sell and b.free is not None and b.free <= BAGS_LOW:
        return Plan(_d(Intent.SERVICE, "BAG_MAKE_SPACE", "bags are nearly full",
                       0.75, ("dead", "combat"), service="bags"), True, "service.bags_full")

    if can_repair and b.durability_min is not None and b.durability_min < 0.35:
        return Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "durability is low", 0.65,
                       ("dead", "combat"), service="repair"), True, "service.durability")

    conjured = context.conjured() if context is not None else frozenset()
    if ((context is None or context.can_restock(b.money_copper, state.guide.step_id))
            and ((b.food_id is not None and b.food_count == 0 and "food" not in conjured
                  and _affordable(b.food_id, b.money_copper))
                 or (b.drink_id is not None and b.drink_count == 0
                     and "drink" not in conjured and _affordable(b.drink_id, b.money_copper)))):
        return Plan(_d(Intent.SERVICE, "BUY_AMMO_REAGENT_FOOD", "confirmed food or drink is empty",
                       0.8, ("dead", "combat"), service="supplies"), True, "service.supplies")

    # Last: spells a trainer would teach now. A paladin that never trained fought to level
    # 8 on Seal of Righteousness and Holy Light rank 1, losing to two wolves at once. It can
    # wait for a meal: the walk to Goldshire's trainer is 640 yards of Elwynn.
    if context is not None and _recover(state, context) is None and context.can_train(state):
        return Plan(_d(Intent.SERVICE, "TRAIN_CLASS", "the class trainer has spells to teach",
                       0.6, ("dead", "combat"), service="train"), True, "service.train")

    # And a home near the work: a hearthstone bound to Northshire took a level 9 back
    # there four times in a day from Goldshire and Fargodeep.
    if context is not None and _recover(state, context) is None and context.can_bind(state):
        return Plan(_d(Intent.SERVICE, "BIND_HEARTH", "home is far from the guide's work",
                       0.55, ("dead", "combat"), service="bind"), True, "service.bind")

    # A flight master passed is a node to fly back to later: only a visited node can be.
    if context is not None and _recover(state, context) is None and context.can_discover(state):
        return Plan(_d(Intent.SERVICE, "DISCOVER_FLIGHT", "an unvisited flight master is near",
                       0.5, ("dead", "combat"), service="discover"), True, "service.discover")

    return None


def _recover(state: State, context: Context | None = None) -> Plan | None:
    v = state.vitals
    if v.combat is True:
        return None
    hp, power = v.hp, v.power
    caster = is_caster(state.char.cls)
    measured = None
    if caster and context is not None and context.mana_line is not None:
        try:
            measured = context.mana_line()
        except Exception:
            measured = None
    line = measured if measured is not None else rest_mana(caster)
    low_mana = v.power_type is PowerType.MANA and power is not None and power < line
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
    for plan in (preempt(state), _fight(state), service(state, context=context),
                 _recover(state, context)):
        if plan is not None:
            return plan

    plan = _guide(state, node) or _fallback(state)
    return _derate(plan) if _blind(state) else plan


def wants_teacher(plan: Plan) -> bool:
    """Would a teacher call improve this tick? Advisory, never blocking."""
    return not plan.confident or plan.decision.confidence < CONFIDENT
