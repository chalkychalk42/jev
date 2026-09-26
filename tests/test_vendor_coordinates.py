"""The real supplier catalog must cross the database/JSON boundary in numeric yards."""

import json
import math
import sqlite3

import pytest

from jev.guide.coords import bounds_by_radio_id, map_to_world, world_to_map
from jev.world import vendor
from tools.gen_vendor_catalog import generate


def test_generator_normalizes_sqlite_text_coordinates_without_changing_values():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE world_item_template (
                entry INT, class INT, Quality INT, SellPrice INT, InventoryType INT, startquest INT,
                subclass INT, ContainerSlots INT, BagFamily INT);
            INSERT INTO world_item_template VALUES (7073,15,0,6,0,0,0,0,0);
            INSERT INTO world_item_template VALUES (5572,1,1,250,18,0,0,6,0);
            CREATE TABLE world_quest_template (SrcItemId INT);
            INSERT INTO world_quest_template VALUES (0);
            CREATE TABLE world_creature_template (
                Entry INT, Name TEXT, NpcFlags INT, VendorTemplateId INT, Faction INT,
                GossipMenuId INT);
            INSERT INTO world_creature_template VALUES (465,'Fixture Merchant',128,0,12,0);
            INSERT INTO world_creature_template VALUES (14845,'Stamp Thunderhorn',128,0,12,0);
            INSERT INTO world_creature_template VALUES (295,'Fixture Innkeeper',65536,0,12,0);
            INSERT INTO world_creature_template VALUES (296,'Hostile Innkeeper',65536,0,29,0);
            INSERT INTO world_creature_template VALUES (523,'Fixture Flier',8195,0,12,4106);
            CREATE TABLE world_gossip_menu_option (menu_id INT, id INT, option_id INT,
                option_text TEXT);
            INSERT INTO world_gossip_menu_option VALUES (4106, 0, 4, 'I need a ride.');
            CREATE TABLE dbc_FactionTemplate (id INT, c3 INT, c4 INT, c5 INT);
            INSERT INTO dbc_FactionTemplate VALUES (12, 2, 0, 4);
            INSERT INTO dbc_FactionTemplate VALUES (29, 4, 0, 2);
            CREATE TABLE world_creature (
                guid INT, id INT, map INT, position_x TEXT, position_y TEXT, position_z TEXT);
            INSERT INTO world_creature VALUES (1,465,0,'-1.25','2.5','3e-2');
            INSERT INTO world_creature VALUES (12420,14845,0,'5','5','5');
            INSERT INTO world_creature VALUES (2,295,0,'10','20','30');
            INSERT INTO world_creature VALUES (3,296,0,'40','50','60');
            INSERT INTO world_creature VALUES (4,523,0,'70','80','90');
            CREATE TABLE world_game_event_creature (guid INT, event INT);
            INSERT INTO world_game_event_creature VALUES (12420, 81);
            CREATE TABLE world_npc_vendor (entry INT,item INT,ExtendedCost INT,condition_id INT);
            INSERT INTO world_npc_vendor VALUES (465,159,0,0);
            CREATE TABLE world_npc_vendor_template (
                entry INT,item INT,ExtendedCost INT,condition_id INT);
        """)
        result = generate(db, {})
    # The Darkmoon Faire's merchant stands there only while the event runs.
    assert [v["name"] for v in result["vendors"]] == ["Fixture Merchant"]
    merchant = result["vendors"][0]
    assert merchant["world"] == [-1.25, 2.5, 0.03]
    assert all(type(value) is float for value in merchant["world"])
    assert json.loads(json.dumps(result))["vendors"][0]["world"] == [-1.25, 2.5, 0.03]
    assert merchant["entry"] == 465 and merchant["items"] == [159]
    assert result["bags"] == {"5572": 6}
    assert result["innkeepers"] == [{"entry": 295, "name": "Fixture Innkeeper", "map_id": 0,
                                     "sides": ["alliance"], "world": [10.0, 20.0, 30.0]},
                                    {"entry": 296, "name": "Hostile Innkeeper", "map_id": 0,
                                     "sides": ["horde"], "world": [40.0, 50.0, 60.0]}]
    inn, = [vendor.Innkeeper(**{k: v for k, v in i.items() if k != "sides"})
            for i in result["innkeepers"] if "alliance" in i["sides"]]
    assert inn.name == "Fixture Innkeeper"
    assert result["flightmasters"] == [{"entry": 523, "name": "Fixture Flier", "map_id": 0,
                                        "sides": ["alliance"], "world": [70.0, 80.0, 90.0],
                                        "gossip": "I need a ride."}]


def test_loader_accepts_older_string_coordinates_as_numeric_yards(monkeypatch):
    monkeypatch.setattr(vendor, "catalog", lambda: {"vendors": [
        {"entry": 465, "name": "Fixture Merchant", "map_id": 0,
         "world": ["-1.25", "2.5", "3e-2"], "items": [159]}]})
    merchant, = vendor.merchants(0, items=frozenset({159}))
    assert merchant.world == (-1.25, 2.5, 0.03)
    assert all(type(value) is float for value in merchant.world)


def test_real_catalog_world_coordinates_are_finite_json_numbers():
    raw = json.loads(vendor.CATALOG.read_text())
    assert raw["vendors"]
    for merchant in raw["vendors"]:
        assert len(merchant["world"]) == 3
        assert all(type(value) in (int, float) and math.isfinite(value)
                   for value in merchant["world"]), merchant["entry"]


def test_real_suppliers_pass_zone_predicate_and_nearest_world_yard_ranking():
    bounds = bounds_by_radio_id("data/zones-tbc-243.json")
    # The running route begins in Elwynn; its same-zone predicate and distance rank
    # are exercised against generated data instead of hand-created numeric merchants.
    elwynn = next(b for b in bounds.values() if b.area_id == 12)
    candidates = []
    for merchant in vendor.merchants(elwynn.map_id, items=frozenset({159})):
        point = world_to_map(*merchant.world[:2], elwynn)
        if point is not None and all(0 <= component <= 1 for component in point):
            candidates.append(merchant)
    assert candidates, "the exact starting water needs a reachable-zone supplier"
    here = map_to_world(0.48, 0.43, elwynn)
    nearest = min(candidates, key=lambda merchant: math.dist(merchant.world[:2], here))
    assert math.isfinite(math.dist(nearest.world[:2], here))
    point = world_to_map(*nearest.world[:2], elwynn)
    assert map_to_world(*point, elwynn) == pytest.approx(nearest.world[:2])


def test_a_merchant_that_mends_gear_is_marked_so():
    """The server's repair flag, for choosing a repairer as merchants are chosen (V201)."""
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE world_item_template (
                entry INT, class INT, Quality INT, SellPrice INT, InventoryType INT, startquest INT,
                subclass INT, ContainerSlots INT, BagFamily INT);
            CREATE TABLE world_quest_template (SrcItemId INT);
            CREATE TABLE world_creature_template (
                Entry INT, Name TEXT, NpcFlags INT, VendorTemplateId INT, Faction INT,
                GossipMenuId INT);
            INSERT INTO world_creature_template VALUES (78,'Fixture Smith',4224,0,12,0);
            INSERT INTO world_creature_template VALUES (152,'Fixture Grocer',128,0,12,0);
            CREATE TABLE world_gossip_menu_option (menu_id INT, id INT, option_id INT,
                option_text TEXT);
            CREATE TABLE dbc_FactionTemplate (id INT, c3 INT, c4 INT, c5 INT);
            CREATE TABLE world_creature (
                guid INT, id INT, map INT, position_x TEXT, position_y TEXT, position_z TEXT);
            INSERT INTO world_creature VALUES (1,78,0,'1','2','3');
            INSERT INTO world_creature VALUES (2,152,0,'4','5','6');
            CREATE TABLE world_game_event_creature (guid INT, event INT);
            CREATE TABLE world_npc_vendor (entry INT,item INT,ExtendedCost INT,condition_id INT);
            CREATE TABLE world_npc_vendor_template (
                entry INT,item INT,ExtendedCost INT,condition_id INT);
        """)
        result = generate(db, {})
    smith, grocer = result["vendors"]
    assert smith["repairs"] is True and "repairs" not in grocer


def test_loader_reads_the_repair_flag(monkeypatch):
    monkeypatch.setattr(vendor, "catalog", lambda: {"vendors": [
        {"entry": 78, "name": "Fixture Smith", "map_id": 0, "world": [1, 2, 3], "items": [],
         "repairs": True},
        {"entry": 152, "name": "Fixture Grocer", "map_id": 0, "world": [4, 5, 6], "items": []}]})
    smith, grocer = vendor.merchants(0)
    assert smith.repairs is True and grocer.repairs is False


def test_goods_only_quests_off_the_guides_ask_for_are_sold():
    """V241: a trade good or gem some quest somewhere asks for was never sold, and the level
    8 mage's bags filled with a Tigerseye, Malachite, Linen Cloth and Small Spider Legs,
    "no junk" at each merchant (session 212)."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE world_item_template (entry INT, class INT, subclass INT, SellPrice INT,
            InventoryType INT, Quality INT, startquest INT, ContainerSlots INT, BagFamily INT,
            BuyPrice INT);
        INSERT INTO world_item_template VALUES (2589, 7, 0, 13, 0, 1, 0, 0, 0, 0);
        INSERT INTO world_item_template VALUES (818, 3, 7, 100, 0, 2, 0, 0, 0, 0);
        INSERT INTO world_item_template VALUES (769, 7, 0, 3, 0, 1, 0, 0, 0, 0);
        CREATE TABLE world_quest_template (entry INT, SrcItemId INT, ReqItemId1 INT);
        INSERT INTO world_quest_template VALUES (7, 0, 2589);
        INSERT INTO world_quest_template VALUES (8, 0, 818);
        INSERT INTO world_quest_template VALUES (86, 0, 769);
        CREATE TABLE world_creature_template (Entry INT, Name TEXT, NpcFlags INT, Faction INT,
            FactionAlliance INT, GossipMenuId INT);
        CREATE TABLE world_creature (guid INT, id INT, map INT, position_x TEXT,
            position_y TEXT, position_z TEXT);
        CREATE TABLE world_game_event_creature (guid INT, event INT);
        CREATE TABLE world_npc_vendor (entry INT, item INT, ExtendedCost INT, condition_id INT);
        CREATE TABLE world_npc_vendor_template (entry INT, item INT, ExtendedCost INT,
            condition_id INT);
        CREATE TABLE world_gossip_menu_option (menu_id INT, id INT, option_id INT,
            option_text TEXT);
        CREATE TABLE dbc_FactionTemplate (id INT, c3 INT, c4 INT, c5 INT);
    """)
    everyone = generate(db, {})["surplus_prices"]
    guided = generate(db, {}, guide_quests={86})["surplus_prices"]
    assert everyone == {}, "every quest's goods kept, as before"
    assert guided == {"2589": 13, "818": 100}, "the guides' own quest's meat kept, the rest sold"
