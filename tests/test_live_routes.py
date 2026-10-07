"""Every race and class's route as a live guide (V412).

The hive's 52 routes (JevHive `hive.routes`, built by `hive.convert` on this server's world
database) are their authors' and live outside the repository: in `data/routes` once installed
(`tools/install_routes.py`), or where the hive keeps them. Each test of a route skips where the
route is not on this machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev.guide import live_routes
from jev.guide.coords import bounds_by_radio_id, navigation_frame
from jev.guide.graph import Graph

ROOT = Path(__file__).resolve().parents[1]
WORLD_DB = ROOT / "data/knowledge/tbc-243.sqlite"


def test_every_race_and_class_a_character_can_be_has_a_route_name():
    assert len(live_routes.NAMES) == 52
    assert live_routes.route_name(6, 11) == "tauren_druid"
    assert live_routes.route_name(10, 2) == "bloodelf_paladin"
    assert live_routes.route_name(4, 9) is None, "no night elf warlock on 2.4.3"
    assert live_routes.route_name(None, 1) is None


@pytest.mark.parametrize("name", live_routes.NAMES)
def test_each_route_loads_compiles_to_its_supported_route_and_its_frames_resolve(name):
    path = live_routes.find(name)
    if path is None:
        pytest.skip(f"{name}: no route on this machine (tools/install_routes.py)")
    ready = live_routes.readiness(name, path, world_db=WORLD_DB)
    assert ready.ok, ready.problems
    assert ready.supported_steps > 100, "a route of a few steps is no route"
    assert "boat or zeppelin" not in ready.needs, "every route keeps to its race's continent"
    graph = Graph.load(path)
    zones = bounds_by_radio_id(str(ROOT / "data/zones-tbc-243.json"))
    frame = navigation_frame(graph.coord_zone_id, next(
        rid for rid, z in zones.items() if z.area_id == graph.coord_zone_id), zones)
    assert frame is not None and len(ready.maps) == 1 and ready.maps[0] == frame.map_id
    # A session begun in any zone a step lies in reads its position in the route's frame.
    for rid, zone in zones.items():
        if any(n.zone_id == zone.area_id for n in graph.nodes):
            assert navigation_frame(graph.coord_zone_id, rid, zones) == frame
    assert path.with_name(f"{name}.spawns.json").is_file(), "its spawns, beside it"


def test_what_each_route_needs_is_read_from_where_its_steps_stand():
    """The tauren's mesas, the night elves' Darkshore and Darnassus, by place, not by the zone a
    quest is filed under (Cairne Bloodhoof's quests are the Barrens')."""
    found = {name: live_routes.find(name) for name in ("tauren_druid", "nightelf_hunter",
                                                       "human_mage", "undead_priest")}
    if None in found.values():
        pytest.skip("the routes are not on this machine")
    needs = {name: live_routes.readiness(name, path, world_db=WORLD_DB).needs
             for name, path in found.items()}
    assert any("Thunder Bluff's mesas" in k for k in needs["tauren_druid"])
    assert any("Darkshore" in k for k in needs["nightelf_hunter"])
    assert any("Darnassus" in k for k in needs["nightelf_hunter"])
    assert needs["human_mage"] == {} and needs["undead_priest"] == {}, \
        "the Undercity is walked into through its sewers"


def test_the_live_runner_plays_a_named_route_as_the_hive_does(tmp_path, monkeypatch, capsys):
    """`--route NAME` plays the route's supported quests, offline-checked as any guide."""
    from jev.run import cli

    name = "orc_warrior"
    if live_routes.find(name) is None:
        pytest.skip("the routes are not on this machine")
    assert cli.main(["--check", "--route", name]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["graph"].startswith("hive_orc_warrior")
    assert checked["route_mode"] == "supported" and checked["coordinate_frame"] == 14
    with pytest.raises(SystemExit):
        cli.main(["--check", "--route", "nightelf_warlock"])


def test_auto_picks_the_logged_in_characters_route_but_keeps_a_character_on_its_guide(tmp_path):
    from types import SimpleNamespace

    from jev.run import cli

    if live_routes.find("tauren_druid") is None:
        pytest.skip("the routes are not on this machine")
    human = Graph.load(ROOT / "content/tbc/ally_human_1_12.json")
    values = {"char.race_id": 6, "char.class_id": 11, "char.key": 77}
    fresh = SimpleNamespace(playhead=tmp_path / "none.json", graph=None)
    graph = cli.route_for(fresh, human, values)
    assert graph.graph_id.startswith("hive_tauren_druid") and fresh.graph.name == "tauren_druid.json"
    kept = tmp_path / "character.json"
    kept.write_text(json.dumps({"graph_id": "alli_human_12_20"}))
    on_guide = SimpleNamespace(playhead=kept, graph=None)
    assert cli.route_for(on_guide, human, {**values, "char.race_id": 1, "char.class_id": 8}) is human
    none = SimpleNamespace(playhead=None, graph=None)
    assert cli.route_for(none, human, {"char.race_id": 4, "char.class_id": 9}) is human
