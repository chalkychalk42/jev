"""Portable exact-schema world retrieval, with no game, network or user's database."""

import hashlib
import json
import sqlite3
import struct

import pytest

from jev.play.world_knowledge import WorldKnowledge, readonly_uri


@pytest.fixture
def world_db(tmp_path):
    path = tmp_path / "snapshot #1?.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE world_quest_template (entry INTEGER PRIMARY KEY,Title TEXT,MinLevel INTEGER,
            ReqItemId1 INTEGER,ReqItemCount1 INTEGER);
        CREATE TABLE world_creature_template (Entry INTEGER PRIMARY KEY,Name TEXT,NpcFlags INTEGER,
            LootId INTEGER,VendorTemplateId INTEGER,TrainerTemplateId INTEGER);
        CREATE TABLE world_item_template (entry INTEGER PRIMARY KEY,name TEXT,BuyPrice INTEGER);
        CREATE TABLE world_spell_template (Id INTEGER PRIMARY KEY,SpellName TEXT,Rank1 TEXT,
            CastingTimeIndex INTEGER,RangeIndex INTEGER,DurationIndex INTEGER,ManaCost INTEGER);
        CREATE TABLE world_gameobject_template (entry INTEGER PRIMARY KEY,name TEXT,type INTEGER,data1 INTEGER);
        CREATE TABLE world_creature (guid INTEGER PRIMARY KEY,id INTEGER,map INTEGER,
            position_x REAL,position_y REAL,position_z REAL);
        CREATE TABLE world_gameobject (guid INTEGER PRIMARY KEY,id INTEGER,map INTEGER,
            position_x REAL,position_y REAL,position_z REAL);
        CREATE TABLE world_creature_spawn_entry (guid INTEGER,entry INTEGER);
        CREATE TABLE world_gameobject_spawn_entry (guid INTEGER,entry INTEGER);
        CREATE TABLE world_spawn_group (Id INTEGER,Type INTEGER);
        CREATE TABLE world_spawn_group_entry (Id INTEGER,Entry INTEGER);
        CREATE TABLE world_spawn_group_spawn (Id INTEGER,Guid INTEGER);
        CREATE TABLE world_creature_questrelation (id INTEGER,quest INTEGER);
        CREATE TABLE world_creature_involvedrelation (id INTEGER,quest INTEGER);
        CREATE TABLE world_gameobject_questrelation (id INTEGER,quest INTEGER);
        CREATE TABLE world_gameobject_involvedrelation (id INTEGER,quest INTEGER);
        CREATE TABLE world_creature_loot_template (entry INTEGER,item INTEGER,ChanceOrQuestChance REAL,
            mincountOrRef INTEGER,maxcount INTEGER);
        CREATE TABLE world_reference_loot_template (entry INTEGER,item INTEGER,ChanceOrQuestChance REAL,
            mincountOrRef INTEGER,maxcount INTEGER);
        CREATE TABLE world_gameobject_loot_template (entry INTEGER,item INTEGER,ChanceOrQuestChance REAL,
            mincountOrRef INTEGER,maxcount INTEGER);
        CREATE TABLE world_npc_vendor (entry INTEGER,item INTEGER,maxcount INTEGER,incrtime INTEGER);
        CREATE TABLE world_npc_vendor_template (entry INTEGER,item INTEGER,maxcount INTEGER,incrtime INTEGER);
        CREATE TABLE world_npc_trainer (entry INTEGER,spell INTEGER,spellcost INTEGER,reqlevel INTEGER);
        CREATE TABLE world_npc_trainer_template (entry INTEGER,spell INTEGER,spellcost INTEGER,reqlevel INTEGER);
        CREATE TABLE dbc_SpellCastTimes (id INTEGER,c1 INTEGER,c2 INTEGER,c3 INTEGER);
        CREATE TABLE dbc_SpellDuration (id INTEGER,c1 INTEGER,c2 INTEGER,c3 INTEGER);
        CREATE TABLE dbc_SpellRange (id INTEGER,c1 INTEGER,c2 INTEGER);
        CREATE TABLE entities (kind TEXT,id INTEGER,name TEXT,rank TEXT,details TEXT);
        CREATE TABLE private_characters (name TEXT, secret TEXT);
        INSERT INTO private_characters VALUES ('SecretName','must never appear');
        INSERT INTO world_quest_template VALUES (33,'Wolf supplies',1,20,8);
        INSERT INTO world_creature_template VALUES
            (10,'Wolf',144,100,200,300),(11,'Wolf scout',0,0,0,0),(12,'Wolf cub',0,0,0,0);
        INSERT INTO world_item_template VALUES (20,'Meat',10),(21,'Reference prize',100),(22,'100%_literal',1);
        INSERT INTO world_spell_template VALUES (635,'Holy Light','Rank 1',20,5,6,35);
        INSERT INTO world_gameobject_template VALUES (30,'Crate',3,101);
        INSERT INTO world_creature VALUES (1,10,0,1,2,3),(2,-1,0,4,5,6),(3,-1,0,7,8,9);
        INSERT INTO world_creature_spawn_entry VALUES (2,10);
        INSERT INTO world_spawn_group VALUES (1,0),(2,1);
        INSERT INTO world_spawn_group_entry VALUES (1,10),(2,30);
        INSERT INTO world_spawn_group_spawn VALUES (1,3),(2,13);
        INSERT INTO world_gameobject VALUES (11,30,0,11,12,13),(12,-1,0,14,15,16),(13,-1,0,17,18,19);
        INSERT INTO world_gameobject_spawn_entry VALUES (12,30);
        INSERT INTO world_creature_questrelation VALUES (10,33);
        INSERT INTO world_creature_involvedrelation VALUES (11,33);
        INSERT INTO world_gameobject_questrelation VALUES (30,33);
        INSERT INTO world_creature_loot_template VALUES (100,20,25,1,1),(100,0,50,-500,1);
        INSERT INTO world_reference_loot_template VALUES (500,0,50,-501,1),(501,21,100,1,1),(501,0,50,-500,1);
        INSERT INTO world_gameobject_loot_template VALUES (101,20,100,1,1);
        INSERT INTO world_npc_vendor VALUES (11,20,1,60);
        INSERT INTO world_npc_vendor_template VALUES (200,20,0,0);
        INSERT INTO world_npc_trainer VALUES (11,635,10,1);
        INSERT INTO world_npc_trainer_template VALUES (300,635,20,1);
        INSERT INTO dbc_SpellCastTimes VALUES (20,2500,0,2500);
        INSERT INTO dbc_SpellDuration VALUES (6,4294967295,0,4294967295);
        INSERT INTO entities VALUES ('spell',635,'Holy Light','Rank 1','{"description":"Heals a friendly target for $s1."}');
    """)
    metadata = {"schema_version": 1, "ruleset": "tbc-2.4.3-8606", "complete": True,
                "pack_id": "synthetic-fixture", "sources": {"mangos-tbc": "fixture-revision"},
                "scope": "portable test rows, no live character data"}
    db.executemany("INSERT INTO metadata VALUES (?,?)", [(k, json.dumps(v)) for k, v in metadata.items()])
    bits = struct.unpack("<I", struct.pack("<f", 40.0))[0]
    db.execute("INSERT INTO dbc_SpellRange VALUES (5,0,?)", (bits,))
    db.commit()
    db.close()
    return path


def record(world, query, **kwargs):
    return world.search(query, **kwargs)["records"][0]


def test_snapshot_identity_provenance_and_read_only_connection(world_db):
    original = world_db.read_bytes()
    knowledge = WorldKnowledge(world_db)
    assert knowledge.sha256 == hashlib.sha256(original).hexdigest()
    assert knowledge.source["metadata"]["ruleset"] == "tbc-2.4.3-8606"
    assert knowledge.source["metadata"]["sources"]["mangos-tbc"] == "fixture-revision"
    assert knowledge.source["live_observation"] is False
    with knowledge._connect() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("UPDATE world_item_template SET BuyPrice=0")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    assert world_db.read_bytes() == original
    assert not world_db.with_name(world_db.name + "-journal").exists()
    assert not world_db.with_name(world_db.name + "-wal").exists()


def test_snapshot_change_invalidates_existing_context(world_db):
    knowledge = WorldKnowledge(world_db)
    with sqlite3.connect(world_db) as db:
        db.execute("UPDATE world_item_template SET BuyPrice=11 WHERE entry=20")
    with pytest.raises(ValueError, match="changed"):
        knowledge.search("item 20")
    assert WorldKnowledge(world_db).sha256 != knowledge.sha256


@pytest.mark.parametrize(("key", "value"), [("complete", False), ("schema_version", 2),
                                             ("ruleset", "retail")])
def test_unknown_incomplete_or_wrong_ruleset_snapshot_is_not_used(world_db, key, value):
    with sqlite3.connect(world_db) as db:
        db.execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(value), key))
    with pytest.raises(ValueError, match="supported TBC"):
        WorldKnowledge(world_db)


def test_missing_metadata_and_missing_database_are_explicit(tmp_path, world_db):
    absent = WorldKnowledge(tmp_path / "absent.sqlite")
    assert not absent.available and absent.search("quest 33")["records"] == []
    assert not (tmp_path / "absent.sqlite").exists()
    with sqlite3.connect(world_db) as db:
        db.execute("DROP TABLE metadata")
    with pytest.raises(ValueError, match="provenance"):
        WorldKnowledge(world_db)


def test_names_and_ids_use_the_actual_template_columns(world_db):
    knowledge = WorldKnowledge(world_db)
    assert record(knowledge, "quest 33")["data"]["Title"] == "Wolf supplies"
    assert record(knowledge, "npc 10")["data"]["Name"] == "Wolf"
    assert record(knowledge, "item 20")["data"]["name"] == "Meat"
    assert record(knowledge, "ability 635")["data"]["SpellName"] == "Holy Light"
    assert record(knowledge, "gameobject Crate")["data"]["entry"] == 30


def test_quest_relations_use_id_column_and_include_objects(world_db):
    related = record(WorldKnowledge(world_db), "quest 33")["related"]
    assert related["npc_givers"] == [{"id": 10}]
    assert related["npc_turn_in"] == [{"id": 11}]
    assert related["object_givers"] == [{"id": 30}]
    assert related["object_turn_in"] == []


@pytest.mark.parametrize(("query", "expected"), [("npc 10", {1, 2, 3}), ("object 30", {11, 12, 13})])
def test_direct_random_and_spawn_group_positions_remain_possible_spawns(world_db, query, expected):
    related = record(WorldKnowledge(world_db), query)["related"]
    assert {row["guid"] for row in related["possible_spawns"]} == expected
    assert {row["source"] for row in related["possible_spawns"]} == {"direct", "random_entry", "spawn_group"}
    assert "possible_spawns" not in related["truncated"]


def test_limits_truthfully_report_entity_and_relation_truncation(world_db):
    knowledge = WorldKnowledge(world_db)
    result = knowledge.search("npc Wolf", limit=1)
    assert len(result["records"]) == 1 and result["truncated"]
    related = result["records"][0]["related"]
    assert len(related["possible_spawns"]) == 1
    assert "possible_spawns" in related["truncated"]
    assert len(related["loot"]) == 1 and "loot" in related["truncated"]
    # One quest then three NPC names match; the overall budget cuts within a later category.
    result = knowledge.search("Wolf", limit=2)
    assert len(result["records"]) == 2 and result["truncated"]


def test_nested_reference_loot_is_bounded_and_cycles_do_not_loop(world_db):
    knowledge = WorldKnowledge(world_db)
    related = record(knowledge, "npc 10")["related"]
    assert {row["entry"] for row in related["reference_loot"]} == {500, 501}
    assert all(len(row["rows"]) <= 6 for row in related["reference_loot"])
    related = record(knowledge, "loot 21")["related"]
    assert related["direct_loot_sources"] == []
    assert related["reference_loot_sources"][0]["Entry"] == 10
    assert related["reference_loot_sources"][0]["reference_entry"] == 500
    assert "chance" not in related  # reference conditions are not flattened into invented probabilities


def test_item_sources_include_creatures_chests_and_template_vendors(world_db):
    related = record(WorldKnowledge(world_db), "item Meat")["related"]
    assert related["direct_loot_sources"][0]["Entry"] == 10
    assert related["direct_object_loot_sources"][0]["entry"] == 30
    assert related["direct_vendors"][0]["entry"] == 11
    assert related["template_vendors"][0]["Entry"] == 10
    assert related["template_vendors"][0]["vendor_template"] == 200


def test_vendor_and_trainer_queries_filter_npc_flags_and_keep_template_rows(world_db):
    knowledge = WorldKnowledge(world_db)
    assert len(knowledge.search("vendor Wolf")["records"]) == 1
    related = record(knowledge, "trainer Wolf")["related"]
    assert related["trainer_template"][0]["spell"] == 635
    assert related["vendor_template"][0]["item"] == 20


def test_spell_dbc_cast_range_signed_duration_and_description(world_db):
    related = record(WorldKnowledge(world_db), "spell Holy Light")["related"]
    assert related["base_cast_ms"] == 2500
    assert related["min_range_yards"] == 0 and related["max_range_yards"] == 40
    assert related["base_duration_ms"] == -1
    assert related["indexed_description"]["details"]["description"] == "Heals a friendly target for $s1."
    assert related["direct_trainers"][0]["entry"] == 11
    assert related["template_trainers"][0]["Entry"] == 10


def test_lookup_text_cannot_be_sql_or_select_private_tables(world_db):
    knowledge = WorldKnowledge(world_db)
    assert knowledge.search("npc %' OR 1=1 --")["records"] == []
    assert knowledge.search("private_characters SecretName")["records"] == []
    assert "must never appear" not in json.dumps(knowledge.search("SecretName"))
    assert record(knowledge, "item 100%_literal")["data"]["entry"] == 22
    assert knowledge.search("item %")["records"][0]["data"]["entry"] == 22


@pytest.mark.parametrize(("query", "limit"), [("", 6), ("x" * 241, 6), ("wolf", 0),
                                              ("wolf", 13), ("wolf", True), ("wolf", 1.5)])
def test_lookup_budget_and_query_are_validated_before_sql(world_db, query, limit):
    with pytest.raises(ValueError):
        WorldKnowledge(world_db).search(query, limit=limit)


def test_uri_quotes_filesystem_metacharacters_and_source_is_detached(world_db):
    uri = readonly_uri(world_db)
    assert "%23" in uri and "%3F" in uri
    assert uri.endswith("?mode=ro&immutable=1")
    knowledge = WorldKnowledge(world_db)
    source = knowledge.source
    source["metadata"]["sources"]["mangos-tbc"] = "changed by caller"
    assert knowledge.source["metadata"]["sources"]["mangos-tbc"] == "fixture-revision"
