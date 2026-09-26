"""The radio may change maps mid-leg; the measured follower keeps one coordinate frame."""

import math
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
        if start != end:                     # a plan; a point query probes the floors
            asked.append(start[2])
        return outside

    client.query.path = path
    client.travel.position = lambda: (0.49, 0.42)

    def follow(route, *, timeout_s, replan, memory=None, **kw):
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
    at = [(0.4766, 0.4137)]
    client.travel.position = lambda: at[0]
    walked = []

    def follow(route, *, timeout_s, replan, memory=None, **kw):
        walked.append(route)
        return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                               stuck_events=0, detail="")

    client.travel.follow = follow
    client.approach(rib)                 # a walk that went nowhere, whatever it reports
    assert walked == [ground] and asked == [86.4, 83.4]
    asked.clear()
    client.approach(rib)
    assert asked == [81.9], "the next plan from here starts on the ground the walk ended on"
    at[0] = world_to_map(rib[0] - 60.0, rib[1], ELWYNN)
    client.travel.follow = lambda route, **kw: SimpleNamespace(
        outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0, stuck_events=0, detail="")
    assert not client.approach(rib), "arrived at a route's end, 60 yards from the destination"


def test_a_start_far_above_the_destination_is_found_by_widening_the_heights():
    """Echo Ridge Mine's wooden platform stands 44 yards above the grind below it; only a
    start between 88 and 96 was on its mesh (run 20260924T015205-5e57fc)."""
    from jev.clients.travel import Outcome
    from jev.guide.path import Path, PathStatus

    client, _values = client_in("Elwynn", (0.4787, 0.3220))
    here = map_to_world(0.4787, 0.3220, ELWYNN)
    wolves = (here[0] - 900.0, here[1] + 600.0, 43.9)
    nowhere = Path(PathStatus.PARTIAL, ((here[0], here[1], 88.0),))
    route = Path(PathStatus.COMPLETE, ((here[0], here[1], 90.35), wolves))
    client.query.path = lambda map_id, start, end: route if 86.0 <= start[2] <= 97.0 else nowhere
    at = [(0.4787, 0.3220)]
    client.travel.position = lambda: at[0]
    walked = []

    def follow(path, **kw):
        walked.append(path)
        at[0] = world_to_map(wolves[0], wolves[1], ELWYNN)
        return SimpleNamespace(outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0,
                               stuck_events=0, detail="")

    client.travel.follow = follow
    assert client.approach(wolves)
    assert walked == [route]


@pytest.mark.parametrize(("yards", "limit"), [(100.0, 180.0), (1038.0, 1038.0 / 7.0 * 2.0),
                                              (3000.0, 540.0)])
def test_a_walks_limit_grows_with_its_route(yards, limit):
    """A flat 180 s was about the clean time for the 1,038 yards to Gerard Tiller."""
    from jev.clients.travel import Outcome
    from jev.guide.path import Path, PathStatus

    client, _values = client_in("Elwynn", (0.49, 0.42))
    here = map_to_world(0.49, 0.42, ELWYNN)
    end = (here[0] + yards, here[1], 60.0)
    client.query.path = lambda map_id, start, finish: Path(
        PathStatus.COMPLETE, ((here[0], here[1], 60.0), end))
    client.travel.position = lambda: (0.49, 0.42)
    given = []
    client.travel.follow = lambda path, *, timeout_s, **kw: given.append(timeout_s) or SimpleNamespace(
        outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0, stuck_events=0, detail="")
    client.approach(end, timeout_s=180.0)
    assert given == [pytest.approx(limit)]


def inn(here, upper=64.0):
    """A hall with a floor above it and a staircase between, as the Lion's Pride Inn has:
    stairs rising 0.7 a yard over ten yards (solid underneath), the hall at 57 west of them
    and on under the floor above at 64 east of them. A point query answers the surface
    nearest its height."""
    from jev.guide.path import Path, PathStatus

    x0 = here[0]

    def surfaces(x):
        if x < x0 + 1.0:
            return [57.0]
        if x < x0 + 10.0:
            return [57.0 + 0.7 * (x - x0)]
        if x <= x0 + 15.0:
            return [57.0, upper]
        return [57.0]                                # past the balcony: the hall only

    def path(map_id, start, end):
        if start[:2] != end[:2]:
            return Path(PathStatus.COMPLETE, ((start[0], start[1], 57.0), end))
        here_x = start[0]
        # The floor above ends at the balcony: beyond it, its nearest point is its edge.
        candidates = [(here_x, z) for z in surfaces(here_x)]
        if here_x > x0 + 15.0:
            candidates.append((x0 + 15.0, upper))
        x, z = min(candidates, key=lambda c: math.dist((c[0], c[1]), (here_x, start[2])))
        return Path(PathStatus.COMPLETE, ((x, start[1], z),))
    return path


def test_the_height_follows_the_stairs_and_stays_on_the_floor_above():
    """One x and y inside the Lion's Pride Inn is two places. A plan started in the hall
    walked a character round the floor above William Pestle for four minutes (session 95)."""
    client, values = client_in("Elwynn", (0.49, 0.42))
    here = map_to_world(0.49, 0.42, ELWYNN)
    client.query.path = inn(here)
    client._ground = (here[0], here[1], 57.0)
    heights = []
    for dx in (0.0, 3.0, 6.0, 9.0, 12.0, 14.0):     # up the stairs, onto the floor above
        values["pos.mx"], values["pos.my"] = world_to_map(here[0] + dx, here[1], ELWYNN)
        client._tracked_at = -math.inf
        client.position()
        heights.append(round(client._ground[2], 1))
    assert heights == [57.0, 59.1, 61.2, 63.3, 64.0, 64.0]
    values["flags.falling"] = True                  # a jump where it stands
    client._tracked_at = -math.inf
    client.position()
    values["flags.falling"] = False
    client._tracked_at = -math.inf
    client.position()
    assert client._ground[2] == 64.0, "a jump is not a fall to the floor below"
    values["pos.mx"], values["pos.my"] = world_to_map(here[0] + 17.0, here[1], ELWYNN)
    client._tracked_at = -math.inf                  # off the balcony, into the hall
    client.position()
    assert client._ground[2] == 57.0, "beyond the floor's edge the floor is looked for below"


def test_a_plan_starts_on_the_floor_the_character_was_tracked_to():
    from jev.clients.travel import Outcome

    client, values = client_in("Elwynn", (0.49, 0.42))
    here = map_to_world(0.49, 0.42, ELWYNN)
    upstairs = inn(here)
    asked = []

    def path(map_id, start, end):
        asked.append(round(start[2], 1))
        return upstairs(map_id, start, end)

    client.query.path = path
    client._ground = (here[0], here[1], 64.0)
    client.travel.position = lambda: (0.49, 0.42)
    client.travel.follow = lambda route, **kw: SimpleNamespace(
        outcome=Outcome.ARRIVED, remaining_yards=0.0, turns=0, stuck_events=0, detail="")
    client.approach((here[0] + 30.0, here[1], 57.0))
    assert asked[0] == 64.0, "the plan started in the hall below"


def test_on_a_route_up_stairs_over_the_hall_the_height_is_the_routes():
    """The inn's stairs rise over its hall, and the hall is always the nearer surface:
    continuity alone left a character walked up to the trainers on the floor above on
    the hall in 21 of 25 samplings (review, 25 September). On the route, its height wins."""
    from jev.guide.path import Path, PathStatus

    client, values = client_in("Elwynn", (0.49, 0.42))
    here = map_to_world(0.49, 0.42, ELWYNN)
    x0 = here[0]

    def surfaces(x):
        if x < x0 + 1.0:
            return [57.0]
        if x < x0 + 10.0:
            return [57.0, 57.0 + 0.7 * (x - x0)]        # the hall goes on under the stairs
        return [57.0, 64.0]

    def path(map_id, start, end):
        z = min(surfaces(start[0]), key=lambda s: abs(s - start[2]))
        return Path(PathStatus.COMPLETE, ((start[0], start[1], z),))

    client.query.path = path
    client._ground = (x0, here[1], 57.0)
    client._following = ((x0, here[1], 57.0), (x0 + 10.0, here[1], 64.0),
                         (x0 + 14.0, here[1], 64.0))
    heights = []
    for dx in (0.0, 3.0, 6.0, 9.0, 12.0, 14.0):
        values["pos.mx"], values["pos.my"] = world_to_map(x0 + dx, here[1], ELWYNN)
        client._tracked_at = -math.inf
        client.position()
        heights.append(round(client._ground[2], 1))
    assert heights == [57.0, 59.1, 61.2, 63.3, 64.0, 64.0]


@pytest.mark.parametrize(("indoors", "upper", "expected"), [
    (True, 64.0, [57.0, 64.0, 57.0]),   # the hall, then the floor above, then the hall again
    (False, 64.0, [57.0, 57.0, 57.0]),  # outdoors the floor under the field is a mine's
    (True, 82.0, [57.0, 57.0, 57.0]),   # 25 yards up is no storey: a hill over a tunnel
])
def test_blocked_where_a_plan_started_the_re_plan_tries_the_other_floor(indoors, upper,
                                                                         expected):
    """Sessions 110 and 111 began upstairs in the Lion's Pride Inn on plans from the hall
    below, and every re-plan, at the height of the route being followed, walked the same
    hall route into the same upstairs walls. Above Fargodeep Mine a walk to a merchant was
    re-planned from the mine below the field (session 112)."""
    from jev.clients.travel import Outcome

    client, values = client_in("Elwynn", (0.49, 0.42))
    values["pos.indoors"] = indoors
    here = map_to_world(0.49, 0.42, ELWYNN)
    spot = world_to_map(here[0] + 12.0, here[1], ELWYNN)      # the hall, and the floor above
    probe = inn(here, upper)
    starts = []

    def path(map_id, start, end):
        if start[:2] != end[:2]:
            starts.append(round(start[2], 1))
        return probe(map_id, start, end)

    client.query.path = path
    client.travel.position = lambda: spot
    lines = []
    client.on_path = lines.append

    def follow(route, *, timeout_s, replan, memory=None, **kw):
        replan(spot)
        replan(spot)
        return SimpleNamespace(outcome=Outcome.STUCK, remaining_yards=40.0, turns=0,
                               stuck_events=9, detail="")

    client.travel.follow = follow
    assert not client.approach((here[0] - 40.0, here[1] + 5.0, 57.0))
    assert starts == expected
    assert any("planning from the floor at" in line for line in lines) is (expected[1] != 57.0)


def test_walks_are_planned_clear_of_the_learned_danger_at_the_characters_level():
    """V161: the planner asks the danger map for the character's level as read now."""
    from jev.guide.route_memory import DangerAvoidingQuery, RouteMemory

    client, values = client_in("Elwynn", (0.5, 0.5))
    values["char.level"] = 12
    danger = Mock()
    danger.hot.return_value = [(1.0, 2.0, 0.9)]
    with_travel(client, ELWYNN, Mock(), arrival_yards=5, zones=ZONES,
                route_memory=RouteMemory(), danger=danger)
    assert isinstance(client.query, DangerAvoidingQuery)
    assert client.query.hot(0) == [(1.0, 2.0, 0.9)]
    danger.hot.assert_called_once_with(0, 12)


def test_a_walk_wedged_indoors_backs_out_the_way_it_came_in():
    """V230: the mage stood in the corner between William Pestle's barrels and the window of
    the Lion's Pride Inn for four minutes in two sessions, each plan out ending there."""
    client, values = client_in("Elwynn", (0.49, 0.42))
    door = map_to_world(0.49, 0.42, ELWYNN)
    values["pos.indoors"] = False
    client.position()
    walked = [(door[0] + dx, door[1] + dy) for dx, dy in ((0, 4), (0, 8), (4, 8), (8, 8), (9, 8))]
    values["pos.indoors"] = True
    for x, y in walked:
        values["pos.mx"], values["pos.my"] = world_to_map(x, y, ELWYNN)
        client.position()
    routes = []

    def follow(route, **kw):
        routes.append(route)
        values["pos.indoors"] = False               # out of the door
        return SimpleNamespace(outcome=None)

    client.travel.follow = follow
    assert client.back_out() is True
    points = [p[:2] for p in routes[0].points]
    assert points[0] == pytest.approx(walked[-2]), "from the last point kept, 3 yards apart"
    assert points[-1] == pytest.approx(door), "to the last point read outdoors"
    assert [p for p in points[1:-1]] == pytest.approx(list(reversed(walked[:-2])))


def test_no_way_in_known_is_no_way_out():
    """Indoors since the session began, or after a hearthstone's jump: nothing to walk back."""
    client, values = client_in("Elwynn", (0.49, 0.42))
    client.travel.follow = lambda route, **kw: pytest.fail("walked a way in it never saw")
    here = map_to_world(0.49, 0.42, ELWYNN)
    values["pos.indoors"] = True
    for dy in (0, 4, 8):
        values["pos.mx"], values["pos.my"] = world_to_map(here[0], here[1] + dy, ELWYNN)
        client.position()
    assert client.back_out() is False, "began indoors"
    values["pos.indoors"] = False
    client.position()
    values["pos.indoors"] = True
    values["pos.mx"], values["pos.my"] = world_to_map(here[0] + 200, here[1], ELWYNN)
    client.position()
    values["pos.mx"], values["pos.my"] = world_to_map(here[0] + 200, here[1] + 4, ELWYNN)
    client.position()
    assert client.back_out() is False, "a jump is not a way in"


def test_the_way_in_is_kept_without_its_loops():
    """V230: a wedged walk moves about the same few yards for minutes; a trail of every step
    outgrew its limit and lost the door (session 198, 13 minutes in the Lion's Pride Inn)."""
    from jev.run.client import TRAIL_POINTS

    client, values = client_in("Elwynn", (0.49, 0.42))
    door = map_to_world(0.49, 0.42, ELWYNN)
    values["pos.indoors"] = False
    client.position()
    values["pos.indoors"] = True

    def at(dx, dy):
        values["pos.mx"], values["pos.my"] = world_to_map(door[0] + dx, door[1] + dy, ELWYNN)
        client.position()

    for dy in (4, 8, 12):
        at(0, dy)
    for _ in range(TRAIL_POINTS):                   # back and forth in a corner
        at(4, 12)
        at(8, 12)
        at(4, 12)
        at(0, 12)
    assert client._trail_anchored
    assert [p[:2] for p in client._trail] == pytest.approx(
        [door] + [(door[0], door[1] + dy) for dy in (4, 8, 12)])
