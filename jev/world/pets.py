"""A hunter's pet, from the world snapshot (V389): its spells, the beasts a hunter can tame and
where they spawn, what each pet eats, and what a food is worth to it.

Taming the Beast's last quest teaches Tame Beast, Call Pet and Dismiss Pet (spell 1579), and
Training the Beast teaches Beast Training, Feed Pet and Revive Pet (5300); no trainer in the
world database teaches any of them (V388). The server's rules, as CMaNGOS has them:

- A beast is tameable when it is of the beast type, of a family, and flagged tameable
  (`CreatureInfo::isTameable`); a beast above the hunter's level is refused
  (PETTAME_TOOHIGHLEVEL), and so is any taming while a pet is out, a charm held, or a pet kept
  alive or dead (Tame Beast's SPELL_ATTR_EX2_NO_ACTIVE_PETS). The pet keeps the beast's level.
- A pet eats what its family's diet holds (`CreatureFamily.petFoodMask`, one bit for each of
  `item_template.FoodType` 1-8: meat, fish, cheese, bread, fungus, fruit, raw meat, raw fish),
  and a food is worth 35,000 happiness a tick to a pet at most five levels above it, 17,000 to
  one six to ten above, 8,000 to one eleven to fourteen above and nothing beyond
  (`Pet::GetCurrentFoodBenefitLevel`); Feed Pet's effect ticks ten times (spell 1539).
- Feed Pet eats its food at once and gives its happiness through that effect: an aura on the
  pet that ticks every 2 s for 20 s, the first tick 2 s after the feed (`EffectAmplitude1`
  2,000, `DurationIndex` 18). A second feed while it runs replaces it (`Unit::
  AddSpellAuraHolder`, AURA_REMOVE_BY_STACK): the first one's food is gone and its ticks yet
  to come with it, so feeds a second apart eat a food each and give nothing (V490).
- A pet tamed starts unhappy (166,500 of 1,050,000; 333,000 a state, `Pet::
  CreateBaseAtCreature`) and loses happiness at 70,000 a minute at loyalty 1, half that at 2
  (`Pet::LooseHappiness`, half as fast again in combat); unhappy, its loyalty falls 100 a minute
  (`Pet::TickLoyaltyChange`), content it rises 50 and happy 100, and below none, from 1,000 at
  the taming, it may run away (one time in three: one in three it turns aggressive, one in
  three stays where it is, passive, `Pet::ModifyLoyalty`). Unhappy it does 75% of its damage,
  happy 125% (`Pet::GetConditionalTotalPhysicalDamageModifier`).
- Tame Beast costs 48% of the hunter's base mana and Revive Pet 80% (`ManaCostPercentage`).

Read from the snapshot once a process; nothing without it.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"

HUNTER = 3
TAME_BEAST = 1515
CALL_PET = 883
DISMISS_PET = 2641
REVIVE_PET = 982
FEED_PET = 6991
BEAST_TRAINING = 5149

# `HappinessState`, as the bridge's "happy" and the client's `GetPetHappiness` give it.
UNHAPPY, CONTENT, HAPPY = 1, 2, 3
# Feed Pet's effect (spell 1539): how long it runs and how often it ticks. A feed cast while one
# runs replaces it before the rest of its ticks (V490).
FEED_EFFECT = 1539
FEED_EFFECT_S = 20.0
FEED_TICK_S = 2.0
# `Pet::GetCurrentFoodBenefitLevel`: the levels above a food's item level a pet may stand and
# still get each share of it, best first.
BENEFITS = ((5, 35000), (10, 17000), (14, 8000))
FULL_BENEFIT = BENEFITS[0][1]
# `creature_template`: the beast type, and the tameable flag of `CreatureTypeFlags`.
BEAST = 1
TAMEABLE = 0x1
# Spawns are kept in cells this wide, as the hostile spawns are (`jev.world.hostiles`).
CELL_YARDS = 60.0


def _connect(path: Path):
    from jev.play.world_knowledge import readonly_uri

    return contextlib.closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=1))


def benefit(pet_level: int | None, item_level: int | None) -> int:
    """What one tick of a food of `item_level` gives a pet of `pet_level`: 35,000, 17,000,
    8,000 or nothing. Either unknown, nothing."""
    if not isinstance(pet_level, int) or not isinstance(item_level, int):
        return 0
    return next((value for above, value in BENEFITS if pet_level <= item_level + above), 0)


@dataclass(frozen=True)
class Food:
    """A food a pet may eat: its kind (`FoodType`), its item level, and what a merchant asks
    for one purchase of it (`BuyPrice`, for `BuyCount` of them)."""

    entry: int
    name: str
    kind: int
    item_level: int
    price: int
    count: int

    @property
    def bit(self) -> int:
        """Its bit in a diet (`convertEnumToFlag(FoodType)`)."""
        return 1 << (self.kind - 1)


@lru_cache(maxsize=4)
def foods(path: Path = WORLD_DB) -> dict[int, Food]:
    """Every item with a food type, by entry; none without the snapshot."""
    if not Path(path).is_file():
        return {}
    try:
        with _connect(Path(path)) as db:
            return {r[0]: Food(int(r[0]), str(r[1]), int(r[2]), int(r[3] or 0), int(r[4] or 0),
                               max(1, int(r[5] or 1)))
                    for r in db.execute("select entry, name, FoodType, ItemLevel, BuyPrice, "
                                        "BuyCount from world_item_template where FoodType > 0")}
    except sqlite3.Error:
        return {}


@lru_cache(maxsize=4)
def diets(path: Path = WORLD_DB) -> dict[int, int]:
    """Each creature family's diet, its `petFoodMask` (`CreatureFamily.dbc` column 7)."""
    if not Path(path).is_file():
        return {}
    try:
        with _connect(Path(path)) as db:
            return {int(r[0]): int(r[1] or 0)
                    for r in db.execute("select id, c7 from dbc_CreatureFamily")}
    except sqlite3.Error:
        return {}


@lru_cache(maxsize=4096)
def family_of(entry: int | None, path: Path = WORLD_DB) -> int | None:
    """The family of the creature `entry` (a tamed pet keeps its beast's entry)."""
    if not entry or not Path(path).is_file():
        return None
    try:
        with _connect(Path(path)) as db:
            row = db.execute("select Family from world_creature_template where Entry = ?",
                             (int(entry),)).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] else None


def eats(family: int | None, item: int | None, path: Path = WORLD_DB) -> bool:
    """Is `item` in the diet of a pet of `family`?"""
    food = foods(path).get(item) if item else None
    return food is not None and bool(diets(path).get(family or 0, 0) & food.bit)


def worth(family: int | None, pet_level: int | None, item: int | None,
          path: Path = WORLD_DB) -> int:
    """What a tick of `item` gives a pet of `family` and `pet_level`: nothing outside its diet."""
    if not eats(family, item, path):
        return 0
    return benefit(pet_level, foods(path)[item].item_level)


def in_bags(family: int | None, pet_level: int | None, counts: dict[int, int],
            path: Path = WORLD_DB) -> tuple[int | None, int]:
    """What of the bags' `counts` (item -> how many) a pet of `family` and `pet_level` eats:
    the food worth most to it, the lower item level first of those worth as much (the cheaper
    one goes first), `None` for none; and how many of the bags' foods it eats in full."""
    eaten = [(worth(family, pet_level, item, path), item) for item, count in counts.items()
             if count > 0]
    eaten = [(value, item) for value, item in eaten if value > 0]
    if not eaten:
        return None, 0
    top = max(value for value, _ in eaten)
    best = min((foods(path)[item].item_level, item) for value, item in eaten if value == top)[1]
    full = sum(counts[item] for value, item in eaten if value >= FULL_BENEFIT)
    return best, full


def to_buy(family: int | None, pet_level: int | None, sold, path: Path = WORLD_DB) -> Food | None:
    """The food to buy for a pet of `family` and `pet_level` among the items `sold`: of its
    diet and worth the most to it, the lowest item level that is (it lasts the fewest levels
    and costs least), then the cheapest; `None` for none sold that it eats."""
    options = [(worth(family, pet_level, item, path), foods(path)[item]) for item in sold
               if item in foods(path)]
    options = [(value, food) for value, food in options if value > 0]
    if not options:
        return None
    top = max(value for value, _ in options)
    return min((food for value, food in options if value == top),
               key=lambda food: (food.item_level, food.price / food.count, food.entry))


def price(item: int | None, path: Path = WORLD_DB) -> int | None:
    """What one purchase of the food `item` costs, in copper; `None` for none known."""
    food = foods(path).get(item) if item else None
    return food.price if food is not None else None


@lru_cache(maxsize=64)
def mana_cost(spell: int, class_id: int | None, level: int | None,
              path: Path = WORLD_DB) -> int | None:
    """The mana `spell` costs a character of `class_id` and `level`: its flat cost and its share
    of the class's base mana at the level (`Spell::CalculatePowerCost`); `None` unknown."""
    if not isinstance(class_id, int) or not isinstance(level, int) or not Path(path).is_file():
        return None
    try:
        with _connect(Path(path)) as db:
            row = db.execute("select ManaCost, ManaCostPercentage from world_spell_template "
                             "where Id = ?", (spell,)).fetchone()
            base = db.execute("select basemana from world_player_classlevelstats where "
                              "class = ? and level = ?", (class_id, level)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or base is None:
        return None
    return int(row[0] or 0) + int(base[0] or 0) * int(row[1] or 0) // 100


@dataclass(frozen=True)
class Beast:
    """A kind of beast a hunter may tame: its template, its name and the name id the strip
    hashes it to, its family and levels, and where it spawns, `(map, x, y, z)`."""

    entry: int
    name: str
    name_id: int
    family: int
    low: int
    high: int
    points: tuple[tuple[int, float, float, float], ...]


@lru_cache(maxsize=2)
def _beasts(path: Path = WORLD_DB) -> tuple[dict[int, Beast],
                                            dict[int, dict[tuple[int, int], list[tuple]]]]:
    """Every tameable beast of normal rank whose family eats something, and its spawns, fixed
    and random, by map and cell: one read of the snapshot (about 670 kinds, 19,000 spawns)."""
    from jev.perceive.radio_frame import name_id

    if not Path(path).is_file():
        return {}, {}
    try:
        with _connect(Path(path)) as db:
            templates = {r[0]: r for r in db.execute(
                "select Entry, Name, Family, MinLevel, MaxLevel from world_creature_template "
                "where CreatureType = ? and Family <> 0 and (CreatureTypeFlags & ?) <> 0 "
                "and Rank = 0", (BEAST, TAMEABLE))}
            fed = diets(path)
            templates = {e: r for e, r in templates.items() if fed.get(int(r[2]), 0)}
            if not templates:
                return {}, {}
            marks = ",".join("?" * len(templates))
            entries = list(templates)
            points: dict[int, list] = {}
            for entry, m, x, y, z in db.execute(
                    "select id, map, cast(position_x as real), cast(position_y as real), "
                    f"cast(position_z as real) from world_creature where id in ({marks}) "
                    "union all select e.entry, c.map, cast(c.position_x as real), "
                    "cast(c.position_y as real), cast(c.position_z as real) from "
                    "world_creature_spawn_entry e join world_creature c on c.guid = e.guid "
                    f"where e.entry in ({marks})", (*entries, *entries)):
                points.setdefault(int(entry), []).append((int(m), float(x), float(y), float(z)))
    except sqlite3.Error:
        return {}, {}
    kinds, cells = {}, {}
    for entry, row in templates.items():
        if not points.get(entry):
            continue
        kinds[entry] = Beast(int(entry), str(row[1]), name_id(str(row[1])), int(row[2]),
                             int(row[3] or 0), int(row[4] or 0), tuple(points[entry]))
        for m, x, y, z in points[entry]:
            key = (math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS))
            cells.setdefault(m, {}).setdefault(key, []).append((x, y, z, entry))
    return kinds, cells


def tameable(map_id: int | None, x: float, y: float, radius: float, level: int | None,
             path: Path = WORLD_DB) -> list[tuple[Beast, tuple[tuple[float, float, float], ...]]]:
    """The beasts a hunter of `level` may tame round (x, y) on `map_id`: kinds that may stand at
    its level or one below (`MinLevel`-`MaxLevel`), with their spawn points within `radius`
    yards, the kind with the nearest spawn first. None with the map or the level unknown."""
    if map_id is None or not isinstance(level, int):
        return []
    kinds, cells = _beasts(path)
    grid = cells.get(map_id) or {}
    cx, cy = math.floor(x / CELL_YARDS), math.floor(y / CELL_YARDS)
    span = math.ceil(radius / CELL_YARDS)
    found: dict[int, list[tuple[float, float, float]]] = {}
    for i in range(cx - span, cx + span + 1):
        for j in range(cy - span, cy + span + 1):
            for sx, sy, sz, entry in grid.get((i, j), ()):
                kind = kinds[entry]
                if (kind.low <= level and kind.high >= level - 1
                        and math.dist((sx, sy), (x, y)) <= radius):
                    found.setdefault(entry, []).append((sx, sy, sz))
    return sorted(((kinds[e], tuple(p)) for e, p in found.items()),
                  key=lambda pair: min(math.dist(q[:2], (x, y)) for q in pair[1]))
