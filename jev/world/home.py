"""Where the hearthstone takes this character. Remembered, because the strip does not say.

A human's hearthstone starts bound to Northshire Abbey, and every recovery by hearthstone -
a death trap, a wedged walk - sent a level 9 questing round Goldshire and Fargodeep back
there: four times on 24 September, the last one onto the vineyard hill session 74 could
not leave. So the character binds itself at the inn nearest the guide's work
(`LiveBody._bind`), and what it bound, or where a hearthstone last set it down, is kept
beside its playhead.
"""

from __future__ import annotations

import json
import pathlib

from jev.persist import atomic_json

Point = tuple[float, float, float]


def load_home(path: pathlib.Path | None) -> Point | None:
    """The remembered home in world yards, or `None` when it is not known."""
    if path is None:
        return None
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        world = data["world"]
        return float(world[0]), float(world[1]), float(world[2])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def save_home(path: pathlib.Path | None, world: Point, *, name: str) -> None:
    """Remember a home. Best effort: failing to remember must not fail the run."""
    if path is None:
        return
    try:
        atomic_json(pathlib.Path(path), {"format": 1, "name": name,
                                         "world": [float(v) for v in world]})
    except OSError:
        pass
