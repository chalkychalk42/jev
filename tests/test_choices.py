"""Choices learned from their outcomes (`jev.learn.choices`, DECISIONS V158)."""

from __future__ import annotations

import json
import random

from jev.learn.choices import (
    Arm,
    ChoiceLog,
    ChoiceMemory,
    Stations,
    backfill_hunts,
    draw,
    objective_key,
    pooled,
    station_key,
)


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def test_records_persist_and_reload(tmp_path):
    memory = ChoiceMemory(tmp_path / "choices.json", clock=Clock())
    memory.record("hunt.station", "creature:1@10,20", True, 12.0)
    memory.record("hunt.station", "creature:1@10,20", False, 8.0)
    again = ChoiceMemory(tmp_path / "choices.json")
    arm = again.arms("hunt.station")["creature:1@10,20"]
    assert (arm.tries, arm.wins, arm.seconds) == (2, 1, 20.0)
    assert again.arms("hunt.station", "creature:2@") == {}


def test_the_pooled_rate_sees_one_of_each():
    assert pooled([]) == 0.5
    assert pooled([Arm(tries=8, wins=0), Arm(tries=2, wins=2)]) == 3 / 12


def test_a_draw_follows_the_record_and_an_unproven_option_the_pool():
    rng = random.Random(7)
    good, bad = Arm(tries=10, wins=8), Arm(tries=10, wins=0)
    goods = [draw(good, 0.3, rng) for _ in range(500)]
    bads = [draw(bad, 0.3, rng) for _ in range(500)]
    news = [draw(None, 0.3, rng) for _ in range(2000)]
    assert sum(goods) / 500 > 0.6 and sum(bads) / 500 < 0.1
    assert abs(sum(news) / 2000 - 0.3) < 0.05, "an unproven option draws the pooled rate"


def test_stations_that_paid_off_come_first_and_the_barren_sink():
    """Wolves Across the Border: its best spawn points found a wolf 3 of 5 and 4 of 9 times,
    while eight points visited three times or more never had one."""
    memory = ChoiceMemory(clock=Clock())
    good, barren, fresh = (0.0, 0.0), (40.0, 0.0), (80.0, 0.0)
    for won in (True, True, True, False, False):
        memory.record("hunt.station", station_key("creature:9", good), won, 20.0)
    for _ in range(8):
        memory.record("hunt.station", station_key("creature:9", barren), False, 20.0)
    firsts, lasts = [], []
    for seed in range(200):
        order = Stations(memory, "hunt.station", "creature:9",
                         rng=random.Random(seed)).order([barren, fresh, good])
        firsts.append(order[0])
        lasts.append(order[-1])
    assert firsts.count(good) > 140, "the proven station leads most laps"
    assert lasts.count(barren) > 140, "the barren one is walked last"
    assert firsts.count(fresh) > 10, "an unproven station still gets its turn"


def test_a_lap_keeps_the_tours_order_unless_a_record_says_otherwise():
    """V252: ordered by draws alone, the mage's first station was on average 90 yards further
    than the nearest, on records that were noise."""
    memory = ChoiceMemory(clock=Clock())
    tour = [(float(40 * i), 0.0) for i in range(6)]
    firsts = [Stations(memory, "hunt.station", "creature:9",
                       rng=random.Random(seed)).order(tour)[0] for seed in range(300)]
    assert firsts.count(tour[0]) > firsts.count(tour[5]) * 3, "the tour's first leads"
    for _ in range(8):
        memory.record("hunt.station", station_key("creature:9", tour[4]), True, 20.0)
    for _ in range(4):
        for barren in tour[:2]:
            memory.record("hunt.station", station_key("creature:9", barren), False, 20.0)
    firsts = [Stations(memory, "hunt.station", "creature:9",
                       rng=random.Random(seed)).order(tour)[0] for seed in range(300)]
    assert firsts.count(tour[4]) > 100, "a proven station leads from further along"
    assert firsts.count(tour[0]) < firsts.count(tour[4]) / 3, "the barren first one does not"


def test_a_visit_is_timed_and_recorded_as_it_went(tmp_path):
    clock = Clock(10.0)
    memory = ChoiceMemory(clock=Clock())
    log = ChoiceLog(tmp_path / "choices.jsonl", clock=Clock())
    stations = Stations(memory, "hunt.station", "creature:9", log=log, clock=clock,
                        rng=random.Random(1))
    stations.order([(0.0, 0.0), (5.0, 5.0)])
    stations.arrive((0.0, 0.0))
    clock.now = 40.0
    stations.leave(True)
    stations.arrive((5.0, 5.0))
    clock.now = 55.0
    stations.arrive((0.0, 0.0))                  # moved on without saying: that one found nothing
    stations.leave(False)
    arms = memory.arms("hunt.station")
    assert (arms["creature:9@0,0"].tries, arms["creature:9@0,0"].wins) == (2, 1)
    assert arms["creature:9@0,0"].seconds == 30.0
    assert (arms["creature:9@5,5"].tries, arms["creature:9@5,5"].wins) == (1, 0)
    assert arms["creature:9@5,5"].seconds == 15.0
    rows = [json.loads(line) for line in (tmp_path / "choices.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["choice", "outcome", "outcome", "outcome"]
    assert rows[1]["won"] is True and rows[1]["seconds"] == 30.0


def test_a_hunt_is_learned_under_its_creature_else_its_step():
    assert objective_key(2864, "step_a") == "creature:2864"
    assert objective_key(None, "grind_elwynn_9_11") == "step:grind_elwynn_9_11"


def _evidence(run, rows):
    run.mkdir(parents=True)
    (run / "executions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_earlier_runs_are_counted_once_from_their_evidence(tmp_path):
    runs = tmp_path / "runs"
    _evidence(runs / "r1", [
        {"operation": "hunt.request", "phase": "event", "t": 0.0, "step_id": "s",
         "data": {"wanted_name_id": 9}},
        {"operation": "hunt.approach", "phase": "begin", "t": 1.0, "data": {"destination": [0, 0, 5]}},
        {"operation": "fight", "phase": "end", "t": 20.0, "code": "killed"},
        {"operation": "hunt.approach", "phase": "begin", "t": 30.0, "data": {"destination": [40, 0, 5]}},
        {"operation": "fight", "phase": "end", "t": 40.0, "code": "no_target"},
        {"operation": "hunt", "phase": "end", "t": 50.0},
    ])
    _evidence(runs / "r2", [
        {"operation": "hunt.request", "phase": "event", "t": 0.0, "step_id": "s",
         "data": {"wanted_name_id": 9}},
        {"operation": "hunt.approach", "phase": "begin", "t": 1.0, "data": {"destination": [0, 0, 5]}},
        {"operation": "hunt", "phase": "end", "t": 9.0},
    ])
    (runs / "r2" / "choices.jsonl").write_text("")      # logged its own: never counted here
    memory = ChoiceMemory(tmp_path / "choices.json")
    assert backfill_hunts(runs.iterdir(), memory) == 2
    arms = memory.arms("hunt.station")
    assert (arms["creature:9@0,0"].tries, arms["creature:9@0,0"].wins,
            arms["creature:9@0,0"].seconds) == (1, 1, 29.0)
    assert (arms["creature:9@40,0"].wins, arms["creature:9@40,0"].seconds) == (0, 20.0)
    assert backfill_hunts(runs.iterdir(), ChoiceMemory(tmp_path / "choices.json")) == 0, \
        "a run is counted once"
