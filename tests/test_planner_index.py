"""The planner's memory looked up by place and kept between asks (V325): the same answers as
looking through everything every time, as the earlier code did."""

from __future__ import annotations

import json
import math
import random
import sys
import time

from jev.guide.path import MmapQuery, PathStatus
from jev.guide.route_memory import (
    CAMP_S,
    COMPACT_SLACK_S,
    DANGER_S,
    Danger,
    DangerAvoidingQuery,
    RouteMemory,
    _Near,
    _passes,
    counts_for,
)
from jev.learn.danger import HOT_EVENTS, HOT_FACTOR, DangerMap, centre_of


def _looked_through(memory: RouteMemory, map_id, now, level):
    """`dangers_on` as it was: every death, every time."""
    return [d for d in memory.dangers if d.map_id == map_id
            and (now - d.at < DANGER_S or d.camp(now)) and counts_for(d.level, level)]


def _polyline(rng: random.Random):
    n = rng.choice([1, 2, 2, 3, 5, 12, 40])
    x, y = rng.uniform(-600, 600), rng.uniform(-600, 600)
    points = [(x, y, 0.0)]
    for _ in range(n - 1):
        x, y = x + rng.uniform(-150, 150), y + rng.uniform(-150, 150)
        points.append((x, y, 0.0))
    return tuple(points)


def test_the_places_a_route_passes_are_the_same_looked_up_by_square_as_measured_all():
    rng = random.Random(325)
    for _ in range(400):
        spots = [(rng.uniform(-700, 700), rng.uniform(-700, 700),
                  rng.choice([10.0, 25.0, 35.0, 100.0, rng.uniform(10, 100)]), "why",
                  rng.random() < 0.2) for _ in range(rng.randrange(0, 60))]
        near = _Near(spots)
        for _ in range(10):
            points = _polyline(rng)
            if rng.random() < 0.3 and spots:        # a route that ends right at one's reach
                x, y, reach, *_ = rng.choice(spots)
                points = (*points, (x + reach, y, 0.0))
            every = [s for s in spots if _passes(points, s[0], s[1], s[2])]
            assert near.passed(points) == every
            assert near.first(points) == (every[0] if every else None)
    assert _Near([]).passed(((0.0, 0.0, 0.0),)) == []
    assert _Near([(0.0, 0.0, 10.0)]).passed(()) == [], "no route passes nothing"
    odd = [(math.nan, 0.0, 10.0), (0.0, 0.0, 10.0)]
    assert _Near(odd).passed(((0.0, 5.0, 0.0), (5.0, 5.0, 0.0))) == [odd[1]]
    assert _Near(odd).passed(((math.inf, 5.0, 0.0), (5.0, 5.0, 0.0))) == [
        s for s in odd if _passes(((math.inf, 5.0, 0.0), (5.0, 5.0, 0.0)), *s[:3])]


def test_the_deaths_kept_are_those_looked_through_whenever_asked_and_however_changed():
    rng = random.Random(29)
    memory = RouteMemory()
    base = 1_790_000_000.0
    memory.dangers = [Danger(rng.choice([0, 1]), rng.uniform(-500, 500), rng.uniform(-500, 500),
                             base - rng.uniform(0, 3 * DANGER_S), rng.choice([None, 5, 8, 12]),
                             rng.choice([None, None, base + rng.uniform(-CAMP_S, CAMP_S)]),
                             rng.choice([None, 1, 2]))
                      for _ in range(300)]
    now = base - DANGER_S
    for step in range(3000):
        now += rng.choice([0.0, 0.0005, 1.0, 17.0, 300.0, -40.0])
        map_id, level = rng.choice([0, 1, 2]), rng.choice([None, 4, 5, 8, 12, 20])
        if step % 97 == 0:                          # a death, as the character dies
            memory.died(map_id, (rng.uniform(-500, 500), rng.uniform(-500, 500)), now=now,
                        level=level, who=rng.choice([1, 2]))
        if step % 211 == 0:                         # the hive's merge: the list replaced
            memory.dangers = [*memory.dangers, Danger(0, 1.0, 2.0, now, 5)]
        if step % 307 == 0:                         # one added in place
            memory.dangers.append(Danger(1, 3.0, 4.0, now - 10.0, None))
        assert memory.dangers_on(map_id, now, level) == _looked_through(memory, map_id, now,
                                                                        level)


def test_a_memory_drops_only_the_deaths_no_rule_keeps_when_it_loads_and_saves(tmp_path):
    file = tmp_path / "route-memory.json"
    latest = 1_790_000_000.0
    rows = [{"map_id": 0, "x": float(k), "y": 0.0, "at": latest - ago, "level": 5,
             "camp_until": camp, "who": 1}
            for k, (ago, camp) in enumerate([
                (0.0, None),                                    # the latest: kept
                (DANGER_S - 1.0, None),                         # kept a second more
                (DANGER_S + COMPACT_SLACK_S - 1.0, None),       # past it, within the slack
                (DANGER_S + COMPACT_SLACK_S + 1.0, None),       # past every rule: dropped
                (5 * DANGER_S, latest + 60.0),                  # a camp still held: kept
                (5 * DANGER_S, latest - COMPACT_SLACK_S - 1.0),  # a camp over: dropped
            ])]
    file.write_text(json.dumps({"format": 1, "passages": [], "blocked": [], "dangers": rows}))
    everything = [Danger(**row) for row in rows]
    memory = RouteMemory(file)
    assert [d.x for d in memory.dangers] == [0.0, 1.0, 2.0, 4.0]
    for now in (latest, latest + 1.0, latest + DANGER_S, latest + 10 * DANGER_S):
        assert [(d.x, d.at) for d in memory.dangers_on(0, now, 5)] == [
            (d.x, d.at) for d in everything if now - d.at < DANGER_S or d.camp(now)]
    memory.dangers = everything                     # as the hive's merge brings them back
    memory._save()
    assert [d.x for d in RouteMemory(file).dangers if d.map_id == 0] == [0.0, 1.0, 2.0, 4.0]


def test_a_walk_keeps_clear_of_the_same_deaths_asked_again_as_time_passes():
    now = [1000.0]
    memory = RouteMemory()
    memory.died(0, (100.0, 0.0), now=1000.0, level=8)
    query = DangerAvoidingQuery(None, memory, clock=lambda: now[0], level=lambda: 8)
    assert [s[:2] for s in query._kept(0, (0.0, 0.0, 0.0), (200.0, 0.0, 0.0))] == [(100.0, 0.0)]
    now[0] = 1000.0 + DANGER_S - 0.01
    assert query._kept(0, (0.0, 0.0, 0.0), (200.0, 0.0, 0.0)), "kept to the last"
    now[0] = 1000.0 + DANGER_S
    assert query._kept(0, (0.0, 0.0, 0.0), (200.0, 0.0, 0.0)) == [], "and let go at once"


def _hot_looked_through(danger: DangerMap, map_id, level):
    cells = [c for c in (*danger.cells, *(c for c in danger.lent if c not in danger.cells))
             if c.startswith(f"{map_id}:")]
    seconds = attacks = 0
    for cell in cells:
        s, a, _ = danger._totals(cell, level)
        seconds, attacks = seconds + s, attacks + a
    pooled = attacks / (seconds / 60) if seconds else 0.0
    hot = []
    for cell in cells:
        _, a, _ = danger._totals(cell, level)
        rate = danger.rate(cell, level, pooled)
        if a >= HOT_EVENTS and rate >= HOT_FACTOR * pooled:
            hot.append((*centre_of(cell)[1:], rate))
    return pooled, hot


def test_the_hot_cells_kept_between_asks_are_those_summed_afresh_after_every_change(tmp_path):
    rng = random.Random(161)
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"format": 1, "cells": {
        f"{rng.choice([0, 1])}:{rng.randrange(-9, 9)},{rng.randrange(-9, 9)}": {
            str(rng.randrange(3, 15)): [rng.uniform(10, 900), rng.randrange(0, 9), 0]}
        for _ in range(80)}}))
    danger = DangerMap(prior=prior)
    for step in range(600):
        danger.add(f"{rng.choice([0, 1, 530])}:{rng.randrange(-9, 9)},{rng.randrange(-9, 9)}",
                   rng.randrange(3, 15), seconds=rng.uniform(0, 300),
                   attacks=rng.choice([0, 0, 1, 3]), bad=rng.choice([0, 1]))
        if step == 300:                             # a map replaced whole
            danger.cells = {cell: {k: list(v) for k, v in levels.items()}
                            for cell, levels in danger.cells.items()}
        for _ in range(3):
            map_id, level = rng.choice([0, 1, 530, 7]), rng.randrange(3, 15)
            pooled, hot = _hot_looked_through(danger, map_id, level)
            assert danger.hot(map_id, level) == hot
            assert danger.pooled(map_id, level) == pooled
    assert danger.hot(0, None) == []
    danger.hot(0, 5).clear()
    assert danger.hot(0, 5) == _hot_looked_through(danger, 0, 5)[1], "a copy, each time"


def test_a_sidecar_whose_output_ends_is_gone_at_once_at_every_ask(tmp_path):
    """One reader for the sidecar's life (V325): once its output has ended each ask is told so
    at once, as a read started for each ask was, not after the deadline."""
    script = tmp_path / "sidecar.py"
    script.write_text("import sys, time\nprint('{\"ready\":true}', flush=True)\n"
                      "sys.stdin.readline()\nprint('{\"status\":\"nopath\"}', flush=True)\n"
                      "import os\nos.close(1)\ntime.sleep(30)\n")
    q = MmapQuery(script, tmp_path, launcher=(sys.executable,), timeout_s=5)
    try:
        assert q.path(0, (0.0, 0.0, 0.0), (1.0, 1.0, 0.0)).status is PathStatus.NOPATH
        started = time.monotonic()
        for _ in range(2):
            gone = q.path(0, (0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
            assert gone.status is PathStatus.UNAVAILABLE and "went away" in gone.detail
        assert time.monotonic() - started < 3
    finally:
        procs = list(q._procs.values())
        q.close()
    assert all(p.stdout.closed for p in procs)
