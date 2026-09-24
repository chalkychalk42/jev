"""Flights: which node is which, when a flight beats the walk, and taking one."""

from jev.clients.taxi import Flew, TaxiDesk
from jev.world.taxi import Node, flight, load_nodes, save_node, visited
from jev.world.vendor import FlightMaster

THOR = FlightMaster(523, "Thor", 0, (-10628.3, 1037.3, 34.2), "I need a ride.")
ARIENA = FlightMaster(931, "Ariena Stormfeather", 0, (-9435.2, -2234.9, 69.2),
                      "Show me where I can fly.")
LAKESHIRE = Node(4242, "Ariena Stormfeather", ARIENA.world)
SENTINEL = Node(1717, "Thor", THOR.world)


def test_a_visited_node_is_remembered_by_the_name_painted_where_it_stands(tmp_path):
    path = tmp_path / "character.taxi.json"
    assert load_nodes(path) == {}
    save_node(path, LAKESHIRE)
    save_node(path, SENTINEL)
    assert load_nodes(path) == {4242: LAKESHIRE, 1717: SENTINEL}
    assert visited((-9436.0, -2235.0, 69.0), load_nodes(path))
    assert not visited((-8835.8, 490.1, 109.7), load_nodes(path)), "Stormwind: never visited"


def test_the_redridge_crossing_is_flown_once_both_ends_are_known():
    """Sentinel Hill to Lakeshire walks 4,000 yards and more; from Thor's side of the hill
    it is a short walk, a flight and a short walk."""
    here, lakeshire_quest = (-10600.0, 1000.0), (-9300.0, -2150.0)
    plan = flight(here, lakeshire_quest, {4242: LAKESHIRE}, [THOR, ARIENA])
    assert plan == (THOR, LAKESHIRE)
    assert flight(here, lakeshire_quest, {}, [THOR, ARIENA]) is None, "nowhere known to land"
    assert flight(here, (-10500.0, 800.0), {4242: LAKESHIRE}, [THOR]) is None, "a short walk"
    far = (-10000.0, 400.0)
    assert flight(far, lakeshire_quest, {4242: LAKESHIRE}, [THOR]) is None, \
        "no flight master near the start"


def _paints(nodes, *, taxi=True):
    """The strip painting an open map, one node a paint, round and round."""
    i = 0
    while True:
        index = i % len(nodes) + 1
        name_id, kind, x, y = nodes[index - 1]
        yield {"ui.taxi": taxi, "taxi.total": len(nodes), "taxi.index": index,
               "taxi.name_id": name_id, "taxi.type": kind, "taxi.x": x, "taxi.y": y,
               "flags.on_taxi": False, "ui.modal": False}
        i += 1


class _Hid:
    def __init__(self):
        self.clicks, self.taps = [], []

    def click(self, x, y, **kw):
        self.clicks.append((x, y))
        return True

    def tap(self, key):
        self.taps.append(key)
        return True


def _desk(paints, hid, *, flying=None):
    now = [0.0]
    stream = iter(paints)
    state = {"clicked_at": None}

    def read():
        values = next(stream)
        if hid.clicks and flying is not None:
            elapsed = now[0] - (state["clicked_at"] or now[0])
            state["clicked_at"] = state["clicked_at"] or now[0]
            values = {**values, "flags.on_taxi": flying(elapsed)}
        return values

    def sleep(seconds):
        now[0] += seconds

    return TaxiDesk(hid, read, (0, 0), (1600, 900), clock=lambda: now[0], sleep=sleep)


def test_the_flight_is_one_click_on_the_node_and_ends_on_landing():
    hid = _Hid()
    nodes = [(1717, 1, 0.3, 0.5), (4242, 2, 0.6, 0.25), (9999, 0, None, None)]
    desk = _desk(_paints(nodes), hid, flying=lambda s: 0.5 < s < 40)
    assert desk.fly(4242, flight_s=120) is Flew.LANDED
    assert hid.clicks == [(960, 225)], "the Lakeshire button, where the strip says it is"
    assert desk.here == 1717, "the node painted as here is the one standing here"


def test_a_node_not_on_the_map_as_somewhere_to_fly_is_not_clicked():
    hid = _Hid()
    nodes = [(1717, 1, 0.3, 0.5), (4242, 0, None, None)]
    assert _desk(_paints(nodes), hid).fly(4242, flight_s=120) is Flew.NO_NODE
    assert hid.clicks == []


def test_a_click_that_never_takes_off_says_so():
    hid = _Hid()
    nodes = [(1717, 1, 0.3, 0.5), (4242, 2, 0.6, 0.25)]
    desk = _desk(_paints(nodes), hid, flying=lambda s: False)
    assert desk.fly(4242, flight_s=120) is Flew.NOT_TAKEN


def test_a_closed_map_is_no_map_and_is_left_alone():
    hid = _Hid()
    desk = _desk(_paints([(1717, 1, 0.3, 0.5)], taxi=False), hid)
    assert desk.fly(4242, flight_s=120) is Flew.NO_MAP
    desk.close()
    assert hid.taps == [], "nothing open to shut"


# --- the body --------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import Mock  # noqa: E402

from test_live_body import body  # noqa: E402
from test_runtime_records import seen  # noqa: E402

from jev.clients.choose import Chose  # noqa: E402
from jev.clients.interact import Result as Interacted  # noqa: E402
from jev.coach.policy import Context, decide  # noqa: E402

# The body fixture's zone is 100 yards square with its step at world (50, 50).
NEAR_MASTER = FlightMaster(523, "Thor", 0, (60.0, 50.0, 0.0), "I need a ride.")


def _taxi_body(monkeypatch, tmp_path, masters=(NEAR_MASTER,)):
    b = body()
    b.taxi_memory = tmp_path / "character.taxi.json"
    monkeypatch.setattr("jev.run.body.flightmasters", lambda map_id, side: masters)
    return b


def test_an_unvisited_flight_master_near_is_visited_and_its_node_remembered(monkeypatch, tmp_path):
    b = _taxi_body(monkeypatch, tmp_path)
    assert b.discoverable(seen())
    context = Context()
    context.discoverable = lambda state: True
    plan = decide(seen(), context=context)
    assert plan.decision.skill == "DISCOVER_FLIGHT" and plan.rule == "service.discover"

    b.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.GOSSIP), detail="")
    opened = {"map": False}

    def choose(line):
        opened["map"] = line == "I need a ride."
        return Chose.CHOSE

    b.chooser = SimpleNamespace(run=Mock(side_effect=choose), detail="")
    paint = iter(_paints([(1717, 1, 0.3, 0.5), (4242, 0, None, None)]))
    b.client.read = lambda: ({**next(paint), "vitals.hp": 1.0} if opened["map"]
                             else {"vitals.hp": 1.0, "ui.taxi": False})
    b.client.hid.tap = lambda key: opened.update(map=False) or True
    result = b._discover(seen())
    assert result.outcome.value == "succeeded", result.detail
    assert load_nodes(b.taxi_memory) == {1717: Node(1717, "Thor", NEAR_MASTER.world)}
    assert not b.discoverable(seen()), "visited now"
    assert opened["map"] is False, "the map was shut"


def test_a_long_walk_flies_its_long_part_then_walks_on(monkeypatch, tmp_path):
    b = _taxi_body(monkeypatch, tmp_path)
    save_node(b.taxi_memory, Node(4242, "Ariena Stormfeather", (5000.0, 5000.0, 0.0)))
    walked = []
    b.client.approach = lambda world, **kw: walked.append(tuple(world)) or True
    b.client.read = lambda: {"vitals.hp": 1.0, "vitals.combat": False, "vitals.dead": False,
                             "vitals.ghost": False}
    b.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.TAXI), detail="")
    flown = []

    class Desk:
        here, detail = 1717, ""

        def __init__(self, *a, **k):
            pass

        def fly(self, name_id, *, flight_s):
            flown.append((name_id, flight_s))
            return Flew.LANDED

        def close(self):
            pass

    monkeypatch.setattr("jev.run.body.TaxiDesk", Desk)
    assert b._approach((5100.0, 5000.0, 0.0))
    assert walked == [NEAR_MASTER.world, (5100.0, 5000.0, 0.0)], "to the flight master, then on"
    assert flown and flown[0][0] == 4242
    assert 1717 in load_nodes(b.taxi_memory), "the node taken off from is remembered too"
