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



def test_a_visit_that_ended_in_a_death_counts_as_several_that_paid_nothing(tmp_path):
    """V274: the level 10 mage's deaths were Prowler pairs about once a session (sessions
    244-253); a station where pairs keep killing it is one to stand at less."""
    from jev.learn.choices import DEATH_VISITS

    memory = ChoiceMemory(clock=Clock())
    log = ChoiceLog(tmp_path / "choices.jsonl", clock=Clock())
    stations = Stations(memory, "hunt.station", "creature:9", log=log, clock=Clock(),
                        rng=random.Random(1))
    stations.arrive((0.0, 0.0))
    stations.died()
    stations.died()                               # nothing open any more: nothing more
    arm = memory.arms("hunt.station")["creature:9@0,0"]
    assert (arm.tries, arm.wins) == (DEATH_VISITS, 0)
    rows = [json.loads(line) for line in (tmp_path / "choices.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "death" and rows[-1]["visits"] == DEATH_VISITS


def test_a_death_at_a_station_is_charged_the_rewards_extra_seconds():
    """V310: a death costs `DEATH_S` more than its time, spread over its extra visits; at 0 s
    each they made the station look faster for every death at it."""
    from jev.learn.choices import DEATH_S

    clock = Clock(0.0)
    memory = ChoiceMemory(clock=Clock())
    stations = Stations(memory, "hunt.station", "creature:9", clock=clock, rng=random.Random(1))
    stations.arrive((0.0, 0.0))
    clock.now = 30.0
    stations.died()
    assert memory.arms("hunt.station")["creature:9@0,0"].seconds == 30.0 + DEATH_S


def _visits(memory, key, tries, wins, seconds_each):
    for n in range(tries):
        memory.record("hunt.station", key, n < wins, seconds_each)


def test_payoffs_a_second_order_the_stations_not_payoffs_a_visit():
    """V310: a station that yields 60% of visits at twice the seconds a visit is worse than
    one next door at 50%: 0.03 a second against 0.05. Drawn by the chance alone, the far one
    led."""
    memory = ChoiceMemory(clock=Clock())
    near, far = (10.0, 0.0), (90.0, 0.0)
    _visits(memory, station_key("creature:9", near), 20, 10, 10.0)
    _visits(memory, station_key("creature:9", far), 20, 12, 20.0)
    firsts = [Stations(memory, "hunt.station", "creature:9",
                       rng=random.Random(seed)).order([far, near])[0] for seed in range(300)]
    assert firsts.count(near) > 200, "the near one leads, though second on the tour"


def test_a_never_tried_station_still_gets_its_turn_against_a_fast_one():
    """V310: an untried station draws the pooled rate over the pooled pace."""
    memory = ChoiceMemory(clock=Clock())
    good, fresh = (0.0, 0.0), (40.0, 0.0)
    _visits(memory, station_key("creature:9", good), 10, 5, 15.0)
    for _ in range(10):
        memory.record("hunt.station", station_key("creature:9", (80.0, 0.0)), False, 30.0)
    firsts = [Stations(memory, "hunt.station", "creature:9",
                       rng=random.Random(seed)).order([good, fresh])[0] for seed in range(400)]
    assert 20 < firsts.count(fresh) < 200


def test_a_payoff_draw_shrinks_its_pace_toward_the_pool():
    """V310: one slow try does not decide an option's pace; many do. With no seconds
    recorded anywhere the draw is the chance alone, as before."""
    from jev.learn.choices import pace

    rng = random.Random(3)
    assert pace([]) is None and pace([Arm(tries=3, wins=1)]) is None
    assert pace([Arm(tries=2, seconds=30.0), Arm(tries=2, seconds=10.0)]) == 10.0
    once, often = Arm(tries=1, wins=1, seconds=100.0), Arm(tries=40, wins=40, seconds=4000.0)
    fresh = [draw(None, 0.5, rng, 20.0) for _ in range(3000)]
    ones = [draw(once, 0.5, rng, 20.0) for _ in range(3000)]
    oftens = [draw(often, 0.5, rng, 20.0) for _ in range(3000)]

    def mean(xs):
        return sum(xs) / len(xs)

    assert abs(mean(fresh) - 0.5 / 20.0) < 0.004, "untried: the pooled rate over the pooled pace"
    assert mean(oftens) < 0.011, "forty tries of 100 s: 0.01 a second"
    assert mean(ones) > 0.012, "one try of 100 s is pulled toward the pool's 20 s"
    assert 0.0 <= draw(Arm(tries=3, wins=1), 0.3, rng) <= 1.0


def test_a_visit_is_charged_its_walk_and_a_walk_cut_short_is_charged_to_none():
    """V310: a far station's walk is what it costs over a near one; V252: a walk that never
    arrived says nothing of where it was going."""
    clock = Clock(0.0)
    memory = ChoiceMemory(clock=Clock())
    stations = Stations(memory, "hunt.station", "creature:9", clock=clock, rng=random.Random(1))
    stations.walking()
    clock.now = 25.0                              # the walk there
    stations.arrive((0.0, 0.0))
    clock.now = 40.0
    stations.leave(True)
    stations.walking()                            # cut short: never arrives
    clock.now = 60.0
    stations.walking()
    clock.now = 70.0
    stations.arrive((5.0, 5.0))
    clock.now = 75.0
    stations.leave(False)
    arms = memory.arms("hunt.station")
    assert arms["creature:9@0,0"].seconds == 40.0
    assert arms["creature:9@5,5"].seconds == 15.0, "from the walk that arrived, not the one cut short"


def test_the_recovery_that_pays_off_sooner_is_drawn_though_it_pays_off_less():
    """V310: after a failed routine, the attempt that is done 60% of the time in 20 s beats one
    done 70% of the time in 120 s."""
    from jev.learn.choices import Choice

    memory = ChoiceMemory(clock=Clock())
    for n in range(20):
        memory.record("recover.after_failure", "ACCEPT_QUEST:x@routine", n < 12, 20.0)
        memory.record("recover.after_failure", "ACCEPT_QUEST:x@tutor", n < 14, 120.0)
    picks = [Choice(memory, "recover.after_failure", rng=random.Random(seed))
             .pick("ACCEPT_QUEST:x", ("tutor", "routine")) for seed in range(200)]
    assert picks.count("routine") > 180

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


def test_a_prior_counts_at_a_discount_up_to_a_cap_and_is_never_written(tmp_path):
    """V290: another's record of the same choices (the hive's) is read beside the
    character's own: each of its visits a quarter of one, an option taking at most ten
    visits' worth from it, and the character's own file holding only its own."""
    from jev.learn.choices import PRIOR_CAP, PRIOR_WEIGHT

    hive = ChoiceMemory(tmp_path / "prior.json", clock=Clock())
    for won in (True, True, False, False):
        hive.record("hunt.station", "creature:1@10,20", won, 30.0)
    for _ in range(200):
        hive.record("hunt.station", "creature:1@50,60", False, 10.0)
    memory = ChoiceMemory(tmp_path / "choices.json", clock=Clock(), prior=tmp_path / "prior.json")
    memory.record("hunt.station", "creature:1@10,20", True, 12.0)
    arms = memory.arms("hunt.station")
    near = arms["creature:1@10,20"]
    assert (near.tries, near.wins) == (1 + 4 * PRIOR_WEIGHT, 1 + 2 * PRIOR_WEIGHT)
    assert arms["creature:1@50,60"].tries == PRIOR_CAP, "a few of its own outweigh it"
    assert memory.arms("hunt.station", lent=False).keys() == {"creature:1@10,20"}
    again = ChoiceMemory(tmp_path / "choices.json")
    assert again.arms("hunt.station").keys() == {"creature:1@10,20"}, "the prior is not saved"
    assert ChoiceMemory(tmp_path / "choices.json", prior=tmp_path / "none.json").lent == {}


def test_a_prior_that_cannot_be_read_is_no_prior(tmp_path):
    """V290: the prior is another's file; a broken one leaves the session its own memory."""
    (tmp_path / "prior.json").write_text("{not json")
    memory = ChoiceMemory(tmp_path / "choices.json", prior=tmp_path / "prior.json")
    assert memory.lent == {} and memory.arms("hunt.station") == {}


def test_the_heal_line_with_the_fewest_seconds_a_kill_is_drawn():
    """V311: a line's try is its fight's cycle, a death `DEATH_S` more and no kill. The low
    line whose cycles were shorter but died once in ten costs 69 s a kill against the high
    line's 60: a hundred cycles each tell them apart most of the time, not every time."""
    from jev.clients.fight import HEAL_POINT
    from jev.learn.choices import DEATH_S, Choice

    memory = ChoiceMemory(clock=Clock())
    for n in range(100):
        died = n < 10
        memory.record(HEAL_POINT, "all@0.40", not died, 50.0 + (DEATH_S if died else 0.0))
        memory.record(HEAL_POINT, "all@0.60", True, 60.0)
    picks = [Choice(memory, HEAL_POINT, rng=random.Random(seed)).pick("all", ("0.40", "0.60"))
             for seed in range(300)]
    assert picks.count("0.60") > 225
    fresh = [Choice(memory, HEAL_POINT, rng=random.Random(seed))
             .pick("all", ("0.40", "0.50", "0.60")) for seed in range(300)]
    assert fresh.count("0.50") > 15, "the untried line gets its turn"
