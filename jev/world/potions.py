"""Healing and mana potions (DECISIONS V555): what each restores and from what level.

Characters carry them from quests and loot, and nothing used them: hive-782, a rogue of 13 with
eight Minor Healing Potions in its bags, went from full health to none in 24 s against two
(9 Oct 00:59). A potion shares a two-minute cooldown with every other (spell category 4), so a
fight drinks one at most. The table is `tools/gen_potions.py`'s.
"""

from __future__ import annotations

import json
import pathlib
from functools import lru_cache

POTIONS = pathlib.Path(__file__).resolve().parents[2] / "content/tbc/potions.json"
# The potions' shared cooldown (`spellcategorycooldown_1`, 120000 ms on every one).
COOLDOWN_S = 120.0
# Drunk in a fight under this much health: a heal's line is 0.40 (`HEAL_IN_COMBAT`), a last
# resort's 0.15; a potion is instant and off the global cooldown, so it waits for a heal first.
HEAL_BELOW = 0.35
# A class that spends mana drinks one under this much mana in a fight, while its health needs
# none: a caster's mana is its damage, and its heals.
MANA_BELOW = 0.12


@lru_cache(maxsize=1)
def table() -> dict[int, dict]:
    try:
        raw = json.loads(POTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): v for k, v in (raw.get("items") or {}).items()}


def kind(item: int | None) -> str | None:
    """"heal" or "mana" for a potion; `None` for anything else."""
    row = table().get(item) if item is not None else None
    return row["kind"] if row else None


def best(rows: dict[int, int], which: str, level: int | None) -> int | None:
    """The potion of `which` kind the bags hold (`rows`, item: count) that restores most and the
    level may use, or `None`."""
    held = [(row["max"], item) for item, row in table().items()
            if row["kind"] == which and rows.get(item, 0) > 0
            and (level is None or row["level"] <= level)]
    return max(held)[1] if held else None
