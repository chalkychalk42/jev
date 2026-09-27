#!/usr/bin/env python3
"""Each death a session recorded: the step it died on, the skill then armed, its health and mana
when the fight began, the most attackers counted, and the units it had selected.

    python tools/deaths_report.py 244 250 253        # sessions by number (captures/live-N.log)

Read-only: a session's log names its run, whose ticks and executions are read here with the
world database's creature names. Nothing here attaches to a client. It is how the level 10-12
mage's deaths were found to be fights with two to six attackers (V269-V275, 27 September).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from session_report import DB, RUNS, _jsonl, _run_of, session_log  # noqa: E402

# How far before a death the fight that killed it is looked for.
WINDOW_S = 40.0


def creature_names(db: Path = DB) -> dict[int, str]:
    """Each name the strip can paint, as "Name lo-hi" (the lowest levels first)."""
    from jev.perceive.radio_frame import name_id

    names: dict[int, str] = {}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            for name, low, high in con.execute(
                    "select Name, MinLevel, MaxLevel from world_creature_template "
                    "where MinLevel between 1 and 70 order by MinLevel"):
                if name:
                    names.setdefault(name_id(name), f"{name} {low}-{high}")
    except sqlite3.Error:
        pass
    return names


def deaths(run: Path, names: dict[int, str] | None = None) -> list[dict]:
    """The run's deaths: when, the step, the skill armed, health and mana at the first look in
    combat within `WINDOW_S` before, the most attackers counted, and the units selected."""
    names = names or {}
    ticks = list(_jsonl(run / "ticks.jsonl"))
    looks = [e for e in _jsonl(run / "executions.jsonl") if e.get("operation") == "combat.observed"]
    out, dead_before = [], False
    for tick in ticks:
        state = tick.get("state") or {}
        dead = bool((state.get("vitals") or {}).get("dead"))
        if dead and not dead_before:
            at = state.get("t") or tick.get("t")
            window = [e for e in looks if at - WINDOW_S <= e.get("t", 0) <= at]
            data = [e.get("data") or {} for e in window]
            first = next((d for d in data if d.get("vitals.combat")), {})
            selected = Counter(d.get("target.name_id") for d in data if d.get("target.name_id"))
            out.append({
                "t": at,
                "step": (state.get("guide") or {}).get("step_id"),
                "skill": tick.get("armed_skill"),
                "hp": first.get("vitals.hp"),
                "mana": first.get("vitals.power"),
                "attackers": max((d.get("combat.attackers") or 0 for d in data), default=0),
                "units": [names.get(k, str(k)) for k, _ in selected.most_common(3)],
            })
        dead_before = dead
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=int, nargs="+")
    args = parser.parse_args(argv)
    names = creature_names()
    for number in args.sessions:
        log = session_log(number)
        run = _run_of(log) if log is not None and log.exists() else None
        if run is None:
            print(f"{number}: no run found")
            continue
        for d in deaths(RUNS / run, names):
            hp = "?" if d["hp"] is None else f"{d['hp']:.2f}"
            mana = "?" if d["mana"] is None else f"{d['mana']:.2f}"
            print(f"{number} {(d['step'] or '')[-34:]} {d['skill']} hp {hp} mana {mana} "
                  f"attackers {d['attackers']} {d['units']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
