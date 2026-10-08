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

What is drawn is an option's payoffs per second, not per try (V310): its chance divided by a
draw of its seconds a try, shrunk the same way toward the choice's pooled seconds. A station
that yields 60% of visits 90 yards away no longer beats one that yields 50% next door, and a
heal line is learned by the seconds a kill costs (`jev.clients.fight`), not by how often a
fight went well.

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
# A lap keeps the tour's own order as its prior (`jev.run.hunt.spawn_tour`: lone spawns before
# packs, each to the nearest left): a station further along it leads only on a draw this much
# better a place (V252). Ordered by draws alone, the mage's first station was on average 90
# yards further than the nearest (sessions 205-217), on records that were mostly walks cut
# short, not visits.
TOUR_DECAY = 0.85
# A visit that ended in the character's death counts as this many visits that paid nothing
# (V274): a station where a pair keeps killing the character is one to stand at less. The
# level 10 mage's deaths were Prowler pairs about once a session (sessions 244-253).
DEATH_VISITS = 4
# What a death costs beyond the time it took, in seconds (V310): the hive's reward charges each
# death this much more played time (JevHive `docs/plans/reward.md`, `DEATH_S`), for what the
# played hours do not show - the live client's durability, the goal's cap on deaths. A visit
# that ended in one spreads it over its `DEATH_VISITS - 1` extra visits; a heal line's cycle
# carries it whole.
DEATH_S = 120.0
# A try is counted at least this many seconds when an option's pace is drawn (V310): a death's
# extra visits were recorded at 0 s before it, and an option of those alone would otherwise
# draw a pace of nothing.
SECONDS_FLOOR = 1.0
# Another's record of the same choices, read beside the character's own and never written
# (V290): the hive's, whose bots play this code on a server of their own. Each of its visits
# counts as `PRIOR_WEIGHT` of the character's own, and an option takes at most `PRIOR_CAP`
# visits' worth from it, so a few of the character's own outweigh it. Without the file,
# nothing changes; deleting it undoes it.
PRIOR_WEIGHT = 0.25
PRIOR_CAP = 10.0
# Jev (`jev.coach.judge`) picks a lap's first station among the draw's best this many, and is
# asked once in this long by one chooser: a hunt orders all its laps at its start, and the
# laps after the first begin where the one before ended.
JEV_STATIONS = 8
JEV_AGAIN_S = 60.0


@dataclass
class Arm:
    """One option's record: tries, how many paid off, and the seconds they took."""

    tries: int = 0
    wins: int = 0
    seconds: float = 0.0
    updated: float = 0.0


class ChoiceMemory:
    """Every choice point's options and their records, kept as one JSON file."""

    def __init__(self, file: str | Path | None = None, *, clock: Callable[[], float] = time.time,
                 prior: str | Path | None = None):
        self.file = Path(file) if file is not None else None
        self.clock = clock
        self._lock = threading.Lock()
        self.points: dict[str, dict[str, Arm]] = {}
        # Runs already counted from their evidence before choices were logged (`backfill`).
        self.backfilled: set[str] = set()
        if self.file is not None and self.file.exists():
            self.points, self.backfilled = _read(self.file)
        # The prior's records (`PRIOR_WEIGHT`), in the same format; never saved. One that
        # cannot be read is no prior: it is another's file, and the session plays without it.
        self.lent: dict[str, dict[str, Arm]] = {}
        if prior is not None and Path(prior).exists():
            try:
                self.lent, _ = _read(Path(prior))
            except (OSError, ValueError, TypeError, AttributeError):
                self.lent = {}

    def arms(self, point: str, prefix: str = "", *, lent: bool = True) -> dict[str, Arm]:
        """A choice point's options whose key starts with `prefix`: the character's own
        record, and with `lent` the prior's added at its discount."""
        with self._lock:
            out = {key: Arm(**asdict(arm)) for key, arm in (self.points.get(point) or {}).items()
                   if key.startswith(prefix)}
            for key, arm in (self.lent.get(point) or {}).items() if lent else ():
                if not key.startswith(prefix) or arm.tries <= 0:
                    continue
                share = min(PRIOR_WEIGHT, PRIOR_CAP / arm.tries)
                mine = out.setdefault(key, Arm())
                mine.tries += arm.tries * share
                mine.wins += arm.wins * share
                mine.seconds += arm.seconds * share
            return out

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


def _read(file: Path) -> tuple[dict[str, dict[str, Arm]], set[str]]:
    document = json.loads(file.read_text(encoding="utf-8"))
    if document.get("format") != FORMAT:
        return {}, set()
    return ({point: {key: Arm(**arm) for key, arm in arms.items()}
             for point, arms in (document.get("points") or {}).items()},
            set(document.get("backfilled") or ()))


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


def pace(arms: Iterable[Arm]) -> float | None:
    """The pooled seconds a try across a choice's options (V310); `None` while none has its
    time recorded, and a draw is then of the chance alone, as before."""
    arms = list(arms)
    tries = sum(a.tries for a in arms)
    seconds = sum(max(0.0, a.seconds) for a in arms)
    if tries <= 0 or seconds <= 0:
        return None
    return max(SECONDS_FLOOR, seconds / tries)


def draw(arm: Arm | None, rate: float, rng: random.Random, pooled_s: float | None = None) -> float:
    """A draw of an option's chance of paying off: its record, shrunk toward `rate`. With
    `pooled_s` (the choice's `pace`), a draw of its payoffs per second (V310): the chance times
    a draw of its tries per second, shrunk toward the pooled pace by `PRIOR_VISITS` tries.

    Tries per second is drawn as an exponential's rate is (a gamma of the tries, over the
    seconds they took), so an option seldom tried is uncertain in its pace as in its chance,
    and one never tried draws around the pooled rate over the pooled pace: it gets its turn."""
    tries, wins = (arm.tries, arm.wins) if arm is not None else (0, 0)
    alpha = PRIOR_VISITS * rate + wins
    beta = PRIOR_VISITS * (1.0 - rate) + (tries - wins)
    chance = rng.betavariate(max(alpha, 1e-6), max(beta, 1e-6))
    if pooled_s is None:
        return chance
    seconds = max(arm.seconds, tries * SECONDS_FLOOR) if arm is not None else 0.0
    spent = PRIOR_VISITS * max(SECONDS_FLOOR, pooled_s) + seconds
    return chance * rng.gammavariate(PRIOR_VISITS + tries, 1.0 / spent)


def _logged(value: float) -> float:
    """A draw as the log keeps it: four figures, since payoffs a second are small."""
    return float(f"{value:.4g}")


def record_text(arm: Arm | None, unit: str = "each") -> str:
    """An option's record as Jev reads it (`jev.coach.judge`)."""
    if arm is None or arm.tries <= 0:
        return "never tried"
    tries = round(arm.tries, 1)
    # The seconds a payoff costs are what is learned (V310), so Jev reads them too.
    per_win = f", {arm.seconds / arm.wins:.0f} s a payoff" if arm.wins > 0 else ""
    return (f"tried {tries:g}, paid off {round(arm.wins, 1):g} ({arm.wins / arm.tries:.0%}), "
            f"{arm.seconds / arm.tries:.0f} s {unit}{per_win}")


def station_key(objective: str, station: Sequence[float]) -> str:
    """A station is its objective and its point to the yard: spawn points are the world
    database's own, the same every time they are toured."""
    return f"{objective}@{round(station[0])},{round(station[1])}"


class Stations:
    """Where a hunt or a gather stands next: its stations best first by what each has
    yielded before (`hunt.station`, `gather.station`)."""

    def __init__(self, memory: ChoiceMemory, point: str, objective: str, *,
                 log: ChoiceLog | None = None, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic, judge=None):
        self.memory, self.point, self.objective = memory, point, objective
        self.log = log
        self.rng = rng or random.Random()
        self.clock = clock
        # Jev, when it coaches the character (`jev.coach.judge.Judge`): it picks where a lap
        # begins among the draw's best, with their records and how far each is.
        self.judge = judge
        self._judged_at: float | None = None
        self._open: tuple[tuple, float] | None = None
        self._walk: float | None = None
        # Who ordered the last lap: "jev" when the judge picked where it begins, which the hunt
        # keeps (V512); "local" for the draw alone.
        self.by: str | None = None

    def order(self, stations: Sequence[Sequence[float]]) -> list[tuple]:
        """One lap of `stations`, the most payoffs a second first (V310), each a little less
        likely the further along the tour's own order it stands (`TOUR_DECAY`)."""
        stations = [tuple(s) for s in stations]
        arms = self.memory.arms(self.point, f"{self.objective}@")
        rate, pooled_s = pooled(arms.values()), pace(arms.values())
        draws = [draw(arms.get(station_key(self.objective, s)), rate, self.rng, pooled_s)
                 for s in stations]
        order = sorted(range(len(stations)), key=lambda i: -draws[i] * TOUR_DECAY ** i)
        by = "local"
        now = self.clock()
        if (self.judge is not None and len(stations) > 1
                and (self._judged_at is None or now - self._judged_at >= JEV_AGAIN_S)):
            self._judged_at = now
            first = self._judged(stations, order[:JEV_STATIONS], arms)
            if first is not None:
                order = [first] + [i for i in order if i != first]
                by = "jev"
        self.by = by
        if self.log is not None:
            self.log.write({"event": "choice", "point": self.point, "objective": self.objective,
                            "rate": round(rate, 4), "by": by,
                            "pace": None if pooled_s is None else round(pooled_s, 2),
                            "options": [station_key(self.objective, s) for s in stations],
                            "draws": [_logged(d) for d in draws],
                            "order": [station_key(self.objective, stations[i]) for i in order]})
        return [stations[i] for i in order]

    def _judged(self, stations: list[tuple], best: list[int], arms: dict) -> int | None:
        """The station Jev would begin at, of `best`; `None` when it has no answer. The one it
        named for this objective within the judge's `STATION_TTL_S` is taken again unasked,
        while it is among the best: a hunt begun again is the same question."""
        memo = getattr(self.judge, "stations", None)
        if memo is not None:
            from jev.coach.judge import STATION_TTL_S
            kept = memo.get((self.point, self.objective))
            if kept is not None and self.clock() - kept[1] < STATION_TTL_S:
                for i in best:
                    if station_key(self.objective, stations[i]) == kept[0]:
                        return i
        try:
            origin = self.judge.origin() if hasattr(self.judge, "origin") else None
        except Exception:
            origin = None
        options = {}
        for n, i in enumerate(best, 1):
            station = stations[i]
            where = ""
            if origin is not None:
                where = (f"{((station[0] - origin[0]) ** 2 + (station[1] - origin[1]) ** 2) ** 0.5:.0f}"
                         " yd away; ")
            options[f"s{n}"] = where + record_text(
                arms.get(station_key(self.objective, station)), "a visit")
        try:
            picked = self.judge.pick(self.point, self.objective, options)
        except Exception:
            picked = None
        if picked not in options:
            return None
        chosen = best[int(picked[1:]) - 1]
        if memo is not None:
            memo[(self.point, self.objective)] = (station_key(self.objective, stations[chosen]),
                                                  self.clock())
        return chosen

    def walking(self) -> None:
        """The walk to the next station begins: a visit that arrives is timed from here, its
        walk included (V310). A far station's walk is what it costs over a near one; a walk
        cut short on the way is charged to none (V252)."""
        self._walk = self.clock()

    def arrive(self, station: Sequence[float]) -> None:
        """A station chosen: its visit is timed from here, or from its walk's start."""
        self.leave(False)
        since, self._walk = (self._walk if self._walk is not None else self.clock()), None
        self._open = (tuple(station), since)

    def died(self) -> None:
        """The open station's visit ended in the character's death (V274), charged `DEATH_S`
        over its extra visits (V310)."""
        if self._open is None:
            return
        key = station_key(self.objective, self._open[0])
        self.leave(False)
        for _ in range(DEATH_VISITS - 1):
            self.memory.record(self.point, key, False, DEATH_S / (DEATH_VISITS - 1))
        if self.log is not None:
            self.log.write({"event": "death", "point": self.point, "key": key,
                            "visits": DEATH_VISITS, "seconds": DEATH_S})

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


class Choice:
    """A choice among a few named options, each with its own record per objective: which
    heal line a fight holds, whether a failed routine is retried or handed to the tutor.
    `pick` draws; `outcome` records how the option it gave did."""

    def __init__(self, memory: ChoiceMemory, point: str, *, log: ChoiceLog | None = None,
                 rng: random.Random | None = None, judge=None,
                 judged: frozenset[str] | None = None, unit: str = "each"):
        self.memory, self.point, self.log = memory, point, log
        self.unit = unit                     # what a try's seconds are, as Jev reads them
        self.rng = rng or random.Random()
        # Jev, when it coaches the character (`jev.coach.judge.Judge`), picks with each
        # option's record in front of it; `judged` limits it to those objectives (a choice
        # made in the middle of a fight is not one to wait on).
        self.judge, self.judged = judge, judged

    def pick(self, objective: str, options: Sequence[str]) -> str:
        """The option with the most payoffs a second drawn (V310)."""
        arms = self.memory.arms(self.point, f"{objective}@")
        rate, pooled_s = pooled(arms.values()), pace(arms.values())
        draws = [draw(arms.get(f"{objective}@{option}"), rate, self.rng, pooled_s)
                 for option in options]
        chosen = options[max(range(len(options)), key=lambda i: draws[i])]
        by = "local"
        if (self.judge is not None and len(options) > 1
                and (self.judged is None or objective in self.judged)):
            try:
                picked = self.judge.pick(self.point, objective, {
                    option: record_text(arms.get(f"{objective}@{option}"), self.unit)
                    for option in options})
            except Exception:
                picked = None
            if picked in options:
                chosen, by = picked, "jev"
        if self.log is not None:
            self.log.write({"event": "choice", "point": self.point, "objective": objective,
                            "rate": round(rate, 4), "options": list(options), "by": by,
                            "pace": None if pooled_s is None else round(pooled_s, 2),
                            "draws": [_logged(d) for d in draws], "chosen": chosen})
        return chosen

    def outcome(self, objective: str, option: str, won: bool, seconds: float = 0.0) -> Arm:
        key = f"{objective}@{option}"
        arm = self.memory.record(self.point, key, won, seconds)
        if self.log is not None:
            self.log.write({"event": "outcome", "point": self.point, "key": key,
                            "won": bool(won), "seconds": round(seconds, 2),
                            "tries": arm.tries, "wins": arm.wins})
        return arm
