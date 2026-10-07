"""Solvency before skills (V393): a repair reserve, the repair before the trainer, the bags sold
for a repair the purse cannot pay, no pulls on a broken weapon while its repair is near, and a
hunter's ammunition. The cases are the hive's (7 Oct): hive-523, a level 13 rogue with a broken
weapon and 46 copper, its repair waiting for 124; hive-468, a level 15 rogue, broken, killed four
times by 02:42 with 8 copper and 43 copper of goods in its bags."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_body import body
from test_runtime_records import seen

from jev.clients.fight import Fight
from jev.clients.repair import Repair, Repaired
from jev.clients.vendor import Vended, Vendor
from jev.coach import judge
from jev.coach.policy import (
    BROKEN_STAND_S,
    Context,
    broken_wait,
    decide,
    repair_reserve,
    services,
)
from jev.coach.schema import Decision, Intent
from jev.guide.graph import Node
from jev.orch.runtime import Armed
from jev.run.hunt import Hunt, Hunted
from jev.world.combat import for_class
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    GuidePos,
    Pos,
    Sense,
    State,
    StepKind,
    Ui,
    Vitals,
)
from jev.world.vendor import Merchant, Supply, ammo_for, ammo_kind, junk_prices


def _s(level=13, cls="rogue", race="bloodelf", step="grind", **bags) -> State:
    return State(t=1_000.0, client_id="c", char=Char(level=level, cls=cls, race=race),
                 vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                 bags=Bags(free=bags.pop("free", 10), **bags),
                 pos=Pos(zone="Ghostlands", zone_id=3433, mx=0.5, my=0.5),
                 guide=GuidePos(step_id=step), ui=Ui(loot=False, modal=False),
                 sense=Sense(addon_ok=True, vision_conf=1.0))


GRIND = Node(id="grind", kind=StepKind.GRIND, zone="Ghostlands", zone_id=3433, pos=(0.5, 0.5))
ACCEPT = Node(id="accept", kind=StepKind.QUEST_ACCEPT, zone="Ghostlands", zone_id=3433,
              pos=(0.5, 0.5))


# --- the reserve -----------------------------------------------------------------------

def test_the_repair_reserve_is_a_full_repair_from_nothing_at_the_level():
    """The hive's melee kits cost a median 189, 407 and 892 copper to mend from nothing at
    levels 6-10, 11-15 and 16-20; a rogue's 2.5 times the level squared, at the middle of
    each, a warrior's 3, a hunter's 0.8 and a mage's cloth 1.2."""
    assert [repair_reserve(level, "rogue") for level in (8, 13, 18)] == [160, 422, 810]
    assert repair_reserve(13, "warrior") == 507 and repair_reserve(12, "hunter") == 115
    assert repair_reserve(20, "mage") == 480 and repair_reserve(13) == 422, "unread: a rogue's"
    assert repair_reserve(None) == 0 and repair_reserve(0) == 0 and repair_reserve(True) == 0


def test_a_purchase_keeps_the_repair_reserve_or_the_trainers_due_whichever_is_more():
    context = Context()
    context.reserve = lambda state: 800                  # a level 12 spell
    assert context.kept(_s(level=12)) == 800
    context.reserve = lambda state: 0                    # nothing left to learn
    assert context.kept(_s(level=12)) == 360
    out_of_food = dict(food_id=2070, food_count=0, durability_min=1.0)
    assert not services(_s(level=12, money_copper=360, **out_of_food), context=context), \
        "the reserve is not spent on bread"
    assert services(_s(level=12, money_copper=500, **out_of_food),
                    context=context)[0].decision.skill == "BUY_AMMO_REAGENT_FOOD"


# --- the order ---------------------------------------------------------------------------

def test_under_60_percent_a_repairer_a_short_detour_off_is_visited():
    context = Context()
    context.repairer_near = lambda state: True
    assert [p.rule for p in services(_s(durability_min=0.5), context=context)] == [
        "service.repair_near"]
    assert services(_s(durability_min=0.65), context=context) == []
    context.repairer_near = lambda state: False
    assert services(_s(durability_min=0.5), context=context) == [], "far off: not for 50%"


@pytest.mark.parametrize(("smith", "near"), [
    ((480.0, 480.0), True),      # on the way to the step
    ((500.0, 380.0), True),      # 120 yards aside: 42 more
    ((900.0, 900.0), False),     # the other way: 1,131 more
    ((80.0, 980.0), False),      # 70 yards from the step, but 600 more from here
])
def test_a_repairer_is_near_when_walking_by_it_is_a_short_detour(monkeypatch, smith, near):
    from jev.guide.coords import ZoneBounds

    b = body(StepKind.GRIND)
    b.client.bounds = ZoneBounds(1, 0, 1000, 0, 1000, 0)    # the step stands at (50, 50)
    repairer = Merchant(54, "Smith", 0, (*smith, 0.0), frozenset(), repairs=True)
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (repairer,))
    here = seen(guide=GuidePos(step_id="quest"))            # at (500, 500)
    assert b.repairer_near(here) is near
    assert not b.repairer_near(here.model_copy(update={"pos": Pos(zone="z")})), "unplaced"


def test_the_repair_is_asked_for_below_60_percent():
    """The skill does what the policy asks under 60%, not "not needed" and asked again."""
    worn = Repair(hid=None, read=lambda: {"bags.durability_min": 0.5}, visit=lambda: False)
    assert worn.needed()
    assert not Repair(hid=None, read=lambda: {"bags.durability_min": 0.6},
                      visit=lambda: False).needed()


# --- too poor ------------------------------------------------------------------------------

def test_hive_523_with_46_copper_is_sent_to_the_repairer_again():
    """Its purse file held a repair blocked at 24 copper since 6 Oct 05:00, waiting for 124
    (V196) while it fought on with every item at nothing."""
    context = Context()
    context.restore_purse({"repair_blocked": True, "repair_money": 24})
    broken = _s(durability_min=0.0, money_copper=46)
    assert services(broken, context=context)[0].rule == "service.broken"


def _repairing(monkeypatch, answers, *, sold=2, census=None):
    """A level 13 rogue's body at a smith a yard off: the repair answers `answers` in turn, and
    the sale at the smith sells `sold` stacks of the bags' `census`."""
    b = body(StepKind.GRIND)
    smith = Merchant(54, "Smith", 0, (50, 51, 0), frozenset(), repairs=True)
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (smith,))
    b.client.read = lambda: {"vitals.hp": 1.0, "bags.durability_min": 0.0, "bags.free": 4,
                             "char.class_id": 4, "char.race_id": 10, "char.level": 13}
    left = list(answers)

    def run():
        b._repairer_at = (smith.name, smith.world, (0.49, 0.5))
        return left.pop(0)
    b.repair = SimpleNamespace(run=run, detail="")
    junk = next(iter(junk_prices()))
    sales = []

    class FakeVendor:
        def __init__(self, hid, read, visit, origin, size, eligible=None):
            self.visit, self.eligible, self.sold_stacks = visit, eligible, 0
            self.last_census = {}

        def bag_items(self, **kw):
            self.last_census = census if census is not None else {(0, 1): (junk, 5)}
            return {item for item, _ in self.last_census.values()}

        def run(self, **kw):
            sales.append((kw, self.eligible))
            self.sold_stacks = sold
            return Vended.DONE if sold else Vended.NO_JUNK
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    return b, sales, junk


def test_a_repair_the_purse_cannot_pay_sells_the_bags_to_the_repairer_and_is_pressed_again(
        monkeypatch):
    """hive-523's bags held 249 copper of junk and 144 of surplus: with them sold, Repair All
    reaches its main hand (slot 15) after the armour, 280 of the 407 copper."""
    b, sales, junk = _repairing(monkeypatch, [Repaired.TOO_POOR, Repaired.DONE])
    b.policy_context.repair_failed(24)
    result = b._repair(_s(durability_min=0.0, money_copper=46))
    assert result.code == "done" and not b.policy_context.repair_blocked
    (kw, eligible), = sales
    assert kw["expected_name"] == "Smith" and kw["min_free"] >= 999, "everything it may sell"
    assert junk in eligible


def test_hive_468_too_poor_with_its_bags_sold_plays_on_until_it_has_more(monkeypatch):
    """With 8 copper and 43 of goods, Repair All spends 32 on its belt and never reaches the
    40 its Long Bayonet costs (the main hand alone would need the bridge's per-item repair).
    Its repair is not asked again until it has more copper or more to sell, and meanwhile its
    grind pulls: with no pull there is no loot and no copper."""
    b, sales, _ = _repairing(monkeypatch, [Repaired.TOO_POOR, Repaired.TOO_POOR])
    result = b._repair(_s(level=15, durability_min=0.0, money_copper=8))
    assert result.code == "too_poor" and len(sales) == 1
    b.policy_context.repair_failed(8)              # as the supervisor records it
    context = Context()
    context.restore_purse(b.policy_context.purse())
    assert context.repair_free == 4
    poor = _s(level=15, durability_min=0.0, money_copper=8, free=4)
    context.disarmed = lambda state: True
    context.service_failed("VENDOR_REPAIR", poor.t)
    assert services(poor, context=context) == []
    assert broken_wait(poor, GRIND, context) is None, "too poor: no wait, the grind earns"
    assert decide(poor, GRIND, context=context).decision.skill == "GRIND_UNTIL"
    later = poor.model_copy(update={"t": poor.t + 61})
    richer = later.model_copy(update={"bags": Bags(free=4, durability_min=0.0, money_copper=9)})
    assert services(richer, context=context)[0].rule == "service.broken"
    fuller = later.model_copy(update={"bags": Bags(free=3, durability_min=0.0, money_copper=8)})
    assert services(fuller, context=context)[0].rule == "service.broken"


def test_with_nothing_to_sell_the_repair_is_not_pressed_twice(monkeypatch):
    b, sales, _ = _repairing(monkeypatch, [Repaired.TOO_POOR, Repaired.DONE],
                             census={(0, 1): (6948, 1)})          # the hearthstone
    assert b._repair(_s(durability_min=0.0, money_copper=8)).code == "too_poor"
    assert sales == []


# --- no pulls on a broken weapon ----------------------------------------------------------

def _broken_context(*, failed_ago: float | None = 30.0) -> Context:
    context = Context()
    context.disarmed = lambda state: True
    if failed_ago is not None:
        context.service_failed("VENDOR_REPAIR", 1_000.0 - failed_ago)
    return context


def test_a_broken_weapon_pulls_nothing_while_its_repair_is_a_moment_off():
    broken = _s(durability_min=0.0, money_copper=500)
    plan = decide(broken, GRIND, context=_broken_context())
    assert plan.rule == "wait.broken" and plan.decision.skill is None
    assert decide(broken, ACCEPT, context=_broken_context()).rule != "wait.broken", \
        "a hand-in or an accept goes on"
    whole = _broken_context()
    whole.disarmed = lambda state: False
    assert decide(broken, GRIND, context=whole).rule != "wait.broken", "armour broken only"
    long = _broken_context()
    long.service_failed("VENDOR_REPAIR", 1_000.0)          # two in a row: two minutes
    assert decide(broken, GRIND, context=long).decision.skill == "GRIND_UNTIL"
    assert BROKEN_STAND_S == 60.0
    due = decide(broken, GRIND, context=_broken_context(failed_ago=None))
    assert due.rule == "service.broken", "with nothing holding it, the repair itself"


def test_jev_is_not_offered_the_grind_while_a_broken_weapon_waits():
    broken = _s(durability_min=0.0, money_copper=500)
    context = _broken_context()
    floor = decide(broken, GRIND, context=context)
    picks = judge.candidates(broken, GRIND, context, floor)
    assert floor in picks and all(p.decision.skill != "GRIND_UNTIL" for p in picks)


def test_the_weapon_is_read_broken_from_its_greyed_blows():
    """A rogue with a broken weapon pressed Sinister Strike 0.5 times a minute against 13.4:
    the client greys it out, and the hive's strip does as the server's `item_ok` says."""
    f = Fight(hid=None, read=lambda: None, read_frame=lambda: None,
              profile=for_class(4, 10))                   # Sinister Strike in slot 2
    rogue = {"bags.durability_min": 0.0, "vitals.power": 1.0, "vitals.power_max": 100,
             "char.class_id": 4, "char.race_id": 10}
    assert f.disarmed({**rogue, "bars.usable": 0b1}) is True
    assert f.disarmed({**rogue, "bars.usable": 0b11}) is False, "armour broken, blade whole"
    assert f.disarmed({**rogue, "vitals.power": 0.2, "bars.usable": 0}) is False, \
        "too little energy tells nothing: the last reading stands"
    assert f.disarmed({**rogue, "bags.durability_min": 0.4}) is False
    assert f.disarmed_seen is None
    warrior = Fight(hid=None, read=lambda: None, read_frame=lambda: None,
                    profile=for_class(1, 1))              # Heroic Strike on the stance page
    out_of_combat = {"bags.durability_min": 0.0, "vitals.power": 0.0, "vitals.power_max": 1000,
                     "bars.usable": 0, "char.class_id": 1}
    assert warrior.disarmed(out_of_combat) is None, "no rage: nothing tells"
    assert warrior.disarmed({**out_of_combat, "vitals.power": 0.3}) is True


def test_the_body_says_a_weapon_is_broken_as_the_fight_read_it_or_by_the_class():
    b = body()
    broken = _s(durability_min=0.0)
    assert b.disarmed(broken), "a rogue with something broken, nothing read"
    assert not b.disarmed(_s(cls="mage", race="human", durability_min=0.0))
    b.fight.disarmed_seen = False
    assert not b.disarmed(broken), "read: the blade is whole"
    assert not b.disarmed(_s(durability_min=0.3))


def test_a_hunt_takes_no_pull_while_the_repair_comes_first():
    fight = SimpleNamespace(run=Mock(), top_up=lambda *a, **k: False, top_ups=0)
    rest = SimpleNamespace(until=lambda *a, **k: None, detail="")
    hunt = Hunt(fight=fight, rest=rest, read=lambda: {"vitals.combat": False},
                approach=lambda *a, **k: True, progress=lambda: (0, 5), say=lambda s: None)
    hunt.pull_refused = lambda values: "equipment is broken"
    assert hunt.run((0.0, 0.0, 0.0), 30.0, None) is Hunted.SERVICE_NEEDED
    assert hunt.detail == "equipment is broken" and not fight.run.called


def test_the_body_refuses_a_pull_for_a_repair_due_once_the_meal_is_eaten(monkeypatch):
    b = body(StepKind.GRIND)
    b.arm = Armed(Decision(goal="g", intent=Intent.ADVANCE, skill="GRIND_UNTIL",
                           abort_if=["dead"], confidence=1, why="fixture"),
                  ArmedBy.POLICY, 0, "guide", "d", "quest")
    reading = {"vitals.hp": 1.0, "bags.durability_min": 0.0}
    b.client.read = lambda: reading
    b.client.recent_state = lambda age: _s(durability_min=0.0, money_copper=500)
    assert b._pull_refused({"bags.durability_min": 0.9}) is None, "whole gear: no reading"
    assert b._pull_refused(reading) == "equipment is broken"
    b.policy_context.repair_failed(500)
    assert b._pull_refused(reading) is None, "too poor: the pull earns"


# --- a hunter's ammunition --------------------------------------------------------------

def test_a_hunter_shoots_what_it_carries_or_what_its_race_began_with():
    assert ammo_kind(3) == "bullet" and ammo_kind(6) == "bullet" and ammo_kind(2) == "arrow"
    assert ammo_kind(3, (2512,)) == "arrow", "a dwarf with a bow"
    assert ammo_for("arrow", 12)[0] == (2515, 50) and ammo_for("bullet", 9) == ((2516, 10),)


def test_a_hunters_ammunition_floor_is_bought_with_the_whole_purse():
    """22 of the hive's 50 hunters carried none on 7 Oct; Jev never bought it. The floor's
    rounds are the weapon's other half, paid as a repair is (V401, amending V393's reserve)."""
    context = Context()
    context.ammo_low = lambda state: 50
    context.reserve = lambda state: 10_000                 # a trainer's due does not hold it
    hunter = dict(level=12, cls="hunter", race="orc", durability_min=1.0)
    assert services(_s(money_copper=50, **hunter), context=context)[0].rule == "service.ammo"
    assert services(_s(money_copper=49, **hunter), context=context) == [], "short of it"
    context.ammo_low = lambda state: None
    assert services(_s(money_copper=10_000, **hunter), context=context) == []


def test_the_body_counts_a_hunters_rounds_at_the_census_and_prices_the_next():
    b = body()
    hunter = _s(level=12, cls="hunter", race="orc")
    assert b.ammo_low(hunter) is None, "not counted"
    b._count_ammo({(0, 1): (2512, 150), (0, 2): (2512, 30), (0, 3): (117, 5)},
                  {"char.class_id": 3, "char.race_id": 2})
    assert b._ammo_count == 180 and b._ammo_carried == (2512,)
    assert b.ammo_low(hunter) == 10, "a stack of the Rough Arrows loaded, 300 the floor (V401)"
    assert b.ammo_low(_s(level=12)) is None, "a rogue"
    b._count_ammo({(0, 1): (2512, 400)}, {"char.class_id": 3, "char.race_id": 2})
    assert b.ammo_low(hunter) is None


def test_a_restock_buys_the_ammunition_keeping_the_repair_reserve(monkeypatch):
    b = body()
    b.client.read = lambda: {"vitals.hp": 1, "char.class_id": 3, "char.race_id": 2,
                             "char.level": 12}
    b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE, skill="BUY_AMMO_REAGENT_FOOD",
                           abort_if=["dead"], why="ammunition", confidence=1), ArmedBy.POLICY, 0,
                  "guide", "d", "quest")
    bowyer = Merchant(1, "Bowyer", 0, (51, 50, 0), frozenset({2512, 2515}))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (bowyer,))
    b.interact = SimpleNamespace(open_on=Mock(return_value=SimpleNamespace(opened=True)))
    calls = []

    class FakeVendor:
        detail, sold_stacks, bought_units = "", 0, 3

        def __init__(self, hid, read, visit, origin, size, eligible=None):
            pass

        def run(self, **kw):
            calls.append(kw)
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    b._count_ammo({(0, 1): (2512, 40)}, {"char.class_id": 3, "char.race_id": 2})
    state = seen(char=Char(level=12, cls="hunter", race="orc"),
                 bags=Bags(food_id=117, food_count=5))
    assert b.execute(b.arm, state, lambda: None).outcome.value == "succeeded"
    (ammo,) = calls[0]["supplies"]
    assert (ammo.item_id, ammo.role, ammo.desired, ammo.reserve) == (2512, "ammo", 300, 0), \
        "the floor of what is loaded (V401); no purse read, no fill"
    assert b._ammo_count is None, "counted again at the next census"


def test_a_supply_keeps_its_own_reserve_where_it_has_one():
    bought = []
    v = Vendor(hid=None, read=lambda: None, visit=lambda: True)
    v._safe = lambda values: values or {}
    v._await = lambda *a, **k: {}
    v._sell = lambda min_free: None
    v._buy = lambda supply, reserve: bought.append((supply.role, reserve))
    v.read = lambda: {"ui.modal": False, "vitals.combat": False, "vitals.dead": False,
                      "vitals.ghost": False}
    v.run(expected_name="Bowyer", supplies=(Supply(1, "bread", "food"),
                                            Supply(2, "arrows", "ammo", reserve=360)),
          reserve_copper=800, sell=False)
    assert bought == [("food", 800), ("ammo", 360)]


def test_printed_reasons_stay_short_and_ascii():
    broken = _s(durability_min=0.0, money_copper=500)
    plan = decide(broken, GRIND, context=_broken_context())
    assert plan.decision.why.isascii() and len(plan.decision.why) < 80
