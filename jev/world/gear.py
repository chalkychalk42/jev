"""Which items in the bags are worth wearing: the upgrade decision, from catalog facts.

A level 8 paladin fought the night in its starting clothes and its Worn Mace (1-3 damage)
while its bags held a Militia Hammer (3-6), a Pikeman Shield (55 armour), Loose Chain
Gloves (48), an Outfitter Belt (19), a cloak and shoes - quest rewards and loot the bot had
never put on (run 20260924T090629-93a85b). Bags were equipped; gear never was.

The strip does not say what is worn, so the bot remembers what it put on, slot by slot, in
a file beside the character's playhead. A slot it has never filled is taken to be empty or
worse: a fresh character's is. Whatever comes off lands in the bags, where a later look
compares it against the remembered score and leaves it - so a wrong first guess is undone
by the next look, not repeated.

Only what a fresh character of the class can use: its starting proficiencies (Mail,
One-Handed Maces, Shield...) from the world database, its level, and the item's class and
race masks. Two-handers are left out: one would take the shield off.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import cache

from jev.persist import atomic_json

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "content/tbc/gear-catalog.json"


@dataclass(frozen=True)
class Piece:
    item_id: int
    slot: str
    score: float


# Past any item's required level: `keep` asks whether a piece will ever be worth wearing.
MAX_LEVEL = 255


@cache
def catalog(path: pathlib.Path = CATALOG) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}, "proficiencies": {}}


def usable(item_id: int, class_id: int | None, race_id: int | None, level: int | None,
           facts: dict | None = None) -> Piece | None:
    """The item as a piece this character can wear now, or `None`."""
    facts = facts if facts is not None else catalog()
    item = facts["items"].get(str(item_id))
    if item is None or class_id is None or race_id is None or level is None:
        return None
    if item["level"] > level:
        return None
    if item["classes"] not in (-1, 0) and not item["classes"] & (1 << (class_id - 1)):
        return None
    if item["races"] not in (-1, 0) and not item["races"] & (1 << (race_id - 1)):
        return None
    known = {tuple(k) for k in facts["proficiencies"].get(f"{race_id}:{class_id}", ())}
    if tuple(item["kind"]) not in known:
        return None
    return Piece(item_id=item_id, slot=item["slot"], score=float(item["score"]))


def upgrades(bag_items: Iterable[int], worn: Mapping[str, float], *, class_id: int | None,
             race_id: int | None, level: int | None, facts: dict | None = None) -> list[Piece]:
    """The best usable bag item for each slot that beats what is remembered worn there."""
    best: dict[str, Piece] = {}
    for item_id in set(bag_items):
        piece = usable(item_id, class_id, race_id, level, facts)
        if piece is None or piece.score <= worn.get(piece.slot, 0.0):
            continue
        if piece.slot not in best or piece.score > best[piece.slot].score:
            best[piece.slot] = piece
    return sorted(best.values(), key=lambda p: p.slot)


def keep(bag_items: Iterable[int], worn: Mapping[str, float], *, class_id: int | None,
         race_id: int | None, facts: dict | None = None) -> frozenset[int]:
    """Bag gear worth keeping: better than what is remembered worn in its slot, now or once
    the character reaches the item's level. The rest of the gear may be sold."""
    kept = set()
    for item_id in set(bag_items):
        piece = usable(item_id, class_id, race_id, MAX_LEVEL, facts)
        if piece is not None and piece.score > worn.get(piece.slot, 0.0):
            kept.add(item_id)
    return frozenset(kept)


def load_worn(path: pathlib.Path | None) -> dict[str, float]:
    """What the bot remembers putting on, slot to score. Missing or unreadable is empty."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    slots = data.get("slots") if isinstance(data, dict) else None
    return {k: float(v["score"]) for k, v in (slots or {}).items()
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float))}


def save_worn(path: pathlib.Path | None, pieces: Iterable[Piece]) -> None:
    """Remember pieces put on, over what was remembered before."""
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slots = dict(data.get("slots") or {})
    except (OSError, ValueError, AttributeError):
        slots = {}
    for piece in pieces:
        slots[piece.slot] = {"item_id": piece.item_id, "score": piece.score}
    with suppress(OSError):
        atomic_json(path, {"format": 1, "slots": slots})
