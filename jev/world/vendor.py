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
    # What the purse keeps back from this one's purchase, where it is not the visit's own
    # (`Vendor.run`'s `reserve_copper`): a hunter's ammunition keeps the repair reserve alone
    # (V393). `None`: the visit's.
    reserve: int | None = None


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


# A hunter's ammunition (V393): the white arrows and bullets merchants sell, 200 rounds a
# purchase, as (the level that may use it, item id, copper a purchase), lowest first
# (world_item_template class 6, RequiredLevel and BuyPrice; each sold by 45-102 of the
# catalog's merchants). Arrows for a bow or a crossbow, bullets for a gun: dwarf and tauren
# hunters begin with a gun (Old Blunderbuss), the others with a bow or, the draenei, a crossbow
# - what every one of the hive's 50 hunters still wore on 7 Oct.
AMMO = {"arrow": ((1, 2512, 10), (10, 2515, 50), (25, 3030, 300), (40, 11285, 1000),
                  (55, 28053, 1600), (65, 28056, 3000)),
        "bullet": ((1, 2516, 10), (10, 2519, 50), (25, 3033, 300), (40, 11284, 1000),
                   (55, 28060, 1600), (65, 28061, 3000))}
AMMO_KIND = {item: kind for kind, rows in AMMO.items() for _, item, _ in rows}
AMMO_STACK = 200                     # the rounds a purchase brings (BuyCount)
GUN_RACES = frozenset({3, 6})
HUNTER = 3
# What a ranged weapon fires, by its kind (item subclass, V401): a bow (2) or a crossbow (18)
# arrows, a gun (3) bullets, as the server checks the ammunition loaded against the weapon
# (`Spell::CheckItems`: SPELL_FAILED_NO_AMMO otherwise).
FIRES = {2: "arrow", 18: "arrow", 3: "bullet"}
# The ammunition a hunter is created with loaded (CharStartOutfit): every one of the hive's 50
# hunters had 2512 or 2516 in its ammo slot on 7 Oct, whatever it carried, for nothing but the
# client's control or the server's own (`CMSG_SET_AMMO`) loads another, and running out leaves
# it loaded (`Spell::TakeAmmo` takes rounds, not the slot). Two of them, Zhudea and Kosdothyt,
# carried 600 Sharp Arrows each (2515, bought by V393 from level 10) and no Rough Arrow: every
# shot "no ammo".
AMMO_STARTING = {"arrow": 2512, "bullet": 2516}


def ammo_kind(race_id: int | None, carried=(), weapon: int | None = None) -> str:
    """What a hunter shoots: what its ranged weapon fires (`weapon`, the worn one's item
    subclass, V401), else the kind of ammunition it carries (`carried`, item ids), else what its
    race's starting weapon takes."""
    if weapon in FIRES:
        return FIRES[weapon]
    for item in carried:
        if item in AMMO_KIND:
            return AMMO_KIND[item]
    return "bullet" if race_id in GUN_RACES else "arrow"


def ammo_rank(item: int | None) -> int:
    """How good a round is among its kind's (`AMMO`, the level that may use it rising with its
    damage): -1 for what is not ammunition."""
    kind = AMMO_KIND.get(item)
    return -1 if kind is None else next(i for i, row in enumerate(AMMO[kind]) if row[1] == item)


def ammo_for(kind: str, level: int | None) -> tuple[tuple[int, int], ...]:
    """The ammunition of `kind` a hunter of `level` may use, best first, as (item id, copper a
    purchase)."""
    return tuple((item, price) for need, item, price in reversed(AMMO.get(kind, ()))
                 if level is None or need <= level)


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


# -- food and drink, kept as a player keeps them (V550) -----------------------------------
#
# A player buys the best food and drink the level can use at the merchants it passes, about
# twenty of each, and eats and drinks whenever it rests. Jev bought only the race's starting
# food (61 health over 18 s), ten of it, only once the bags held none, and only from a merchant
# a 400-yard walk off (V205): in the hive's 8 Oct 04:00-15:30 (a third of the runs, 3,303 of
# them), 1,320 restocks gave up "too far" against 202 that bought, and two thirds to all of a
# class's rest time was spent standing while the body regenerated. What every merchant sells
# is in the catalog's `provisions` (`tools/gen_vendor_catalog.py`): each food's and drink's
# role, the level that may use it - its tier: 1, 5, 15, 25 ... restoring 61, 243, 552, 874
# health or 151, 436, 835, 1,344 mana - its price and what one purchase brings (five).
PROVISIONS_DESIRED = 20
# Under this many of the best a merchant near sells, a merchant a short detour off is visited
# (`LiveBody.larder`); a merchant visited for anything else tops up whatever is under
# `PROVISIONS_DESIRED` less one purchase.
PROVISIONS_LOW = 10
# Food and drink stack twenty to a bag slot.
PROVISION_STACK = 20
# Of the purse above what it keeps (the trainer's due, the repair reserve), a visit spends at
# most this share on food and drink - but a first purchase for a role the bags hold none of:
# the rest is for a wand, a bow, a bag (V398, V403, V260). The hive's characters kept 4 copper
# a kill at levels 6-10 and 7.5 at 11-15 after all they spent (8 Oct 04:00-15:30), and food and
# drink of the level's tier cost about 25 copper a rest each.
PROVISIONS_SHARE = 0.5
# A merchant near is walked to for no fewer purchases than this, from that share.
NEAR_PURCHASES = 2


@dataclass(frozen=True)
class Provision:
    item_id: int
    role: str                        # "food", "drink" or "both"
    level: int                       # the level that may use it: its tier
    price: int                       # copper a purchase
    count: int = 5                   # what one purchase brings
    sell: int = 0                    # what a merchant pays for one


@lru_cache(maxsize=1)
def provisions() -> dict[int, Provision]:
    """Every food and drink a merchant sells for copper, by item (V550)."""
    raw = (catalog().get("provisions") or {}).get("items") or {}
    return {int(k): Provision(item_id=int(k), role=v["role"], level=int(v["level"]),
                              price=int(v["price"]), count=int(v.get("count") or 5),
                              sell=int(v.get("sell") or 0))
            for k, v in raw.items()}


@lru_cache(maxsize=1)
def _provision_sides() -> dict[int, frozenset[str]]:
    raw = (catalog().get("provisions") or {}).get("sides") or {}
    return {int(k): frozenset(v) for k, v in raw.items()}


def provisioner_serves(entry: int, side: str | None) -> bool:
    """Whether a merchant who sells food or drink serves `side`: one the catalog names no side
    for (a creature of no faction a player is friends with) serves none; a side not known is
    served by all."""
    sides = _provision_sides().get(entry)
    return side is None or sides is None or side in sides


def _of_role(item: int, role: str) -> bool:
    row = _consumables().get(item)
    return row is not None and row["role"] in (role, "both")


def stock(rows: dict[int, int], role: str, level: int | None, at_least: int = 0) -> int:
    """How many the bags hold (`rows`, item: count) of `role`'s food or drink a character of
    `level` can use, of tier `at_least` or better: anything eaten or drunk, bought, looted,
    a quest's or conjured (`content/tbc/consumables.json`)."""
    return sum(count for item, count in rows.items()
               if count > 0 and _of_role(item, role)
               and (level is None or _consumables()[item]["level"] <= level)
               and _consumables()[item]["level"] >= at_least)


def best_tier(role: str, level: int | None, sold: frozenset[int] | set[int]) -> int | None:
    """The best tier of `role` that `sold` holds and `level` may use, or `None`."""
    tiers = [p.level for item, p in provisions().items()
             if item in sold and p.role in (role, "both")
             and (level is None or p.level <= level)]
    return max(tiers, default=None)


@dataclass(frozen=True)
class Larder:
    """What the bags hold to eat and drink, as the policy asks it (`Context.larder`, V550),
    from the body's last bag census."""
    roles: tuple[str, ...]            # what the bar keeps that the character does not conjure
    empty: tuple[str, ...] = ()       # of them, none at all in the bags
    price: int | None = None          # the cheapest purchase in the zone of what those eat
    near: tuple[str, ...] = ()        # under `PROVISIONS_LOW`, a merchant a short detour off
    near_price: int | None = None     # the cheapest purchase there


def provision_to_buy(role: str, level: int | None, sold, spare: int | None,
                     held: dict[int, int] | None = None) -> Provision | None:
    """What a merchant selling `sold` sells of `role` for a character of `level` to buy (V550):
    the best tier the level may use whose purchase `spare` pays (all of them for a purse not
    known); of a tier, one the bags already hold first (it stacks), then the lowest item."""
    held = held or {}
    offers = [p for item, p in provisions().items()
              if item in sold and p.role in (role, "both")
              and (level is None or p.level <= level)
              and (spare is None or p.price <= spare)]
    if not offers:
        return None
    return min(offers, key=lambda p: (-p.level, not held.get(p.item_id), p.item_id))


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
    return _for_sale(_wands(), _wand_merchants(map_id, side), class_id, race_id, level, spare,
                     worn, here, yards)


def _for_sale(items: dict[int, tuple[int, int]], sold: tuple[Merchant, ...], class_id: int,
              race_id: int, level: int, spare: int, worn: float, here: tuple[float, float],
              yards: float) -> tuple[Merchant, int, int] | None:
    """Of `items` (item: (level, price)) as `sold` sells them, the best by gear score the
    character can use at its level and pay with `spare`, better than `worn`, from a merchant
    within `yards` of `here`, the nearer of two (V398, V403)."""
    import math

    from jev.world.gear import usable

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


# -- the weapon the class fights with (V403) ---------------------------------------------
#
# A hunter's bow, gun or crossbow is its weapon, and every one of the hive's 42 hunters at 9-13
# still had the one it was created with (7 Oct). Merchants sell bows from level 3 (Hornwood
# Recurve Bow, 4.5 damage a second, 2 silver 85) and 11 (Laminated Recurve Bow, 8.5, 17s 52c;
# Fine Shortbow, 8.8, 31s 85c), guns from 4 (Ornate Blunderbuss, 5.0, 4s 14c) and 9 (Hunter's
# Boomstick, 7.6, 13s 24c), against the starting ones' 3.3; no merchant sells a crossbow below
# level 21 (a draenei's comes from a quest). Worth a trainer's walk, as a wand is.
WEAPON_YARDS = WAND_YARDS


def weapon_for_sale(class_id: int | None, race_id: int | None, level: int | None,
                    spare: int | None, worn: float, map_id: int | None,
                    here: tuple[float, float] | None, side: str | None, *,
                    yards: float = WEAPON_YARDS) -> tuple[Merchant, int, int] | None:
    """The weapon to buy for the ranged slot (V403) as (merchant, item, price), as a wand is
    (`wand_for_sale`): a bow, a gun or a crossbow the character can use (its race's starting
    proficiency, `jev.world.gear.usable`), the best its level allows by damage a second and
    better than `worn`, sold within `yards` of `here` by a merchant of its side for no more
    than `spare` copper. `None` for anything unknown."""
    if None in (class_id, race_id, level, spare, map_id, here, side):
        return None
    return _for_sale(_weapons(), _weapon_merchants(map_id, side), class_id, race_id, level,
                     spare, worn, here, yards)


@lru_cache(maxsize=1)
def _weapons() -> dict[int, tuple[int, int]]:
    """Each bow, gun and crossbow a merchant sells: (the level that may use it, its price)."""
    raw = (catalog().get("weapons") or {}).get("items") or {}
    return {int(k): (int(v["level"]), int(v["price"])) for k, v in raw.items()}


@lru_cache(maxsize=16)
def _weapon_merchants(map_id: int, side: str) -> tuple[Merchant, ...]:
    """The merchants on this map who sell such a weapon and serve this side."""
    sides = (catalog().get("weapons") or {}).get("sides") or {}
    weapons = set(_weapons())
    return tuple(m for m in merchants(map_id)
                 if m.items & weapons and side in sides.get(str(m.entry), ()))
