"""Working an objective: the radius, not the pin, and the server's counter, not ours."""

from __future__ import annotations

from jev.clients.fight import Fought
from jev.clients.rest import Rested
from jev.run.hunt import DRY_LOOKS, STATIONS, Hunt, Hunted, stations


class _Fight:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.pressed: list[int] = []
        self.closed = 0
        self.detail = ""

    def run(self, name_id=None, **_):
        out = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return out


class _Rest:
    def __init__(self, outcome=Rested.HEALTHY):
        self.outcome = outcome
        self.detail = ""
        self.calls = 0

    def until(self, *_a, **_k):
        self.calls += 1
        return self.outcome


def _hunt(outcomes, counts, rest=None, approach=None):
    seq = list(counts)
    state = {"i": 0}

    def progress():
        c = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return c

    walked = []
    return Hunt(fight=_Fight(outcomes), rest=rest or _Rest(), read=lambda: {},
                approach=approach or (lambda p: walked.append(p) or True),
                progress=progress, say=lambda _s: None), walked


def test_stations_start_at_the_centre_and_then_ring_it():
    """The node is usually a reasonable place to stand; the ring is for when it is not."""
    posts = stations((100.0, 200.0, 50.0), 30.0)
    assert posts[0] == (100.0, 200.0, 50.0)
    assert len(posts) == STATIONS + 1
    for x, y, z in posts[1:]:
        assert z == 50.0
        reach = ((x - 100.0) ** 2 + (y - 200.0) ** 2) ** 0.5
        assert 15.0 < reach < 30.0, "the ring left the disk the node describes"


def test_the_counter_ends_it_and_nothing_counts_its_own_kills():
    """`quests.o0_have` is the server's tally. A swing that missed, a mob somebody else
    tagged and a kill for a different quest are indistinguishable from the inside."""
    h, _ = _hunt([Fought.KILLED], [(10, 10)])
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DONE
    assert h.fight.calls == 0, "it fought after the objective was already complete"


def test_nothing_to_fight_moves_to_another_station():
    """Standing on the pin and looking harder is how a live run reached 1/10 and reported
    'not visible' twenty times while the camp wandered fifteen yards away."""
    h, walked = _hunt([Fought.NO_TARGET], [(1, 10)])
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=2)
    assert h.moves > 1, "it never left the first station"
    assert len(walked) > 1
    assert h.fight.calls >= DRY_LOOKS


def test_a_station_it_cannot_stand_on_is_not_a_dead_end():
    h, _ = _hunt([Fought.NO_TARGET], [(1, 10)], approach=lambda p: False)
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.UNREACHABLE
    assert h.moves == STATIONS + 1, "it gave up before trying the whole disk"


def test_dying_stops_the_hunt_rather_than_looping():
    h, _ = _hunt([Fought.DIED], [(1, 10)])
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DIED


def test_being_hurt_rests_and_running_out_of_food_stops():
    h, _ = _hunt([Fought.TOO_HURT], [(1, 10)], rest=_Rest(Rested.HEALTHY))
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=1)
    assert h.rest.calls >= 1

    h2, _ = _hunt([Fought.TOO_HURT], [(1, 10)], rest=_Rest(Rested.NO_FOOD))
    assert h2.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.NO_FOOD


def test_an_interrupted_rest_is_not_a_failure():
    """Interrupted means something is already hitting us; the next pass fights it."""
    h, _ = _hunt([Fought.TOO_HURT, Fought.KILLED], [(1, 10), (2, 10), (10, 10)],
                 rest=_Rest(Rested.INTERRUPTED))
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DONE
