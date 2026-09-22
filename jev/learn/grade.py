"""Grade recorded decisions at +60 seconds. No model and no game required.

Only an explicitly applied decision can receive credit. Old diagnostic recordings with
no decision links remain useful traces; this command never invents labels for them.
Reruns replace this derived stream atomically and leave the source corpus untouched.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, fields, replace
from itertools import pairwise
from pathlib import Path

from jev.learn.episode import DecisionRow, TickRow, grade, read
from jev.world.state_v1 import State


@dataclass
class Report:
    graded: int = 0
    good: int = 0
    pending: int = 0
    unapplied: int = 0
    incomplete: int = 0


def _row(cls, raw):
    return cls(**{f.name: raw[f.name] for f in fields(cls) if f.name in raw})


def grade_run(directory: str | Path, *, window_s: float = 60.0,
              closed: bool = False, max_gap_s: float = 2.0) -> Report:
    """Grade one JSONL run. Incomplete live windows stay pending, not bad.

    A missing observation interval is not sixty seconds of observed safety. Closed runs
    with gaps or short tails get abandoned grades, never positive training labels.
    """
    if window_s <= 0 or max_gap_s <= 0:
        raise ValueError("window and maximum observation gap must be positive")
    directory = Path(directory)
    report = Report()
    ticks_path, decisions_path = directory / "ticks.jsonl", directory / "decisions.jsonl"
    if not ticks_path.exists() or not decisions_path.exists():
        return report
    clients: dict[tuple[str, str], list[TickRow]] = defaultdict(list)
    for raw in read(ticks_path):
        row = _row(TickRow, raw)
        state = State.model_validate(row.state)
        if state.client_id != row.client_id or state.t != row.t:
            raise ValueError(f"tick identity/timestamp disagrees with its state: {row.tick_id}")
        clients[(row.run_id, row.client_id)].append(row)
    for rows in clients.values():
        rows.sort(key=lambda r: (r.t, r.tick_id))
    output = []
    skills_path = directory / "skills.jsonl"
    failed = {(r.get("run_id"), r.get("decision_id")) for r in read(skills_path)
              if r.get("decision_id") and r.get("outcome") in ("aborted", "timed_out", "unknown")} if skills_path.exists() else set()
    seen = set()
    for raw in read(decisions_path):
        decision = _row(DecisionRow, raw)
        identity = (decision.run_id, decision.decision_id)
        if identity in seen:
            raise ValueError(f"duplicate decision identity: {identity}")
        seen.add(identity)
        rows = clients[(decision.run_id, decision.client_id)]
        start = next((i for i, r in enumerate(rows)
                      if r.tick_id == decision.tick_id
                      and r.decision_id == decision.decision_id), None)
        if (start is None or decision.status != "ok" or not decision.intent
                or decision.intent == "escalate"):
            report.unapplied += 1
            continue
        baseline = rows[start]
        if baseline.t != decision.t:
            raise ValueError(f"decision timestamp disagrees with its tick: {identity}")
        deadline = decision.t + window_s
        window = [r for r in rows[start:] if r.t <= deadline]
        covered = bool(rows and rows[-1].t >= deadline)
        gap = any(b.t - a.t > max_gap_s for a, b in pairwise(window))
        gap = gap or deadline - window[-1].t > max_gap_s
        trusted = [r.t for r in window if ((sense := r.state.get("sense", {})).get("addon_ok")
                   or (sense.get("vision_conf") or 0) >= 0.5)]
        gap = gap or (not trusted or trusted[0] != baseline.t
                      or deadline - trusted[-1] > max_gap_s
                      or any(b - a > max_gap_s for a, b in pairwise(trusted)))
        if not covered and not closed:
            report.pending += 1
            continue
        incomplete = not covered or gap
        result = grade(decision, window, window_s=window_s, run_ended=incomplete)
        if identity in failed:
            result = replace(result, good=False)
        output.append(asdict(result))
        report.graded += 1
        report.good += int(result.good)
        report.incomplete += int(incomplete)

    # JSONL is authoritative. A stale parquet derivative must not hide new grades.
    target = directory / "grades.jsonl"
    import tempfile

    fd, name = tempfile.mkstemp(prefix=".grades-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in output:
                handle.write(json.dumps(row, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(target)
    finally:
        Path(name).unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="individual run directories")
    parser.add_argument("--closed", action="store_true", help="runs have ended; abandon short tails")
    parser.add_argument("--window", type=float, default=60.0)
    args = parser.parse_args()
    for directory in args.runs:
        result = grade_run(directory, window_s=args.window, closed=args.closed)
        print(json.dumps({"run": str(directory), **asdict(result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
