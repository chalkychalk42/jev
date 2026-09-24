"""Generated identities for conservative selling and exact starting-bar restocking."""

from __future__ import annotations

import json
import pathlib
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "content/tbc/vendor-catalog.json"


@dataclass(frozen=True)
class Supply:
    item_id: int
    name: str
    role: str
    desired: int = 10
    slot: int | None = None


@dataclass(frozen=True)
class Merchant:
    entry: int
    name: str
    map_id: int
    world: tuple[float, float, float]
    items: frozenset[int]


@lru_cache(maxsize=1)
def catalog() -> dict:
    raw = json.loads(CATALOG.read_text())
    if raw.get("schema") != 1:
        raise ValueError("unsupported vendor catalog schema")
    return raw


def junk_ids() -> frozenset[int]:
    return frozenset(catalog()["junk"])


def junk_prices() -> dict[int, int]:
    return {int(k): int(v) for k, v in catalog()["junk_prices"].items()}


def bag_slots() -> dict[int, int]:
    """General bags, any item fits, and how many slots each adds."""
    return {int(k): int(v) for k, v in (catalog().get("bags") or {}).items()}


def supplies_for(class_id: int | None, race_id: int | None) -> tuple[Supply, ...]:
    """Exact profile only; another race's food would leave the current bar empty."""
    roles = catalog()["supplies"].get(f"{race_id}:{class_id}", {})
    return tuple(Supply(item_id=r["item_id"], name=r["name"], role=role, slot=r["slot"])
                 for role, r in sorted(roles.items()))


def merchants(map_id: int, *, items: frozenset[int] = frozenset()) -> tuple[Merchant, ...]:
    """All matching spawns on the current world map; caller ranks by world-yard distance.

    Item facts narrow the candidates. Actual merchant identity, cash prices, available
    stock, and transactions must still be observed on arrival.
    """
    return tuple(Merchant(entry=v["entry"], name=v["name"], map_id=v["map_id"],
                          # Normalize older catalog files too; a dataclass annotation
                          # does not convert JSON strings into world-yard numbers.
                          world=tuple(float(value) for value in v["world"]),
                          items=frozenset(v["items"]))
                 for v in catalog()["vendors"]
                 if v["map_id"] == map_id and items <= set(v["items"])
                 and not str(v["name"]).startswith("["))       # "[DND]" placeholders


# -- which merchants answered ----------------------------------------------------------
#
# Goldshire has ten merchants within seventy yards; the three nearest are a fruit seller who
# walks her round, a trade supplier behind a wall and an armourer at the forge, and all
# three failed a full-bag sale (session 65). Whether a merchant can be reached and clicked
# is a fact about the world, not the character, so it is remembered for every character:
# failures count against a merchant until one sale with it succeeds.


def load_merchant_failures(path: pathlib.Path | None) -> dict[int, int]:
    if path is None:
        return {}
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): int(v) for k, v in (raw.get("failures") or {}).items()
            if isinstance(v, int) and v > 0}


def note_merchant(path: pathlib.Path | None, entry: int, *, failed: bool) -> None:
    """Count a failure against a merchant, or clear its count on a sale."""
    if path is None:
        return
    from jev.persist import atomic_json

    failures = load_merchant_failures(path)
    if failed:
        failures[entry] = failures.get(entry, 0) + 1
    else:
        failures.pop(entry, None)
    with suppress(OSError):
        atomic_json(pathlib.Path(path), {"format": 1,
                                         "failures": {str(k): v for k, v in sorted(failures.items())}})
