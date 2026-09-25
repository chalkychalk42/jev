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
(`jev.learn.choices.backfill_hunts`): a run is counted when a later session starts.
"""

from __future__ import annotations

import bisect
import json
import math
import threading
from collections.abc import Callable, Iterable
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


def cell_of(map_id: int, x: float, y: float) -> str:
    return f"{map_id}:{math.floor(x / CELL_YARDS)},{math.floor(y / CELL_YARDS)}"


def centre_of(cell: str) -> tuple[int, float, float]:
    map_id, _, rest = cell.partition(":")
    cx, cy = (int(v) for v in rest.split(","))
    return int(map_id), (cx + 0.5) * CELL_YARDS, (cy + 0.5) * CELL_YARDS


class DangerMap:
    """Seconds out of combat and attacks begun, per cell and character level, as JSON."""

    def __init__(self, file: str | Path | None = None):
        self.file = Path(file) if file is not None else None
        self._lock = threading.Lock()
        # cell -> level -> [seconds, attacks, bad attacks]
        self.cells: dict[str, dict[str, list[float]]] = {}
        self.counted: set[str] = set()
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            if document.get("format") == FORMAT:
                self.cells = document.get("cells") or {}
                self.counted = set(document.get("counted") or ())

    def add(self, cell: str, level: int, *, seconds: float = 0.0, attacks: int = 0,
            bad: int = 0) -> None:
        with self._lock:
            row = self.cells.setdefault(cell, {}).setdefault(str(level), [0.0, 0, 0])
            row[0] += seconds
            row[1] += attacks
            row[2] += bad

    def _totals(self, cell: str, level: int) -> tuple[float, int, int]:
        seconds = attacks = bad = 0
        for at, (s, a, b) in (self.cells.get(cell) or {}).items():
            if level - LEVELS_BELOW <= int(at) <= level + LEVELS_ABOVE:
                seconds, attacks, bad = seconds + s, attacks + a, bad + b
        return seconds, attacks, bad

    def rate(self, cell: str, level: int, pooled: float) -> float:
        """Attacks a minute in `cell` at about `level`, shrunk toward `pooled`."""
        seconds, attacks, _ = self._totals(cell, level)
        return (attacks + PRIOR_MINUTES * pooled) / (seconds / 60 + PRIOR_MINUTES)

    def pooled(self, map_id: int, level: int) -> float:
        """The map's own attacks a minute at about `level`."""
        seconds = attacks = 0
        for cell in self.cells:
            if cell.startswith(f"{map_id}:"):
                s, a, _ = self._totals(cell, level)
                seconds, attacks = seconds + s, attacks + a
        return attacks / (seconds / 60) if seconds else 0.0

    def hot(self, map_id: int, level: int | None) -> list[tuple[float, float, float]]:
        """The cells of `map_id` a route should keep clear of at `level`: their centres and
        attack rates. None without a level read."""
        if level is None:
            return []
        with self._lock:
            pooled = self.pooled(map_id, level)
            result = []
            for cell in self.cells:
                if not cell.startswith(f"{map_id}:"):
                    continue
                _, attacks, _ = self._totals(cell, level)
                rate = self.rate(cell, level, pooled)
                if attacks >= HOT_EVENTS and rate >= HOT_FACTOR * pooled:
                    _, x, y = centre_of(cell)
                    result.append((x, y, rate))
            return result

    def save(self) -> None:
        with self._lock:
            if self.file is None:
                return
            atomic_json(self.file, {"format": FORMAT, "cells": self.cells,
                                    "counted": sorted(self.counted)})


def count_runs(runs: Iterable[Path], danger: DangerMap,
               bounds_for: Callable[[int], object | None]) -> int:
    """Count every run not yet counted; returns the attacks added. `bounds_for(area_id)`
    gives a zone's map box (`jev.guide.coords.ZoneBounds`), to put ticks in world yards."""
    added = 0
    for run in sorted(Path(r) for r in runs):
        if run.name in danger.counted:
            continue
        ticks, evidence = run / "ticks.jsonl", run / "executions.jsonl"
        if not ticks.exists():
            continue
        added += count_run(ticks, evidence if evidence.exists() else None, danger, bounds_for)
        danger.counted.add(run.name)
    danger.save()
    return added


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
