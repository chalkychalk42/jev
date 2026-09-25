#!/usr/bin/env python3
"""What a session did, and whether the day's new machinery fired: read-only, WSL.

    tools/session_check.py [N]      session N, or the newest in the loop's log

It prints the session's end, its run, the lines that prove the learned parts loaded
(danger, choices, the band rule), a Traceback if any, XP and deaths from the ticks, kills,
tutor requests, the choice log's events by point, and the evidence of the caster's and
the loot's new moves (range steps, roots, conjures, far-loot steps). For the T-0 checks
(docs/plans/forty-eight-hour-ledger.md) and every session after.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from keep_status import LOOP_LOG, RUNS, deaths, run_summary, session_log, sessions  # noqa: E402

PROOFS = (("danger", r"^danger: .*cells learned"),
          ("choices", r"^choices: .*hunt station visits"),
          ("band rule", r"outgrown at level \d+"),
          ("guide handed over", r"finished; continuing with"))
EVIDENCE = (("range steps", "approach.request", "range_step"),
            ("roots", "engage.root", None),
            ("conjures", "conjure.request", None),
            ("far-loot steps", "loot.step", None))


def _rows(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def check(number: int) -> list[str]:
    out = []
    text = LOOP_LOG.read_text(encoding="utf-8", errors="replace") if LOOP_LOG.exists() else ""
    ended = {n: (code, secs) for n, code, secs in sessions(text)}
    code, secs = ended.get(number, (None, None))
    out.append(f"session {number}: " + (f"exit={code} after {secs}s" if code is not None
                                         else "not ended"))
    log = session_log(number)
    if not log.exists():
        out.append(f"  no log at {log.name}")
        return out
    body = log.read_text(encoding="utf-8", errors="replace")
    found = re.search(r"recording to .*runs[\\/](\S+)", body)
    run = RUNS / found.group(1).strip() if found else None
    out.append(f"  run: {run.name if run else 'none recorded'}")
    for name, pattern in PROOFS:
        line = re.search(pattern, body, re.MULTILINE)
        out.append(f"  [{'ok' if line else '--'}] {name}"
                   + (f": {body[line.start():body.find(chr(10), line.start())].strip()}"
                      if line else ""))
    trace = body.find("Traceback")
    out.append("  [!!] Traceback: " + body[trace:trace + 300].replace("\n", " | ")
               if trace >= 0 else "  [ok] no Traceback")
    out.append(f"  kills: {len(re.findall(r'^COMBAT_PROFILE: killed', body, re.MULTILINE))}")
    if run is None or not run.is_dir():
        return out
    summary = run_summary(run)
    if summary is not None and summary["level"] is not None:
        gain = summary["gain"]
        out.append(f"  level {summary['level']:.3f}"
                   + (f" ({gain:+.3f})" if gain is not None else "")
                   + f", character {summary['key']}")
    ticks = run / "ticks.jsonl"
    if ticks.exists():
        out.append(f"  deaths: {deaths(ticks, since=0)}")
    requests = sum(1 for _ in _rows(run / "play-teacher.jsonl"))
    out.append(f"  tutor records: {requests}")
    points = Counter((row.get("point"), row.get("event")) for row in _rows(run / "choices.jsonl"))
    out.append("  choices: " + (", ".join(f"{p} {e} {n}" for (p, e), n in sorted(points.items()))
                                or "none"))
    seen = Counter()
    for row in _rows(run / "executions.jsonl"):
        for name, operation, mode in EVIDENCE:
            if row.get("operation") == operation and (
                    mode is None or (row.get("data") or {}).get("mode") == mode):
                seen[name] += 1
    out.append("  new moves: " + (", ".join(f"{k} {v}" for k, v in seen.items()) or "none"))
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        number = int(args[0])
    else:
        text = LOOP_LOG.read_text(encoding="utf-8", errors="replace") if LOOP_LOG.exists() else ""
        started = re.findall(r"^session (\d+) start", text, re.MULTILINE)
        if not started:
            print("no session in the loop's log")
            return 1
        number = int(started[-1])
    print("\n".join(check(number)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
