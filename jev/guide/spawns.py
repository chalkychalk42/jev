"""Where each hunt's target actually spawns: routine data kept beside the guide, not in it.

A hunt stands on these points, nearest the cluster's centre first, instead of on rings
round a centre the mobs may not be near (`jev.run.hunt.spawn_stations`).

Beside the guide rather than in it, deliberately. The guide file's bytes are part of the
tutor's knowledge fingerprint (`jev.play.knowledge`), and the motor learner trains on the
newest fingerprint only, so writing sixteen points into every hunting step would have
started the learner's corpus again. The tutor never needs spawn lists; the scripted hunt
does. So they live in `<guide>.spawns.json`, keyed by step id, and by `step#creature` for
a step with more than one creature to hunt.
"""

from __future__ import annotations

import json
from pathlib import Path

from jev.persist import atomic_json

Point = tuple[float, float, float]


def path_for(guide: str | Path) -> Path:
    guide = Path(guide)
    return guide.with_name(f"{guide.stem}.spawns.json")


def save(guide: str | Path, table: dict[str, list]) -> Path:
    path = path_for(guide)
    atomic_json(path, {"format": 1, "spawns": dict(sorted(table.items()))})
    return path


def load(guide: str | Path) -> dict[str, tuple[Point, ...]]:
    """The table for a guide, or empty: a hunt without it walks rings, as it always did."""
    try:
        document = json.loads(path_for(guide).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("format") != 1:
        return {}
    return {key: tuple(tuple(float(v) for v in point) for point in points)
            for key, points in (document.get("spawns") or {}).items()}


def lookup(table: dict[str, tuple[Point, ...]], step_id: str,
           creature_id: int | None = None) -> tuple[Point, ...]:
    """The points for one creature of a step if known, else the step's own."""
    if creature_id is not None and f"{step_id}#{creature_id}" in table:
        return table[f"{step_id}#{creature_id}"]
    return table.get(step_id, ())
