"""Summarise what a recorded run actually did: fights, facing, loot, progress. Read-only.

    python tools/run_summary.py runs/RUN [--store LEARNING_STORE]

Every number comes from the run's own records (`executions.jsonl`, `ticks.jsonl`,
`play-*.jsonl`); nothing is inferred from logs or screenshots. `learner` is what the motor
learner holds and what this run added to it, by the learner's own qualification rules and
training gates: game progress alone does not say whether the tutor's play taught anything.
The store defaults to the one the teaching launcher records (`var/teaching-launch.json`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]


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


def default_store() -> Path | None:
    """The learning store the teaching launcher passes, as a path this Python can open."""
    launch = ROOT / "var" / "teaching-launch.json"
    if not launch.exists():
        return None
    args = json.loads(launch.read_text(encoding="utf-8")).get("args") or []
    if "--learning-store" not in args[:-1]:
        return None
    value = args[args.index("--learning-store") + 1]
    windows = PureWindowsPath(value)
    if sys.platform != "win32" and windows.drive:
        return Path("/mnt", windows.drive.rstrip(":").lower(), *windows.parts[1:])
    return Path(value)


def learner(run_id: str, store: Path | None) -> dict | None:
    """What the motor learner holds, and what this run added, by its own rules and gates."""
    if store is None or not (store / "motor").exists():
        return None
    sys.path.insert(0, str(ROOT))
    from jev.play.learning import LearningConfig, MotorLearner, _label, _qualified

    motor = MotorLearner(store / "motor")
    config = LearningConfig()
    need_runs = config.min_train_runs + config.min_holdout_runs
    need_rows = config.min_train_examples + config.min_holdout_examples
    records = motor.records()
    usable = [r for r in records if _qualified(r) and _label(r.get("action") or {})]
    progress = {}
    for capability in sorted({r.get("capability") for r in records if r.get("capability")}):
        mine = [r for r in usable if r.get("capability") == capability]
        progress[capability] = {
            "runs": f"{len({r['run_id'] for r in mine})}/{need_runs}",
            "examples": f"{len(mine)}/{need_rows}",
            "this_run": sum(r["run_id"] == run_id for r in mine),
            "this_run_actions": sum(r["run_id"] == run_id and r.get("capability") == capability
                                    for r in records),
        }
    return {"trained": sorted(motor.status().get("capabilities") or {}), "capabilities": progress}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--store", type=Path, default=None,
                        help="learning store (default: the teaching launcher's)")
    args = parser.parse_args(argv)
    summary = summarise(args.run)
    summary["learner"] = learner(args.run.name, args.store or default_store())
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
