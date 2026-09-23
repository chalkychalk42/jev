"""Summarise what a recorded run actually did: fights, facing, loot, progress. Read-only.

    python tools/run_summary.py runs/RUN

Every number comes from the run's own records (`executions.jsonl`, `ticks.jsonl`,
`play-*.jsonl`); nothing is inferred from logs or screenshots.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            break                                   # a crash tail is not a record
    return out


def summarise(run: Path) -> dict:
    executions = _rows(run / "executions.jsonl")
    ticks = _rows(run / "ticks.jsonl")
    t0 = executions[0]["t"] if executions else (ticks[0]["t"] if ticks else 0.0)

    def ended(operation):
        return [r for r in executions if r.get("operation") == operation and r.get("phase") == "end"]

    fights = [r for r in executions if r.get("operation") == "fight.summary"]
    faces = ended("target.face")
    hovers = ended("target.hover")
    loots = [r for r in executions if r.get("operation") == "loot.request"]
    changes = [r for r in executions if r.get("operation") == "loot.change"]
    calibrations = ended("camera.calibrate")
    begins = {r["operation_id"]: r["t"] for r in executions
              if r.get("operation") == "camera.calibrate" and r.get("phase") == "begin"}
    first, last = (ticks[0].get("state") or {}, ticks[-1].get("state") or {}) if ticks else ({}, {})

    def xp(state):
        char = state.get("char") or {}
        return char.get("level"), char.get("xp_pct")

    def quests(state):
        return {q.get("quest_id"): [o.get("have") for o in q.get("objectives") or ()]
                for q in state.get("quests") or ()}

    teaching = _rows(run / "play-actions.jsonl")
    decisions = [r for r in teaching if r.get("event") == "result"]
    tutor = [r for r in _rows(run / "play-teacher.jsonl") if r.get("event") != "transport"]
    episodes = _rows(run / "play-episodes.jsonl")
    return {
        "run": run.name,
        "seconds": round((ticks[-1]["t"] - ticks[0]["t"]) if len(ticks) > 1 else 0.0, 1),
        "fights": dict(Counter(r.get("code") for r in fights)),
        "fight_details": [{"t": round(r["t"] - t0, 1), "outcome": r.get("code"),
                           "detail": (r.get("detail") or "")[:80],
                           "strides": (r.get("data") or {}).get("closed"),
                           "pressed": (r.get("data") or {}).get("pressed_slots")} for r in fights],
        "facing": dict(Counter(r.get("code") for r in faces)),
        "turns_per_face": round(sum((r.get("data") or {}).get("turns") or 0 for r in faces)
                                / max(1, len(faces)), 2),
        "hovers": dict(Counter(r.get("code") for r in hovers)),
        "loot_clicks": dict(Counter(r.get("code") for r in loots)),
        "loot_taken": [r.get("detail") for r in changes],
        "camera_calibration_s": [round(r["t"] - begins.get(r["operation_id"], r["t"]), 1)
                                 for r in calibrations],
        "level_xp": {"start": xp(first), "end": xp(last)},
        "quest_counters": {"start": quests(first), "end": quests(last)},
        "money_copper": {"start": (first.get("bags") or {}).get("money_copper"),
                         "end": (last.get("bags") or {}).get("money_copper")},
        "tutor": {"decisions": len(decisions),
                  "replies": dict(Counter(r.get("status") for r in tutor)),
                  "actions": dict(Counter(
                      ((r.get("action") or {}).get("name") or (r.get("action") or {}).get("control")
                       or (r.get("action") or {}).get("kind")) for r in decisions)),
                  "episodes": dict(Counter(r.get("code") for r in episodes)),
                  "scripted_fallbacks": sum(r.get("event") == "scripted_fallback" for r in teaching)},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(summarise(args.run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
