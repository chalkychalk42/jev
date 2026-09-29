"""Where the character gets attacked, learned from every run (DECISIONS V161).

A walk's route is a choice among ways (`route_memory.DangerAvoidingQuery`), and the outcome
that matters on the way is being attacked: 690 fights over three days began with something
attacking a character that had picked no fight, 55 of them went badly, and they cluster -
the camps and the mine mouths ran one to three attacks a minute of walking through them,
open ground next to none. The bot's own pulls were safe (1 bad engagement in 208).

The map keeps, for each 30-yard cell of each world map and each character level, the
seconds spent there out of combat and the attacks that began there. A cell's rate is its
attacks per minute there, shrunk toward the map's own rate at the same levels, and counted
only from one level below the character's to two above (`LEVELS_BELOW`, `LEVELS_ABOVE`): a
camp the character has outgrown stops counting as it levels, and a new character's is not
diluted by an older one's walks past it, unattacked, ten levels later. Cells whose rate is high enough are spots a route keeps clear
of (`hot`), unless the walk begins or ends at one.

Counted from runs' own files (ticks and evidence), each run once, like the hunt stations
(`jev.learn.choices.backfill_hunts`): a run is counted when a later session starts, once it
is finished (`FRESH_S`). Runs begun `SETTLE_S` or more before the newest seen, and before any
still being played, are settled (`through`), and only newer ones are looked at (V326).
"""

from __future__ import annotations

import bisect
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

from jev.persist import atomic_json

FORMAT = 1
CELL_YARDS = 30.0
# Consecutive ticks further apart than this are a gap in the record, not time spent there.
MAX_TICK_GAP_S = 5.0
# A fight that begins within this of the last one's end is that one continued (an add),
# not a fresh attack on the way.
CHAINED_S = 5.0
# Below this health a fight went badly (as in `jev.clients.fight.BAD_FIGHT_HP`'s sense).
BAD_HP = 0.25
# The pooled rate a cell starts from, worth this many minutes of its own.
PRIOR_MINUTES = 2.0
# The levels a character's danger is counted from, around its own.
LEVELS_BELOW = 1
LEVELS_ABOVE = 2
# A route keeps clear of a cell attacked at least this many times the map's own rate, on at
# least this many attacks. Held out, trained on the earlier 70% of runs: the cells it called
# hot were attacked 1.55 times a minute in the later runs, the rest 0.71 (25 September).
HOT_FACTOR = 2.0
HOT_EVENTS = 3
# Another's map of the same cells, read beside the character's own and never written (V290):
# the hive's. Each of its seconds and attacks counts as `PRIOR_WEIGHT` of the character's
# own, and a cell takes at most `PRIOR_CAP_S` seconds' worth from it at a level, so a few
# minutes of the character's own outweigh it. Without the file, nothing changes.
PRIOR_WEIGHT = 0.25
PRIOR_CAP_S = 600.0
# A run's name begins with its start, local time to the second (`jev.learn.episode.Recorder`:
# 20260929T101904-abc123): every one of the hive's 31,770 and the live bot's 497 on 29 Sep.
# Runs begun this long before the newest run seen are settled: counted, or never to be, as a
# run's first tick is written minutes after its directory (8 of the live bot's runs have
# none: sessions that stopped before playing, 24-26 Sep). They are not looked at again, and
# the map keeps no name of one (`DangerMap.through`). Two hours is more than a clock set
# back an hour in autumn or WSL's 23 s steps (28 Sep) can put a new run's name behind.
SETTLE_S = 2 * 3600.0
# A run is still being played while any of its files was written this recently: it is left
# uncounted, to be counted whole at a later start. A run writes no record of its end
# (`Recorder.close` only closes its files), and a crashed one could not; the hive's
# `calibrate.FRESH_S` waits as long for the same reason. The hive counted every run in
# `var/runs` at each of its bots' starts, others' being played among them, and never again:
# its map had 9,457 cells on 29 Sep, where 2,400 of its finished runs alone give 18,619.
FRESH_S = 20 * 60.0
RUN_FILES = ("ticks.jsonl", "executions.jsonl", "manifest.json")
STAMP_FORMAT = "%Y%m%dT%H%M%S"
_STAMP = re.compile(r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])T"
                    r"(?:[01]\d|2[0-3])[0-5]\d[0-5]\d")


def _stamp(name: str) -> str | None:
    """The start a run's name begins with (`STAMP_FORMAT`), or None for one without."""
    return name[:15] if _STAMP.match(name) else None


def cell_of(map_id: int, x: float, y: float) -> str:
    return f"{map_id}:{math.floor(x / CELL_YARDS)},{math.floor(y / CELL_YARDS)}"


def centre_of(cell: str) -> tuple[int, float, float]:
    map_id, _, rest = cell.partition(":")
    cx, cy = (int(v) for v in rest.split(","))
    return int(map_id), (cx + 0.5) * CELL_YARDS, (cy + 0.5) * CELL_YARDS


class DangerMap:
    """Seconds out of combat and attacks begun, per cell and character level, as JSON."""

    def __init__(self, file: str | Path | None = None, *, prior: str | Path | None = None):
        self.file = Path(file) if file is not None else None
        self._lock = threading.Lock()
        # cell -> level -> [seconds, attacks, bad attacks]
        self.cells: dict[str, dict[str, list[float]]] = {}
        self.counted: set[str] = set()
        # Every run begun at or before this (`STAMP_FORMAT`) is settled (`SETTLE_S`), and
        # `counted` names only later ones and runs whose names carry no start. The hive's map
        # named 34,182 runs on 29 Sep, a third of its 2.5 MB rewritten at every count. A file
        # without it (V161-V325) settles none: its runs are looked up by name as before.
        self.through = ""
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            if document.get("format") == FORMAT:
                self.cells = document.get("cells") or {}
                self.counted = set(document.get("counted") or ())
                through = document.get("through")
                self.through = through if isinstance(through, str) else ""
        # The prior's cells (`PRIOR_WEIGHT`), in the same format; never saved. One that cannot
        # be read is no prior: it is another's file, and the session plays without it.
        self.lent: dict[str, dict[str, list[float]]] = {}
        if prior is not None and Path(prior).exists():
            try:
                document = json.loads(Path(prior).read_text(encoding="utf-8"))
                if document.get("format") == FORMAT:
                    self.lent = {cell: {str(level): [float(v) for v in row][:3]
                                        for level, row in levels.items()}
                                 for cell, levels in (document.get("cells") or {}).items()}
            except (OSError, ValueError, TypeError, AttributeError):
                self.lent = {}
        # What `hot` and `pooled` answered, and each map's cells, for the maps as they are
        # (`_cache`). The planner asks `hot` at every plan and re-plan, each ranking of
        # merchants and each start height (`jev.run.client`), and each answer summed every
        # cell of the map three times over: in the hive's farm processes on 29 Sep, 9,208
        # cells, `hot` and its sums were a third of a process's time (py-spy, 20 s). The
        # cells change only through `add` (`count_runs`, at a session's start) and by being
        # replaced whole, and either is a new answer.
        self._version = 0
        self._kept: tuple[tuple, dict] = ((), {})

    def add(self, cell: str, level: int, *, seconds: float = 0.0, attacks: int = 0,
            bad: int = 0) -> None:
        with self._lock:
            row = self.cells.setdefault(cell, {}).setdefault(str(level), [0.0, 0, 0])
            row[0] += seconds
            row[1] += attacks
            row[2] += bad
            self._version += 1

    def _totals(self, cell: str, level: int) -> tuple[float, int, int]:
        seconds, attacks, bad = _band(self.cells.get(cell), level)
        lent_s, lent_a, lent_b = _band(self.lent.get(cell), level)
        if lent_s > 0:
            share = min(PRIOR_WEIGHT, PRIOR_CAP_S / lent_s)
            seconds, attacks, bad = (seconds + lent_s * share, attacks + lent_a * share,
                                     bad + lent_b * share)
        return seconds, attacks, bad

    def _cache(self) -> dict:
        """What was worked out from the maps as they are now: kept until `add` or either map
        is replaced (`self.cells`, `self.lent`), each a new one."""
        signature = (self._version, id(self.cells), len(self.cells), id(self.lent),
                     len(self.lent))
        kept = self._kept
        if kept[0] != signature:
            by_map: dict[str, list[str]] = {}
            for cell in (*self.cells, *(c for c in self.lent if c not in self.cells)):
                by_map.setdefault(cell.partition(":")[0], []).append(cell)
            kept = self._kept = (signature, {"cells": by_map})
        return kept[1]

    def _cells(self, map_id: int) -> list[str]:
        """The cells of `map_id` either map knows, the character's own first."""
        return list(self._cache()["cells"].get(f"{map_id}", ()))

    def rate(self, cell: str, level: int, pooled: float) -> float:
        """Attacks a minute in `cell` at about `level`, shrunk toward `pooled`."""
        seconds, attacks, _ = self._totals(cell, level)
        return (attacks + PRIOR_MINUTES * pooled) / (seconds / 60 + PRIOR_MINUTES)

    def _worked(self, map_id: int, level: int) -> tuple[float, list[tuple[float, float, float]]]:
        """`pooled` and `hot` of `map_id` at `level`, each cell's totals summed once, kept
        until the maps change (`_cache`)."""
        cache = self._cache()
        key = (map_id, level)
        try:
            worked = cache.get(key)
        except TypeError:                    # a level that is no key: worked out each time
            key, worked = None, None
        if worked is not None:
            return worked
        cells = self._cells(map_id)
        totals = [self._totals(cell, level) for cell in cells]
        seconds = attacks = 0
        for s, a, _ in totals:
            seconds, attacks = seconds + s, attacks + a
        pooled = attacks / (seconds / 60) if seconds else 0.0
        hot = []
        for cell, (s, a, _) in zip(cells, totals, strict=True):
            rate = (a + PRIOR_MINUTES * pooled) / (s / 60 + PRIOR_MINUTES)   # as `rate`
            if a >= HOT_EVENTS and rate >= HOT_FACTOR * pooled:
                _, x, y = centre_of(cell)
                hot.append((x, y, rate))
        worked = (pooled, hot)
        if key is not None:
            cache[key] = worked
        return worked

    def pooled(self, map_id: int, level: int) -> float:
        """The map's own attacks a minute at about `level`."""
        return self._worked(map_id, level)[0]

    def hot(self, map_id: int, level: int | None) -> list[tuple[float, float, float]]:
        """The cells of `map_id` a route should keep clear of at `level`: their centres and
        attack rates. None without a level read."""
        if level is None:
            return []
        with self._lock:
            return list(self._worked(map_id, level)[1])

    def save(self) -> None:
        with self._lock:
            if self.file is None:
                return
            document = {"format": FORMAT, "cells": self.cells, "counted": sorted(self.counted)}
            if self.through:
                document["through"] = self.through
            atomic_json(self.file, document)

    def settle(self, newest: str, *, playing: str | None = None) -> None:
        """Runs begun `SETTLE_S` before `newest` (a start, `STAMP_FORMAT`), and before
        `playing`, the start of the oldest run still being played, are settled: `through`
        moves up to then, and their names leave `counted`."""
        try:
            through = (datetime.strptime(newest, STAMP_FORMAT)
                       - timedelta(seconds=SETTLE_S)).strftime(STAMP_FORMAT)
            if playing is not None:
                through = min(through, (datetime.strptime(playing, STAMP_FORMAT)
                                        - timedelta(seconds=1)).strftime(STAMP_FORMAT))
        except ValueError:                   # 30 February: a name, not a start
            return
        if through <= self.through:
            return
        self.through = through
        self.counted = {name for name in self.counted
                        if (stamp := _stamp(name)) is None or stamp > through}


def _band(levels: dict[str, list[float]] | None, level: int) -> tuple[float, int, int]:
    """A cell's seconds, attacks and bad attacks from one level below `level` to two above."""
    seconds = attacks = bad = 0
    for at, (s, a, b) in (levels or {}).items():
        if level - LEVELS_BELOW <= int(at) <= level + LEVELS_ABOVE:
            seconds, attacks, bad = seconds + s, attacks + a, bad + b
    return seconds, attacks, bad


def runs_in(directory: str | Path, danger: DangerMap, *, skip: str | None = None) -> list[Path]:
    """The run directories in `directory` begun after `danger.through`, or with no start in
    their names, but `skip`: what `count_runs` would look at, read without a stat of each
    run (`os.scandir`). Listing the hive's 31,770 runs a directory at a time took 130 ms
    (29 Sep), at each of its sessions' starts."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name == skip:
                continue
            stamp = _stamp(entry.name)
            if (stamp is None or stamp > danger.through) and entry.is_dir():
                found.append(directory / entry.name)
    return found


def _written(run: Path) -> float | None:
    """When any of a run's files (`RUN_FILES`) was last written; None for none."""
    latest = None
    for name in RUN_FILES:
        try:
            written = (run / name).stat().st_mtime
        except OSError:
            continue
        latest = written if latest is None else max(latest, written)
    return latest


def count_runs(runs: Iterable[Path], danger: DangerMap,
               bounds_for: Callable[[int], object | None], *, now: float | None = None) -> int:
    """Count every finished run not yet counted; returns the attacks added. `bounds_for(area_id)`
    gives a zone's map box (`jev.guide.coords.ZoneBounds`), to put ticks in world yards.
    The map is saved when a run was counted. Runs are counted oldest first, as before V326;
    only those not settled (`DangerMap.through`) are looked at, and sorted. One written to
    within `FRESH_S` of `now` is being played: neither counted nor settled."""
    now = time.time() if now is None else now
    added, newest, counted, playing = 0, danger.through, False, None
    fresh = []
    for run in runs:
        run = Path(run)
        stamp = _stamp(run.name)
        if stamp is not None:
            if stamp <= danger.through:
                continue
            newest = max(newest, stamp)
        if run.name not in danger.counted:
            fresh.append(run)
    for run in sorted(fresh):
        written = _written(run)
        if written is not None and now - written < FRESH_S:     # a clock set back, too
            stamp = _stamp(run.name)
            if stamp is not None and (playing is None or stamp < playing):
                playing = stamp
            continue
        ticks, evidence = run / "ticks.jsonl", run / "executions.jsonl"
        if not ticks.exists():
            continue
        added += count_run(ticks, evidence if evidence.exists() else None, danger, bounds_for)
        danger.counted.add(run.name)
        counted = True
    # Settled after the counting, so a run is never passed over before it is looked at. A
    # save only for something counted: every session start rewrote the whole map, the
    # hive's 2.5 MB at each of its bots' (about 300, 15 minutes each), new runs or none.
    danger.settle(newest, playing=playing)
    if counted:
        danger.save()
    return added


def rebuild(runs: Iterable[Path], bounds_for: Callable[[int], object | None],
            file: str | Path | None = None, *, now: float | None = None) -> DangerMap:
    """A danger map counted afresh from the finished `runs`, saved to `file` (replaced),
    for a map counted from runs still being played (before V326, the hive's). Runs being
    played are left for its next count, as ever. `tools/rebuild_danger.py`."""
    danger = DangerMap()
    danger.file = Path(file) if file is not None else None
    count_runs(runs, danger, bounds_for, now=now)
    danger.save()
    return danger


def count_run(ticks: Path, evidence: Path | None, danger: DangerMap,
              bounds_for: Callable[[int], object | None]) -> int:
    """A run's exposure and fresh attacks into `danger`; returns the attacks counted."""
    from jev.guide.coords import map_to_world

    times, places = [], []
    previous = None
    for line in ticks.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        state = row.get("state") or {}
        pos, vitals, char = state.get("pos") or {}, state.get("vitals") or {}, state.get("char") or {}
        bounds = bounds_for(pos.get("coord_zone_id")) if pos.get("coord_zone_id") is not None else None
        if bounds is None or pos.get("mx") is None or pos.get("my") is None:
            previous = None
            continue
        world = map_to_world(pos["mx"], pos["my"], bounds)
        level = char.get("level")
        if world is None or not isinstance(level, int):
            previous = None
            continue
        t = row.get("t")
        if not isinstance(t, (int, float)):
            previous = None
            continue
        place = (cell_of(bounds.map_id, world[0], world[1]), level)
        times.append(t)
        places.append(place)
        # Time spent where the last tick was, out of combat there: exposure to attack.
        if (previous is not None and previous[2] is not True
                and 0 < t - previous[0] < MAX_TICK_GAP_S):
            danger.add(previous[1][0], previous[1][1], seconds=t - previous[0])
        previous = (t, place, vitals.get("combat"))
    if evidence is None or not times:
        return 0
    counted = 0
    fight = None
    last_end = None
    for line in evidence.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        operation, phase = row.get("operation"), row.get("phase")
        if operation == "fight" and phase == "begin":
            fight = {"t": row.get("t"), "first": None, "low": None, "died": False}
        elif fight is not None and operation == "combat.observed":
            data = row.get("data") or {}
            if fight["first"] is None:
                fight["first"] = data
            hp = data.get("vitals.hp")
            if isinstance(hp, (int, float)) and not isinstance(hp, bool):
                fight["low"] = hp if fight["low"] is None else min(fight["low"], hp)
            fight["died"] = fight["died"] or data.get("vitals.dead") is True
        elif fight is not None and operation == "fight" and phase == "end":
            began, fight = fight, None
            chained = last_end is not None and began["t"] is not None and began["t"] - last_end < CHAINED_S
            last_end = row.get("t")
            if (chained or began["first"] is None or began["first"].get("vitals.combat") is not True
                    or not isinstance(began["t"], (int, float))):
                continue
            i = min(bisect.bisect_left(times, began["t"]), len(times) - 1)
            if abs(times[i] - began["t"]) > MAX_TICK_GAP_S:
                continue
            bad = (row.get("code") == "died" or began["died"]
                   or (began["low"] is not None and began["low"] < BAD_HP))
            cell, level = places[i]
            danger.add(cell, level, attacks=1, bad=int(bad))
            counted += 1
    return counted
