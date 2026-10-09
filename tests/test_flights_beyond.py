"""Flights the live body takes (V408): the nodes the server gives a race at its creation, known
from the start, and a walk no route on foot finishes flown where a flight finishes it.

The hive flew by the server's own word of the nodes a character knows (its taxi mask) and the
rule `hive.taxi.beyond`; the live body knew only the nodes its memory had seen on a flight
master's map, and none for a walk with no way: from Teldrassil every walk to Darkshore ends in
the sea, 1,147 yards out from Vesprystus, and the night elves' route stopped at level 12.
"""

from __future__ import annotations

import math

import pytest

from jev.guide.coords import bounds_by_radio_id
from jev.guide.path import Path, PathStatus
from jev.perceive.radio_frame import name_id, zone_id
from jev.world.taxi import STARTING_NODES, beyond, starting_nodes
from jev.world.taxi import Node, planned_flight
from jev.world.vendor import flightmasters

DOLANAAR = (9876.0, 940.0, 1306.0)
LAIRD = (6436.4, 451.0, 9.6)                      # in Auberdine, across the sea
VESPRYSTUS = (8640.58, 841.12, 23.35)
AUBERDINE = name_id("Auberdine, Darkshore")
RUTHERAN = name_id("Rut'theran Village, Teldrassil")


def _network():
    from types import SimpleNamespace

    masters = [SimpleNamespace(name="start", world=(100.0, 0.0, 0.0)),
               SimpleNamespace(name="near", world=(2900.0, 0.0, 0.0)),
               SimpleNamespace(name="other", world=(2500.0, 100.0, 0.0))]
    nodes = {i: Node(i, m.name, m.world) for i, m in enumerate(masters[1:], 1)}
    return masters, nodes


def test_planned_flight_compares_actual_walks_and_all_candidate_landings():
    masters, nodes = _network()
    end = (3000.0, 0.0, 0.0)
    walks = {(None, end): 5000.0, (None, masters[0].world): 100.0,
             (nodes[1].world, end): 3000.0, (nodes[2].world, end): 600.0}
    chosen = planned_flight((0, 0), end, masters, nodes, _walk(walks))
    # The closest landing's detour loses; the second one's actual walk wins. Stopping
    # after the first reachable landing previously missed this option.
    assert chosen == (masters[0], nodes[2], 100.0)
    # A portal makes the direct walk much shorter than the straight line. Do not fly.
    walks[None, end] = 600.0
    assert planned_flight((0, 0), end, masters, nodes, _walk(walks)) is None


def test_planned_flight_requires_both_ground_legs_and_a_known_landing():
    masters, nodes = _network()
    end = (3000.0, 0.0, 0.0)
    assert planned_flight((0, 0), end, masters, nodes, _walk({})) is None
    assert planned_flight((0, 0), end, masters, nodes,
                          _walk({(None, masters[0].world): 100.0})) is None
    walk = _walk({(None, masters[0].world): 100.0, (nodes[1].world, end): 100.0})
    assert planned_flight((0, 0), end, masters, nodes, walk) == (masters[0], nodes[1], 100.0)
    assert planned_flight((0, 0), end, masters, {}, walk) is None
    assert planned_flight((0, 0), end, masters, nodes, walk, skip=lambda *_: True) is None


def test_short_approaches_never_plan_flights():
    from unittest.mock import Mock

    masters, nodes = _network()
    walk = Mock(side_effect=AssertionError("local approach planned a flight"))
    assert planned_flight((0, 0), (100, 0, 0), masters, nodes, walk) is None
    walk.assert_not_called()


def test_planning_is_bounded_and_cooldowns_cover_routine_flights(monkeypatch):
    from test_live_body import body
    from jev.run.body import FLIGHT_FAILED_S

    masters, nodes = _network()
    end = (3000.0, 0.0, 0.0)
    calls = []
    def walk(a, b):
        calls.append((a, b))
        return 5000.0 if a is None and b == end else 100.0
    b = body()
    b._side, b._planned_walk = "alliance", walk
    chosen = b._flight_plan((0, 0), end, nodes, masters)
    assert chosen is not None and len(calls) <= 1 + 3 + 3 * 3
    clock = [100.0]
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: clock[0])
    for master in masters:
        for node in nodes.values():
            b._flight_failed(master, node, None)  # old routine flights passed no walk
    assert b._flight_plan((0, 0), end, nodes, masters) is None
    clock[0] += FLIGHT_FAILED_S + 1
    assert b._flight_plan((0, 0), end, nodes, masters) is not None


@pytest.mark.parametrize(("race", "map_id", "side", "names"), [
    (4, 1, "alliance", {"Auberdine, Darkshore", "Rut'theran Village, Teldrassil"}),
    (6, 1, "horde", {"Thunder Bluff, Mulgore"}),
    (2, 1, "horde", {"Orgrimmar, Durotar"}),
    (8, 1, "horde", {"Orgrimmar, Durotar"}),
    (5, 0, "horde", {"Undercity, Tirisfal"}),
    (1, 0, "alliance", {"Stormwind, Elwynn"}),
    (3, 0, "alliance", {"Ironforge, Dun Morogh"}),
    (7, 0, "alliance", {"Ironforge, Dun Morogh"}),
    (10, 530, "horde", {"Silvermoon City"}),
    (11, 530, "alliance", {"The Exodar"}),
])
def test_every_race_knows_the_nodes_the_server_gives_it_at_its_creation(race, map_id, side,
                                                                        names):
    """mangos-tbc `PlayerTaxi::InitTaxiNodesForLevel`, as the flight master's map names them
    and with the flight master who stands at each."""
    nodes = starting_nodes(race, map_id, side)
    assert set(nodes) == {name_id(n) for n in names}
    for node in nodes.values():
        assert any(m.name == node.flightmaster for m in flightmasters(map_id, side))
    assert starting_nodes(race, 571, side) == {}, "none on another map"
    assert set(STARTING_NODES) == {1, 2, 3, 4, 5, 6, 7, 8, 10, 11}


def _walk(ways):
    def walk(start, end):
        for (a, b), yards in ways.items():
            if (start is None if a is None else start is not None and math.dist(a[:2], start[:2]) < 5) \
                    and math.dist(b[:2], end[:2]) < 5:
                return yards
        return None
    return walk


def test_a_walk_no_route_finishes_is_flown_from_the_flight_master_the_planner_walks_to():
    """From Dolanaar to Laird in Auberdine: Vesprystus is 1,300 yards off by a straight line,
    past Jev's own reach for a flight (600), and a walk through the Darnassus portal."""
    masters = flightmasters(1, "alliance")
    nodes = starting_nodes(4, 1, "alliance")
    vesprystus = next(m for m in masters if m.name == "Vesprystus")
    walk = _walk({(None, vesprystus.world): 1988.0, (nodes[AUBERDINE].world, LAIRD): 110.0})
    master, node, to_master = beyond(DOLANAAR[:2], LAIRD, masters, nodes, walk)
    assert (master.name, node.name_id, to_master) == ("Vesprystus", AUBERDINE, 1988.0)
    # No walk to any flight master, or none on from the landing: no flight.
    assert beyond(DOLANAAR[:2], LAIRD, masters, nodes, _walk({})) is None
    assert beyond(DOLANAAR[:2], LAIRD, masters, nodes,
                  _walk({(None, vesprystus.world): 1988.0})) is None
    # A flight that came to nothing lately is not tried again.
    assert beyond(DOLANAAR[:2], LAIRD, masters, nodes, walk,
                  skip=lambda m, n: n.name_id == AUBERDINE) is None


def test_a_landing_no_nearer_the_end_than_where_the_character_stands_is_none():
    masters = flightmasters(1, "alliance")
    nodes = starting_nodes(4, 1, "alliance")
    near_auberdine = (LAIRD[0] + 300.0, LAIRD[1], 10.0)
    vesprystus = next(m for m in masters if m.name == "Vesprystus")
    walk = _walk({(None, vesprystus.world): 3000.0, (nodes[RUTHERAN].world, LAIRD): 50.0})
    assert beyond(near_auberdine[:2], LAIRD, masters, {RUTHERAN: nodes[RUTHERAN]}, walk) is None


def test_the_live_body_flies_where_no_walk_finishes_by_its_own_seeded_memory(tmp_path):
    """A night elf at Dolanaar with nothing in its taxi memory: the seeded nodes and the rule
    give it the flight the hive's server-known nodes gave the hive's."""
    from test_live_body import body

    b = body()
    b.client.bounds = bounds_by_radio_id("data/zones-tbc-243.json")[zone_id("Teldrassil")]
    b.taxi_memory = tmp_path / "character.taxi.json"
    b._race_id, b._side = 4, "alliance"
    vesprystus = next(m for m in flightmasters(1, "alliance") if m.name == "Vesprystus")
    walks = {}

    def plan_to(world):
        if math.dist(world[:2], vesprystus.world[:2]) < 5:
            return Path(PathStatus.COMPLETE, (DOLANAAR, vesprystus.world), "mmap")
        return Path(PathStatus.PARTIAL, (DOLANAAR, (9000.0, 900.0, 0.0)), "mmap")

    b.client.plan_to = plan_to
    b._walk_between = lambda a, z: walks.setdefault((tuple(a[:2]), tuple(z[:2])), 120.0)
    nodes = b._taxi_nodes()
    assert set(nodes) == {AUBERDINE, RUTHERAN}
    master, node, walk = b._flight_plan(DOLANAAR[:2], LAIRD, nodes,
                                          flightmasters(1, "alliance"))
    assert (master.name, node.flightmaster) == ("Vesprystus", "Caylais Moonfeather")
    assert walk == pytest.approx(math.dist(DOLANAAR, vesprystus.world))
    b._flight_failed(master, node, walk)
    assert b._flight_plan(DOLANAAR[:2], LAIRD, nodes, flightmasters(1, "alliance")) is None
    # A walk the planner finishes is walked; and a short one never asks.
    b._flights_failed = {}
    b.client.plan_to = lambda world: Path(PathStatus.COMPLETE, (DOLANAAR, LAIRD), "mmap")
    assert b._flight_plan(DOLANAAR[:2], LAIRD, nodes, flightmasters(1, "alliance")) is None
    assert b._flight_plan(DOLANAAR[:2], (DOLANAAR[0] + 100, DOLANAAR[1], 1300.0), nodes,
                            flightmasters(1, "alliance")) is None
    # The seeded nodes count as visited: no discovery walk to them.
    from jev.guide.coords import world_to_map

    b._position = lambda: world_to_map(*VESPRYSTUS[:2], b.client.bounds)
    assert b._undiscovered_master() is None
