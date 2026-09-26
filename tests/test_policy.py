"""The floor under everything: a valid plan, always, with no teacher and no network."""

from __future__ import annotations

import pytest

from jev.coach.policy import decide, preempt, wants_teacher
from jev.coach.schema import Intent
from jev.coach.verifier import verify
from jev.guide.graph import Graph
from jev.skills.catalog import NAMES
from jev.world.state_v1 import Bags, Pos, PowerType, Sense, State, Target, Ui, Vitals


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
         "COMBAT_PROFILE"),
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


@pytest.mark.parametrize("power_type", [None, PowerType.RAGE, PowerType.ENERGY])
def test_only_known_mana_can_need_a_drink(power_type):
    state = _s(vitals=Vitals(hp=1, combat=False, power=0, power_type=power_type))
    assert decide(state).decision.skill != "EAT_DRINK"
    mana = state.model_copy(update={"vitals": state.vitals.model_copy(update={"power_type": PowerType.MANA})})
    assert decide(mana).decision.skill == "EAT_DRINK"


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


def test_a_trainer_with_something_to_teach_is_a_service_and_a_failed_visit_waits_a_level():
    from jev.coach.policy import Context, service
    from jev.coach.verifier import verify
    from jev.world.state_v1 import Char

    asked = []
    context = Context()
    context.trainable = lambda state: asked.append(state.char.level) or True
    state = _s(char=Char(level=8))
    plan = service(state, context=context)
    assert plan.decision.skill == "TRAIN_CLASS" and plan.decision.params == {"service": "train"}
    assert verify(plan.decision, state, NAMES).ok
    context.train_failed(8)
    assert service(state, context=context) is None
    assert service(_s(char=Char(level=9)), context=context).decision.skill == "TRAIN_CLASS"
    # Nothing to teach, no trainer function, or in a fight: no training.
    context.trainable = lambda state: False
    assert service(_s(char=Char(level=9)), context=context) is None
    assert service(_s(char=Char(level=9)), context=Context()) is None
    context.trainable = lambda state: True
    fighting = _s(char=Char(level=9), vitals=Vitals(hp=1.0, power=1.0, combat=True))
    assert service(fighting, context=context) is None


def test_training_waits_for_a_meal():
    from jev.coach.policy import Context, service
    from jev.world.state_v1 import Char

    context = Context()
    context.trainable = lambda state: True
    hurt = _s(char=Char(level=8), vitals=Vitals(hp=0.4, power=1.0, combat=False))
    assert service(hurt, context=context) is None


def test_repairs_and_bags_come_before_training():
    from jev.coach.policy import Context, service
    from jev.world.state_v1 import Char

    context = Context()
    context.trainable = lambda state: True
    broken = _s(char=Char(level=8), bags=Bags(free=10, durability_min=0.0))
    assert service(broken, context=context).decision.skill == "VENDOR_REPAIR"


def test_a_caster_drinks_sooner_than_a_paladin():
    """V164: a level-1 mage's Fireball is 30 of 165 mana, and a Kobold Vermin takes three."""
    from jev.coach.policy import _recover
    from jev.world.state_v1 import Char, PowerType, State, Vitals

    def at(cls, power):
        return State(t=0, client_id="c", char=Char(cls=cls, level=1),
                     vitals=Vitals(hp=1.0, power=power, power_type=PowerType.MANA,
                                   combat=False, dead=False, ghost=False))

    assert _recover(at("mage", 0.5)) is not None
    assert _recover(at("paladin", 0.5)) is None
    assert _recover(at("paladin", 0.3)) is not None
    assert _recover(at("mage", 0.6)) is None


def test_a_caster_carries_twice_the_water():
    from jev.world.vendor import supplies_for

    mage = {s.role: s.desired for s in supplies_for(8, 1)}
    paladin = {s.role: s.desired for s in supplies_for(2, 1)}
    assert mage.get("drink") == 20 and paladin.get("drink") == 10
    assert mage.get("food") == paladin.get("food") == 10


def test_the_body_drinks_to_the_policys_line_so_a_rest_is_not_armed_again():
    """The policy armed a mage's rest at 45% mana while the body drank only below 35%: a
    rest that did nothing, armed again at once."""
    from jev.world.combat import drink_to, rest_mana

    assert rest_mana(True) > 0.45 > rest_mana(False)
    assert drink_to(True) > rest_mana(True) and drink_to(False) > rest_mana(False)


def test_a_spell_the_purse_can_pay_for_comes_before_a_repair_of_gear_not_broken():
    """V240: the level 7 mage's repairs took the copper Frostbolt waited for (session 208)."""
    from jev.coach.policy import Context, service
    from jev.world.state_v1 import Char

    context = Context()
    context.trainable = lambda state: True
    worn = _s(char=Char(level=8), bags=Bags(free=10, durability_min=0.3))
    assert service(worn, context=context).decision.skill == "TRAIN_CLASS"
    broken = _s(char=Char(level=8), bags=Bags(free=10, durability_min=0.0))
    assert service(broken, context=context).decision.skill == "VENDOR_REPAIR", "broken first"
    context.trainable = lambda state: False
    assert service(worn, context=context).decision.skill == "VENDOR_REPAIR", "nothing to learn"


def test_a_restock_does_not_buy_what_the_character_conjures():
    """V166: the bar's starting water runs out, and the conjured water is in the bags."""
    from jev.coach.policy import Context, service
    from jev.world.state_v1 import Bags, State, Vitals

    empty = State(t=0, client_id="c", vitals=Vitals(hp=1.0, combat=False),
                  bags=Bags(free=10, durability_min=1.0, money_copper=500, drink_id=159,
                            drink_count=0, food_id=2070, food_count=5))
    assert service(empty, context=Context()).decision.skill == "BUY_AMMO_REAGENT_FOOD"
    conjuring = Context()
    conjuring.conjures = lambda: frozenset({"drink"})
    plan = service(empty, context=conjuring)
    assert plan is None or plan.decision.skill != "BUY_AMMO_REAGENT_FOOD"


def test_a_casters_measured_line_decides_its_rest():
    """V170: a mage whose kills cost 30% of its mana rests below 35%, not 55%."""
    from jev.coach.policy import Context, _recover
    from jev.world.state_v1 import Char, PowerType, State, Vitals

    state = State(t=0, client_id="c", char=Char(cls="mage", level=8),
                  vitals=Vitals(hp=1.0, power=0.45, power_type=PowerType.MANA,
                                combat=False, dead=False, ghost=False))
    measured = Context()
    measured.mana_line = lambda: 0.35
    assert _recover(state, measured) is None
    assert _recover(state, Context()) is not None, "unmeasured: the fixed 55%"


def test_a_purchase_the_purse_could_not_pay_waits_for_the_purse_it_needed():
    """V186: a level 1 mage out of water and too poor walked to the merchant and back after
    every kill, each copper looted making its purse "more" than at the failure."""
    from jev.coach.policy import Context

    context = Context()
    context.supplies_failed(10)
    assert context.can_restock(11), "without the need, any gain is a reason to try again"
    context.supplies_need(60)
    assert not context.can_restock(11) and not context.can_restock(59)
    assert context.can_restock(60)


def test_a_restock_is_not_walked_to_without_one_purchase_in_the_purse():
    """V195: a level 2 mage with 10 copper and water at 25 a stack walked from Northshire to
    Goldshire's merchants and on, 2,000 yards, for water it could not buy."""
    from test_runtime_records import seen

    from jev.coach.policy import service
    from jev.world.state_v1 import Bags

    def with_purse(copper):
        return seen(bags=Bags(free=10, durability_min=1.0, money_copper=copper, food_id=2070,
                              food_count=5, drink_id=159, drink_count=0))

    assert service(with_purse(10)) is None, "one stack of water is 25 copper"
    plan = service(with_purse(25))
    assert plan is not None and plan.decision.skill == "BUY_AMMO_REAGENT_FOOD"
    assert service(with_purse(None)) is not None, "a purse not read is not a reason to stay"


def test_a_restock_buys_only_with_what_is_above_the_trainers_due():
    """V215: the level 5 mage sold its bags for 134 copper and spent the 82 left after a
    repair on water, while Conjure Water, 100 copper, went untrained."""
    from test_runtime_records import seen

    from jev.coach.policy import Context, service
    from jev.world.state_v1 import Bags

    def with_purse(copper):
        return seen(bags=Bags(free=10, durability_min=1.0, money_copper=copper, food_id=2070,
                              food_count=5, drink_id=159, drink_count=0))

    saving = Context()
    saving.reserve = lambda state: 100
    assert service(with_purse(82), context=saving) is None
    assert service(with_purse(125), context=saving).decision.skill == "BUY_AMMO_REAGENT_FOOD"
    assert service(with_purse(82), context=Context()).decision.skill == "BUY_AMMO_REAGENT_FOOD"
    saving.reserve = lambda state: 1 // 0
    assert service(with_purse(82), context=saving).decision.skill == "BUY_AMMO_REAGENT_FOOD", \
        "a reserve that cannot be worked out keeps nothing"
    asked = []
    saving.reserve = lambda state: asked.append(state) or 100
    stocked = seen(bags=Bags(free=10, durability_min=1.0, money_copper=82, food_id=2070,
                             food_count=5, drink_id=159, drink_count=3))
    assert service(stocked, context=saving) is None and not asked, \
        "nothing to buy, nothing asked of the spellbook"


def test_a_repair_the_purse_could_not_pay_waits_for_the_purse_to_grow():
    """V196: after a repair the purse could not pay, any copper more walked a broke level 2
    mage back to the smith after every kill."""
    from jev.coach.policy import REPAIR_RETRY_COPPER, Context

    broke = Context()
    broke.repair_failed(10)
    assert not broke.can_repair(11) and not broke.can_repair(10 + REPAIR_RETRY_COPPER - 1)
    assert broke.can_repair(10 + REPAIR_RETRY_COPPER)
    short = Context()
    short.repair_failed(500)
    assert not short.can_repair(999) and short.can_repair(1000), "or doubled, whichever is more"
    assert not short.can_repair(None)


def test_the_purses_lessons_are_kept_and_cleared():
    """V206: each new session forgot what the purse could not pay, and the level 4 mage's
    first act every session was its hearthstone and a walk to a smith it still could not
    pay (26 September)."""
    from jev.coach.policy import Context

    saves = []
    first = Context(saved=lambda: saves.append(1))
    first.repair_failed(45)
    first.supplies_failed(20)
    first.supplies_need(25)
    assert len(saves) == 3
    later = Context()
    later.restore_purse(first.purse())
    assert not later.can_repair(50) and later.can_repair(145)
    assert not later.can_restock(20) and later.can_restock(25)
    later.saved = lambda: saves.append(2)
    later.repaired()
    later.restocked()
    assert later.can_repair(0) and later.purse()["supplies_needed"] is None
    assert saves[-2:] == [2, 2]
    later.restore_purse({"repair_blocked": "yes", "repair_money": True})
    assert later.repair_blocked is False and later.repair_money is None, "only its own values"



def test_fights_that_never_engage_pause_combat_so_the_walk_goes_on():
    """V212: a Defias Smuggler at 6% health threw knives from out of sight; the paladin
    turned and healed through 74 and 41 fights "not visible" in two sessions. After three
    fights that never engaged, combat stays off the floor for a while, save the panic."""
    from jev.coach.policy import FIGHT_PAUSE_S, Context

    context = Context()
    hit = _s(vitals=Vitals(hp=0.6, combat=True), target=Target(has=True, in_melee=False))
    assert decide(hit, context=context).decision.skill == "COMBAT_PROFILE"
    for _ in range(2):
        context.fight_ended("not_visible", hit.t)
    context.fight_ended("killed", hit.t)
    assert not context.fight_paused(hit.t), "a kill between them starts the count again"
    for _ in range(3):
        context.fight_ended("not_visible", hit.t)
    assert context.fight_paused(hit.t)
    assert decide(hit, context=context).decision.skill != "COMBAT_PROFILE"
    critical = _s(vitals=Vitals(hp=0.1, combat=True), target=Target(has=True, in_melee=False))
    assert decide(critical, context=context).decision.skill == "COMBAT_PROFILE", "the panic"
    later = hit.model_copy(update={"t": hit.t + FIGHT_PAUSE_S + 1})
    assert decide(later, context=context).decision.skill == "COMBAT_PROFILE"
