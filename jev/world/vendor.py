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
    repairs: bool = False            # mends gear (the server's repair flag)


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


@dataclass(frozen=True)
class Innkeeper:
    entry: int
    name: str
    map_id: int
    world: tuple[float, float, float]


def innkeepers(map_id: int, side: str | None) -> tuple[Innkeeper, ...]:
    """Innkeepers on this world map who serve this side; caller ranks by distance."""
    return tuple(Innkeeper(entry=i["entry"], name=i["name"], map_id=i["map_id"],
                           world=tuple(float(v) for v in i["world"]))
                 for i in catalog().get("innkeepers") or ()
                 if i["map_id"] == map_id and (side is None or side in i.get("sides", ())))


@dataclass(frozen=True)
class FlightMaster:
    entry: int
    name: str
    map_id: int
    world: tuple[float, float, float]
    gossip: str | None = None        # the line that opens the map; none opens it directly


def flightmasters(map_id: int, side: str | None) -> tuple[FlightMaster, ...]:
    """Flight masters on this world map who serve this side; caller ranks by distance."""
    return tuple(FlightMaster(entry=f["entry"], name=f["name"], map_id=f["map_id"],
                              world=tuple(float(v) for v in f["world"]),
                              gossip=f.get("gossip"))
                 for f in catalog().get("flightmasters") or ()
                 if f["map_id"] == map_id and (side is None or side in f.get("sides", ())))


def supply_prices() -> dict[int, int]:
    """What one purchase of each food and drink the profiles use costs, in copper (V195)."""
    return {int(r["item_id"]): int(r.get("price") or 0)
            for roles in (catalog().get("supplies") or {}).values() for r in roles.values()}


def surplus_prices() -> dict[int, int]:
    """White and green gear, trade goods and recipes no quest needs, with their sell prices."""
    return {int(k): int(v) for k, v in (catalog().get("surplus_prices") or {}).items()}


def bag_slots() -> dict[int, int]:
    """General bags, any item fits, and how many slots each adds."""
    return {int(k): int(v) for k, v in (catalog().get("bags") or {}).items()}


# A caster drinks after most fights, so it carries twice the water (V164).
CASTER_DRINKS = 20


def supplies_for(class_id: int | None, race_id: int | None) -> tuple[Supply, ...]:
    """Exact profile only; another race's food would leave the current bar empty."""
    from jev.world.combat import for_class

    caster = for_class(class_id, race_id).caster
    roles = catalog()["supplies"].get(f"{race_id}:{class_id}", {})
    return tuple(Supply(item_id=r["item_id"], name=r["name"], role=role, slot=r["slot"],
                        desired=CASTER_DRINKS if caster and role == "drink" else 10)
                 for role, r in sorted(roles.items()))


CONSUMABLES = pathlib.Path(__file__).resolve().parents[2] / "content/tbc/consumables.json"


@lru_cache(maxsize=1)
def _consumables() -> dict[int, dict]:
    try:
        raw = json.loads(CONSUMABLES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): v for k, v in (raw.get("items") or {}).items()}


def consumable_role(item_id: int | None) -> str | None:
    """"food", "drink" or "both" for a food or drink; `None` for anything else."""
    row = _consumables().get(item_id) if item_id is not None else None
    return row["role"] if row else None


def consumables(role: str, level: int | None) -> tuple[int, ...]:
    """The foods (`role` "food") or drinks ("drink") a character of `level` can use, best
    first: conjured before bought (it is free, and gone at logout), then the higher item
    level (`tools/gen_consumables.py`, V166)."""
    usable = [(item, row) for item, row in _consumables().items()
              if row["role"] in (role, "both") and (level is None or row["level"] <= level)]
    usable.sort(key=lambda pair: (not pair[1]["conjured"], -pair[1]["item_level"], pair[0]))
    return tuple(item for item, _ in usable)


def merchants(map_id: int, *, items: frozenset[int] = frozenset()) -> tuple[Merchant, ...]:
    """All matching spawns on the current world map; caller ranks by world-yard distance.

    Item facts narrow the candidates. Actual merchant identity, cash prices, available
    stock, and transactions must still be observed on arrival.
    """
    return tuple(Merchant(entry=v["entry"], name=v["name"], map_id=v["map_id"],
                          # Normalize older catalog files too; a dataclass annotation
                          # does not convert JSON strings into world-yard numbers.
                          world=tuple(float(value) for value in v["world"]),
                          items=frozenset(v["items"]), repairs=v.get("repairs") is True)
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
