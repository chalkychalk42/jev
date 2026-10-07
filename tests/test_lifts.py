"""Lifts (V382): the planner's way between Thunder Bluff's mesas and the ground, and the walk
that rides one.

On the navmesh the mesas are islands: from Innkeeper Pala the way toward Bloodhoof Village
ends at the edge of Hunter Rise, 877 yards short, and from the ground the way up ends at the
foot of a lift (measured on the map 1 tiles, 6 Oct). Through the Mesa Elevator at (-1308,
185) it is a walk of 158 yards to its upper deck, a ride, and 1,277 on from the ground under
its lower stop. 27 of the hive's 315 bots stood on the mesas at once on 6 Oct with no way
down.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev.clients.travel import Outcome
from jev.guide import path as path_module
from jev.guide.coords import bounds_by_radio_id, world_to_map
from jev.guide.path import (Lift, LiftLeg, LiftStop, Path, PathStatus, TeleportQuery,
                            lift_stops, load_lifts)
from jev.perceive.fields import FIELDS
from jev.perceive.radio_frame import RadioReading, zone_id
from jev.run.client import Client, with_travel
from jev.world.state_v1 import SenseFault

ZONES = bounds_by_radio_id("data/zones-tbc-243.json")
MULGORE = ZONES[zone_id("Mulgore")]

LIFT = Lift(guid=18435, entry=4171, name="Mesa Elevator", map_id=1, x=-1308.38, y=185.29,
            period_ms=30000, stops=(LiftStop(68.59, 0, 5000), LiftStop(130.08, 15000, 20000)))
TOP_DECK = (-1311.38, 180.09, 130.54)       # its upper deck, on the mesh
FOOT = (-1308.38, 185.29, 59.60)            # the ground under its lower stop
PALA = (-1300.3, 38.5, 129.3)
BLOODHOOF = (-2340.0, -370.0, -9.0)
# The mesh's way from the inn toward Bloodhoof: to the edge of Hunter Rise, and no further.
EDGE = Path(PathStatus.PARTIAL, (PALA, (-1400.0, -60.0, 158.0), (-1504.3, -103.5, 154.6)), "mmap")
WALK_IN = Path(PathStatus.COMPLETE, (PALA, (-1290.0, 120.0, 131.0), TOP_DECK), "mmap")
WALK_ON = Path(PathStatus.COMPLETE, (FOOT, (-1700.0, -100.0, 10.0), BLOODHOOF), "mmap")


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


@pytest.fixture
def decks(monkeypatch):
    """The decks beside the lift's stops, as `lift_decks` finds them on the real mesh."""
    monkeypatch.setattr(path_module, "_DECKS", {})

    def found(query, map_id, stop):
        return [TOP_DECK] if stop[2] > 100 else [FOOT]

    monkeypatch.setattr(path_module, "lift_decks", found)


def mesh():
    return Mesh({(PALA, BLOODHOOF): EDGE, (PALA, TOP_DECK): WALK_IN,
                 (FOOT, BLOODHOOF): WALK_ON})


# -- the world database ----------------------------------------------------------------------

def test_thunder_bluffs_and_the_undercitys_lifts_are_known_from_the_world_database():
    lifts = {lift.guid: lift for lift in load_lifts("data/knowledge/tbc-243.sqlite")}
    for guid in (18298, 18435, 20639, 20640):            # the four Mesa Elevators
        lift = lifts[guid]
        assert lift.map_id == 1 and lift.name == "Mesa Elevator"
        assert lift.period_ms in (30000, 30033)
        assert lift.wait_s() == pytest.approx(15.0, abs=0.1)
        assert lift.ride_s(0, 1) == pytest.approx(10.0, abs=0.1)
        assert lift.ride_s(1, 0) == pytest.approx(10.0, abs=0.1)
    assert [round(s.z, 1) for s in lifts[18435].stops] == [68.6, 130.1]
    assert [round(s.z, 1) for s in lifts[20639].stops] == [69.0, 140.5]
    for guid in (44892, 44901, 44962):                   # the Undercity's three
        assert [round(s.z, 1) for s in lifts[guid].stops] == [-40.8, 55.7]
    assert not {44890, 44891, 44893, 44909, 44954, 44976} & set(lifts), \
        "the Undercity's lift doors move 5 and 8 yards: no lifts"
    assert 9411 not in lifts and 9450 in lifts, "the Plunger rides over the Vator in one shaft"
    assert 6945 not in lifts, "a cart that moves along is no lift"


def test_a_stop_that_ends_the_cycle_and_begins_it_is_one():
    """The Undervator's path: at the bottom from 15.2 s to the cycle's end at 16.7 s and from
    its start to 3.5 s, at the top from 7.0 to 11.8 s."""
    period, stops = lift_stops([(0, 0.0), (167, 0.0), (3500, 0.0), (7000, 96.5),
                                (11833, 96.5), (15167, 0.0), (16667, 0.0)])
    assert period == 16667
    assert sorted((s.z, s.arrives_ms, s.leaves_ms) for s in stops) == [
        (0.0, 15167, 3500), (96.5, 7000, 11833)]
    lift = Lift(1, 1, "Undervator", 0, 0.0, 0.0, period, tuple(sorted(stops, key=lambda s: s.z)))
    assert lift.ride_s(0, 1) == pytest.approx(3.5)
    assert lift.ride_s(1, 0) == pytest.approx(3.334)


# -- the planner -----------------------------------------------------------------------------

def test_a_mesa_with_no_way_down_on_foot_is_left_by_its_lift(decks):
    route = TeleportQuery(mesh(), (), [LIFT], can_ride=lambda: True).path(1, PALA, BLOODHOOF)
    assert route.status is PathStatus.COMPLETE and route.usable
    leg = route.teleport
    assert isinstance(leg, LiftLeg) and not leg.up and leg.lift is LIFT
    assert route.detail == "through the Mesa Elevator down at (-1308, 185)"
    assert leg.at == TOP_DECK and leg.exit == FOOT
    assert route.points[:route.jump] == WALK_IN.points, "walked to the upper deck"
    assert route.points[route.jump:] == WALK_ON.points, "and on from the ground under it"
    walked = WALK_IN.length_yards() + WALK_ON.length_yards()
    assert route.length_yards() == pytest.approx(walked), "the ride is no yards of walking"
    assert leg.wait_s == 15.0 and leg.ride_s == 10.0


def test_no_lift_is_offered_to_a_follower_that_cannot_ride_one(decks):
    """The live client's keys cannot see a platform, and its radio paints no height."""
    for can_ride in (None, lambda: False):
        route = TeleportQuery(mesh(), (), [LIFT], can_ride=can_ride).path(1, PALA, BLOODHOOF)
        assert route is EDGE


def test_a_lift_far_from_both_ends_of_a_walk_is_not_tried(decks):
    """A lift helps a walk that begins or ends by its cliff: a failed walk in the Barrens asks
    nothing of Thunder Bluff's."""
    far, farther = (-400.0, -2600.0, 92.0), (-900.0, -3200.0, 92.0)
    planner = Mesh({(far, farther): Path(PathStatus.PARTIAL, (far, (-500.0, -2700.0, 92.0)))})
    route = TeleportQuery(planner, (), [LIFT], can_ride=lambda: True).path(1, far, farther)
    assert route.status is PathStatus.PARTIAL and planner.asked == [(far, farther)]


def test_a_way_on_foot_is_kept_where_there_is_one(decks):
    near = (PALA[0] - 40.0, PALA[1] + 30.0, 129.0)
    direct = Path(PathStatus.COMPLETE, (PALA, near), "mmap")
    query = TeleportQuery(Mesh({(PALA, near): direct}), (), [LIFT], can_ride=lambda: True)
    assert query.path(1, PALA, near) is direct


# -- the walk --------------------------------------------------------------------------------

def mulgore_client(start, *, ride=None):
    """A client standing at `start` (world yards) on Mulgore's map, with the lift."""
    values = {f.name: None for f in FIELDS}
    here = world_to_map(start[0], start[1], MULGORE)
    values.update({"pos.zone_id": zone_id("Mulgore"), "pos.mx": here[0], "pos.my": here[1]})
    client = Client(hwnd=1, hid=Mock(), cap=Mock(), origin=(0, 0), size=(1600, 900))
    client.reading = lambda: RadioReading(values=values, ok=True, fault=SenseFault.NONE, seq=1)
    lines = []
    planner = mesh()
    with_travel(client, MULGORE, planner, arrival_yards=5.0, zones=ZONES, teleports=(),
                lifts=(LIFT,), say=lines.append)
    if ride is not None:
        # A follower that rides any leg, as the hive's does (V382); the live keys ride only
        # by a platform's times (V407, tests/test_lift_keys.py).
        client.travel.ride = ride
        client.travel.rideable = lambda leg: True
    client.lines = lines

    def stand(world):
        values["pos.mx"], values["pos.my"] = world_to_map(world[0], world[1], MULGORE)
        return client.position()

    client.stand = stand
    return client, planner


def arrived(route):
    return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                           stuck_events=0, detail="", end=route.points[-1])


def test_the_walk_rides_the_lift_and_goes_on_from_where_it_leaves_the_character(decks):
    rides = []

    def ride(leg):
        rides.append(leg)
        client.stand(leg.exit)
        return leg.exit

    client, planner = mulgore_client(PALA, ride=ride)
    walked, arrivals = [], []

    def follow(route, **kw):
        walked.append(route)
        arrivals.append(client.travel.arrival_yards)
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    assert client.approach(BLOODHOOF) is True, client.lines
    assert [r.points for r in walked] == [WALK_IN.points, WALK_ON.points]
    assert len(rides) == 1 and rides[0].at == TOP_DECK and rides[0].exit == FOOT
    assert arrivals[0] == pytest.approx(1.5), "the walk in ends at the deck"
    assert client.travel.arrival_yards == 5.0
    assert planner.asked[-1] == (FOOT, BLOODHOOF), \
        "planned on from where the ride left it, on the ground's floor"
    assert any("at the Mesa Elevator down at (-1308, 185): waiting for it (about 15 s), then "
               "10 s aboard" in line for line in client.lines), client.lines
    assert any("landed at (-1308.4, 185.3, 59.6)" in line for line in client.lines)


def test_a_ride_that_does_not_happen_ends_the_walk_short(decks):
    client, _planner = mulgore_client(PALA, ride=lambda leg: None)
    client.travel.ride_detail = "not at (-1308.4, 185.3, 68.6) in 30 s"

    def follow(route, **kw):
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    assert client.approach(BLOODHOOF) is False
    assert any("the Mesa Elevator down at (-1308, 185): no ride (not at (-1308.4, 185.3, 68.6)"
               in line for line in client.lines), client.lines


def test_the_live_walk_takes_no_lift(decks):
    """The live follower has no ride: the walk is the mesh's, to the edge, as before."""
    client, _planner = mulgore_client(PALA)
    walked = []

    def follow(route, **kw):
        walked.append(route)
        client.stand(route.points[-1])
        return arrived(route)

    client.travel.follow = follow
    assert client.approach(BLOODHOOF) is False
    assert [r.points for r in walked] == [EDGE.points]
