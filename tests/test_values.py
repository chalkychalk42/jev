"""What the hive measured each quest and grind to be worth (`jev.learn.values`, V312)."""

from __future__ import annotations

import json

from jev.learn.values import Value, Values, rib_name

# The hive's format (JevHive `hive.values`), trimmed.
DOC = {"format": 1, "written": "2026-09-28T20:00", "death_s": 120,
       "quests": {"7": {"5-6": {"xp_h": 1900, "progress_h": 0.31, "score": 0.3, "deaths_h": 0.4,
                                "hours": 3.2, "characters": 7, "crowd": 2, "crowded": False,
                                "classes": {"mage": {"xp_h": 1500, "deaths_h": 1.1,
                                                     "hours": 2.5, "characters": 3}}}},
                  "8": {"5-6": {"xp_h": 400, "deaths_h": 0.0, "hours": 4.0, "characters": 9,
                                "crowd": 5, "crowded": True}}},
       "grinds": {"grind_durotar_5_7": {"5-6": {"xp_h": 1200, "deaths_h": 0.2, "hours": 3.0,
                                                "characters": 4}},
                  "grind_elwynn_5_7": {"5-6": {"xp_h": 1600, "deaths_h": 0.6, "hours": 1.0,
                                               "characters": 2}},
                  "gate_6_elwynn": {"5-6": {"xp_h": 800, "hours": 0.5, "characters": 1}}},
       "titles": {"7": ["Kobold Camp Cleanup", 3]}}


def _values(tmp_path, doc=DOC):
    (tmp_path / "values.json").write_text(json.dumps(doc))
    return Values.load(tmp_path / "values.json")


def test_a_quest_is_read_at_the_characters_band_and_its_class_on_enough_hours(tmp_path):
    values = _values(tmp_path)
    assert values.quest(7, 6) == Value(xp_h=1900.0, deaths_h=0.4, hours=3.2, chars=7)
    assert values.quest(7, 5, "Mage").xp_h == 1500.0, "the class's own on two hours"
    assert values.quest(7, 5, "priest").xp_h == 1900.0, "no class's own: the pool's"
    assert values.quest(7, 7) is None and values.quest(9, 5) is None
    assert values.quest(None, 5) is None and values.quest(7, None) is None
    assert values.quest(7, 6).text() == "measured 1,900 XP/h, 0.4 deaths/h, 7 chars"
    assert values.quest(8, 5) is None, "crowded: the hive's crowding, not the quest"


def test_a_grind_is_its_step_past_its_guide_whichever_guide_it_is_in(tmp_path):
    values = _values(tmp_path)
    assert rib_name("alli_human_1_12_grind_elwynn_5_7") == "grind_elwynn_5_7"
    assert rib_name("hive_orc_warrior_1_20_gate_6_elwynn") == "gate_6_elwynn"
    assert rib_name("grind_elwynn_5_7") == "grind_elwynn_5_7"
    assert values.grind("alli_human_1_12_grind_elwynn_5_7", 5).xp_h == 1600.0
    pooled = values.grinds(5)
    assert pooled.xp_h == (1200.0 * 3 + 1600.0 * 1 + 800.0 * 0.5) / 4.5 and pooled.of == 3
    assert pooled.deaths_h == (0.2 * 3 + 0.6 * 1) / 4
    assert pooled.text() == "measured 1,244 XP/h, 0.3 deaths/h, 3 grinds"
    assert values.grinds(12) is None


def test_no_file_a_broken_one_or_another_format_is_no_values(tmp_path):
    assert Values.load(tmp_path / "none.json") is None
    (tmp_path / "values.json").write_text("{not json")
    assert Values.load(tmp_path / "values.json") is None
    assert _values(tmp_path, {**DOC, "format": 2}) is None
    assert _values(tmp_path, {"format": 1, "quests": {}, "grinds": {}}) is None
    odd = _values(tmp_path, {"quests": {"7": {"5-6": {"deaths_h": 1}, "x": 3},
                                        "9": ["not", "a", "band"]}, "grinds": []})
    assert odd.quest(7, 5) is None and odd.quest(9, 5) is None and odd.grinds(5) is None
