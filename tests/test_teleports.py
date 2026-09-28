"""Walks through teleports (V305): the planner's links, and the walk on from where one lands.

The night elves' way off Teldrassil is the Darnassus portal (areatrigger 527) down to
Rut'theran. On the navmesh the way from Darnassus toward Rut'theran ends at the edge of the
tree, 1,227 yards over the village; through the portal it is 455 yards to the portal and 97 on
from where it puts the character (measured on the map 1 tiles, 28 Sep).
"""

from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import jev.clients.travel as travel_module
from jev.clients.travel import Outcome
from jev.clients.walk_sim import SimHid, SimTime, WalkWorld
from jev.guide.coords import bounds_by_radio_id, map_to_world, world_to_map
from jev.guide.path import Path, PathStatus, Teleport, TeleportQuery, load_teleports
from jev.perceive.fields import FIELDS
from jev.perceive.radio_frame import RadioReading, zone_id
from jev.run import client as client_module
from jev.run.client import Client, with_travel
from jev.world.state_v1 import SenseFault

ZONES = bounds_by_radio_id("data/zones-tbc-243.json")
TELDRASSIL = ZONES[zone_id("Teldrassil")]

MYDRANNUL = (9918.06, 2190.34, 1328.27)
NESSA = (8694.04, 950.26, 12.9)
PORTAL = Teleport(trigger_id=527, name="Teddrassil - Ruth Theran", map_id=1,
                  at=(9947.5, 2630.0, 1318.6), radius=10.0, box=(0.0, 0.0, 0.0, 0.0),
                  exit=(8785.79, 966.98, 30.2))
INSIDE = (9947.48, 2630.04, 1318.93)       # the portal's middle, on the mesh
# The mesh's way from Darnassus toward Rut'theran: to the edge of the tree, and no further.
EDGE = Path(PathStatus.PARTIAL, (MYDRANNUL, (9365.3, 1408.0, 1278.6), (9266.7, 1073.9, 1257.8)),
            "mmap")
WALK_IN = Path(PathStatus.COMPLETE, (MYDRANNUL, (9951.5, 2561.6, 1317.1), INSIDE), "mmap")
WALK_ON = Path(PathStatus.COMPLETE, (PORTAL.exit, (8702.4, 946.7, 13.5), NESSA), "mmap")


class Mesh:
    """A fake planner: each (start, end) asked, answered from a table by the nearest ends."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def path(self, map_id, start, end):
        self.asked.append((tuple(start), tuple(end)))
        for (a, b), answer in self.answers.items():
            if math.dist(a[:2], start[:2]) < 1.0 and math.dist(b[:2], end[:2]) < 1.0:
                return answer
        return Path(PathStatus.NOPATH, detail="no such way")

    def close(self):
        pass


def test_a_route_that_does_not_get_there_on_foot_goes_through_a_teleport():
    mesh = Mesh({(MYDRANNUL, NESSA): EDGE, (MYDRANNUL, PORTAL.at): WALK_IN,
                 (PORTAL.exit, NESSA): WALK_ON})
    route = TeleportQuery(mesh, [PORTAL]).path(1, MYDRANNUL, NESSA)
    assert route.status is PathStatus.COMPLETE and route.usable
    assert route.teleport is PORTAL and route.detail.startswith("through areatrigger 527")
    assert route.points[:route.jump] == WALK_IN.points, "the walk ends inside the trigger"
    assert PORTAL.room(route.points[route.jump - 1]) > 2.0
    assert route.points[route.jump:] == WALK_ON.points, "and goes on from where it lands"
    assert route.walk_in().points == WALK_IN.points and route.walk_in().teleport is None
    walked = WALK_IN.length_yards() + WALK_ON.length_yards()
    assert route.length_yards() == pytest.approx(walked), "the jump is no yards of walking"


def test_a_route_that_gets_there_on_foot_is_left_alone():
    near = (MYDRANNUL[0] + 60.0, MYDRANNUL[1] + 80.0, 1330.0)
    direct = Path(PathStatus.COMPLETE, (MYDRANNUL, (MYDRANNUL[0] + 30.0, MYDRANNUL[1], 1329.0),
                                        near), "mmap")
    mesh = Mesh({(MYDRANNUL, near): direct})
    assert TeleportQuery(mesh, [PORTAL]).path(1, MYDRANNUL, near) is direct
    assert mesh.asked == [(MYDRANNUL, near)], "no teleport was even asked about"


def test_a_long_way_round_is_taken_only_where_a_teleport_is_shorter():
    # Complete, but five times the straight line: the portal's way is shorter, and is taken.
    around = Path(PathStatus.COMPLETE, (MYDRANNUL, (12000.0, 5000.0, 600.0), NESSA), "mmap")
    mesh = Mesh({(MYDRANNUL, NESSA): around, (MYDRANNUL, PORTAL.at): WALK_IN,
                 (PORTAL.exit, NESSA): WALK_ON})
    assert TeleportQuery(mesh, [PORTAL]).path(1, MYDRANNUL, NESSA).teleport is PORTAL
    # A teleport whose walk in stops outside its trigger is none to take.
    short = Path(PathStatus.COMPLETE, (MYDRANNUL, (9951.5, 2561.6, 1317.1)), "mmap")
    mesh = Mesh({(MYDRANNUL, NESSA): EDGE, (MYDRANNUL, PORTAL.at): short,
                 (PORTAL.exit, NESSA): WALK_ON})
    assert TeleportQuery(mesh, [PORTAL]).path(1, MYDRANNUL, NESSA) is EDGE
    # Nor one on another map.
    assert TeleportQuery(Mesh({(MYDRANNUL, NESSA): EDGE}), [PORTAL]).path(0, MYDRANNUL,
                                                                        NESSA) is EDGE


def test_the_walk_on_is_asked_once_for_every_height_a_plan_starts_at():
    """A plan with no height known asks at up to 34 (`jev.run.client.START_HEIGHTS`), and the
    walk on from the exit is the same from each."""
    mesh = Mesh({(MYDRANNUL, NESSA): EDGE, (PORTAL.exit, NESSA): WALK_ON})
    query = TeleportQuery(mesh, [PORTAL])
    for dz in (0.0, -3.0, 3.0, -6.0):
        start = (MYDRANNUL[0], MYDRANNUL[1], MYDRANNUL[2] + dz)
        assert query.path(1, start, NESSA) is EDGE, "no walk in from a height off the mesh"
    assert mesh.asked.count((PORTAL.exit, NESSA)) == 1


@pytest.mark.skipif(not pathlib.Path("data/knowledge/tbc-243.sqlite").exists(),
                    reason="needs the world database")
def test_the_darnassus_portal_is_known_from_the_world_database():
    teleports = {t.trigger_id: t for t in load_teleports("data/knowledge/tbc-243.sqlite")}
    portal = teleports[527]
    assert portal.map_id == 1 and portal.radius == pytest.approx(10.0)
    assert portal.at == pytest.approx(PORTAL.at, abs=0.1)
    assert portal.exit == pytest.approx(PORTAL.exit, abs=0.1)
    assert 542 in teleports, "and the way back up, from Rut'theran"
    assert 704 not in teleports, "a jump straight down shows nothing on the map"
    assert 1103 not in teleports, "nor is one a condition gates"


def test_teleports_are_planned_over_every_other_layer_and_re_plans_stay_on_foot():
    """Each walk of a way through a teleport is planned clear of all the layers below keep
    clear of; a re-plan is walked by the follower, which knows nothing of a jump."""
    from jev.guide.exposure import ExposureQuery
    from jev.guide.route_memory import RouteMemory

    client = Client(hwnd=1, hid=Mock(), cap=Mock(), origin=(0, 0), size=(1600, 900))
    with_travel(client, TELDRASSIL, Mock(), arrival_yards=5.0, zones=ZONES,
                route_memory=RouteMemory(), teleports=(PORTAL,))
    assert isinstance(client.query, TeleportQuery)
    assert isinstance(client.query.inner, ExposureQuery)
    assert client._on_foot() is client.query.inner
    assert isinstance(client.query.estimate(), TeleportQuery), "a walk's cost goes through too"


# -- the walk --------------------------------------------------------------------------------


def teldrassil_client(start):
    """A client standing at `start` (world yards), reading Teldrassil's map, through `PORTAL`."""
    values = {f.name: None for f in FIELDS}
    here = world_to_map(start[0], start[1], TELDRASSIL)
    values.update({"pos.zone_id": zone_id("Teldrassil"), "pos.mx": here[0], "pos.my": here[1]})
    client = Client(hwnd=1, hid=Mock(), cap=Mock(), origin=(0, 0), size=(1600, 900))
    client.reading = lambda: RadioReading(values=values, ok=True, fault=SenseFault.NONE, seq=1)
    lines = []
    mesh = Mesh({(MYDRANNUL, NESSA): EDGE, (MYDRANNUL, PORTAL.at): WALK_IN,
                 (PORTAL.exit, NESSA): WALK_ON})
    with_travel(client, TELDRASSIL, mesh, arrival_yards=5.0, zones=ZONES, teleports=(PORTAL,),
                say=lines.append)
    client.lines = lines

    def stand(world):
        values["pos.mx"], values["pos.my"] = world_to_map(world[0], world[1], TELDRASSIL)
        return client.position()

    client.stand = stand
    return client, mesh


def arrived(route):
    return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                           stuck_events=0, detail="", end=route.points[-1])


def test_a_jump_to_the_exit_is_the_walk_going_on():
    client, mesh = teldrassil_client(MYDRANNUL)
    walked = []

    def follow(route, *, abort=None, **kw):
        walked.append(route)
        if len(walked) == 1:
            # Into the portal: it moves the character before the walk has stopped.
            client.stand(route.points[-2])
            assert not abort(), "no jump yet"
            client.stand(PORTAL.exit)
            assert abort(), "the walk in ends at the jump, not as stuck or off its route"
            return SimpleNamespace(outcome=Outcome.ABORTED, remaining_yards=2400.0, turns=0,
                                   stuck_events=0, detail="caller aborted")
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    assert client.approach(NESSA) is True
    assert [r.points for r in walked] == [WALK_IN.points, WALK_ON.points]
    assert walked[1].teleport is None
    assert (PORTAL.exit, NESSA) in mesh.asked, "planned on from where the portal put it"
    assert any("landed at (8785.8, 967.0)" in line for line in client.lines)
    assert client.last_headway == pytest.approx(math.dist(MYDRANNUL[:2], NESSA[:2]), abs=1.0), \
        "no wedge: the jump brought it the whole way"


def test_a_walk_that_stops_in_the_trigger_waits_there_for_the_jump(monkeypatch):
    client, _mesh = teldrassil_client(MYDRANNUL)
    monkeypatch.setattr(client_module, "TELEPORT_LOOK_S", 0.01)
    walked, reads = [], []
    position = client.travel.position

    def later():
        reads.append(1)
        if len(reads) == 3:                    # the server's turn to move it (the hive: 0.5 s)
            client.stand(PORTAL.exit)
        return position()

    def follow(route, **kw):
        walked.append(route)
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    client.travel.position = later
    assert client.approach(NESSA) is True
    assert [r.points for r in walked] == [WALK_IN.points, WALK_ON.points]


def test_a_teleport_that_does_not_move_the_character_ends_the_walk_short(monkeypatch):
    client, _mesh = teldrassil_client(MYDRANNUL)
    monkeypatch.setattr(client_module, "TELEPORT_WAIT_S", 0.05)
    monkeypatch.setattr(client_module, "TELEPORT_LOOK_S", 0.01)
    arrivals = []

    def follow(route, **kw):
        arrivals.append(client.travel.arrival_yards)
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    assert client.approach(NESSA) is False
    assert any("areatrigger 527 (Teddrassil - Ruth Theran) did not move" in line
               for line in client.lines)
    assert client.last_headway is None, "standing in a portal is no wedge"
    assert arrivals == [pytest.approx(PORTAL.room(INSIDE) / 2)], "stopped well inside it"
    assert client.travel.arrival_yards == 5.0, "and the next walk arrives as before"
    here = map_to_world(*client.travel.position(), TELDRASSIL)
    assert PORTAL.room((*here, INSIDE[2])) >= 0, "it stood inside the trigger"


# -- the real follower, on a simulated character ----------------------------------------------

W, H = abs(TELDRASSIL.left - TELDRASSIL.right), abs(TELDRASSIL.top - TELDRASSIL.bottom)


def _yards(world):
    """World yards as `walk_sim`'s map-yards in Teldrassil's box."""
    mx, my = world_to_map(world[0], world[1], TELDRASSIL)
    return mx * W, my * H


@dataclass
class PortalWorld(WalkWorld):
    """A simulated walk with the portal in it: inside its sphere the character is moved to
    its exit, `delay_s` after it walked in."""

    delay_s: float = 0.0
    entered: float | None = None
    moved: bool = False

    def _step(self, dt):
        super()._step(dt)
        if self.moved:
            return
        if self.entered is None and math.dist((self.x, self.y), _yards(PORTAL.at)) < PORTAL.radius:
            self.entered = self.t
        if self.entered is not None and self.t - self.entered >= self.delay_s:
            self.x, self.y = _yards(PORTAL.exit)          # the server's teleport
            self.moved = True


@pytest.mark.parametrize("delay_s", [0.0, 1.0], ids=["on the way in", "standing in it"])
def test_the_follower_walks_on_from_where_the_portal_puts_it(delay_s, monkeypatch):
    """The real `Travel` keyed through a simulated walk: the character walks into the
    portal's sphere and is moved to its exit - at once, as a client's own trigger does, or a
    second after it stopped there, as the hive's bridge looking every 0.5 s may - and walks
    on to Nessa Shadowsong, with not one stuck event."""
    start, first = _yards(MYDRANNUL), _yards(WALK_IN.points[1])
    world = PortalWorld(x=start[0], y=start[1], width_yards=W, height_yards=H,
                        heading=math.atan2(first[1] - start[1], first[0] - start[0]),
                        delay_s=delay_s)
    monkeypatch.setattr(travel_module, "time", SimTime(world))
    monkeypatch.setattr(client_module, "time", SimTime(world))
    client = Client(hwnd=1, hid=SimHid(world), cap=Mock(), origin=(0, 0), size=(1600, 900))

    def reading(tries=6):
        mx, my = world.map_position()
        values = {f.name: None for f in FIELDS}
        values.update({"pos.zone_id": zone_id("Teldrassil"), "pos.mx": mx, "pos.my": my})
        return RadioReading(values=values, ok=True, fault=SenseFault.NONE, seq=1)

    client.reading = reading
    lines = []
    mesh = Mesh({(MYDRANNUL, NESSA): EDGE, (MYDRANNUL, PORTAL.at): WALK_IN,
                 (PORTAL.exit, NESSA): WALK_ON})
    with_travel(client, TELDRASSIL, mesh, arrival_yards=5.0, zones=ZONES, teleports=(PORTAL,),
                say=lines.append)
    assert client.approach(NESSA) is True, lines
    assert world.moved
    here = map_to_world(*world.map_position(), TELDRASSIL)
    assert math.dist(here, NESSA[:2]) <= 5.0 + 1.0
    assert client.last_travel.stuck_events == 0, "the jump was never taken for a stuck walk"
    assert any("through areatrigger 527: landed" in line for line in lines), lines
