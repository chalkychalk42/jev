"""Where the server sends a ghost (V301): the graveyards of the world DB."""

from __future__ import annotations

import sqlite3

import pytest

from jev.world import graveyards


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A world DB of three graveyards on one map: one serving zone 17 for both sides, one
    serving zone 14 for the Horde, nearer the origin, and one serving zone 17 for the
    Alliance only, nearest of all."""
    path = tmp_path / "world.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        create table world_world_safe_locs (id, map, x, y, z, o, name);
        create table world_game_graveyard_zone (id, ghost_loc, link_kind, faction);
        insert into world_world_safe_locs values (249, 1, -1081.4, -3478.7, 63.6, 0, 'Ratchet');
        insert into world_world_safe_locs values (709, 1, -634.6, -4296.0, 40.5, 0, 'Valley');
        insert into world_world_safe_locs values (900, 1, -1000.0, -4000.0, 10.0, 0, 'Keep');
        insert into world_game_graveyard_zone values (249, 17, 0, 0);
        insert into world_game_graveyard_zone values (709, 14, 0, 67);
        insert into world_game_graveyard_zone values (900, 17, 0, 469);
    """)
    db.commit()
    db.close()
    monkeypatch.setattr(graveyards, "WORLD_DB", path)
    graveyards._graveyards.cache_clear()
    yield
    graveyards._graveyards.cache_clear()


def test_the_graveyard_is_the_zones_own_for_the_side(world):
    """The two orcs' ghosts appeared at Ratchet's, the Barrens' own, though the Valley of
    Trials' in Durotar lay nearer one of their bodies (the hive, 28 Sep)."""
    body = (-993.1, -4018.9)
    assert graveyards.nearest(1, *body, side="horde", zone=17) == (-1081.4, -3478.7, 63.6)
    assert graveyards.nearest(1, *body, side="horde") == (-634.6, -4296.0, 40.5), \
        "no zone read: the nearest of the side's on the map"
    assert graveyards.nearest(1, *body, side="alliance", zone=17) == (-1000.0, -4000.0, 10.0)
    assert graveyards.nearest(1, *body, side="horde", zone=99) == (-634.6, -4296.0, 40.5), \
        "a zone none serves: the nearest on the map"
    assert graveyards.nearest(0, *body, side="horde", zone=17) is None, "another map"
    assert graveyards.nearest(1, *body, side=None, zone=17) is None


def test_no_world_db_is_no_graveyard(tmp_path, monkeypatch):
    monkeypatch.setattr(graveyards, "WORLD_DB", tmp_path / "missing.sqlite")
    graveyards._graveyards.cache_clear()
    try:
        assert graveyards.nearest(1, 0.0, 0.0, side="horde") is None
    finally:
        graveyards._graveyards.cache_clear()


def test_the_world_db_names_the_hives_graveyards():
    """Where the hive's ghosts appeared on 28 Sep: Ratchet's for the orcs drowned off it,
    Raven Hill's in Duskwood, Brill's in Tirisfal."""
    assert graveyards.nearest(1, -1052.3, -3936.4, side="horde", zone=17)[:2] == \
        pytest.approx((-1081.4, -3478.7), abs=1.0)
    assert graveyards.nearest(0, -10604.1, 293.9, side="alliance", zone=10)[:2] == \
        pytest.approx((-10606.9, 293.9), abs=1.0)
    assert graveyards.nearest(0, 2348.7, 492.0, side="horde", zone=85)[:2] == \
        pytest.approx((2348.7, 492.0), abs=1.0)
