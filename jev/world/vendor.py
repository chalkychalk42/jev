"""Generated identities for conservative selling and exact starting-bar restocking."""

from __future__ import annotations

import json
import pathlib
import time
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


def bag_prices() -> dict[int, int]:
    """What a merchant asks for each general bag it sells, in copper (V260)."""
    return {int(k): int(v) for k, v in (catalog().get("bag_prices") or {}).items()}


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
# failures count against a merchant until one sale with it succeeds, or until they are
# `FAILURE_KEEP_S` old (V321).
#
# A failure kept for good outlived its cause. For 34 hours from 22:56 on 27 Sep the client
# drew no friendly nameplate (V320), and every visit failed: the file came to count 1-11
# failures against 53 merchants, William MacGregor on Sentinel Hill 11 (2,750 yards), and
# none cleared, as none sold. From Sentinel Hill the ranking then put first merchants 1,350-
# 1,600 yards off that had never been tried, Darkshire's in Duskwood among them, past
# MacGregor 162 yards off: the level 16 mage's bag and repair walks into Duskwood's level
# 18-25 units ended in three deaths in session 419 alone. Shown the plates again, it would have
# gone on so, as a sale far off undoes no failure near. A merchant that really cannot be
# clicked fails again on its next visit: one walk in two hours.
FAILURE_KEEP_S = 7200.0
MEMORY_FORMAT = 2


def _failure_times(raw: dict) -> dict[int, list[float]]:
    """Each merchant's failures as wall times. Format 1 kept counts without times, and a
    count whose age cannot be told is not counted: most of the one on the live bot's disk
    was written while no friendly plate was drawn."""
    if raw.get("format") != MEMORY_FORMAT:
        return {}
    kept: dict[int, list[float]] = {}
    for key, times in (raw.get("failures") or {}).items():
        if isinstance(times, list):
            stamps = [float(t) for t in times
                      if isinstance(t, (int, float)) and not isinstance(t, bool)]
            if stamps:
                with suppress(ValueError):
                    kept[int(key)] = stamps
    return kept


def _read_memory(path: pathlib.Path | None) -> dict[int, list[float]]:
    if path is None:
        return {}
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _failure_times(raw) if isinstance(raw, dict) else {}


def _recent(times: list[float], now: float) -> list[float]:
    """The failures within `FAILURE_KEEP_S` of `now`, either way round: a wall clock set back
    (the WSL clock, 28 Sep) does not keep one for good."""
    return [t for t in times if abs(now - t) < FAILURE_KEEP_S]


def load_merchant_failures(path: pathlib.Path | None, now: float | None = None) -> dict[int, int]:
    """Each merchant's failures since its last sale and within `FAILURE_KEEP_S` of `now`."""
    now = time.time() if now is None else now
    counts = {entry: len(_recent(times, now)) for entry, times in _read_memory(path).items()}
    return {entry: count for entry, count in counts.items() if count > 0}


def note_merchant(path: pathlib.Path | None, entry: int, *, failed: bool,
                  now: float | None = None) -> None:
    """Count a failure against a merchant, or clear its count on a sale. Failures past
    `FAILURE_KEEP_S` are dropped as the file is written."""
    if path is None:
        return
    from jev.persist import atomic_json

    now = time.time() if now is None else now
    failures = {key: kept for key, times in _read_memory(path).items()
                if (kept := _recent(times, now))}
    if failed:
        failures[entry] = [*failures.get(entry, []), now]
    else:
        failures.pop(entry, None)
    with suppress(OSError):
        atomic_json(pathlib.Path(path), {"format": MEMORY_FORMAT,
                                         "failures": {str(k): v for k, v in sorted(failures.items())}})


# -- wands (V398) ----------------------------------------------------------------------
#
# A priest, mage or warlock shoots a wand where it would stand out of mana (V397), and the
# world sells wands only from level 15 (the Smoldering Wand, 33 silver 40), at a wand merchant
# in six capitals - Stormwind, Ironforge, the Undercity, Orgrimmar, the Exodar, Silvermoon -
# none in Darnassus or Thunder Bluff. Below 15 a wand is a quest's reward or a drop. One is
# worth a trainer's walk (`jev.world.training.MAX_TRAINER_YARDS`).
WAND_YARDS = 1500.0


def wand_for_sale(class_id: int | None, race_id: int | None, level: int | None,
                  spare: int | None, worn: float, map_id: int | None,
                  here: tuple[float, float] | None, side: str | None, *,
                  yards: float = WAND_YARDS) -> tuple[Merchant, int, int] | None:
    """The wand to buy (V398) as (merchant, item, price): the best its level allows by its
    gear score (`jev.world.gear.usable`, damage a second) and better than `worn`, the ranged
    slot's, that a merchant of its side within `yards` of `here` sells for no more than
    `spare` copper; the nearer of two merchants. `None` for anything unknown."""
    import math

    from jev.world.gear import usable

    if None in (class_id, race_id, level, spare, map_id, here, side):
        return None
    items, sold = _wands(), _wand_merchants(map_id, side)
    if not sold or not items or level < min(level_ for level_, _ in items.values()):
        return None                                  # asked at every policy look: cheap first
    best, best_key = None, None
    for merchant in sold:
        yards_off = math.dist(merchant.world[:2], here[:2])
        if yards_off > yards:
            continue
        for item in sorted(merchant.items & items.keys()):
            need, price = items[item]
            if need > level or price > spare:
                continue
            piece = usable(item, class_id, race_id, level)
            if piece is None or piece.score <= worn:
                continue
            key = (piece.score, -yards_off)
            if best_key is None or key > best_key:
                best, best_key = (merchant, item, price), key
    return best


@lru_cache(maxsize=1)
def _wands() -> dict[int, tuple[int, int]]:
    """Each wand a merchant sells: (the level that may use it, its price)."""
    raw = (catalog().get("wands") or {}).get("items") or {}
    return {int(k): (int(v["level"]), int(v["price"])) for k, v in raw.items()}


@lru_cache(maxsize=16)
def _wand_merchants(map_id: int, side: str) -> tuple[Merchant, ...]:
    """The merchants on this map who sell a wand and serve this side."""
    sides = (catalog().get("wands") or {}).get("sides") or {}
    wands = set(_wands())
    return tuple(m for m in merchants(map_id)
                 if m.items & wands and side in sides.get(str(m.entry), ()))
