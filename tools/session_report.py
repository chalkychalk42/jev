"""Per-session performance, for comparing ways of playing (docs/plans/nine-hour-session.md).

    python tools/session_report.py --since 88              # a row per session
    python tools/session_report.py --since 88 --compare    # each arm's totals, intervals
    python tools/session_report.py --since 55 --csv captures/metrics.csv
    python tools/session_report.py --since 88 --motor      # + qualified motor examples

Everything is read from what a session leaves behind - its log in captures/, and its run
directory (ticks, executions, tutor calls). Nothing here attaches to a client. XP is
counted in points from the server's own table (`world_player_xp_for_level`), since a level
at 10 is worth more than one at 3 and "levels an hour" hides it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data/knowledge/tbc-243.sqlite"
CAPTURES = ROOT / "captures"
RUNS = ROOT / "runs"
LOOP_LOG = CAPTURES / "session-loop.log"
# Sessions shorter than this are start-up failures, not samples of play.
MIN_MINUTES = 3.0
# Ticks further apart than this are a pause, not play.
MAX_TICK_GAP_S = 5.0


def xp_table(db: Path = DB) -> dict[int, int]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        return {int(level): int(xp) for level, xp in
                con.execute("select lvl, xp_for_next_level from world_player_xp_for_level")}


@dataclass
class Session:
    number: int
    run: str = ""
    arm: str = ""
    minutes: float = 0.0
    level_start: int | None = None
    level_end: int | None = None
    xp: float = 0.0                 # points gained
    xp_per_hour: float = 0.0
    kills: int = 0
    deaths: int = 0
    steps: int = 0                  # guide steps completed (quest progress, lumpy XP aside)
    looted: int = 0
    unlooted: int = 0
    stuck: int = 0
    # Hunt stations visited, and those that found a target (DECISIONS V158): the choice
    # the station learner makes, measured the same way before it existed and after.
    stations: int = 0
    stations_won: int = 0
    walk_timeouts: int = 0
    watchdog: int = 0
    tutor_calls: int = 0
    tutor_median_s: float | None = None
    tutor_unavailable: int = 0
    money_delta: int | None = None
    motor_records: int | None = None
    motor_qualified: int | None = None
    exit_code: int | None = None
    skills: dict = field(default_factory=dict)   # skill -> share of the session's time


def _sessions_from_loop(since: int) -> dict[int, int | None]:
    """Session number -> its exit code, from the loop's own log."""
    exits: dict[int, int | None] = {}
    if not LOOP_LOG.exists():
        return exits
    for line in LOOP_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        start = re.match(r"session (\d+) start", line)
        if start and int(start.group(1)) >= since:
            exits.setdefault(int(start.group(1)), None)
        done = re.match(r"session (\d+) exit=(\d+)", line)
        if done and int(done.group(1)) >= since:
            exits[int(done.group(1))] = int(done.group(2))
    return exits


def _run_of(log: Path) -> str | None:
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
        match = re.search(r"recording to .*[\\/]runs[\\/]([0-9T]+-[0-9a-f]+)", line)
        if match:
            return match.group(1)
    return None


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def measure(number: int, table: dict[int, int], exit_code: int | None = None) -> Session | None:
    log = CAPTURES / f"live-testvvi-{number}.log"
    if not log.exists():
        return None
    session = Session(number=number, exit_code=exit_code)
    text = log.read_text(encoding="utf-8", errors="replace")
    session.walk_timeouts = len(re.findall(r"^  timeout,", text, re.MULTILINE))
    # The follower's count runs for the whole session: each walk's line repeats it, so
    # only its rises are new stuck events (a fall is a fresh follower).
    counts = [int(n) for n in re.findall(r"(\d+) stuck", text)]
    session.stuck = sum(max(0, after - before) for before, after in zip([0, *counts], counts))
    session.watchdog = text.count("watchdog")
    session.tutor_unavailable = text.count("tutor unavailable")
    run = _run_of(log)
    if run is None or not (RUNS / run).is_dir():
        return session
    session.run = run
    directory = RUNS / run
    try:
        config = json.loads((directory / "play-config.json").read_text())
        session.arm = config.get("mode", "") + (":" + config["dispatch"] if config.get("dispatch") else "")
    except (OSError, ValueError):
        session.arm = "off"
    ticks = list(_jsonl(directory / "ticks.jsonl"))
    if len(ticks) >= 2:
        played = 0.0
        by_skill: Counter = Counter()
        # A session that starts dead or a ghost inherits the death of the one before.
        first_vitals = ticks[0]["state"]["vitals"]
        dead_before = first_vitals.get("dead") is True or first_vitals.get("ghost") is True
        for a, b in zip(ticks, ticks[1:]):
            gap = b["t"] - a["t"]
            if gap <= 0:
                continue
            step = min(gap, MAX_TICK_GAP_S)
            played += step
            by_skill[a.get("armed_skill") or "none"] += step
            ca, cb = a["state"]["char"], b["state"]["char"]
            la, lb = ca.get("level"), cb.get("level")
            if isinstance(la, int) and isinstance(lb, int) and la in table:
                xa, xb = ca.get("xp_pct"), cb.get("xp_pct")
                if la == lb and isinstance(xa, (int, float)) and isinstance(xb, (int, float)):
                    session.xp += max(0.0, xb - xa) * table[la]
                elif lb > la:
                    session.xp += (1 - (xa or 0.0)) * table[la] + (xb or 0.0) * table.get(lb, table[la])
            if b.get("tracker_event") == "advance":
                session.steps += 1
            dead = a["state"]["vitals"].get("dead") is True
            if dead and not dead_before:
                session.deaths += 1
            dead_before = dead
        session.minutes = played / 60
        session.xp_per_hour = session.xp / (played / 3600) if played else 0.0
        session.skills = {k: round(v / played, 3) for k, v in by_skill.most_common()} if played else {}
        first, last = ticks[0]["state"], ticks[-1]["state"]
        session.level_start = first["char"].get("level")
        session.level_end = last["char"].get("level")
        m0, m1 = first["bags"].get("money_copper"), last["bags"].get("money_copper")
        if isinstance(m0, int) and isinstance(m1, int):
            session.money_delta = m1 - m0
    for row in _jsonl(directory / "executions.jsonl"):
        operation, phase, code = row.get("operation"), row.get("phase"), row.get("code")
        if operation == "fight" and phase == "end" and code == "killed":
            session.kills += 1
        elif operation == "loot" and phase == "end":
            if code in ("took", "nothing"):
                session.looted += 1
            elif code == "no_corpse":
                session.unlooted += 1
    session.stations, session.stations_won = _station_visits(directory)
    latencies = [row["latency_ms"] / 1000 for row in _jsonl(directory / "play-teacher.jsonl")
                 if row.get("event") != "transport" and isinstance(row.get("latency_ms"), (int, float))]
    session.tutor_calls = len(latencies)
    session.tutor_median_s = round(statistics.median(latencies), 1) if latencies else None
    return session


def _station_visits(directory: Path) -> tuple[int, int]:
    """(hunt station visits, those that found a target): from the run's own choice log
    when it kept one, else from its evidence as `jev.learn.choices` counts earlier runs."""
    from jev.learn.choices import _hunt_visits

    logged = [row for row in _jsonl(directory / "choices.jsonl")
              if row.get("event") == "outcome" and row.get("point") == "hunt.station"]
    if logged or (directory / "choices.jsonl").exists():
        return len(logged), sum(1 for row in logged if row.get("won"))
    evidence = directory / "executions.jsonl"
    if not evidence.exists():
        return 0, 0
    visits = list(_hunt_visits(evidence))
    return len(visits), sum(1 for _, won, _ in visits if won)


def motor_counts(store: Path) -> dict[str, tuple[int, int]]:
    """Run id -> (records, qualified) in the motor learner's durable corpus."""
    from jev.play.learning import MotorLearner, _qualified

    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in MotorLearner(store).records():
        counts[row["run_id"]][0] += 1
        counts[row["run_id"]][1] += int(_qualified(row))
    return {run: (n, q) for run, (n, q) in counts.items()}


def _interval(values: list[float], weights: list[float], *, draws: int = 2000) -> tuple[float, float]:
    """A bootstrap 90% interval of the weighted mean: sessions differ in length."""
    if len(values) < 2:
        return (math.nan, math.nan)
    rng = random.Random(7)
    means = []
    for _ in range(draws):
        picks = [rng.randrange(len(values)) for _ in values]
        total = sum(weights[i] for i in picks)
        means.append(sum(values[i] * weights[i] for i in picks) / total if total else math.nan)
    means.sort()
    return means[int(0.05 * draws)], means[int(0.95 * draws)]


def compare(sessions: list[Session]) -> list[dict]:
    """Each arm's totals: rates over its played hours, and an interval on XP/h."""
    by_arm: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        if s.minutes >= MIN_MINUTES:
            by_arm[s.arm or "?"].append(s)
    out = []
    for arm, group in sorted(by_arm.items()):
        hours = sum(s.minutes for s in group) / 60
        loot_total = sum(s.looted + s.unlooted for s in group)
        low, high = _interval([s.xp_per_hour for s in group], [s.minutes for s in group])
        qualified = [s.motor_qualified for s in group if s.motor_qualified is not None]
        out.append({
            "arm": arm, "sessions": len(group), "hours": round(hours, 2),
            "xp_per_hour": round(sum(s.xp for s in group) / hours) if hours else 0,
            "xp_per_hour_90": (round(low), round(high)) if not math.isnan(low) else None,
            "deaths_per_hour": round(sum(s.deaths for s in group) / hours, 2) if hours else 0,
            "kills_per_hour": round(sum(s.kills for s in group) / hours, 1) if hours else 0,
            "steps_per_hour": round(sum(s.steps for s in group) / hours, 1) if hours else 0,
            "loot_rate": round(sum(s.looted for s in group) / loot_total, 2) if loot_total else None,
            "stuck_per_hour": round(sum(s.stuck for s in group) / hours, 1) if hours else 0,
            "tutor_calls_per_hour": round(sum(s.tutor_calls for s in group) / hours, 1) if hours else 0,
            "qualified_per_hour": round(sum(qualified) / hours, 1) if qualified and hours else None,
        })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=int, default=88, help="first session number")
    parser.add_argument("--compare", action="store_true", help="totals per arm")
    parser.add_argument("--skip", type=int, nargs="*", default=[],
                        help="sessions left out of --compare, e.g. ones a fixed bug spoiled")
    parser.add_argument("--csv", type=Path, help="write every session as a CSV row")
    parser.add_argument("--motor", action="store_true",
                        help="count qualified motor examples (reads the learning store)")
    parser.add_argument("--store", type=Path, default=Path(
        "/mnt/c/Users/NAS/AppData/Local/Jev/learning/foreverv2-3c309d13f4025150/motor"))
    args = parser.parse_args(argv)
    table = xp_table()
    exits = _sessions_from_loop(args.since)
    numbers = sorted(set(exits) | {int(m.group(1)) for p in CAPTURES.glob("live-testvvi-*.log")
                                   if (m := re.search(r"(\d+)\.log$", p.name))
                                   and int(m.group(1)) >= args.since})
    sessions = [s for n in numbers if (s := measure(n, table, exits.get(n))) is not None]
    if args.motor and args.store.exists():
        counts = motor_counts(args.store)
        for s in sessions:
            s.motor_records, s.motor_qualified = counts.get(s.run, (0, 0))
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            names = [f for f in asdict(sessions[0]) if f != "skills"] if sessions else []
            writer = csv.DictWriter(handle, fieldnames=names + ["top_skills"])
            writer.writeheader()
            for s in sessions:
                row = {k: v for k, v in asdict(s).items() if k != "skills"}
                row["top_skills"] = ";".join(f"{k}:{v}" for k, v in list(s.skills.items())[:4])
                writer.writerow(row)
    if args.compare:
        for row in compare([s for s in sessions if s.number not in args.skip]):
            print(json.dumps(row))
        return 0
    print(f"{'#':>4} {'arm':12} {'min':>5} {'lvl':>5} {'xp':>6} {'xp/h':>6} {'kill':>4} {'step':>4} {'die':>3} "
          f"{'loot':>7} {'stuck':>5} {'stns':>7} {'tutor':>5} {'t_med':>5} {'money':>6} {'exit':>4}")
    for s in sessions:
        print(f"{s.number:>4} {s.arm[:12]:12} {s.minutes:5.1f} {s.level_start or 0:>2}-{s.level_end or 0:<2} "
              f"{s.xp:6.0f} {s.xp_per_hour:6.0f} {s.kills:>4} {s.steps:>4} {s.deaths:>3} "
              f"{s.looted:>3}/{s.looted + s.unlooted:<3} {s.stuck:>5} "
              f"{s.stations_won:>3}/{s.stations:<3} {s.tutor_calls:>5} "
              f"{s.tutor_median_s if s.tutor_median_s is not None else '-':>5} "
              f"{s.money_delta if s.money_delta is not None else '-':>6} "
              f"{s.exit_code if s.exit_code is not None else '-':>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
