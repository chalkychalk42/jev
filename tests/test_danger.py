"""Where the character keeps being attacked, learned from runs (`jev.learn.danger`, V161)."""

from __future__ import annotations

import json

from jev.guide.coords import ZoneBounds
from jev.learn.danger import (
    CELL_YARDS,
    DangerMap,
    cell_of,
    centre_of,
    count_run,
    count_runs,
)

# A 300-yard square zone on map 0: map fractions are world yards / 300.
FIELD = ZoneBounds(area_id=12, map_id=0, left=300.0, right=0.0, top=300.0, bottom=0.0)


def _at(x, y):
    """The map fractions of world point (x, y) in `FIELD`."""
    return {"mx": (300.0 - y) / 300.0, "my": (300.0 - x) / 300.0}


def test_a_cell_is_thirty_yards_and_knows_its_centre():
    assert cell_of(0, 45.0, -10.0) == "0:1,-1"
    assert centre_of("0:1,-1") == (0, 1.5 * CELL_YARDS, -0.5 * CELL_YARDS)


def test_a_camp_attacked_often_is_hot_and_ground_walked_quietly_is_not():
    danger = DangerMap()
    camp, field, brush = cell_of(0, 100, 100), cell_of(0, 400, 400), cell_of(0, 700, 700)
    danger.add(camp, 12, seconds=300.0, attacks=6)
    danger.add(field, 12, seconds=3600.0, attacks=3)
    danger.add(brush, 12, seconds=30.0, attacks=2)       # fast, but too few to say
    hot = danger.hot(0, 12)
    assert [(x, y) for x, y, _ in hot] == [centre_of(camp)[1:]]
    assert hot[0][2] > 0.8, "six attacks in five minutes, shrunk a little toward the map's rate"
    assert danger.hot(1, 12) == [], "another map knows nothing of it"
    assert danger.hot(0, None) == [], "no level read, no spots"


def test_a_camp_the_character_has_outgrown_stops_counting():
    danger = DangerMap()
    camp, field = cell_of(0, 100, 100), cell_of(0, 400, 400)
    danger.add(camp, 8, seconds=300.0, attacks=6)
    danger.add(field, 8, seconds=3600.0, attacks=3)
    danger.add(field, 12, seconds=3600.0, attacks=3)
    assert danger.hot(0, 9), "a level above where it was learned still counts it"
    assert danger.hot(0, 12) == []


def test_a_new_characters_danger_is_not_diluted_by_an_older_ones_walks():
    """The mage at level 3 walks where Testvvi walked at 12, unattacked by what it had
    outgrown: those minutes say nothing of the camp's danger to a level 3."""
    danger = DangerMap()
    camp, field = cell_of(0, 100, 100), cell_of(0, 400, 400)
    danger.add(camp, 3, seconds=120.0, attacks=4)
    danger.add(field, 3, seconds=1800.0, attacks=2)
    danger.add(camp, 12, seconds=3600.0)
    assert [(x, y) for x, y, _ in danger.hot(0, 3)] == [centre_of(camp)[1:]]
    assert danger.hot(0, 12) == []


def test_the_map_is_kept_with_the_runs_it_counted(tmp_path):
    danger = DangerMap(tmp_path / "danger.json")
    danger.add(cell_of(0, 1, 1), 12, seconds=5.0, attacks=1, bad=1)
    danger.counted.add("run-1")
    danger.save()
    again = DangerMap(tmp_path / "danger.json")
    assert again.cells == {"0:0,0": {"12": [5.0, 1, 1]}}
    assert again.counted == {"run-1"}


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _tick(t, x, y, *, combat=False, level=12, zone=12):
    return {"t": t, "state": {"pos": {"coord_zone_id": zone, **_at(x, y)},
                              "vitals": {"combat": combat}, "char": {"level": level}}}


def _fight(t0, t1, *, combat, hp=0.9, code="killed"):
    return [{"operation": "fight", "phase": "begin", "t": t0},
            {"operation": "combat.observed", "phase": "event", "t": t0 + 0.2,
             "data": {"vitals.combat": combat, "vitals.hp": hp}},
            {"operation": "fight", "phase": "end", "t": t1, "code": code}]


def test_a_run_gives_its_time_out_of_combat_and_the_attacks_that_began_there(tmp_path):
    ticks, evidence = tmp_path / "ticks.jsonl", tmp_path / "executions.jsonl"
    camp, road = (100.0, 100.0), (200.0, 200.0)
    rows = [_tick(t, *camp) for t in range(0, 10)]                     # 9 s out of combat
    rows += [_tick(t, *camp, combat=True) for t in range(10, 20)]      # the fight: not exposure
    rows += [_tick(t, *road) for t in range(20, 40)]                   # 19 s + the step from 19
    rows += [_tick(t, *road) for t in range(60, 62)]                   # a gap is not time there
    rows += [_tick(70, *road, zone=999)]                               # an unknown zone: skipped
    _write(ticks, rows)
    _write(evidence,
           _fight(9.5, 19.0, combat=True, hp=0.2)       # attacked at the camp; went badly
           + _fight(21.0, 25.0, combat=True)            # an add, chained on: not a fresh attack
           + _fight(33.0, 36.0, combat=False))          # the character's own pull
    danger = DangerMap()
    assert count_run(ticks, evidence, danger, {12: FIELD}.get) == 1
    assert danger.cells[cell_of(0, *camp)] == {"12": [10.0, 1, 1]}
    assert danger.cells[cell_of(0, *road)] == {"12": [20.0, 0, 0]}


def test_each_run_is_counted_once(tmp_path):
    runs = tmp_path / "runs"
    for name in ("r1", "r2"):
        (runs / name).mkdir(parents=True)
        _write(runs / name / "ticks.jsonl", [_tick(t, 100, 100) for t in range(0, 12)])
        _write(runs / name / "executions.jsonl", _fight(11.0, 15.0, combat=True))
    (runs / "r3").mkdir()                                              # nothing recorded yet
    danger = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get) == 2
    assert danger.counted == {"r1", "r2"}, "a run with no ticks waits for them"
    again = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs.iterdir(), again, {12: FIELD}.get) == 0
    assert again.cells[cell_of(0, 100, 100)] == {"12": [22.0, 2, 0]}
