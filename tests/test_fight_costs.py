"""The fight costs report: fights won and lost by attackers, and the roots the client answered."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fight_costs import fights, roots


def _write(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _tick(t, hp, power=None):
    return {"t": t, "state": {"t": t, "vitals": {"hp": hp, "power": power}}}


def _fight(op, start, end, code):
    return [{"operation": "fight", "operation_id": op, "phase": "begin", "t": start},
            {"operation": "fight", "operation_id": op, "phase": "end", "t": end, "code": code}]


def test_fights_are_told_apart_by_the_most_attackers_counted(tmp_path):
    """A single Prowler won at 20% of health, a pair lost; a fight with no target is no fight."""
    _write(tmp_path / "ticks.jsonl", [_tick(10.0, 1.0, 1.0), _tick(15.0, 0.8, 0.7),
                                      _tick(22.0, 0.9, 0.55), _tick(30.0, 1.0, 0.9),
                                      _tick(35.0, 0.3, 0.2), _tick(40.0, 0.0, 0.04)])
    look = {"operation": "combat.observed"}
    _write(tmp_path / "executions.jsonl", [
        *_fight("a", 10.0, 22.0, "killed"),
        {**look, "t": 12.0, "data": {"combat.attackers": 1}},
        *_fight("b", 30.0, 40.0, "died"),
        {**look, "t": 33.0, "data": {"combat.attackers": 2}},
        {"operation": "ability.request", "t": 34.0, "data": {"slot": 10, "role": "root"}},
        *_fight("c", 41.0, 45.0, "no_target"),
    ])
    won, lost = fights(tmp_path)
    assert (won["attackers"], round(won["lost"], 2), won["seconds"], won["died"],
            won["rooted"]) == (1, 0.2, 12.0, False, False)
    assert (lost["attackers"], lost["died"], lost["rooted"]) == (2, True, True)
    assert (won["mana"], lost["mana"]) == (0.55, 0.04), "the mana left as each ended"


def test_a_root_is_answered_by_its_slot_cooling_or_its_mana_gone(tmp_path):
    """V282: five Frost Nova presses in session 275, the client answering only the last."""
    look = {"operation": "combat.observed"}
    press = {"operation": "ability.request", "data": {"slot": 10, "role": "root"}}
    ready = {"bars.ready": 4095, "vitals.power": 0.81}
    _write(tmp_path / "executions.jsonl", [
        {**look, "t": 9.9, "data": ready}, {**press, "t": 10.0},
        {**look, "t": 12.0, "data": ready},                                        # refused
        {**look, "t": 19.9, "data": ready}, {**press, "t": 20.0},
        {**look, "t": 21.0, "data": {"bars.ready": 3583, "vitals.power": 0.72}},   # cooling
        {**look, "t": 29.9, "data": {"vitals.power": 0.9}}, {**press, "t": 30.0},
        {**look, "t": 30.5, "data": {"vitals.power": 0.8}},                        # mana gone
    ])
    assert roots(tmp_path) == (2, 3)
