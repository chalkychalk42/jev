"""The deaths report: each death's step, skill, health and mana as the fight began, attackers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from deaths_report import deaths  # noqa: E402


def _write(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_a_death_is_reported_with_the_fight_that_ended_it(tmp_path):
    """Two Prowlers at a level 10 mage from full health (session 247)."""
    def tick(t, dead, skill="COMBAT_PROFILE"):
        return {"t": t, "armed_skill": skill,
                "state": {"t": t, "vitals": {"dead": dead},
                          "guide": {"step_id": "alli_human_1_12_grind_elwynn_9_11"}}}

    _write(tmp_path / "ticks.jsonl", [tick(100.0, False), tick(130.0, True, "RELEASE_SPIRIT")])
    look = {"operation": "combat.observed"}
    _write(tmp_path / "executions.jsonl", [
        {**look, "t": 50.0, "data": {"vitals.combat": True, "vitals.hp": 0.3}},     # before
        {**look, "t": 100.0, "data": {"vitals.combat": True, "vitals.hp": 0.96,
                                      "vitals.power": 1.0, "combat.attackers": 1,
                                      "target.name_id": 25085}},
        {**look, "t": 120.0, "data": {"vitals.combat": True, "vitals.hp": 0.4,
                                      "combat.attackers": 2, "target.name_id": 25085}},
    ])
    (death,) = deaths(tmp_path, {25085: "Prowler 9-10"})
    assert death["step"] == "alli_human_1_12_grind_elwynn_9_11"
    assert death["skill"] == "RELEASE_SPIRIT"
    assert (death["hp"], death["mana"], death["attackers"]) == (0.96, 1.0, 2)
    assert death["units"] == ["Prowler 9-10"]
