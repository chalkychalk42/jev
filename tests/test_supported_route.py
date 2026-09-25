from pathlib import Path

from jev.guide.generate import generate
from jev.guide.graph import Graph
from jev.guide.route import compile_route
from jev.world.state_v1 import StepKind

SKILLS = frozenset({"TRAVEL_TO", "ACCEPT_QUEST", "TURNIN_QUEST", "GRIND_UNTIL", "VENDOR_REPAIR"})


def human():
    return Graph.load("content/tbc/ally_human_1_12.json")


def test_supported_route_preserves_geometry_and_excludes_elites():
    original = human()
    unchanged = original.model_dump_json()
    result = compile_route(original, available_skills=SKILLS)
    by_id = original.by_id()
    assert result.source_graph_id == original.graph_id
    assert result.graph.graph_id == original.graph_id + ".supported"
    for n in result.graph.nodes:
        old = by_id[n.id]
        assert (n.world, n.pos, n.map_id, n.objective_targets, n.hunt_yards) == (
            old.world, old.pos, old.map_id, old.objective_targets, old.hunt_yards)
    excluded = {ex.quest_id: ex for ex in result.excluded if ex.quest_id}
    assert 3904 not in excluded and 3905 not in excluded, "crates are gathered"
    assert not {37, 45, 71, 39, 59} & set(excluded), "bodies are opened by their tooltip"
    assert "elite" in excluded[176].reason, "Hogger and his gnolls are not a solo fight"
    assert 62 not in excluded and 76 not in excluded, "an exploration is walked into"
    assert not any(n.quest_id in excluded for n in result.graph.nodes)
    assert all(n.kind is not StepKind.TRAIN for n in result.graph.nodes)
    assert any(n.quest_id == 52 for n in result.graph.nodes)
    assert any(n.quest_id == 54 for n in result.graph.nodes)
    assert original.model_dump_json() == unchanged
    assert all(e.goto in {n.id for n in result.graph.ribs()}
               for n in result.graph.nodes if n.quest_id for e in n.on_fail)


def test_supported_route_reaches_the_end_without_visiting_excluded_nodes():
    result = compile_route(human(), available_skills=SKILLS)
    nodes, cursor, visited = result.graph.by_id(), result.graph.entry, set()
    while cursor:
        assert cursor not in visited
        visited.add(cursor)
        cursor = nodes[cursor].next[0] if nodes[cursor].next else None
    expected = {n.id for n in result.graph.nodes if n.quest_id is not None}
    assert expected <= visited


def test_committed_graph_is_reproducible_from_the_local_database(tmp_path):
    generated = generate("data/knowledge/tbc-243.sqlite", graph_id="alli_human_1_12",
                         faction="alliance", zone_ids=(9, 12),
                         zone_names={9: "Northshire", 12: "Elwynn"})
    output = tmp_path / "graph.json"
    generated.save(output)
    assert output.read_bytes() == Path("content/tbc/ally_human_1_12.json").read_bytes()


def test_alternative_prerequisite_and_explicit_rewarded_predecessor_are_honoured():
    source = human()
    # Either group unlocks the quest; an unavailable alternative is not a mandatory
    # dependency. This is the server's positive prevQuests semantics.
    nodes = tuple(n.model_copy(update={"quest_prerequisites": ((783,), (999999,))})
                  if n.quest_id == 33 else n for n in source.nodes)
    graph = source.model_copy(update={"nodes": nodes})
    result = compile_route(graph, available_skills=SKILLS)
    assert any(n.quest_id == 33 for n in result.graph.nodes)
    assert not any(n.quest_id == 147 for n in result.graph.nodes)
    continued = compile_route(graph, available_skills=SKILLS, completed_quests=frozenset({123}))
    assert any(n.quest_id == 147 for n in continued.graph.nodes)


def test_a_quest_that_pays_nothing_is_left_out_unless_another_needs_it():
    """V163: Thunderbrew Lager, the 12-20 guide's first quest, paid no experience and was
    about 2,600 yards of walking for a keg of lager."""
    source = Graph.load("content/tbc/ally_human_12_20.json")
    kept = compile_route(source, available_skills=SKILLS)
    assert any(n.quest_id == 117 for n in kept.graph.nodes)
    result = compile_route(source, available_skills=SKILLS, worthless=frozenset({117}))
    assert not any(n.quest_id == 117 for n in result.graph.nodes)
    assert result.graph.get(result.graph.entry).quest_id != 117
    assert any(e.quest_id == 117 and "no experience" in e.reason for e in result.excluded)
    in_route = {n.quest_id for n in kept.graph.nodes}
    needed = next(p for n in kept.graph.nodes for group in n.quest_prerequisites for p in group
                  if p in in_route)
    assert any(n.quest_id == needed for n in compile_route(
        source, available_skills=SKILLS, worthless=frozenset({needed})).graph.nodes), \
        "a quest another needs stays, whatever it pays"


def test_the_worthless_quests_are_read_from_the_world_snapshot(tmp_path):
    import sqlite3

    from jev.run.cli import worthless_quests

    db = tmp_path / "world.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE world_quest_template (entry INTEGER, "
                           "RewMoneyMaxLevel INTEGER, RewChoiceItemId1 INTEGER)")
        connection.executemany("INSERT INTO world_quest_template VALUES (?, ?, ?)",
                               [(117, 0, 0), (102, 780, 0), (9, 0, 2211)])
    source = Graph.load("content/tbc/ally_human_12_20.json")
    assert worthless_quests(db, source) == frozenset({117}), "a reward to choose is worth it"
    assert worthless_quests(tmp_path / "missing.sqlite", source) == frozenset()
