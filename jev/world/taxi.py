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
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass

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
