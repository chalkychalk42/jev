"""The radio may change maps mid-leg; the measured follower keeps one coordinate frame."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_runtime_records import seen

from jev.coach.policy import decide
from jev.coach.schema import Decision, Intent
from jev.coach.verifier import verify
from jev.guide.coords import (
    bounds_by_radio_id,
    map_to_world,
    navigation_frame,
    world_to_map,
)
from jev.guide.graph import Graph, Node
from jev.guide.tracker import Event, Tracker
from jev.perceive.fields import FIELDS
from jev.perceive.radio_frame import RadioReading, zone_id
from jev.run.client import Client, with_travel
from jev.world.state_v1 import Pos, SenseFault, State, StepKind

ZONES = bounds_by_radio_id("data/zones-tbc-243.json")
ELWYNN, STORMWIND = (ZONES[zone_id(name)] for name in ("Elwynn", "Stormwind"))


def client_in(region, raw, corpse=None):
    values = {f.name: None for f in FIELDS}
    values.update({"pos.zone_id": zone_id(region), "pos.mx": raw[0], "pos.my": raw[1],
                   "pos.corpse_mx": corpse[0] if corpse else None,
                   "pos.corpse_my": corpse[1] if corpse else None})
    client = Client(hwnd=1, hid=Mock(), cap=Mock(), origin=(0, 0), size=(1600, 900))
    client.reading = lambda: RadioReading(values=values, ok=True, fault=SenseFault.NONE, seq=1)
    with_travel(client, ELWYNN, Mock(), arrival_yards=5, zones=ZONES)
    return client, values


def test_actual_zone_change_keeps_world_position_and_raw_region_honest():
    # Morgan Pestle's source coordinate, used by the real generated quest61 turn-in.
    world = (-8857.6904296875, 625.4979858398438)
    elwynn, city = world_to_map(*world, ELWYNN), world_to_map(*world, STORMWIND)
    client, values = client_in("Elwynn", elwynn, corpse=elwynn)
    assert client.position() == pytest.approx(elwynn)
    values.update({"pos.zone_id": zone_id("Stormwind"), "pos.mx": city[0], "pos.my": city[1],
                   "pos.corpse_mx": city[0], "pos.corpse_my": city[1]})
    assert client.position() == pytest.approx(elwynn)
    assert map_to_world(*client.position(), client.bounds) == pytest.approx(world)
    observed = client.state()
    assert observed.pos.zone == "Stormwind"
    assert observed.pos.zone_id == zone_id("Stormwind")
    assert observed.pos.coord_zone_id == ELWYNN.area_id == 12
    assert (observed.pos.raw_mx, observed.pos.raw_my) == pytest.approx(city)
    assert (observed.pos.mx, observed.pos.my) == pytest.approx(elwynn)
    assert observed.pos.corpse == pytest.approx(elwynn)
    assert (client.read()["pos.corpse_mx"], client.read()["pos.corpse_my"]) == pytest.approx(elwynn)
    assert State.model_validate_json(observed.model_dump_json()) == observed


def test_starting_inside_stormwind_uses_the_declared_elwynn_guide_frame():
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    assert graph.coord_zone_id == 12
    assert navigation_frame(graph.coord_zone_id, zone_id("Stormwind"), ZONES) == ELWYNN
    assert navigation_frame(graph.coord_zone_id, zone_id("Durotar"), ZONES) is None
    assert navigation_frame(graph.coord_zone_id, None, ZONES) is None


def test_same_frame_retains_measured_map_fractions_exactly():
    raw = (0.49931412, 0.4211685)
    client, _values = client_in("Elwynn", raw)
    assert client.position() == raw
    assert (client.state().pos.mx, client.state().pos.my) == raw


@pytest.mark.parametrize("region", ["Unknown map", "Durotar"])
def test_unknown_or_another_continent_cannot_be_reported_arrived(region):
    client, _values = client_in(region, (0.5, 0.5), corpse=(0.5, 0.5))
    assert client.position() is None
    assert client.state().pos.mx is None and client.state().pos.corpse is None
    client.travel.position = lambda: client.position()
    assert not client.approach((0, 0, 0))
    client.query.path.assert_not_called()
    node = Node(id="travel", kind=StepKind.TRAVEL, zone="Elwynn", zone_id=12,
                coord_zone_id=12, pos=(0.5, 0.5))
    graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(node,))
    tracker = Tracker(graph, node.id)
    state = client.state()
    tracker.enter(node.id, state)
    assert tracker.tick(state).event is not Event.ADVANCE


def test_absent_corpse_zero_sentinel_stays_unknown_after_a_zone_transform():
    client, _values = client_in("Stormwind", (0.5, 0.5), corpse=(0, 0))
    assert client.state().pos.corpse is None
    assert client.read()["pos.corpse_mx"] is None


def test_normalized_fraction_can_extend_beyond_rectangle_without_clamping():
    with pytest.raises(ValidationError):
        Pos(mx=1.1, my=0.5)
    pos = Pos(coord_zone_id=12, mx=1.1, my=-0.2, raw_mx=0.5, raw_my=0.5)
    assert Pos.model_validate_json(pos.model_dump_json()) == pos


def test_guide_actions_name_the_coordinate_frame_not_a_false_current_region():
    graph = Graph.load("content/tbc/ally_human_1_12.json")
    node = next(n for n in graph.nodes if n.quest_id == 61 and n.kind is StepKind.QUEST_TURNIN)
    state = seen(pos=Pos(zone="Stormwind", zone_id=zone_id("Stormwind"), coord_zone_id=12,
                         mx=node.pos[0], my=node.pos[1]))
    plan = decide(state, node).decision
    assert plan.params["coord_zone_id"] == 12 and "zone" not in plan.params
    assert verify(plan, state, frozenset({"TURNIN_QUEST"})).ok
    wrong_frame = Decision(goal="bad frame", intent=Intent.REJOIN, skill="TRAVEL_TO",
                           params={"coord_zone_id": 1519}, confidence=1,
                           why="fixture", abort_if=["dead"])
    assert verify(wrong_frame, state, frozenset({"TRAVEL_TO"})).rule == "coordinate_frame"
    wrong = state.model_copy(update={"pos": state.pos.model_copy(update={"coord_zone_id": 1519})})
    from test_live_body import body

    from jev.run.body import LiveBody

    b = body()
    b.graph = graph
    b.client.bounds = SimpleNamespace(area_id=1519)
    b.arm.step_id = node.id
    b.arm.decision = plan
    assert "different map coordinate frames" in LiveBody._parameters(b)
    assert not verify(plan, wrong, frozenset({"TURNIN_QUEST"})).ok


def test_a_replan_starts_at_the_height_of_the_route_that_got_us_here():
    """Measured 23 September: on Northshire Abbey's stone ledge, off the navmesh on both
    sides of the back wall, a start at Marshal McBride's floor height snapped inside and
    every re-plan walked straight into the wall (27 stuck events, 8.2 yards away)."""
    from jev.clients.travel import Outcome
    from jev.guide.path import Path, PathStatus

    client, _values = client_in("Elwynn", (0.49, 0.42))
    here = map_to_world(0.49, 0.42, ELWYNN)
    mcbride = (here[0] + 8.0, here[1] - 3.0, 82.0)
    outside = Path(PathStatus.COMPLETE, ((here[0] - 2.0, here[1], 80.6),
                                         (here[0] - 6.0, here[1] + 10.0, 81.1), mcbride))
    asked = []

    def path(map_id, start, end):
        asked.append(start[2])
        return outside

    client.query.path = path
    client.travel.position = lambda: (0.49, 0.42)

    def follow(route, *, timeout_s, replan, memory=None):
        replan((0.49, 0.42))
        return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                               stuck_events=1, detail="")

    client.travel.follow = follow
    assert client.approach(mcbride)
    assert asked == [82.0, 80.6], "the re-plan borrowed the destination's height"


def test_a_start_height_that_snaps_onto_the_wrong_floor_is_not_the_plan():
    """Beside Northshire's merchant wagons the destination's height put the start on a
    wagon: five yards of partial path, walked and called arrival, 160 yards short (run
    20260924T013702-7f5692). Four yards lower the mesh has the whole route."""
    from jev.clients.travel import Outcome
    from jev.guide.path import Path, PathStatus

    client, _values = client_in("Elwynn", (0.4766, 0.4137))
    here = map_to_world(0.4766, 0.4137, ELWYNN)
    rib = (here[0] + 157.0, here[1] - 43.0, 86.4)
    island = Path(PathStatus.PARTIAL, ((here[0], here[1], 85.6), (here[0] + 4.3, here[1] + 1.6, 86.4)))
    ground = Path(PathStatus.COMPLETE, ((here[0], here[1], 81.9), (here[0] + 90.0, here[1] - 20.0, 81.6),
                                        rib))
    asked = []

    def path(map_id, start, end):
        asked.append(round(start[2], 1))
        return island if start[2] > 84.0 else ground

    client.query.path = path
    client.travel.position = lambda: (0.4766, 0.4137)
    walked = []

    def follow(route, *, timeout_s, replan, memory=None):
        walked.append(route)
        return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                               stuck_events=0, detail="")

    client.travel.follow = follow
    assert client.approach(rib)
    assert walked == [ground] and asked == [86.4, 83.4]
    asked.clear()
    client.approach(rib)
    assert asked == [81.9], "the next plan from here starts on the ground the walk ended on"
