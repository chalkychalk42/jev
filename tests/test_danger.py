"""Where the character keeps being attacked, learned from runs (`jev.learn.danger`, V161)."""

from __future__ import annotations

import json
import time

from jev.guide.coords import ZoneBounds
from jev.learn.danger import (
    CELL_YARDS,
    FRESH_S,
    DangerMap,
    cell_of,
    centre_of,
    count_run,
    count_runs,
)

# Runs are counted once finished (V326): a count at this time sees every run written in the
# test as finished.
LATER = time.time() + 2 * FRESH_S

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
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=LATER) == 2
    assert danger.counted == {"r1", "r2"}, "a run with no ticks waits for them"
    again = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs.iterdir(), again, {12: FIELD}.get, now=LATER) == 0
    assert again.cells[cell_of(0, 100, 100)] == {"12": [22.0, 2, 0]}


def test_a_prior_marks_a_camp_the_character_has_not_walked_at_a_discount(tmp_path):
    """V290: the hive's map of the same cells is read beside the character's own. A camp
    only the hive was attacked in is kept clear of; its seconds and attacks count a quarter
    of the character's own, ten minutes' worth at most, and they are never saved."""
    from jev.learn.danger import PRIOR_CAP_S, PRIOR_WEIGHT

    hive = DangerMap(tmp_path / "prior.json")
    camp, field = cell_of(0, 100, 100), cell_of(0, 400, 400)
    hive.add(camp, 5, seconds=600.0, attacks=40)
    hive.add(field, 5, seconds=36000.0, attacks=12)
    hive.save()
    danger = DangerMap(tmp_path / "danger.json", prior=tmp_path / "prior.json")
    assert [(x, y) for x, y, _ in danger.hot(0, 5)] == [centre_of(camp)[1:]]
    assert danger._totals(camp, 5)[:2] == (600.0 * PRIOR_WEIGHT, 40 * PRIOR_WEIGHT)
    seconds, attacks, _ = danger._totals(field, 5)
    assert seconds == PRIOR_CAP_S and attacks == 12 * PRIOR_CAP_S / 36000.0
    danger.add(field, 5, seconds=5.0)
    danger.save()
    assert DangerMap(tmp_path / "danger.json").cells == {field: {"5": [5.0, 0, 0]}}
    assert DangerMap(prior=tmp_path / "none.json").hot(0, 5) == []


def test_a_danger_prior_that_cannot_be_read_is_no_prior(tmp_path):
    """V290: a broken prior file leaves the character's own map as it is."""
    (tmp_path / "prior.json").write_text('{"format": 1, "cells": {"0:1,1": {"5": ["x"]}}}')
    assert DangerMap(prior=tmp_path / "prior.json").lent == {}
    (tmp_path / "prior.json").write_text("[]")
    assert DangerMap(prior=tmp_path / "prior.json").lent == {}


def _run(runs, name, *, ticks=True):
    (runs / name).mkdir(parents=True)
    if ticks:
        _write(runs / name / "ticks.jsonl", [_tick(t, 100, 100) for t in range(0, 12)])
        _write(runs / name / "executions.jsonl", _fight(11.0, 15.0, combat=True))


def test_runs_begun_two_hours_before_the_newest_are_settled_and_not_looked_at_again(
        tmp_path, monkeypatch):
    """V326: the hive's map named 34,182 runs and every session start sorted its 31,770 run
    directories. A run's name begins with its start; those begun `SETTLE_S` before the
    newest seen are settled, and neither named in the map nor looked at again."""
    import jev.learn.danger as danger_module
    from jev.learn.danger import runs_in

    runs = tmp_path / "runs"
    for name in ("20260929T080000-aaaaaa", "20260929T095959-bbbbbb", "20260929T100001-cccccc",
                 "20260929T120000-dddddd", "r1"):
        _run(runs, name)
    _run(runs, "20260929T070000-eeeeee", ticks=False)       # a session that never played
    _run(runs, "20260929T115000-ffffff", ticks=False)       # one not playing yet
    danger = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs_in(runs, danger), danger, {12: FIELD}.get, now=LATER) == 5
    assert danger.through == "20260929T100000"
    assert danger.counted == {"20260929T100001-cccccc", "20260929T120000-dddddd", "r1"}
    again = DangerMap(tmp_path / "danger.json")
    assert (again.through, again.counted) == (danger.through, danger.counted)
    assert sorted(r.name for r in runs_in(runs, again)) == [
        "20260929T100001-cccccc", "20260929T115000-ffffff", "20260929T120000-dddddd", "r1"]
    assert runs_in(runs, again, skip="r1")[-1].name != "r1"
    assert runs_in(tmp_path / "none", again) == []

    # Only the run not yet playing is opened, and counted once it has ticks; one settled
    # without ticks stays as it was.
    opened = []
    real = danger_module.count_run
    monkeypatch.setattr(danger_module, "count_run",
                        lambda ticks, *rest: opened.append(ticks.parent.name) or real(ticks, *rest))
    for name in ("20260929T070000-eeeeee", "20260929T115000-ffffff"):
        _write(runs / name / "ticks.jsonl", [_tick(t, 100, 100) for t in range(0, 12)])
    assert count_runs(runs.iterdir(), again, {12: FIELD}.get, now=LATER) == 0
    assert opened == ["20260929T115000-ffffff"]
    assert again.cells[cell_of(0, 100, 100)] == {"12": [66.0, 5, 0]}


def test_the_map_is_saved_only_when_a_run_was_counted(tmp_path):
    """V326: every session start rewrote the map, the hive's 2.5 MB, new runs or none."""
    runs = tmp_path / "runs"
    _run(runs, "20260929T100000-aaaaaa")
    danger = DangerMap(tmp_path / "danger.json")
    count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=LATER)
    (tmp_path / "danger.json").unlink()
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=LATER) == 0
    assert not (tmp_path / "danger.json").exists()
    _run(runs, "20260929T101500-bbbbbb")
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=LATER) == 1
    assert (tmp_path / "danger.json").exists()


def test_a_map_from_before_the_mark_is_read_by_its_names_and_then_settled(tmp_path):
    """V326: a file of V161-V325 has no `through`; its runs are known by name, counted
    never again, and its old names leave it at the next save."""
    runs = tmp_path / "runs"
    for name in ("20260928T100000-aaaaaa", "20260929T100000-bbbbbb", "20260929T110000-cccccc"):
        _run(runs, name)
    (tmp_path / "danger.json").write_text(json.dumps(
        {"format": 1, "cells": {cell_of(0, 100, 100): {"12": [22.0, 2, 0]}},
         "counted": ["20260928T100000-aaaaaa", "20260929T100000-bbbbbb", "20260927T000000-gone00"]}))
    danger = DangerMap(tmp_path / "danger.json")
    assert danger.through == ""
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=LATER) == 1
    saved = json.loads((tmp_path / "danger.json").read_text())
    assert saved["format"] == 1 and saved["through"] == "20260929T090000"
    assert saved["counted"] == ["20260929T100000-bbbbbb", "20260929T110000-cccccc"]
    assert saved["cells"][cell_of(0, 100, 100)] == {"12": [33.0, 3, 0]}


def _age(run, now, seconds):
    """Every file of `run` last written `seconds` before `now`."""
    import os
    for path in run.iterdir():
        os.utime(path, (now - seconds, now - seconds))


def test_a_run_still_being_played_is_counted_whole_once_finished(tmp_path):
    """V326: the hive counted others' runs while they were played, and never again, so its
    map had 9,457 cells where 2,400 finished runs give 18,619. A run written to within
    `FRESH_S` is left uncounted and unnamed, and the mark stays behind it however new the
    newest run is; once finished it is counted whole."""
    now = 1_790_700_000.0
    runs = tmp_path / "runs"
    _run(runs, "20260929T100000-aaaaaa")
    _run(runs, "20260929T101500-bbbbbb")                  # a long session, still playing
    _run(runs, "20260929T130000-cccccc")
    _age(runs / "20260929T100000-aaaaaa", now, FRESH_S + 1)
    _age(runs / "20260929T101500-bbbbbb", now, 60)
    _age(runs / "20260929T130000-cccccc", now, FRESH_S + 1)
    danger = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs.iterdir(), danger, {12: FIELD}.get, now=now) == 2
    assert "20260929T101500-bbbbbb" not in danger.counted
    assert danger.through == "20260929T101459", "behind the run being played, not 11:00"
    assert danger.counted == {"20260929T130000-cccccc"}, "10:00 settled; 10:15 not counted"
    # More ticks, then the session ends: counted with all of them, and the mark moves on.
    _write(runs / "20260929T101500-bbbbbb" / "ticks.jsonl",
           [_tick(t, 100, 100) for t in range(0, 23)])
    _age(runs / "20260929T101500-bbbbbb", now, 0)
    later = now + FRESH_S
    again = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs.iterdir(), again, {12: FIELD}.get, now=later) == 1
    assert again.cells[cell_of(0, 100, 100)] == {"12": [44.0, 3, 0]}
    assert again.through == "20260929T110000"
    assert count_runs(runs.iterdir(), again, {12: FIELD}.get, now=later) == 0


def test_the_live_bots_crashed_session_is_counted_at_a_later_start(tmp_path):
    """V326: a live session counts its earlier runs at its start, its own left out. One that
    crashed wrote no end, as none does: it is counted at the first start 20 minutes on."""
    from jev.learn.danger import runs_in

    now = 1_790_700_000.0
    runs = tmp_path / "runs"
    _run(runs, "20260929T100000-aaaaaa")                  # crashed at its last tick
    _run(runs, "20260929T100200-bbbbbb", ticks=False)     # this session's, starting
    _age(runs / "20260929T100000-aaaaaa", now, 90)
    danger = DangerMap(tmp_path / "danger.json")
    assert count_runs(runs_in(runs, danger, skip="20260929T100200-bbbbbb"), danger,
                      {12: FIELD}.get, now=now) == 0
    assert danger.counted == set() and danger.through < "20260929T100000"
    assert count_runs(runs_in(runs, danger, skip="20260929T120000-cccccc"), danger,
                      {12: FIELD}.get, now=now + FRESH_S) == 1
    assert danger.counted == {"20260929T100000-aaaaaa"}


def test_a_map_is_rebuilt_from_the_finished_runs(tmp_path):
    """V326: `rebuild` counts a map afresh, for one counted from runs being played."""
    from jev.learn.danger import rebuild

    now = 1_790_700_000.0
    runs = tmp_path / "runs"
    for name in ("20260929T100000-aaaaaa", "20260929T101000-bbbbbb", "20260929T102000-cccccc"):
        _run(runs, name)
        _age(runs / name, now, FRESH_S + 1)
    _age(runs / "20260929T102000-cccccc", now, 5)
    (tmp_path / "danger.json").write_text(json.dumps(
        {"format": 1, "cells": {cell_of(0, 100, 100): {"12": [3.0, 0, 0]}},
         "counted": ["20260929T100000-aaaaaa", "20260929T102000-cccccc"]}))
    danger = rebuild(runs.iterdir(), {12: FIELD}.get, tmp_path / "danger.json", now=now)
    saved = DangerMap(tmp_path / "danger.json")
    assert saved.cells == danger.cells == {cell_of(0, 100, 100): {"12": [22.0, 2, 0]}}
    assert saved.counted == {"20260929T100000-aaaaaa", "20260929T101000-bbbbbb"}
