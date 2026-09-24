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
                Entry INT, Name TEXT, NpcFlags INT, VendorTemplateId INT);
            INSERT INTO world_creature_template VALUES (465,'Fixture Merchant',128,0);
            INSERT INTO world_creature_template VALUES (14845,'Stamp Thunderhorn',128,0);
            CREATE TABLE world_creature (
                guid INT, id INT, map INT, position_x TEXT, position_y TEXT, position_z TEXT);
            INSERT INTO world_creature VALUES (1,465,0,'-1.25','2.5','3e-2');
            INSERT INTO world_creature VALUES (12420,14845,0,'5','5','5');
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
