"""Where units that attack on sight stand (V247): the generator's hostility and the lookup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from gen_hostile_spawns import hostile_sides, reputation_sides  # noqa: E402

from jev.world import hostiles  # noqa: E402

# (faction, flags, ours, friendly, hostile, enemy x4, friend x4), as dbc_FactionTemplate.
MONSTER = (14, 0, 8, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
BEAST_HATES_ALL = (29, 17, 8, 0, 1, 28, 0, 0, 0, 29, 0, 0, 0)
NEUTRAL_CREATURE = (7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
STORMWIND = (72, 0, 2, 2, 4, 0, 0, 0, 0, 0, 0, 0, 0)
# dbc_Faction for Stormwind: list 19; races (dwarf, night elf, gnome, draenei), (the Horde's),
# (human); classes; standings 3100, -42000, 4000; flags 17, 6 (at war), 17.
STORMWIND_FACTION = (19, 1100, 690, 1, 0, 0, 0, 0, 0, 3100, 2 ** 32 - 42000, 4000, 0,
                     17, 6, 17, 0)


@pytest.mark.parametrize(("template", "factions", "sides"), [
    (MONSTER, {}, 3),                          # a kobold: both sides
    (BEAST_HATES_ALL, {}, 3),                  # a Mangy Wolf's
    (NEUTRAL_CREATURE, {}, 0),                 # a boar, a deer
    (STORMWIND, {72: STORMWIND_FACTION}, 2),   # a guard: the Horde only, by standing
])
def test_hostility_is_the_servers_reaction_to_a_player(template, factions, sides):
    assert hostile_sides(template, factions) == sides


def test_a_faction_without_reputation_is_read_by_its_template():
    assert reputation_sides((2 ** 32 - 1, *([0] * 16))) is None


def test_near_keeps_the_side_the_level_and_the_radius(tmp_path, monkeypatch):
    rows = [[0.0, 10.0, 5.0, 5, 6, 3, 10.0],      # a wolf, 10 yards off
            [0.0, 15.0, 5.0, 1, 2, 3, 5.0],       # grey to a level 8
            [0.0, 20.0, 5.0, 60, 65, 2, 0.0],     # a guard, hostile to the Horde
            [0.0, 200.0, 5.0, 5, 6, 3, 10.0]]     # too far
    path = tmp_path / "hostile-spawns.json"
    path.write_text(json.dumps({"format": 1, "maps": {"0": rows}}))
    monkeypatch.setattr(hostiles, "HOSTILES_PATH", path)
    hostiles._index.cache_clear()
    try:
        assert hostiles.near(0, 0.0, 0.0, 70.0, side="alliance", level=8) == [
            (0.0, 10.0, 5.0, 0.0)], "a 10-yard wanderer carries no more than the ordinary"
        assert len(hostiles.near(0, 0.0, 0.0, 70.0, side="horde", level=8)) == 2
        assert len(hostiles.near(0, 0.0, 0.0, 70.0, side="alliance", level=None)) == 2
        assert hostiles.near(0, 0.0, 0.0, 70.0, side=None, level=8) == []
        assert hostiles.near(1, 0.0, 0.0, 70.0, side="alliance", level=8) == []
    finally:
        hostiles._index.cache_clear()


def test_the_generated_index_names_the_mages_killers():
    """The Mangy Wolves beside session 219's first body, and none in Goldshire's inn."""
    near_body = hostiles.near(0, -9626.0, 505.0, 30.0, side="alliance", level=8)
    assert near_body, "the wolves round the body"
    assert hostiles.near(0, -9458.6, 28.6, 20.0, side="alliance", level=8) == []


def test_a_far_wanderer_carries_its_reach(tmp_path, monkeypatch):
    """V255: a Young Forest Bear wanders 30 yards; one attacked the resting level 9 mage 20
    yards from a spawn point (session 224)."""
    import math

    from jev.run.body import REST_CLEAR_YARDS, reclaim_spot, rest_spot

    path = tmp_path / "hostile-spawns.json"
    path.write_text(json.dumps({"format": 1, "maps": {"0": [[0.0, 0.0, 5.0, 8, 9, 3, 30.0]]}}))
    monkeypatch.setattr(hostiles, "HOSTILES_PATH", path)
    hostiles._index.cache_clear()
    try:
        (bear,) = hostiles.near(0, 0.0, 0.0, 70.0, side="alliance", level=9)
    finally:
        hostiles._index.cache_clear()
    assert bear[3] == 20.0
    spot = rest_spot((5.0, 5.0), [bear])
    assert math.dist(spot[:2], (0.0, 0.0)) >= REST_CLEAR_YARDS + 20.0
    assert rest_spot((30.0, 0.0), [bear]) is not None, "25 yards off is inside its wander"
    body = (10.0, 0.0)
    got_up = reclaim_spot(body, (35.0, 0.0), [bear], 25.0)
    assert math.dist(got_up, (0.0, 0.0)) >= 34.0, "the far side of the body from it"


def test_a_unit_above_the_character_carries_a_yard_of_reach_a_level(tmp_path, monkeypatch):
    """V300: a unit notices a character a yard farther for each level it stands above it, 25
    at most (CMaNGOS `Unit::GetAttackDistance`). Raven Hill's level 23-25 spawns stand 29 yards
    from its Spirit Healer, outside the ordinary reach and inside a level 24's of a level 7;
    a level 7 human got up there and was attacked a median 2 s later (the hive, 28 Sep)."""
    import math

    from jev.run.body import REST_CLEAR_YARDS, rest_spot

    rows = [[29.0, 0.0, 30.0, 23, 24, 3, 5.0],     # a level 23-24 unit, 29 yards off
            [0.0, 40.0, 30.0, 70, 70, 3, 0.0],     # far above: 25 yards at most
            [0.0, -20.0, 30.0, 5, 6, 3, 30.0]]     # below the character: its wander only
    path = tmp_path / "hostile-spawns.json"
    path.write_text(json.dumps({"format": 1, "maps": {"0": rows}}))
    monkeypatch.setattr(hostiles, "HOSTILES_PATH", path)
    hostiles._index.cache_clear()
    try:
        extras = {s[:2]: s[3] for s in hostiles.near(0, 0.0, 0.0, 70.0, side="alliance",
                                                     level=7)}
        unlevelled = {s[:2]: s[3] for s in hostiles.near(0, 0.0, 0.0, 70.0, side="alliance",
                                                         level=None)}
    finally:
        hostiles._index.cache_clear()
    assert extras == {(29.0, 0.0): 17.0, (0.0, 40.0): 25.0, (0.0, -20.0): 20.0}
    assert unlevelled == {(29.0, 0.0): 0.0, (0.0, 40.0): 0.0, (0.0, -20.0): 20.0}
    unit = (29.0, 0.0, 30.0, extras[(29.0, 0.0)])
    spot = rest_spot((0.0, 0.0), [unit])
    assert spot is not None, "29 yards from a level 24 is inside its reach of a level 7"
    assert math.dist(spot[:2], unit[:2]) >= REST_CLEAR_YARDS + 17.0


def test_a_spawn_whose_creature_is_drawn_from_a_list_is_indexed():
    """V258: all 103 murloc and 97 Riverpaw spawn points in Elwynn draw their creature from a
    list as they spawn (`world_creature.id` 0), and an index without them saw none of the
    camps that killed the level 9 mage 9 times of 13 (sessions 222-228)."""
    import sqlite3

    from gen_hostile_spawns import generate

    db = sqlite3.connect(":memory:")
    db.executescript("""
        create table dbc_FactionTemplate (id, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13);
        create table dbc_Faction (id, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13,
                                  c14, c15, c16, c17);
        create table world_creature_template (Entry, Name, MinLevel, MaxLevel, Faction, UnitFlags);
        create table world_creature (guid, id, map, position_x, position_y, position_z,
                                     spawndist, MovementType);
        create table world_creature_spawn_entry (guid, entry);
        create table world_spawn_group_spawn (Id, Guid);
        create table world_spawn_group_entry (Id, Entry);
        create table world_spawn_group (Id, Type);
        insert into dbc_FactionTemplate values (18, 19, 1, 8, 0, 1, 0, 0, 0, 0, 19, 0, 0, 0);
        insert into dbc_FactionTemplate values (7, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
        insert into dbc_Faction values (19, 4294967295, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                                        0, 0, 0, 0);
        insert into world_creature_template values (285, 'Murloc', 9, 10, 18, 0);
        insert into world_creature_template values (732, 'Murloc Lurker', 10, 11, 18, 0);
        insert into world_creature_template values (721, 'Rabbit', 1, 1, 7, 0);
        insert into world_creature values (1, 0, 0, '-9500.5', '100.0', '40.0', 5.0, 1);
        insert into world_creature values (2, 721, 0, '-9510.0', '110.0', '40.0', 0.0, 0);
        insert into world_creature values (3, 0, 0, '-9520.0', '120.0', '40.0', 0.0, 0);
        insert into world_creature_spawn_entry values (1, 285);
        insert into world_creature_spawn_entry values (1, 732);
        insert into world_spawn_group_spawn values (7, 3);
        insert into world_spawn_group_entry values (7, 721);
        insert into world_spawn_group values (7, 0);
    """)
    rows = generate(db, maps=(0,))["maps"]["0"]
    assert rows == [[-9500.5, 100.0, 40.0, 9, 11, 3, 5.0]], "the drawn murloc; no rabbits"
