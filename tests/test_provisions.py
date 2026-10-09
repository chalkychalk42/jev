"""bet-food (V550-V554): food and drink kept and used as a player keeps them - the best the level
can use, about twenty of each, bought at the merchants passed; on the bar where a meal presses it;
eaten and drunk together. The cases are the hive's of 8 Oct (`tests/fixtures/provisions-cases.json`):
a night elf hunter of 12 with no food 93 yards from Auberdine's fishmonger, a draenei priest of 12
selling its bags to Little Azimi with no food and ten starting waters, a gnome warlock of 10 with
406 copper at Keeg Gibn's, an orc warrior of 19 with 2 copper at Innkeeper Gryshka's, and orc and
undead warriors whose Tough Jerky sat usable on slot 12 while every meal came from the bags."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest
from test_live_body import body
from test_runtime_records import seen

from jev.clients.rest import Rest, Rested
from jev.clients.vendor import DROP_POINT, Vended, Vendor
from jev.coach.policy import Context, services
from jev.coach.schema import Decision, Intent
from jev.guide.coords import ZoneBounds, world_to_map
from jev.orch.runtime import Armed
from jev.world.combat import Role, for_class
from jev.world.state_v1 import ArmedBy, Bags, Char, GuidePos, Pos, Sense, State, Ui, Vitals
from jev.world.vendor import (
    PROVISIONS_DESIRED,
    Larder,
    Merchant,
    best_tier,
    catalog,
    provision_to_buy,
    provisions,
    stock,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "provisions-cases.json").read_text())
WARRIOR, PALADIN, HUNTER, PRIEST, WARLOCK = 1, 2, 3, 5, 9
HUMAN, ORC, DWARF, NIGHTELF, UNDEAD, GNOME, DRAENEI = 1, 2, 3, 4, 5, 7, 11


def _items(entry: int) -> frozenset[int]:
    return frozenset(next(v for v in catalog()["vendors"] if v["entry"] == entry)["items"])


def _case(kind: str, cls: str) -> dict:
    return next(c for c in CASES[kind] if c["cls"] == cls)


# -- what merchants sell (V550) ------------------------------------------------------------

def test_every_merchants_food_and_drink_is_known_by_tier_and_price():
    sold = provisions()
    assert (sold[2287].role, sold[2287].level, sold[2287].price, sold[2287].count) == \
        ("food", 5, 125, 5), "Haunch of Meat: 243 health, 5 for 1s 25c"
    assert (sold[1179].role, sold[1179].level) == ("drink", 5), "Ice Cold Milk"
    assert (sold[1205].level, sold[159].level) == (15, 1), "Melon Juice, Refreshing Spring Water"
    assert 11109 not in sold, "a quest's chicken feed, sold singly"
    assert 5350 not in sold, "conjured water is no merchant's"
    assert all(p.count == 5 for p in sold.values())


def test_the_best_tier_the_level_allows_and_the_purse_pays_for_is_bought():
    """Little Azimi, the Azure Watch innkeeper, sells bread to Moist Cornbread's tier and milk to
    Morning Glory Dew's."""
    azimi = _items(21145)
    assert provision_to_buy("food", 12, azimi, 2105).item_id == 4537, "Tel'Abim Banana, tier 5"
    assert provision_to_buy("drink", 12, azimi, 2105).item_id == 1179
    assert provision_to_buy("drink", 15, azimi, 5000).item_id == 1205
    assert provision_to_buy("food", 12, azimi, 100).item_id == 4536, "the purse pays tier 1"
    assert provision_to_buy("food", 12, azimi, 10) is None
    assert best_tier("drink", 12, azimi) == 5 and best_tier("food", 4, azimi) == 1
    # Of a tier, the one already in the bags first: it stacks.
    heldan = _items(4307)
    assert provision_to_buy("food", 12, heldan, 500).item_id == 4592
    assert provision_to_buy("food", 3, heldan, 500, {787: 4}).item_id == 787


def test_what_the_bags_hold_counts_whatever_it_is():
    rows = {4537: 6, 159: 10, 5350: 4, 2070: 3}
    assert stock(rows, "food", 12) == 9 and stock(rows, "food", 12, at_least=5) == 6
    assert stock(rows, "drink", 12) == 14, "conjured water is drink"
    assert stock(rows, "drink", 12, at_least=5) == 0


# -- how much is bought (V550) -------------------------------------------------------------

def _shopper(cls, race, level, **values):
    b = body()
    b.training_reserve = lambda state=None: 0
    return b, {"char.class_id": cls, "char.race_id": race, "char.level": level,
               "vitals.hp": 1.0, **values}


def test_the_draenei_priest_at_little_azimis_buys_twenty_bananas_and_twenty_milks():
    case = CASES["bag_visit"][0]
    assert (case["cls"], case["merchant"], case["food_count"]) == ("priest", "Little Azimi", 0)
    b, values = _shopper(PRIEST, DRAENEI, case["level"], **{"bags.money_copper": case["money"],
                                                             "bags.free": 6})
    b._larder_rows = {159: 10}
    got = {s.item_id: s for s in b._provisions_at(_items(case["entry"]), values)}
    assert set(got) == {4537, 1179}
    assert got[4537].desired == got[1179].desired == PROVISIONS_DESIRED
    assert got[4537].reserve == 230, "the repair reserve: 1.6 times 12 squared"
    # With the bags nearly full, the role it holds none of gets a slot, the other none.
    full = b._provisions_at(_items(case["entry"]), {**values, "bags.free": 2})
    assert [(s.item_id, s.desired) for s in full] == [(4537, 20)]


def test_the_purse_above_the_reserve_is_what_is_spent():
    """The gnome warlock at Keeg Gibn's: 406 copper and a reserve of 150. Half of the 256 above
    it is food's, and a first purchase for a role it holds none of comes from all of it: one of
    milk, not two."""
    case = _case("bag_visit", "warlock")
    b, values = _shopper(WARLOCK, GNOME, case["level"], **{"bags.money_copper": case["money"],
                                                            "bags.free": 8})
    b._larder_rows = {}
    (milk,) = b._provisions_at(_items(case["entry"]), values)
    assert (milk.item_id, milk.desired) == (1179, 5), "Keeg Gibn sells no food"
    b._larder_rows = {159: 5}
    assert [(s.item_id, s.desired) for s in b._provisions_at(_items(case["entry"]), values)] \
        == [(1179, 5)], "five starting waters: the share's 128 pays one purchase"
    b._larder_rows = {}
    rich = b._provisions_at(_items(case["entry"]), {**values, "bags.money_copper": 1150})
    assert [(s.item_id, s.desired) for s in rich] == [(1179, 20)], "half of 1,000: four"
    poor = _case("bag_visit", "warrior")
    b, values = _shopper(WARRIOR, ORC, poor["level"], **{"bags.money_copper": poor["money"],
                                                          "bags.free": 8})
    b._larder_rows = {}
    assert b._provisions_at(_items(poor["entry"]), values) == ()


def test_a_bag_with_one_purchase_short_of_twenty_is_not_topped_up():
    b, values = _shopper(WARRIOR, ORC, 12, **{"bags.money_copper": 5000, "bags.free": 8})
    b._larder_rows = {2287: 16, 117: 5}
    assert b._provisions_at(_items(6929), values) == ()
    b._larder_rows = {2287: 15}
    (meat,) = b._provisions_at(_items(6929), values)
    assert (meat.item_id, meat.desired) == (2287, 20)


def test_food_is_bought_at_a_merchant_visited_to_sell(monkeypatch):
    """The draenei priest's bag service at Little Azimi (8 Oct 09:21): it sold and walked away
    with no food. Now the visit buys what the merchant sells, sized on the slots the sale
    empties."""
    case = CASES["bag_visit"][0]
    b, values = _shopper(PRIEST, DRAENEI, case["level"], **{"bags.money_copper": case["money"],
                                                             "bags.free": case["free"]})
    b.client.read = lambda: values
    b.arm = Armed(Decision(goal="bags", intent=Intent.SERVICE, skill="BAG_MAKE_SPACE",
                           abort_if=["dead"], why="bags", confidence=1), ArmedBy.POLICY, 0,
                  "service.bags_full", "d", "quest")
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (
        Merchant(case["entry"], case["merchant"], 0, (51, 50, 0), _items(case["entry"])),))
    b.interact = SimpleNamespace(open_on=Mock(return_value=SimpleNamespace(opened=True)))
    calls = []

    class FakeVendor:
        detail, sold_stacks, bought_units = "", 3, 8
        last_census: ClassVar[dict] = {(0, 1): (159, 10), (0, 2): (4867, 4), (0, 3): (4865, 2),
                                       (0, 4): (0, 0)}

        def __init__(self, hid, read, visit, origin, size, eligible=None):
            calls.append({"eligible": eligible})

        def equip_bags(self, bags, **kw):
            return 0

        def bag_items(self, **kw):
            return {159, 4867, 4865}

        def census(self, **kw):
            return {(0, 1): (159, 10), (0, 2): (4537, 20), (0, 3): (1179, 20)}

        def run(self, **kw):
            calls[-1].update(kw)
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    monkeypatch.setattr("jev.run.body.junk_prices", lambda: {4867: 10, 4865: 5})
    result = b.execute(b.arm, seen(bags=Bags(free=2, money_copper=case["money"])), lambda: None)
    assert result.outcome.value == "succeeded", result
    run = next(c for c in calls if "supplies" in c)
    assert {(s.item_id, s.desired) for s in run["supplies"]} == {(4537, 20), (1179, 20)}
    assert b._larder_rows == {159: 10, 4537: 20, 1179: 20}, "counted again as it leaves"


def test_a_purchase_asked_while_the_bags_hold_bought_food_buys_nothing(monkeypatch):
    """The strip counts the starting food alone: with twenty Haunches of Meat and no Tough Jerky
    it reads empty, and the census says otherwise."""
    b, values = _shopper(WARRIOR, ORC, 12, **{"bags.money_copper": 900, "bags.free": 8})
    b.client.read = lambda: values
    b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE, skill="BUY_AMMO_REAGENT_FOOD",
                           abort_if=["dead"], why="empty", confidence=1), ArmedBy.POLICY, 0,
                  "service.supplies", "d", "quest")
    select = Mock(return_value=())
    monkeypatch.setattr("jev.run.body.merchants", select)

    class Counter:
        def __init__(self, *a, **kw):
            pass

        def census(self, **kw):
            return {(0, 1): (2287, 20)}
    monkeypatch.setattr("jev.run.body.Vendor", Counter)
    result = b.execute(b.arm, seen(bags=Bags(food_id=117, food_count=0, money_copper=900)),
                       lambda: None)
    assert (result.outcome.value, result.code) == ("succeeded", "not_needed")


# -- the policy (V550) -----------------------------------------------------------------------

def _state(*, hp=1.0, money=900, food=0, step="grind", t=1000.0, power=1.0) -> State:
    return State(t=t, client_id="c", char=Char(level=12, cls="warrior", race="orc"),
                 vitals=Vitals(hp=hp, power=power, dead=False, ghost=False, combat=False),
                 bags=Bags(free=10, durability_min=1.0, money_copper=money, food_id=117,
                           food_count=food),
                 pos=Pos(zone="Durotar", zone_id=14, mx=0.5, my=0.5),
                 guide=GuidePos(step_id=step), ui=Ui(loot=False, modal=False),
                 sense=Sense(addon_ok=True, vision_conf=1.0))


def test_the_census_not_the_starting_food_says_what_the_bags_hold():
    context = Context()
    context.larder = lambda state: Larder(roles=("food",), empty=())
    assert not services(_state(food=0), context=context), "twenty Haunches in the bags"
    context.larder = lambda state: Larder(roles=("food",), empty=("food",), price=125)
    (plan,) = services(_state(food=7), context=context)
    assert plan.rule == "service.supplies", "seven Tough Jerky of another tier... none usable"
    assert not services(_state(food=0, money=100), context=context), "the reserve kept"
    context.larder = lambda state: None
    assert services(_state(food=0), context=context)[0].rule == "service.supplies", \
        "no census yet: the strip's count, as before"


def test_a_merchant_near_while_low_is_visited_and_a_far_one_not_asked_again():
    context = Context()
    context.larder = lambda state: Larder(roles=("food",), near=("food",), near_price=125)
    assert not services(_state(food=0), context=context), \
        "900 less a reserve of 432: half of it pays one purchase, not two"
    (plan,) = services(_state(food=0, money=1000), context=context)
    assert (plan.rule, plan.decision.skill) == ("service.provisions", "BUY_AMMO_REAGENT_FOOD")
    assert "food is low" in plan.decision.why
    context.supplies_out_of_reach("too_far")
    assert services(_state(money=1000), context=context)[0].rule == "service.provisions", \
        "the session's 'too far' is for the long walk out of food"
    assert not services(_state(hp=0.5, money=1000), context=context), "a meal first (V259)"
    assert not services(_state(money=300), context=context), "the reserve kept: 300 at 12"
    context.supplies_unreachable("grind", 1000.0)
    assert not services(_state(money=1000), context=context), "out of reach on the step"


def test_the_hunter_93_yards_from_auberdines_fishmonger_with_no_food_has_one_near():
    """hive-689, a night elf hunter of 12 with 310 copper and no food, at Auberdine (8 Oct
    09:28), 93 yards from Heldan Galesong, who sells Longjaw Mud Snapper (tier 5, 20 copper)."""
    case = _case("near_empty", "hunter")
    darkshore = ZoneBounds(148, 1, 2941.66650390625, -3608.333251953125, 8333.3330078125,
                           3966.66650390625)
    b = body()
    b.client.bounds = darkshore
    b.training_reserve = lambda state=None: 0
    b._larder_rows = {159: case["drink_count"]}
    mx, my = world_to_map(*case["world"], darkshore)
    state = State(t=0.0, client_id="c", char=Char(level=case["level"], cls="hunter",
                                                 race="nightelf", faction="alliance"),
                  vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                  bags=Bags(free=case["free"], money_copper=case["money"], food_id=117,
                            food_count=0, drink_id=159, drink_count=case["drink_count"]),
                  pos=Pos(zone="Darkshore", zone_id=148, mx=mx, my=my),
                  guide=GuidePos(step_id=None), ui=Ui(loot=False, modal=False),
                  sense=Sense(addon_ok=True, vision_conf=1.0))
    larder = b.larder(state)
    assert larder.roles == ("drink", "food") and larder.empty == ("food",)
    assert "food" in larder.near and larder.near_price is not None
    assert larder.price is not None and larder.price <= 25
    assert any(m.entry == case["merchant"]["entry"] for m in b._map_provisioners())
    b._provisions_far.update(m.entry for m in b._map_provisioners())
    assert b.larder(state).near == (), "a merchant whose walk was long is not near"
    b._provisions_far.clear()
    b._larder_rows = {159: case["drink_count"], 4592: 12}
    assert "food" not in b.larder(state).near, "twelve of the tier sold here: not low"


# -- a give-up before any walk is no failure (V550) ------------------------------------------

def test_too_far_is_held_by_the_session_not_by_a_half_hour_backoff(tmp_path):
    """341 of the hive's 595 purse files held the purchase off for the full half hour (9 Oct):
    "too far" counted a failure, each in a row doubling the wait, across sessions."""
    from test_supervisor import Body, runtime

    from jev.learn.episode import SkillOutcome
    from jev.run.supervisor import Result, Supervisor

    assert CASES["purse_backoff_9_oct"]["BUY_AMMO_REAGENT_FOOD_wait_s"]["1800"] == 341

    class ShoppingBody(Body):
        available = Body.available | {"BUY_AMMO_REAGENT_FOOD"}

    empty = Bags(free=20, durability_min=1.0, money_copper=5915, food_id=4540, food_count=0,
                 drink_id=159, drink_count=0)
    rt = runtime(tmp_path, [seen(t, bags=empty) for t in (0, 1, 2)])
    body_ = ShoppingBody(result=Result(SkillOutcome.ABORTED, CASES["too_far"]["detail"],
                                       "too_far"))
    body_.allow_finish.set()
    supervisor = Supervisor(rt, body_, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body_.started.wait(1)
        assert supervisor.worker.done.wait(1)
        supervisor.step(1)
        assert not supervisor.stopped.is_set(), supervisor.failure
        context = rt.policy_context
        assert "BUY_AMMO_REAGENT_FOOD" not in context.service_failures
        assert context.supplies_unreachable_step is None
    finally:
        supervisor.close()


# -- the bar (V551) ---------------------------------------------------------------------------

def test_the_best_food_held_goes_on_the_bar_slot_and_is_remembered(tmp_path, monkeypatch):
    b = body()
    b.purse_memory = tmp_path / "purse.json"
    values = {"char.class_id": WARRIOR, "char.race_id": ORC, "char.level": 12,
              "vitals.combat": False, "ui.vendor": False}
    b.client.read = lambda: values
    b._larder_rows = {2287: 20, 117: 2}
    placed = []

    class Placer:
        detail = ""

        def __init__(self, *a, **kw):
            pass

        def place_item(self, item, slot):
            placed.append((item, slot))
            return ok[0]
    ok = [True]
    monkeypatch.setattr("jev.run.body.Vendor", Placer)
    b._stock_bar()
    assert placed == [(2287, 12)], "a warrior's food slot is its stance bar's 84: key 12"
    assert b._bar_items == {12: 2287}
    assert json.loads(b.purse_memory.read_text())["bar_items"] == {"12": 2287}
    b._stock_bar()
    assert placed == [(2287, 12)], "already there"
    b._larder_rows = {2287: 0, 117: 2}
    ok[0] = False
    b._stock_bar()
    b._stock_bar()
    assert placed[1:] == [(117, 12)], "the Jerky when the meat is gone; a miss not retried at once"


def test_a_new_characters_starting_food_is_already_on_its_slot(monkeypatch):
    b = body()
    b.client.read = lambda: {"char.class_id": PRIEST, "char.race_id": HUMAN, "char.level": 3,
                             "vitals.combat": False, "ui.vendor": False}
    b._larder_rows = {2070: 4, 159: 7}
    monkeypatch.setattr("jev.run.body.Vendor", Mock(side_effect=AssertionError("no drag")))
    b._stock_bar()


class _Strip:
    """A strip as the addon paints it for a drag from the bags to the bar: the bag census one slot
    a paint, the bar census likewise, the cursor, the bar's revision."""

    def __init__(self, *, miss=False, held=True, modal_on_drop=False):
        self.slots = [(0, 1, 117, 3), (0, 2, 2287, 20), (0, 3, 0, 0)]
        self.n = 0
        self.cursor = False
        self.locked = None                    # the bag slot whose item is on the cursor
        self.revision = 4
        self.bar = {12: None, 11: None}       # an item on each: no spell painted
        self.mouse = (0, 0)
        self.down_at = None
        self.miss, self.held, self.modal_on_drop = miss, held, modal_on_drop
        self.modal = False
        self.clicks, self.taps = [], []

    def read(self):
        self.n += 1
        bag, slot, item, count = self.slots[self.n % len(self.slots)]
        bar_slot = 11 + self.n % 2
        return {"ui.modal": self.modal, "vitals.combat": False, "vitals.dead": False,
                "vitals.ghost": False, "ui.vendor": False, "cursor.holding": self.cursor,
                "inventory.total": len(self.slots), "inventory.ordinal": self.n % len(self.slots) + 1,
                "inventory.bag": bag, "inventory.slot": slot, "inventory.item_id": item,
                "inventory.count": count, "inventory.locked": (bag, slot) == self.locked,
                "inventory.x": 0.8 + 0.01 * slot, "inventory.y": 0.7,
                "bars.slot": bar_slot, "bars.slot_x": 0.3 + 0.02 * bar_slot, "bars.slot_y": 0.95,
                "bars.slot_spell": self.bar[bar_slot], "bars.revision": self.revision}

    # the input device
    def move_to(self, x, y, steps=0):
        self.mouse = (x, y)
        return True

    def button(self, down, right=False):
        if down:
            self.down_at = self.mouse
            return True
        start, self.down_at = self.down_at, None
        if start is None or start == self.mouse:
            return True
        # picked up as the drag left the bag slot
        if not self.miss and self.mouse == (round((0.3 + 0.02 * 12) * 1600), round(0.95 * 900)):
            self.revision += 1
            self.cursor = self.held           # the old Tough Jerky action handed back
            self.locked = None
        else:
            self.cursor, self.locked = True, (0, 2)
        return True

    def click(self, x=None, y=None, right=False):
        self.clicks.append((x, y))
        if (x, y) == (round(DROP_POINT[0] * 1600), round(DROP_POINT[1] * 900)):
            if self.locked is not None and self.modal_on_drop:
                self.modal = True             # an item from the bags asks to be destroyed
            else:
                self.cursor = False
        elif self.locked is not None:
            self.cursor, self.locked = False, None   # back in its bag slot
        return True

    def tap(self, key):
        self.taps.append(key)
        if key == "esc":
            self.modal, self.cursor, self.locked = False, False, None
        return True


def _placer(strip):
    return Vendor(strip, strip.read, lambda: False, sleep=lambda s: None)


def test_a_drag_from_the_bags_puts_the_meat_on_slot_12_and_lets_the_old_action_go():
    strip = _Strip()
    placer = _placer(strip)
    assert placer.place_item(2287, 12), placer.detail
    assert strip.revision == 5 and strip.cursor is False
    assert strip.clicks == [(round(DROP_POINT[0] * 1600), round(DROP_POINT[1] * 900))], \
        "the handed-back action let go on open world, its bag slot read unlocked first"


def test_a_drop_that_misses_puts_the_item_back_and_never_lets_it_go_on_open_world():
    strip = _Strip(miss=True, modal_on_drop=True)
    placer = _placer(strip)
    assert not placer.place_item(2287, 12)
    assert "missed" in placer.detail and strip.cursor is False and not strip.modal
    assert (round(DROP_POINT[0] * 1600), round(DROP_POINT[1] * 900)) not in strip.clicks


def test_nothing_is_dragged_with_a_shop_open_or_the_cursor_full_or_the_item_absent():
    for change, why in (({"ui.vendor": True}, "shop"), ({"cursor.holding": True}, "cursor")):
        strip = _Strip()
        read = strip.read
        strip.read = lambda read=read, change=change: {**read(), **change}
        placer = _placer(strip)
        assert not placer.place_item(2287, 12) and why in placer.detail
        assert strip.down_at is None and not strip.clicks
    strip = _Strip()
    assert not _placer(strip).place_item(4599, 12)


# -- the meal (V552) ------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.rest.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.rest.time.sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


@pytest.mark.parametrize("case", CASES["warrior_bags"], ids=lambda c: c["race"])
def test_a_warriors_food_on_its_stance_bar_is_pressed_from_slot_12(clock, monkeypatch, case):
    """The orc and undead warriors' meals of 8 Oct: Tough Jerky usable on slot 12 (2049: slots 1
    and 12), every meal from the bags all the same - the profile's slot 84 read as bit 84."""
    events = []
    monkeypatch.setattr("jev.clients.rest.event",
                        lambda name, code="", data=None, **kw: events.append((name, code, data)))
    looks = iter([{**o, "vitals.combat": False, "char.class_id": WARRIOR,
                   "char.race_id": ORC} for o in case["observed"]])
    taps = []
    rest = Rest(hid=SimpleNamespace(tap=lambda key: taps.append(key) or True),
                read=lambda: next(looks), profile=for_class(WARRIOR, ORC),
                use_item=lambda role: pytest.fail("not from the bags"))
    assert rest.until(0.9) is Rested.HEALTHY
    assert taps == ["equals"] and rest.taken == {Role.FOOD: ["bar"]}
    (summary,) = [e for e in events if e[0] == "rest.summary"]
    assert summary[1] == "healthy" and summary[2]["took"] == {"food": "bar"}
    assert summary[2]["takes"] == {"food": 1} and summary[2]["start"][0] == case["observed"][0]["vitals.hp"]


def test_a_meal_with_nothing_to_eat_says_so(clock, monkeypatch):
    events = []
    monkeypatch.setattr("jev.clients.rest.event",
                        lambda name, code="", data=None, **kw: events.append((name, code, data)))
    rising = iter({"vitals.hp": 0.5 + 0.05 * i, "vitals.combat": False, "bars.usable": 1,
                   "char.class_id": WARRIOR, "char.race_id": ORC} for i in range(20))
    rest = Rest(hid=SimpleNamespace(tap=lambda key: True), read=lambda: next(rising),
                profile=for_class(WARRIOR, ORC), use_item=lambda role: False)
    assert rest.until(0.9) is Rested.HEALTHY
    summary = next(e for e in events if e[0] == "rest.summary")
    assert summary[2]["took"] == {"food": "none"} and summary[2]["takes"] == {"food": 0}


def test_a_paladin_short_of_mana_and_hurt_eats_and_drinks_at_once(monkeypatch):
    b = body()
    b.client.read = lambda: {"char.class_id": PALADIN, "char.race_id": HUMAN, "char.level": 12,
                             "vitals.combat": False}
    for name in ("_clear_of_spawns", "_wear_upgrades", "_stock_bar", "_place_spells",
                 "_spend_talents", "_bar_profile", "_conjure"):
        monkeypatch.setattr(b, name, lambda *a, **kw: None)
    b.fight = SimpleNamespace(buff_up=lambda: None, top_up=lambda: False, profile=None)
    asked = []
    b.rest = SimpleNamespace(until_both=lambda h, m: asked.append(("both", h, m)) or Rested.HEALTHY,
                             until=lambda f, role=Role.FOOD: asked.append((role, f)) or Rested.HEALTHY,
                             detail="", taken={})
    state = seen(char=Char(level=12, cls="paladin", race="human"),
                 vitals=Vitals(hp=0.55, power=0.2, power_type="mana", dead=False, ghost=False,
                               combat=False))
    b._rest(state)
    assert asked == [("both", 0.9, 0.75)], "V167 did so for a caster alone"


def test_a_walk_for_food_that_buys_nothing_is_not_asked_for_again(monkeypatch):
    """The empty rule and the merchant near each walk once for a purchase the merchant cannot make
    - dearer than the purse, no room - not again and again."""
    for rule, near, held in (("service.supplies", False, {(0, 1): (0, 0)}),
                             ("service.provisions", True, {(0, 1): (117, 2)})):
        b, values = _shopper(WARRIOR, ORC, 12, **{"bags.money_copper": 900, "bags.free": 8})
        b.client.read = lambda values=values: values
        b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE,
                               skill="BUY_AMMO_REAGENT_FOOD", abort_if=["dead"], why="food",
                               confidence=1), ArmedBy.POLICY, 0, rule, "d", "quest")
        monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (
            Merchant(6929, "Innkeeper Gryshka", 0, (51, 50, 0), _items(6929)),))
        b.interact = SimpleNamespace(open_on=Mock(return_value=SimpleNamespace(opened=True)))

        class Shop:
            detail, sold_stacks, bought_units = "", 0, 0

            def __init__(self, *a, **kw):
                pass

            def census(self, held=held, **kw):
                return dict(held)

            def run(self, **kw):
                return Vended.NOT_NEEDED
        monkeypatch.setattr("jev.run.body.Vendor", Shop)
        result = b.execute(b.arm, seen(bags=Bags(food_id=117, food_count=2, money_copper=900)),
                           lambda: None)
        assert result.outcome.value == "succeeded", result
        if near:
            assert 6929 in b._provisions_far
        else:
            assert b.policy_context.supplies_noted == "nothing_sold"
            assert not b.policy_context.can_restock(900, "quest")


def test_the_report_counts_rests_with_food_against_without_and_restocks(tmp_path, capsys):
    import subprocess
    import sys

    run = tmp_path / "20261009T050000-abc"
    run.mkdir()
    rows = [{"operation": "rest.summary", "phase": "event", "code": "healthy",
             "data": {"took": {"food": "bar"}, "seconds": 6.0}},
            {"operation": "rest.summary", "phase": "event", "code": "healthy",
             "data": {"took": {"food": "none"}, "seconds": 12.0}},
            {"operation": "fight", "phase": "end", "code": "killed"},
            {"operation": "fight", "phase": "end", "code": "killed"},
            {"operation": "bar.item", "phase": "event", "code": "placed", "data": {}}]
    (run / "executions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (run / "skills.jsonl").write_text(json.dumps({
        "skill": "BUY_AMMO_REAGENT_FOOD", "outcome": "aborted", "rule": "service.supplies",
        "detail": CASES["too_far"]["detail"]}) + "\n" + json.dumps({
        "skill": "BUY_AMMO_REAGENT_FOOD", "outcome": "succeeded", "rule": "service.provisions",
        "detail": "sold 0 stacks; bought 8 units"}) + "\n")
    out = subprocess.run([sys.executable, "tools/provisions_report.py", "--runs", str(tmp_path)],
                         capture_output=True, text=True, check=True).stdout
    assert "| with food | 1 | 6.0 | 3.00 |" in out and "| without | 1 | 12.0 | 6.00 |" in out
    assert "| service.supplies | gave_up:too_far | 1 |" in out
    assert "| service.provisions | bought | 1 |" in out and "| bar.item | placed | 1 |" in out


def test_the_share_buys_a_cheaper_tier_where_the_best_would_overspend_it():
    """A warrior of 12 with three Tough Jerky, 700 copper and a reserve of 432:
    the 134 of its share pays no Haunch of Meat at 125 twice, and buys the Jerky's tier, which
    stacks."""
    b, values = _shopper(WARRIOR, ORC, 12, **{"bags.money_copper": 700, "bags.free": 8})
    b._larder_rows = {117: 3}
    (got,) = b._provisions_at(_items(6929), values)
    assert got.item_id == 2287 and got.desired == 5, "134 pays one purchase of the meat"
    b._larder_rows = {117: 3}
    (got,) = b._provisions_at(_items(6929), {**values, "bags.money_copper": 600})
    assert (got.item_id, got.desired) == (117, 18), "84 of share: three purchases of jerky"


def test_leftovers_of_a_lower_tier_are_sold_for_room_but_not_a_pets_food():
    b, values = _shopper(PRIEST, DRAENEI, 16)
    b._larder_rows = {1205: 12, 159: 4, 4537: 3}
    assert set(b._leftovers(values)) == {159}, "twelve Melon Juice: the starting water goes"
    b._larder_rows = {1205: 9, 159: 4}
    assert b._leftovers(values) == {}, "under ten of the better: kept"
    b, values = _shopper(HUNTER, ORC, 16)
    b._larder_rows = {3770: 12, 2287: 6, 1205: 12, 159: 4}
    assert set(b._leftovers(values)) == {159}, "a hunter's pet may eat the meat"


def test_a_warrior_sells_the_water_it_cannot_use_when_the_bags_want_room():
    """hive-745 and hive-685, warriors carrying Refreshing Spring Water and no food (9 Oct)."""
    b, values = _shopper(WARRIOR, ORC, 12)
    b._larder_rows = {159: 6, 117: 2}
    assert set(b._leftovers(values)) == {159}
