"""A spot looked at once, an area looked empty not walked to again first, and a hunt that
begins where the character stands (V510-V512), each from a hunt the hive recorded."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

from jev.clients.fight import Fought
from jev.clients.rest import Rested
from jev.run.hunt import (
    DRY_LOOKS,
    LOOK_REACH_YARDS,
    LOOKED_STALE_S,
    LOOKS_KEPT,
    Hunt,
    Hunted,
    Looked,
    Place,
    spawn_tour,
)

FIXTURES = Path(__file__).parent / "fixtures"
# The look round the hive pads an empty look to (`hive.senses`, the live client's own): 4.6 s.
LOOK_S = 4.6
RUN_YPS = 6.1                 # the hive's walks, yards a second (8 Oct 04:00-15:30)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _xyz(points) -> list[tuple[float, float, float]]:
    return [(float(p[0]), float(p[1]), 0.0) for p in points]


class _Rest:
    detail = ""

    def until(self, *_a, **_k):
        return Rested.HEALTHY


class _Laps:
    """A station chooser whose laps come in the order given, lap after lap."""

    def __init__(self, *laps, by: str = "local"):
        self.laps = [list(lap) for lap in laps]
        self.by = by

    def order(self, tour):
        return self.laps.pop(0) if self.laps else list(tour)

    def leave(self, *_a):
        pass

    walking = died = arrive = leave


class _World:
    """The hunt's world as the record has it: the character where its last walk ended, a look
    that finds a unit only within plate reach of one still standing, each look and walk taking
    the time the hive's took."""

    def __init__(self, at, now, units=()):
        self.here = (float(at[0]), float(at[1]))
        self.now = now
        self.units = [tuple(u) for u in units]
        self.walked: list[tuple] = []
        self.looks = 0
        self.kills: list[tuple] = []

    def clock(self) -> float:
        return self.now

    def where(self):
        return self.here

    def approach(self, point, stop_short: float = 0.0) -> bool:
        self.walked.append(tuple(point))
        self.now += math.dist(self.here, point[:2]) / RUN_YPS
        self.here = (point[0], point[1])
        return True

    def fight(self):
        world = self

        class _Fight:
            pressed = ()
            closed = heals_landed = heals_ignored = top_ups = top_ups_landed = 0
            detail = ""
            broken = False
            last_plate = killed_name_id = None

            def top_up(self, *_a, **_k):
                return True

            def run(self, name_id=None, **_):
                world.looks += 1
                near = [u for u in world.units if math.dist(u, world.here) <= 20.0]
                if near:
                    world.units.remove(near[0])
                    world.kills.append(near[0])
                    world.now += 15.0
                    return Fought.KILLED
                world.now += LOOK_S
                return Fought.NO_TARGET
        return _Fight()


def _hunt(world: _World, *, need: int = 1, looked: Looked | None = None) -> Hunt:
    h = Hunt(fight=world.fight(), rest=_Rest(), read=lambda: {}, approach=world.approach,
             progress=lambda: (len(world.kills), need), say=lambda _s: None)
    h.clock, h.where, h.looked = world.clock, world.where, looked
    return h


def _seeded(looks) -> Looked:
    looked = Looked()
    for look in looks:
        looked.empty(look["at"], look["t"])
    return looked


def test_the_look_memory_covers_its_reach_for_its_window_and_no_further():
    """V511: a station within `LOOK_REACH_YARDS` of an empty look in the last `LOOKED_STALE_S`
    is covered: the hive's arrivals so covered found something 10-22% of the time, those 30-50
    yards off 37%, farther 44.5%."""
    looked = Looked()
    looked.empty((0.0, 0.0), 1000.0)
    assert looked.covered((LOOK_REACH_YARDS - 1.0, 0.0), 1010.0) == 10.0
    assert looked.covered((LOOK_REACH_YARDS + 1.0, 0.0), 1010.0) is None
    assert looked.covered((5.0, 5.0, 40.0), 1000.0 + LOOKED_STALE_S - 1.0) is not None
    assert looked.covered((5.0, 5.0), 1000.0 + LOOKED_STALE_S) is None, "its respawn is past"
    assert looked.covered((0.0, 0.0), 990.0) is None, "a clock set back proves nothing"
    looked.empty((0.0, 0.0), 1100.0)
    assert looked.covered((0.0, 0.0), 1110.0) == 10.0, "the latest look's age"
    looked.empty(None, 1200.0)
    looked.empty((None, None), 1200.0)
    for n in range(2 * LOOKS_KEPT):
        looked.empty((1000.0 + n, 0.0), 1200.0 + n)
    assert len(looked.looks) == LOOKS_KEPT


def test_an_empty_look_is_not_made_twice_from_the_same_spot_and_a_dry_tour_is_toured_as_before():
    """V510, from hive-727 (a blood elf warlock of 11, Elder Springpaws in Eversong, run
    20261008T090236-548b60): the hunt begun afresh at 09:11, every one of the rib's six stations
    looked at empty in the 300 s before, walked to eight of them in three minutes and looked
    twice at each - 16 looks, none finding anything, 35 s of them second looks - before the step
    failed over for no progress at 09:14. One look a station halves the looks; with every
    station covered nothing is reordered or skipped, so the tour and its end, the disk walked,
    are as they were."""
    fx = _fixture("hunt-looked-springpaw.json")
    stations = _xyz(fx["laps"][0])
    laps = [_xyz(lap) for lap in fx["laps"]]
    recorded = fx["recorded"]
    looks = [r for r in recorded if r["op"] == "fight"]
    walks = [r for r in recorded if r["op"] == "hunt.approach"]
    assert len(looks) == 2 * len(walks), "two looks at every station"
    assert sum(r["code"] == "no_target" for r in looks) == len(looks) - 1, "the last cut short"
    seconds = sum(r["s"] for r in looks[1::2])                 # each station's second look
    world = _World(fx["hunt"]["at"], fx["hunt"]["t"])
    looked = _seeded(fx["looks_before"])
    assert all(looked.covered(p, world.now) is not None for p in stations), "all six covered"
    h = _hunt(world, looked=looked)
    h.stations = _Laps(*laps)
    assert h.run((*fx["hunt"]["centre"][:2], 0.0), 100.0, timeout_s=30,
                 spawns=stations) is Hunted.UNREACHABLE
    assert DRY_LOOKS == 1
    assert world.looks == len(world.walked) == 2 * len(stations), "one look a station"
    assert sorted(world.walked) == sorted(laps[0] + laps[1]), "every station walked, none skipped"
    assert 30.0 < seconds < 40.0, f"the second looks the record spent: {seconds:.1f} s"


def test_a_fight_that_came_to_blows_and_nothing_is_still_looked_for_again():
    """V510: only an empty look walks on at once; a fight that timed out, lost its unit or could
    not reach it looks again where it stands, as before, for what may still be there (96% of the
    hive's looks after a timeout found it)."""
    from jev.run.hunt import DRY_PASSES

    calls = []
    for outcomes in ((Fought.NO_TARGET,), (Fought.TIMEOUT, Fought.NO_TARGET),
                     (Fought.TIMEOUT, Fought.TIMEOUT)):
        world = _World((0.0, 0.0), 0.0)
        h = _hunt(world)
        seq = list(outcomes)
        h.fight.run = lambda name_id=None, seq=seq, **_: (calls.append(1) or
                                                          (seq.pop(0) if seq else Fought.KILLED))
        spawns = [(0.0, 0.0, 0.0), (80.0, 0.0, 0.0)]
        h.run((0.0, 0.0, 0.0), 90.0, timeout_s=5, spawns=spawns)
        # Walked on after the first of `outcomes` that leaves the station: an empty look at
        # once, two fights that came to nothing after the second.
        assert world.walked[1] == (80.0, 0.0, 0.0), outcomes
    assert DRY_PASSES == 2


def test_a_station_an_empty_look_just_covered_waits_behind_one_none_has():
    """V511, from hive-820 (a paladin in Elwynn, run 20261008T065515-c2a2d4, 07:05): by the
    station where it had killed two units it looked twice, found nothing, walked 1.7 s to the
    station 14 yards off and looked twice more - 4 looks, 17 s - before the walk to
    (-9778, -103), where it killed again. That station was 14 yards from the empty look made
    there 0-4 s before; (-9778, -103), the next in the lap, 107. Covered, it waits behind the
    first station of its lap no empty look covers, and is walked to after it, not skipped."""
    fx = _fixture("hunt-looked-elwynn.json")
    stations = _xyz(fx["stations"])
    tour = spawn_tour(stations)
    posts = _xyz(fx["laps"][0]) + _xyz(fx["laps"][1])
    covered_station, fresh_station = (-9773.0, -9.0, 0.0), (-9778.0, -103.0, 0.0)
    recorded = [r for r in fx["recorded"] if r["op"] in ("hunt.approach", "fight")]
    before = []
    for r in recorded:
        if r["op"] == "hunt.approach" and tuple(r["destination"]) == (-9778.2, -102.6):
            break
        before.append(r)
    assert sum(r["op"] == "fight" for r in before) == 4
    assert [tuple(r["destination"]) for r in before if r["op"] == "hunt.approach"] == [(-9773.4, -8.6)]
    for looked in (_seeded(fx["looks_before"]), None):
        world = _World(fx["at"], fx["look_t"] + 0.1, units=[k["at"] for k in fx["kills_after"]])
        place = Place()
        place.begin(tour, list(posts), len(tour))
        place.post, place.at = posts.index(covered_station), world.now
        h = _hunt(world, looked=looked, need=len(fx["kills_after"]) + 10)
        h.place = place
        h.run((-9822.5, -32.3, 0.0), 60.0, timeout_s=10, spawns=stations)
        if looked is None:
            assert world.walked[0] == covered_station, "without the memory, as recorded"
            continue
        assert world.walked[0] == fresh_station, world.walked[:3]
        assert world.kills[0] == tuple(fx["kills_after"][0]["at"]), "killed at its first look"
        left = len(tour) - posts.index(covered_station)          # the lap's posts not yet left
        assert covered_station in world.walked[:left], "walked to later in its lap, not skipped"


def test_a_hunt_begun_afresh_walks_first_to_the_nearest_station_none_has_looked_at():
    """V512 and V511, from hive-722, a warrior in the Barrens (run 20261008T131307-713839,
    13:27): its hunt began 4 yards from a station nobody had looked at, and walked first to the
    draw's first, 60 yards off, covered by an empty look 80 s before, then to its second,
    covered 12 s before - two walks and four empty looks, 33 s - and was walking back to the
    station it began beside when the playhead moved. Begun afresh, a hunt walks first to the
    nearest station no empty look covers; Jev's own pick of where a lap begins stands."""
    fx = _fixture("hunt-looked-barrens.json")
    stations = _xyz(fx["stations"])
    lap = _xyz(fx["laps"][0])
    first_recorded = next(r for r in fx["recorded"] if r["op"] == "hunt.approach")
    assert tuple(first_recorded["destination"]) == (-252.2, -3155.6)
    here, beside = fx["at"], (-192.0, -3139.0, 0.0)
    assert math.dist(here, beside[:2]) < 5.0 < 50.0 < math.dist(here, lap[0][:2])
    world = _World(here, fx["t"])
    looked = _seeded(fx["looks_before"])
    covered = [p for p in lap if looked.covered(p, world.now) is not None]
    assert covered == [lap[0], lap[1]], "the draw's first two, as the record found them"
    h = _hunt(world, looked=looked)
    h.stations = _Laps(*[_xyz(x) for x in fx["laps"]])
    h.run((-254.7, -3167.8, 0.0), 55.0, timeout_s=10, spawns=stations)
    assert world.walked[0] == beside, world.walked[:3]
    first_lap = world.walked[:len(lap)]
    assert set(first_lap) == set(lap), "the lap is the lap"
    uncovered = [p for p in first_lap if p not in covered]
    assert first_lap[:len(uncovered)] == uncovered, "the covered two after every other"
    # Where nothing is covered, the nearest leads a lap the draw ordered, and Jev's own pick
    # of where it begins stands: the look memory is what it does not see, the walk is not.
    for by, first in (("local", beside), ("jev", lap[0])):
        world = _World(here, fx["t"])
        h = _hunt(world, looked=Looked())
        h.stations = _Laps(*[_xyz(x) for x in fx["laps"]], by=by)
        h.run((-254.7, -3167.8, 0.0), 55.0, timeout_s=10, spawns=stations)
        assert world.walked[0] == first, (by, world.walked[:2])
    world = _World(here, fx["t"])
    h = _hunt(world)
    h.where = lambda: None
    h.stations = _Laps(*[_xyz(x) for x in fx["laps"]])
    h.run((-254.7, -3167.8, 0.0), 55.0, timeout_s=10, spawns=stations)
    assert world.walked[:2] == lap[:2], "as recorded"


def test_an_empty_look_is_remembered_where_it_was_made():
    """V511: the hunt records each empty look where the character stood; a kill's look that
    found something records nothing."""
    world = _World((0.0, 0.0), 500.0, units=[(80.0, 0.0)])
    looked = Looked()
    h = _hunt(world, looked=looked)
    spawns = [(0.0, 0.0, 0.0), (80.0, 0.0, 0.0)]
    h.run((0.0, 0.0, 0.0), 90.0, timeout_s=5, spawns=spawns)
    assert world.kills == [(80.0, 0.0)]
    assert [(round(x), round(y)) for _t, x, y in looked.looks] == [(0, 0)]


def test_the_body_keeps_a_look_memory_a_objective_on_its_map():
    """V511: kept across the hunts of one objective; another objective's looks say nothing of
    it, another map's begin afresh, and only the latest `LOOKED_KEPT` objectives are kept."""
    from jev.run.body import LOOKED_KEPT, LiveBody

    body = SimpleNamespace(client=SimpleNamespace(bounds=SimpleNamespace(map_id=0)))
    first = LiveBody._hunt_looked(body, "creature:1@step")
    assert LiveBody._hunt_looked(body, "creature:1@step") is first
    assert LiveBody._hunt_looked(body, "creature:2@step") is not first
    for n in range(LOOKED_KEPT):
        LiveBody._hunt_looked(body, f"creature:{10 + n}@step")
    assert LiveBody._hunt_looked(body, "creature:1@step") is not first, "the oldest let go"
    body.client.bounds.map_id = 1
    assert LiveBody._hunt_looked(body, "creature:10@step").map_id == 1
