"""Choices learned from their outcomes (DECISIONS V158).

Where a routine has real alternatives, each alternative keeps a record of how often it paid
off and what it cost, and the routine tries them best first by that record. The record is
written by whatever made the choice - a routine, the tutor, a person - since the outcome
is what is learned, not who chose (V9). Nothing here imitates anyone.

The first choice point is where a hunt stands next (`Stations`). Measured over three days of
runs before this existed: 115 visits to the sixteen spawn points of Wolves Across the
Border found a wolf 17 times - the best points 3 of 5, 4 of 7 and 4 of 9, while 8 points
were visited three times or more and never had one - and a grinding rib's station came up
empty 14 times in a row. Every empty station is a walk and two looks.

Choosing is Thompson sampling: each option's chance of paying off is drawn from what it has
done, shrunk toward its choice's pooled rate, and options are tried in the order drawn. An
unproven option draws from the pooled rate and so gets its turn; one that has paid off
comes first; one that never has sinks, without ever being ruled out.

Each run keeps what was seen, chosen and learned in `choices.jsonl`; the durable record is
one JSON file (`ChoiceMemory`).
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from jev.persist import atomic_json

FORMAT = 1
# How many visits' worth of the pooled rate an option starts with: enough that one lucky or
# unlucky visit does not decide it, few enough that its own record soon does.
PRIOR_VISITS = 2.0


@dataclass
class Arm:
    """One option's record: tries, how many paid off, and the seconds they took."""

    tries: int = 0
    wins: int = 0
    seconds: float = 0.0
    updated: float = 0.0


class ChoiceMemory:
    """Every choice point's options and their records, kept as one JSON file."""

    def __init__(self, file: str | Path | None = None, *, clock: Callable[[], float] = time.time):
        self.file = Path(file) if file is not None else None
        self.clock = clock
        self._lock = threading.Lock()
        self.points: dict[str, dict[str, Arm]] = {}
        # Runs already counted from their evidence before choices were logged (`backfill`).
        self.backfilled: set[str] = set()
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            if document.get("format") == FORMAT:
                self.points = {point: {key: Arm(**arm) for key, arm in arms.items()}
                               for point, arms in (document.get("points") or {}).items()}
                self.backfilled = set(document.get("backfilled") or ())

    def arms(self, point: str, prefix: str = "") -> dict[str, Arm]:
        """A choice point's options whose key starts with `prefix`."""
        with self._lock:
            return {key: Arm(**asdict(arm)) for key, arm in (self.points.get(point) or {}).items()
                    if key.startswith(prefix)}

    def record(self, point: str, key: str, won: bool, seconds: float, *,
               save: bool = True) -> Arm:
        with self._lock:
            arm = self.points.setdefault(point, {}).setdefault(key, Arm())
            arm.tries += 1
            arm.wins += int(bool(won))
            arm.seconds += max(0.0, float(seconds))
            arm.updated = self.clock()
            if save:
                self._save()
            return Arm(**asdict(arm))

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        if self.file is None:
            return
        atomic_json(self.file, {
            "format": FORMAT,
            "points": {point: {key: asdict(arm) for key, arm in arms.items()}
                       for point, arms in self.points.items()},
            "backfilled": sorted(self.backfilled)})


class ChoiceLog:
    """What each choice saw, chose and learned, one JSON line each, in a run's directory."""

    def __init__(self, path: str | Path | None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        if self.path is None:
            return
        line = json.dumps({"t": self.clock(), **row}, sort_keys=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def pooled(arms: Iterable[Arm]) -> float:
    """The pooled chance of paying off across a choice's options, with one of each seen."""
    arms = list(arms)
    return (sum(a.wins for a in arms) + 1) / (sum(a.tries for a in arms) + 2)


def draw(arm: Arm | None, rate: float, rng: random.Random) -> float:
    """A draw of an option's chance of paying off: its record, shrunk toward `rate`."""
    tries, wins = (arm.tries, arm.wins) if arm is not None else (0, 0)
    alpha = PRIOR_VISITS * rate + wins
    beta = PRIOR_VISITS * (1.0 - rate) + (tries - wins)
    return rng.betavariate(max(alpha, 1e-6), max(beta, 1e-6))


def station_key(objective: str, station: Sequence[float]) -> str:
    """A station is its objective and its point to the yard: spawn points are the world
    database's own, the same every time they are toured."""
    return f"{objective}@{round(station[0])},{round(station[1])}"


class Stations:
    """Where a hunt or a gather stands next: its stations best first by what each has
    yielded before (`hunt.station`, `gather.station`)."""

    def __init__(self, memory: ChoiceMemory, point: str, objective: str, *,
                 log: ChoiceLog | None = None, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.memory, self.point, self.objective = memory, point, objective
        self.log = log
        self.rng = rng or random.Random()
        self.clock = clock
        self._open: tuple[tuple, float] | None = None

    def order(self, stations: Sequence[Sequence[float]]) -> list[tuple]:
        """One lap of `stations`, the most likely to pay off first; ties keep the tour's
        own order, which walks them nearest first."""
        stations = [tuple(s) for s in stations]
        arms = self.memory.arms(self.point, f"{self.objective}@")
        rate = pooled(arms.values())
        draws = [draw(arms.get(station_key(self.objective, s)), rate, self.rng) for s in stations]
        order = sorted(range(len(stations)), key=lambda i: -draws[i])
        if self.log is not None:
            self.log.write({"event": "choice", "point": self.point, "objective": self.objective,
                            "rate": round(rate, 4),
                            "options": [station_key(self.objective, s) for s in stations],
                            "draws": [round(d, 4) for d in draws],
                            "order": [station_key(self.objective, stations[i]) for i in order]})
        return [stations[i] for i in order]

    def arrive(self, station: Sequence[float]) -> None:
        """A station chosen: its visit is timed from here."""
        self.leave(False)
        self._open = (tuple(station), self.clock())

    def leave(self, won: bool) -> None:
        """The open station's visit ended; `won` says whether it paid off."""
        if self._open is None:
            return
        station, since = self._open
        self._open = None
        seconds = self.clock() - since
        key = station_key(self.objective, station)
        arm = self.memory.record(self.point, key, won, seconds)
        if self.log is not None:
            self.log.write({"event": "outcome", "point": self.point, "key": key,
                            "won": bool(won), "seconds": round(seconds, 2),
                            "tries": arm.tries, "wins": arm.wins})


def objective_key(name_id: int | None, step_id: str | None) -> str:
    """What a hunt's stations are learned under: the creature it wants, which is the same
    wherever the guide sends it for them, else the step itself."""
    return f"creature:{name_id}" if name_id is not None else f"step:{step_id}"


def backfill_hunts(runs: Iterable[Path], memory: ChoiceMemory) -> int:
    """Count the hunt stations of runs from before choices were logged, from their evidence.

    A run that logged its own choices is never counted here: its outcomes are in the memory
    already, and counting them twice would double their weight. Returns the visits added."""
    added = 0
    for run in sorted(Path(r) for r in runs):
        if run.name in memory.backfilled or (run / "choices.jsonl").exists():
            continue
        evidence = run / "executions.jsonl"
        if not evidence.exists():
            continue
        for key, won, seconds in _hunt_visits(evidence):
            memory.record("hunt.station", key, won, seconds, save=False)
            added += 1
        memory.backfilled.add(run.name)
    memory.save()
    return added


def _hunt_visits(evidence: Path):
    """(station key, found, seconds) for each hunt station an evidence file shows visited:
    from its approach to the next approach, or to the hunt's end."""
    objective = None
    visit = None                 # [key, found, started]
    for line in evidence.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        operation, phase = row.get("operation"), row.get("phase")
        data = row.get("data") or {}
        at = row.get("t") if isinstance(row.get("t"), (int, float)) else None
        if operation == "hunt.request":
            objective = objective_key(data.get("wanted_name_id"), row.get("step_id"))
            visit = None
        elif operation == "hunt.approach" and phase == "begin":
            if visit is not None and at is not None:
                yield visit[0], visit[1], max(0.0, at - visit[2])
            destination = data.get("destination")
            visit = (None if objective is None or not destination or at is None
                     else [station_key(objective, destination), False, at])
        elif visit is not None and operation == "fight" and phase == "end" \
                and row.get("code") == "killed":
            visit[1] = True
        elif visit is not None and operation == "hunt" and phase == "end":
            if at is not None:
                yield visit[0], visit[1], max(0.0, at - visit[2])
            visit = None
