"""Where this character can fly, and when a flight beats the walk.

A flight is taken from a flight master's map, one node at a time (`jev.clients.taxi`), and
the map names its nodes only by the text on their buttons. So a node is known by what it is
called where it stands: at each flight master the character talks to, the node its map
paints as here (CURRENT) is that flight master's, and the node's name hash is remembered
with the flight master's place, beside the playhead. Only nodes a character has visited can
be flown to - the game's own rule - so every node a flight can use has been remembered by
then.

The Westfall-Redridge guide crosses Elwynn five times, up to 4,266 yards a crossing: ten
minutes a walk, and a minute or two by gryphon once both ends are known.

Every race is given nodes at its creation that no visit put in this memory: its capital's, and
the night elves' Auberdine and Rut'theran Village (mangos `PlayerTaxi::InitTaxiNodesForLevel`).
They are known from the start (`starting_nodes`, V408), as the server knows them. And a walk no
route on foot finishes - from Teldrassil every walk to Darkshore ends in the sea - is flown
from the flight master the planner walks to soonest, to the known node the planner walks on
from to the end (`beyond`, the hive's rule, V408).
"""

from __future__ import annotations

import json
import math
import pathlib
import sqlite3
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache

from jev.persist import atomic_json

# Walks shorter than this are walked: the flight master is rarely on the way.
FLY_MIN_YARDS = 1500.0
# A character runs about seven yards a second (`jev.run.client.RUN_YARDS_PER_S`), and a
# gryphon covers several times that; taken low, since a route bends through its hubs.
RUN_YARDS_PER_S = 7.0
FLIGHT_YARDS_PER_S = 20.0
# Talking to the flight master, reading its map, taking off and landing.
FLIGHT_OVERHEAD_S = 45.0
# A flight master farther than this from the character is not walked to for a flight.
ORIGIN_MAX_YARDS = 600.0
# A flight must save at least this share of the walk to be worth its risks.
WORTH = 0.75
# A flight master this close to a remembered node's place is that node's.
SAME_PLACE_YARDS = 40.0
# The nodes the server gives each race at its creation, by race id (mangos-tbc
# `PlayerTaxi::InitTaxiNodesForLevel`): Stormwind, Orgrimmar, Ironforge, the night elves'
# Auberdine and Rut'theran Village, the Undercity, Thunder Bluff, Silvermoon and the Exodar.
# The two Outland nodes it gives each side and the Shattered Sun Staging Area are on another
# continent's network than any race's lands, and left out.
STARTING_NODES: dict[int, tuple[int, ...]] = {
    1: (2,), 2: (23,), 3: (6,), 4: (26, 27), 5: (11,), 6: (22,), 7: (6,), 8: (23,),
    10: (82,), 11: (94,)}
WORLD_DB = pathlib.Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"
# A walk no route on foot finishes (`beyond`): flown from one of the nearest few flight masters
# this near by a straight line, each walked to by the planner, to one of the nearest few known
# landings, each walked on from by the planner. Vesprystus is 2,213 yards from Darnassus's
# trainers and about 1,300 from Dolanaar (the hive's `hive.taxi`, FINDINGS 15).
BEYOND_REACH_YARDS = 3000.0
BEYOND_MASTERS = 3
BEYOND_NODES = 3

Point = tuple[float, float, float]


@dataclass(frozen=True)
class Node:
    name_id: int                 # the node's name, hashed as the strip paints it
    flightmaster: str
    world: Point                 # where its flight master stands


def load_nodes(path: pathlib.Path | None) -> dict[int, Node]:
    """The nodes this character has visited, by name hash. Missing or unreadable is none."""
    if path is None:
        return {}
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return {int(k): Node(int(k), v["flightmaster"], tuple(float(c) for c in v["world"]))
                for k, v in (data.get("nodes") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def save_node(path: pathlib.Path | None, node: Node) -> None:
    """Remember a visited node. Best effort: failing to remember must not fail the run."""
    if path is None:
        return
    nodes = load_nodes(path)
    nodes[node.name_id] = node
    try:
        atomic_json(pathlib.Path(path), {"format": 1, "nodes": {
            str(n.name_id): {"flightmaster": n.flightmaster, "world": list(n.world)}
            for n in nodes.values()}})
    except OSError:
        pass


def visited(master_world: Point, nodes: dict[int, Node]) -> bool:
    """Whether the flight master standing here has had its node remembered."""
    return any(math.dist(n.world[:2], master_world[:2]) <= SAME_PLACE_YARDS
               for n in nodes.values())


def flight(here: tuple[float, float], destination: tuple[float, float],
           nodes: dict[int, Node], masters: Iterable) -> tuple[object, Node] | None:
    """The flight master to walk to and the node to land at, when flying beats walking.

    `masters` are flight masters of the character's side on its map (`flightmasters`). The
    one taken off from need not have been visited: talking to it is the visit.
    """
    walk_yards = math.dist(here, destination)
    if walk_yards < FLY_MIN_YARDS or not nodes:
        return None
    walk_s = walk_yards / RUN_YARDS_PER_S
    best = None
    for master in masters:
        to_master = math.dist(here, master.world[:2])
        if to_master > ORIGIN_MAX_YARDS:
            continue
        for node in nodes.values():
            airborne = math.dist(master.world[:2], node.world[:2])
            if airborne <= SAME_PLACE_YARDS:
                continue                              # this flight master's own node
            seconds = (to_master / RUN_YARDS_PER_S + FLIGHT_OVERHEAD_S
                       + airborne / FLIGHT_YARDS_PER_S
                       + math.dist(node.world[:2], destination) / RUN_YARDS_PER_S)
            if seconds <= walk_s * WORTH and (best is None or seconds < best[0]):
                best = (seconds, master, node)
    return (best[1], best[2]) if best else None


@lru_cache(maxsize=4)
def _taxi_nodes(db_path: str) -> dict[int, tuple[int, tuple[float, float, float], str]]:
    """Every taxi node of TaxiNodes.dbc: id -> (map, world, name). None unreadable."""
    from jev.play.world_knowledge import readonly_uri

    def floating(bits) -> float:
        return struct.unpack("<f", struct.pack("<I", int(bits) & 0xFFFFFFFF))[0]

    try:
        con = sqlite3.connect(readonly_uri(pathlib.Path(db_path)), uri=True, timeout=1)
        try:
            blob = con.execute("select strings from dbc_strings where name = 'dbc_TaxiNodes'"
                               ).fetchone()[0]
            rows = con.execute("select id, c1, c2, c3, c4, c5 from dbc_TaxiNodes").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, TypeError):
        return {}
    out = {}
    for node_id, map_id, x, y, z, name in rows:
        end = blob.find(b"\0", int(name))
        text = blob[int(name):end].decode("utf-8", errors="replace") if end > int(name) else ""
        out[int(node_id)] = (int(map_id), (floating(x), floating(y), floating(z)), text)
    return out


def starting_nodes(race_id: int | None, map_id: int | None, side: str | None, *,
                   db_path: str = str(WORLD_DB)) -> dict[int, Node]:
    """The nodes the server gave this race at its creation on `map_id` (`STARTING_NODES`), as
    this memory keeps a node: by its name's hash as the flight master's map paints it, with the
    place of the flight master of the side that stands at it, else its own (V408)."""
    from jev.perceive.radio_frame import name_id
    from jev.world.vendor import flightmasters

    if race_id is None or map_id is None:
        return {}
    known = _taxi_nodes(db_path)
    masters = flightmasters(map_id, side)
    out: dict[int, Node] = {}
    for node_id in STARTING_NODES.get(race_id, ()):
        row = known.get(node_id)
        if row is None or row[0] != map_id or not row[2]:
            continue
        _, world, name = row
        beside = min(masters, key=lambda m: math.dist(m.world[:3], world), default=None)
        if beside is not None and math.dist(beside.world[:2], world[:2]) <= SAME_PLACE_YARDS:
            node = Node(name_id(name), beside.name, tuple(beside.world))
        else:
            node = Node(name_id(name), name, world)
        out[node.name_id] = node
    return out


def beyond(at: tuple[float, float], destination, masters, nodes: dict[int, Node],
           walk: Callable[[object, object], float | None], *,
           skip: Callable[[object, Node], bool] | None = None) -> tuple[object, Node, float] | None:
    """A flight for a walk the planner finds no way for (V408, the hive's `hive.taxi.beyond`):
    from the flight master it walks to, to the known node it walks on from to the end, the
    soonest there of the `BEYOND_MASTERS` nearest flight masters within `BEYOND_REACH_YARDS`
    and `BEYOND_NODES` landings each; with the walk to the flight master in yards. `walk(start,
    end)`: the planner's walk in yards, `None` where it has no way; a start of `None` is from
    where the character stands. `masters`: the flight masters of the character's side on its
    map; a node is a landing on this map when one of them stands at it. `skip(master, node)`:
    a flight not to try (one that failed lately)."""
    on_map = [n for n in nodes.values()
              if any(math.dist(m.world[:2], n.world[:2]) <= SAME_PLACE_YARDS for m in masters)]
    near = sorted((m for m in masters if math.dist(at[:2], m.world[:2]) <= BEYOND_REACH_YARDS),
                  key=lambda m: math.dist(at[:2], m.world[:2]))[:BEYOND_MASTERS]
    best = None
    for master in near:
        landings = sorted(on_map, key=lambda n: (math.dist(master.world[:2], n.world[:2])
                                                 / FLIGHT_YARDS_PER_S
                                                 + math.dist(n.world[:2], destination[:2])
                                                 / RUN_YARDS_PER_S))
        candidates = [n for n in landings
                      if math.dist(master.world[:2], n.world[:2]) > SAME_PLACE_YARDS
                      and math.dist(n.world[:2], destination[:2]) < math.dist(at[:2],
                                                                              destination[:2])
                      and (skip is None or not skip(master, n))][:BEYOND_NODES]
        if not candidates:
            continue
        to_master = walk(None, master.world)
        if to_master is None:
            continue
        for node in candidates:
            walk_on = walk(node.world, destination)
            if walk_on is None:
                continue
            seconds = (to_master / RUN_YARDS_PER_S + FLIGHT_OVERHEAD_S
                       + math.dist(master.world[:2], node.world[:2]) / FLIGHT_YARDS_PER_S
                       + walk_on / RUN_YARDS_PER_S)
            if best is None or seconds < best[0]:
                best = (seconds, master, node, to_master)
            break
    return best[1:] if best else None
