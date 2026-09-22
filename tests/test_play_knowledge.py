import json

import pytest

from jev.guide.graph import Graph
from jev.play.knowledge import CONTENT, LocalKnowledge


def test_current_loot_objective_uses_generated_mob_source_not_giver():
    knowledge = LocalKnowledge()
    context = knowledge.context({
        "context": {"step_id": "alli_human_1_12_33_wolves_across_the_border_do"},
        "values": {"char.race_id": 1, "char.class_id": 2},
        "state": {"pos": {"world": [-8970.8, -146.9, 81.8]}},
    })
    target = context["objective_sources"][0]
    assert target["kind"] == "loot" and target["required_id"] == 750
    assert target["required_count"] == 8
    assert target["target_name"] == "Young Wolf"
    assert context["next_nodes"][0]["target_name"] == "Eagan Peltskinner"
    assert context["starting_profile"]["profile_id"] == "1:2"
    assert context["supplies"]["drink"]["item_id"] == 159
    assert len(context["merchants"]) <= 4
    distances = [v["distance_yards_from_observed_position"] for v in context["merchants"]]
    assert distances == sorted(distances)
    assert any("do not prove" in text for text in context["unknowns"])


def test_retrieval_finds_local_abilities_items_and_entities_with_provenance():
    knowledge = LocalKnowledge()
    spell = knowledge.search("Holy Light")
    assert any(r["kind"] == "ability" and r["data"]["spell"] == 635 for r in spell["records"])
    source = knowledge.search("Young Wolf")
    assert any(r["kind"] == "objective_source" and r["data"]["required_id"] == 750
               for r in source["records"])
    assert knowledge.search("item 159")["records"]
    assert len(source["fingerprint"]) == 64
    assert all(len(s["sha256"]) == 64 for s in source["sources"])
    assert not knowledge.search("a spell that does not exist here")["records"]


def test_unknowns_do_not_get_a_different_races_starting_bar():
    result = LocalKnowledge().context({"values": {"char.race_id": 999, "char.class_id": 2}})
    assert result["starting_profile"] is None
    assert result["supplies"] is None
    assert any("No exact observed race/class" in text for text in result["unknowns"])


def test_id_queries_match_identity_fields_never_coordinate_fragments():
    knowledge = LocalKnowledge()
    quests = knowledge.search("quest 33")
    assert quests["records"] and quests["omitted"] == 0
    assert all(row["data"]["quest_id"] == 33 for row in quests["records"])
    spells = knowledge.search("spell 635")
    assert spells["records"][0]["kind"] == "ability"
    assert spells["records"][0]["data"]["spell"] == 635
    assert not any(row["kind"] == "merchant" for row in spells["records"])
    items = knowledge.search("item 159")
    assert items["records"][0]["kind"] == "ability"
    assert items["records"][0]["data"]["item"] == 159


def test_missing_files_remain_unknown_and_corrupt_files_fail(tmp_path):
    knowledge = LocalKnowledge(content_dir=tmp_path)
    assert len([s for s in knowledge.sources if not s["available"]]) == 3
    assert not knowledge.search("Young Wolf")["records"]
    (tmp_path / "combat-profiles.json").write_text("{")
    with pytest.raises(ValueError):
        LocalKnowledge(content_dir=tmp_path)


def test_knowledge_snapshot_is_pinned_and_returns_detached_values(tmp_path):
    graph = Graph.load(CONTENT / "ally_human_1_12.json")
    path = tmp_path / "combat-profiles.json"
    path.write_text('{}')
    first = LocalKnowledge(graph, content_dir=tmp_path)
    result = first.search("Young Wolf")
    result["records"][0]["data"]["target_name"] = "mutated"
    assert all(r["data"].get("target_name") != "mutated"
               for r in first.search("Young Wolf")["records"])
    path.write_text(json.dumps({"1:2": {"name": "paladin", "rows": []}}))
    second = LocalKnowledge(graph, content_dir=tmp_path)
    assert second.fingerprint != first.fingerprint
    assert first.context({"values": {"char.race_id": 1, "char.class_id": 2}})["starting_profile"] is None


@pytest.mark.parametrize("query,limit", [("", 6), ("a" * 241, 6), ("wolf", 0), ("wolf", 13)])
def test_lookup_is_bounded(query, limit):
    with pytest.raises(ValueError):
        LocalKnowledge().search(query, limit=limit)
