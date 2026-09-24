"""Verified stock-UI transactions with a responsive fake device, never a client."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jev.clients.vendor import Vended, Vendor
from jev.perceive.radio_frame import name_id
from jev.world.vendor import Supply, catalog, merchants, supplies_for


class Shop:
    def __init__(self):
        self.now = 0.0
        self.clicks = []
        self.keys = []
        self.no_change = False
        self.refuse = False
        self.v = {"ui.modal": False, "ui.vendor": False, "vitals.combat": False,
                  "vitals.dead": False, "vitals.ghost": False,
                  "bags.money_copper": 100, "bags.free": 0,
                  "inventory.ordinal": 1, "inventory.total": 1, "inventory.bag": 0,
                  "inventory.slot": 1, "inventory.item_id": 7073, "inventory.count": 2,
                  "inventory.quality": 0, "inventory.locked": False,
                  "inventory.revision": 0, "inventory.x": 0.8, "inventory.y": 0.5,
                  "merchant.name_id": name_id("Merchant"), "merchant.ready": True, "merchant.page": 1,
                  "merchant.total": 1, "merchant.index": 1, "merchant.item_id": 2070,
                  "merchant.owned": 0, "merchant.price": 20, "merchant.quantity": 5,
                  "merchant.unlimited": True, "merchant.extended": False,
                  "merchant.x": 0.1, "merchant.y": 0.5,
                  "bags.food_id": 2070, "bags.food_count": 0}

    def read(self):
        return self.v.copy()

    def visit(self):
        self.v["ui.vendor"] = True
        return True

    def sleep(self, seconds):
        self.now += seconds

    def tap(self, key):
        self.keys.append(key)
        self.v["ui.vendor"] = False

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))
        if self.refuse:
            return False
        if self.no_change:
            return True
        if not right:
            self.v["inventory.x"] = 0.8
        elif x > 800:
            self.v.update({"inventory.item_id": 0, "inventory.count": 0,
                           "bags.free": 1, "inventory.revision": 1})
            self.v["bags.money_copper"] += 12  # two confirmed 6-copper junk items
        else:
            self.v["bags.money_copper"] -= self.v["merchant.price"]
            self.v["merchant.owned"] += self.v["merchant.quantity"]
            self.v["bags.food_count"] = self.v["merchant.owned"]
        return True

    def body(self):
        return Vendor(self, self.read, self.visit, eligible={7073: 6},
                      clock=lambda: self.now, sleep=self.sleep)


def test_sells_then_restocks_using_confirmed_cash_and_inventory_deltas():
    shop = Shop()
    body = shop.body()
    result = body.run(expected_name="Merchant", min_free=1,
                      supplies=(Supply(2070, "Darnassian Bleu", "food", desired=10),))
    assert result is Vended.DONE
    assert body.sold_stacks == 1 and body.bought_units == 10
    assert shop.v["bags.money_copper"] == 72
    assert shop.clicks == [(1280, 450, True), (160, 450, True), (160, 450, True)]
    assert shop.keys == ["esc"]


@pytest.mark.parametrize("change", [{"inventory.quality": 1}, {"inventory.quality": None},
                                    {"inventory.item_id": 999999},
                                    {"inventory.locked": True}, {"inventory.locked": None},
                                    {"inventory.count": None}])
def test_never_sells_unapproved_or_unread_items(change):
    shop = Shop()
    shop.v.update(change)
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.NO_JUNK
    assert shop.clicks == []


def test_selling_every_eligible_stack_is_enough_even_short_of_the_target():
    """Room to loot again is the point; the policy asks again when the bags are full."""
    shop = Shop()
    body = shop.body()
    assert body.run(expected_name="Merchant", min_free=6) is Vended.DONE
    assert body.sold_stacks == 1 and shop.v["bags.free"] == 1


def test_a_click_is_not_a_sale_and_never_retries_it_blindly():
    shop = Shop()
    shop.no_change = True
    body = shop.body()
    assert body.run(expected_name="Merchant", min_free=1) is Vended.NO_CHANGE
    assert body.sold_stacks == 0 and len(shop.clicks) == 1
    assert shop.now <= 4.1


def test_cash_change_without_bag_change_is_not_a_confirmed_sale():
    shop = Shop()
    original = shop.click

    def only_cash(*args, **kwargs):
        original(*args, **kwargs)
        shop.v["bags.free"] = 0
    shop.click = only_cash
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.NO_CHANGE


def test_exact_copper_required_even_when_old_silver_field_is_present():
    shop = Shop()
    shop.v["bags.money_silver"] = 1
    shop.v["bags.money_copper"] = None
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.BLIND
    assert not shop.clicks


def test_opens_only_the_painted_closed_bag_then_rechecks_slot_before_sale():
    shop = Shop()
    shop.v.update({"inventory.x": None, "inventory.open_x": 0.95, "inventory.open_y": 0.95})
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.DONE
    assert shop.clicks == [(1520, 855, False), (1280, 450, True)]


def test_changed_item_between_observation_and_click_is_never_sold():
    shop = Shop()
    count = 0
    original = shop.read

    def read():
        nonlocal count
        count += 1
        if count >= 4:
            shop.v["inventory.item_id"] = 999999
        return original()
    shop.read = read
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.NO_JUNK
    assert not shop.clicks


def test_wrong_merchant_never_receives_a_click_or_escape():
    shop = Shop()
    assert shop.body().run(expected_name="Other", min_free=1) is Vended.WRONG_VENDOR
    assert not shop.clicks and not shop.keys


@pytest.mark.parametrize("ready", [False, None])
def test_cursor_modifier_and_repair_mode_must_be_positively_ready(ready):
    shop = Shop()
    shop.v["merchant.ready"] = ready
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.REFUSED
    assert not shop.clicks


@pytest.mark.parametrize(("change", "result"), [
    ({"merchant.extended": True}, Vended.UNAVAILABLE),
    ({"merchant.extended": None}, Vended.UNAVAILABLE),
    ({"merchant.price": None}, Vended.BLIND),
    ({"merchant.quantity": None}, Vended.BLIND),
    ({"merchant.owned": None}, Vended.BLIND),
    ({"merchant.unlimited": False, "merchant.stock": 0}, Vended.UNAVAILABLE),
    ({"merchant.price": 101}, Vended.TOO_POOR),
])
def test_unsafe_or_unaffordable_offer_never_receives_purchase(change, result):
    shop = Shop()
    shop.v["bags.free"] = 1
    shop.v.update(change)
    assert shop.body().run(expected_name="Merchant", sell=False,
                           supplies=(Supply(2070, "Cheese", "food"),)) is result
    assert not shop.clicks


def test_purse_reserve_is_honored_before_the_first_purchase():
    shop = Shop()
    shop.v["bags.free"] = 1
    assert shop.body().run(expected_name="Merchant", sell=False, reserve_copper=90,
                           supplies=(Supply(2070, "Cheese", "food"),)) is Vended.TOO_POOR
    assert not shop.clicks


def test_already_stocked_and_absent_offer_have_separate_outcomes():
    shop = Shop()
    shop.v["bags.free"] = 1
    body = shop.body()
    shop.v["bags.food_count"] = 10
    assert body.run(expected_name="Merchant", sell=False,
                    supplies=(Supply(2070, "Cheese", "food"),)) is Vended.NOT_NEEDED
    assert body.run(expected_name="Merchant", sell=False,
                    supplies=(Supply(159, "Water", "drink"),)) is Vended.UNAVAILABLE
    assert not shop.clicks


def test_each_supply_search_rewinds_and_visits_observed_merchant_pages():
    class PagedShop(Shop):
        def __init__(self):
            super().__init__()
            self.page = 1
            self.offset = 0
            self.items = {1: [2070, *range(99001, 99010)], 2: [159]}
            self.owned = {2070: 0, 159: 0}
            self.v.update({"bags.free": 2, "merchant.total": 11,
                           "bags.drink_id": 159, "bags.drink_count": 0})

        def read(self):
            item = self.items[self.page][self.offset]
            self.v.update({"merchant.page": self.page,
                           "merchant.index": (self.page - 1) * 10 + self.offset + 1,
                           "merchant.item_id": item, "merchant.owned": self.owned.get(item, 0),
                           "merchant.next_x": 0.6 if self.page == 1 else None,
                           "merchant.next_y": 0.5 if self.page == 1 else None,
                           "merchant.prev_x": 0.5 if self.page == 2 else None,
                           "merchant.prev_y": 0.5 if self.page == 2 else None})
            return super().read()

        def sleep(self, seconds):
            super().sleep(seconds)
            self.offset = (self.offset + 1) % len(self.items[self.page])

        def click(self, x, y, right=False):
            self.clicks.append((x, y, right))
            if right:
                item = self.items[self.page][self.offset]
                self.owned[item] += 5
                self.v["bags.money_copper"] -= 20
                self.v["bags.food_count"] = self.owned[2070]
                self.v["bags.drink_count"] = self.owned[159]
            else:
                self.page += 1 if x == 960 else -1
                self.offset = 0
            return True

    shop = PagedShop()
    assert shop.body().run(expected_name="Merchant", sell=False,
                           supplies=(Supply(159, "Water", "drink", 5),
                                     Supply(2070, "Cheese", "food", 5))) is Vended.DONE
    assert shop.owned == {159: 5, 2070: 5}
    assert [click for click in shop.clicks if not click[2]] == [(960, 450, False), (800, 450, False)]


def test_cancellation_propagates_once_and_does_not_read_or_press_in_cleanup():
    shop = Shop()
    class Cancelled(Exception):
        pass
    reads = 0
    def cancelled():
        nonlocal reads
        reads += 1
        raise Cancelled("stopped")
    body = Vendor(shop, cancelled, shop.visit, clock=lambda: shop.now, sleep=shop.sleep)
    with pytest.raises(Cancelled, match="stopped"):
        body.run(expected_name="Merchant")
    assert reads == 1 and not shop.keys and not shop.clicks


def test_blind_modal_combat_and_refused_input_are_bounded():
    for key in ("ui.modal", "vitals.combat", "vitals.dead", "vitals.ghost"):
        shop = Shop()
        shop.v[key] = True
        assert shop.body().run(expected_name="Merchant") is Vended.INTERRUPTED
        assert not shop.clicks
    shop = Shop()
    shop.refuse = True
    assert shop.body().run(expected_name="Merchant", min_free=1) is Vended.REFUSED
    assert len(shop.clicks) == 1


@pytest.mark.skipif(not Path("data/knowledge/tbc-243.sqlite").exists(), reason="local DB required")
def test_generated_allowlist_is_poor_and_excludes_all_quest_references():
    """Poor weapons and armour are sold (nothing here equips gear); other kinds of
    equipment, containers, consumables and quest references never are."""
    raw = catalog()
    with sqlite3.connect("file:data/knowledge/tbc-243.sqlite?mode=ro", uri=True) as db:
        for item_id in raw["junk"]:
            kind, quality, equip, quest, price = db.execute(
                "select class,Quality,InventoryType,startquest,SellPrice "
                "from world_item_template where entry=?", (item_id,)).fetchone()
            assert kind not in (0, 1, 6, 11, 12, 13, 16)
            assert kind in (2, 4) or equip == 0
            assert quality == quest == 0 and price > 0
            assert raw["junk_prices"][str(item_id)] == price
        cols = [r[1] for r in db.execute("pragma table_info(world_quest_template)")
                if r[1].startswith(("ReqItemId", "ReqSourceId", "RewItemId", "RewChoiceItemId"))
                or r[1] == "SrcItemId"]
        required = {i for r in db.execute("select " + ",".join(cols) + " from world_quest_template")
                    for i in r if i}
        assert not (set(raw["junk"]) & required)


def test_supply_profiles_and_vendor_spawns_are_exact_generated_facts():
    assert {(s.role, s.item_id) for s in supplies_for(2, 1)} == {("food", 2070), ("drink", 159)}
    assert supplies_for(2, None) == ()
    found = merchants(0, items=frozenset({159}))
    assert found and all(m.map_id == 0 and 159 in m.items for m in found)


@pytest.mark.skipif(not Path("data/knowledge/tbc-243.sqlite").exists(), reason="local DB required")
def test_catalog_and_lua_supply_identities_regenerate_exactly(tmp_path):
    generated, lua = tmp_path / "catalog.json", tmp_path / "Supplies.lua"
    subprocess.run([sys.executable, "tools/gen_vendor_catalog.py", "--out", str(generated),
                    "--lua", str(lua)], check=True, capture_output=True)
    assert generated.read_bytes() == Path("content/tbc/vendor-catalog.json").read_bytes()
    assert lua.read_bytes() == Path("addons/JevRadio/Supplies.lua").read_bytes()


def test_placeholder_vendors_are_not_merchants():
    """Developer placeholders the server flags as vendors - "[DND] TAR Pedestal - Trainer,
    Druid" was walked to on a sale."""
    assert not any(m.name.startswith("[") for m in merchants(0))
    assert not any(v["name"].startswith("[") for v in catalog()["vendors"])
