"""Hunt spawn points live beside the guide, keyed by step and by step#creature."""

from __future__ import annotations

from jev.guide import spawns


def test_a_table_round_trips_beside_its_guide(tmp_path):
    guide = tmp_path / "ally_human_1_12.json"
    path = spawns.save(guide, {"s_do": [[1, 2, 3]], "s_do#299": [[4, 5, 6]]})
    assert path == tmp_path / "ally_human_1_12.spawns.json"
    table = spawns.load(guide)
    assert spawns.lookup(table, "s_do") == ((1.0, 2.0, 3.0),)
    assert spawns.lookup(table, "s_do", 299) == ((4.0, 5.0, 6.0),)
    assert spawns.lookup(table, "s_do", 69) == ((1.0, 2.0, 3.0),), "falls back to the step's"
    assert spawns.lookup(table, "other") == ()


def test_a_missing_or_unreadable_table_means_rings(tmp_path):
    guide = tmp_path / "g.json"
    assert spawns.load(guide) == {}
    spawns.path_for(guide).write_text("{not json")
    assert spawns.load(guide) == {}
