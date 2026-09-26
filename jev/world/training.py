"""Class training: which trainer to visit, what is worth learning, and what goes on the bar.

The facts come from `content/tbc/trainer-catalog.json` (`tools/gen_trainer_catalog.py`):
trainers by class and side, what each teaches at which level and price, and a role for
every spell read from what it does. What the character knows, and what each main-bar
button holds, come from the strip's spellbook and bar censuses (`jev.perceive.spellbook`).

A level 8 paladin fought the night with Seal of Righteousness and Holy Light rank 1 alone,
and lost fights to two wolves at once, while its trainer offered Devotion Aura, Blessing of
Might, Judgement, Divine Protection and Holy Light rank 2 for five silver.

What goes on the bar, by the operator's rule: a new rank goes where the old rank is, and a
new spell goes on any free slot, in any order. Which new spells are worth a slot is decided
here, by role, so a class needs no list of its own:

- one aura, one save, one stun and one last resort: the first learned of each - auras are
  exclusive, and a second save shares the first one's cooldown;
- one long buff per kind of aura it applies (a blessing of attack power, one of mana);
- every strike;
- every conjure (a caster's water and food, V166), and a root (Frost Nova, V169);
- no new heal and no new short buff: the starting bar has its heal and its seal already,
  and a second seal would only replace the first.

What is worth buying follows from the same rules (V237): a spell whose role the fight code
presses, which would go on the bar - a new rank where the old one is, or a new spell the
bar takes. Polymorph, a dispel, Slow Fall or a second save is never bought, and a trainer
is not walked to, nor copper kept back, for one. Among the spells worth buying, what acts
in a fight comes before what is kept up or made between fights (`buy_order`).
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache

CATALOG = pathlib.Path(__file__).resolve().parents[2] / "content/tbc/trainer-catalog.json"

ALLIANCE_RACES = frozenset({1, 3, 4, 7, 11})
HORDE_RACES = frozenset({2, 5, 6, 8, 10})

# A trainer further than this is not worth the walk on its own. Northshire's Brother
# Sammuel stands 50 yards from the Abbey door; Goldshire's Brother Wilhelm is 600 yards on.
# One that teaches two spells or more the purse can pay for is worth twice the walk (V168):
# from Sentinel Hill Brother Wilhelm is 1,400 yards, from Moonbrook about 2,070, and a
# paladin working south Westfall would otherwise not train from 14 to 18.
MAX_TRAINER_YARDS = 1500.0
TRAINER_REACH_SPELLS = 2

# Roles worth a new bar slot, in the order free slots are handed out.
ONE_OF_EACH = ("aura", "save", "stun", "last_resort")
NEW_LINE_ROLES = ("aura", "long_buff", "strike", "save", "stun", "last_resort", "conjure",
                  "root")
BAR_SLOTS = 12

# The roles the fight code presses (`jev.world.combat.TRAINED_ROLES`), in the order a spell
# is worth buying (V237). First what a fight is won with: damage (a strike, a seal), then
# control of what is fought (a root, a stun), then what keeps the character standing in one
# (a save, a last resort, a heal, an aura). Then what is kept up or made between fights: a
# conjure, then a long buff. A spell of any other role - Polymorph, a dispel, a passive -
# is pressed by nothing. The mage with a spell's money a visit bought Conjure Water before
# Frostbolt at level 5 and Conjure Food before Fire Blast at 6, in the stock window's order.
FIGHT_ROLES = ("strike", "short_buff", "root", "stun", "save", "last_resort", "heal", "aura",
               "attack")
BETWEEN_ROLES = ("conjure", "long_buff")
BUY_ORDER = FIGHT_ROLES + BETWEEN_ROLES


@dataclass(frozen=True)
class SpellFacts:
    spell_id: int
    name: str
    rank: int
    role: str
    mana: int = 0
    every_s: float = 0.0
    cooldown_s: float = 0.0
    target: str = "other"
    spends: bool = False
    aura: int | None = None
    # A spell that slows the enemy it hits (Frostbolt): a caster's opener (V165).
    slows: bool = False
    # The item a conjure makes (V166).
    creates: int | None = None

    @property
    def self_cast(self) -> bool:
        """Cast on the caster whatever is selected: a helpful spell aimed at a friend."""
        return self.target == "friend"


@dataclass(frozen=True)
class Offer:
    spell_id: int
    level: int
    cost: int


@dataclass(frozen=True)
class Trainer:
    entry: int
    name: str
    class_id: int
    map_id: int
    world: tuple[float, float, float]
    gossip: str | None
    offers: tuple[Offer, ...]


@dataclass(frozen=True)
class Placement:
    """Put `spell_id`, from the spellbook, on main-bar `slot`, over `replaces` (0: empty)."""

    spell_id: int
    slot: int
    replaces: int = 0


@cache
def catalog(path: pathlib.Path = CATALOG) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"trainers": [], "offers": {}, "spells": {}}


def spell(spell_id: int | None, facts: dict | None = None) -> SpellFacts | None:
    if not spell_id:
        return None
    raw = (facts if facts is not None else catalog())["spells"].get(str(spell_id))
    if raw is None:
        return None
    return SpellFacts(spell_id=int(spell_id), name=raw["name"], rank=raw.get("rank", 0),
                      role=raw["role"], mana=raw.get("mana", 0),
                      every_s=float(raw.get("every_s", 0.0)),
                      cooldown_s=float(raw.get("cooldown_s", 0.0)),
                      target=raw.get("target", "other"), spends=bool(raw.get("spends")),
                      aura=raw.get("aura"), slows=bool(raw.get("slows")),
                      creates=raw.get("creates"))


def side(race_id: int | None) -> str | None:
    return ("alliance" if race_id in ALLIANCE_RACES else
            "horde" if race_id in HORDE_RACES else None)


def trainers(class_id: int | None, race_id: int | None, map_id: int | None,
             facts: dict | None = None) -> list[Trainer]:
    """Every trainer of this class that serves this character's side, on this map."""
    if facts is None:
        return list(_trainers(class_id, race_id, map_id))
    return _find_trainers(class_id, race_id, map_id, facts)


@cache
def _trainers(class_id: int | None, race_id: int | None, map_id: int | None) -> tuple:
    # Asked at every policy look; the catalog does not change under a run.
    return tuple(_find_trainers(class_id, race_id, map_id, catalog()))


def _find_trainers(class_id: int | None, race_id: int | None, map_id: int | None,
                   facts: dict) -> list[Trainer]:
    wanted = side(race_id)
    if class_id is None or wanted is None:
        return []
    out = []
    for t in facts["trainers"]:
        if t["class"] != class_id or wanted not in t["sides"] or t["map_id"] != map_id:
            continue
        offers = tuple(Offer(o["spell"], o["level"], o["cost"])
                       for o in facts["offers"].get(t["offers"], ()))
        out.append(Trainer(entry=t["entry"], name=t["name"], class_id=t["class"],
                           map_id=t["map_id"], world=tuple(t["world"]),
                           gossip=t.get("gossip"), offers=offers))
    return out


def starting_bar(class_id: int | None, race_id: int | None = None) -> dict[int, int | None]:
    """The main bar a character of this class starts with (`jev.world.combat.for_class`): a
    spell id per slot, 0 for an empty one and `None` for an item. What the bar is taken to
    hold while its census is unread."""
    from jev.world.combat import for_class

    bar: dict[int, int | None] = {slot: 0 for slot in range(1, BAR_SLOTS + 1)}
    for ability in for_class(class_id, race_id).abilities:
        bar[(ability.slot - 1) % BAR_SLOTS + 1] = ability.spell_id or None
    return bar


def worth_buying(spell_id: int, known: Iterable[int], bar: Mapping[int, int | None], *,
                 facts: dict | None = None) -> bool:
    """Whether the bot would use this spell once it knows it (V237): the fight code presses
    its role (`BUY_ORDER`), and `placements` would put it on the bar - a new rank where the
    old one is, or a new spell the bar takes - without taking the place of a spell that
    would otherwise go there. So an aura, a save, a stun or a last resort is worth buying
    only as the first of its kind, a long buff only for an aura the bar lacks, and a heal
    or a short buff only as a new rank of one on the bar: at level 12 a mage's Slow Fall,
    a new short buff, is never bought, and Fireball rank 3 is. And with one slot free and
    Frost Nova bought, Dampen Magic is not: `placements` hands a long buff a free slot
    before a root, and would have put it there in Frost Nova's place."""
    facts_of = spell(spell_id, facts)
    if facts_of is None or facts_of.role not in BUY_ORDER:
        return False
    known = set(known)
    after = {p.spell_id for p in placements(bar, {*known, spell_id}, facts=facts)}
    if spell_id not in after:
        return False
    lines = {spell(s, facts).name for s in after}
    return all(spell(p.spell_id, facts).name in lines
               for p in placements(bar, known, facts=facts))


def shopping(offers: Iterable[Offer], known: Iterable[int], bar: Mapping[int, int | None], *,
             facts: dict | None = None) -> list[Offer]:
    """The offers worth buying if all were bought, best first (`buy_order`): each worth
    buying beside the spells known and every offer before it, so none that comes later
    takes a slot one before it needs (V237)."""
    known = set(known)
    have, out = set(known), []
    for offer in sorted(offers, key=lambda o: buy_order(o, known, facts)):
        if offer.spell_id not in have and worth_buying(offer.spell_id, have, bar, facts=facts):
            out.append(offer)
            have.add(offer.spell_id)
    return out


def buy_order(offer: Offer, known: Iterable[int] = (), facts: dict | None = None) -> tuple:
    """Where an offer worth buying ranks, best first (V237): a spell that acts in a fight
    before one kept up or made between fights; then the lowest level taught, the gap the
    character has gone longest without and the cheapest; then damage before control
    before what keeps it standing (`BUY_ORDER`); then a new rank of a spell it knows, a
    button every fight presses already, before a new spell; then the price and the id.
    At level 8 a mage with a spell's money buys Frostbolt rank 2 before Arcane Missiles,
    which a caster presses only when its Fireball cannot be (`Fight._caster_order`)."""
    facts_of = spell(offer.spell_id, facts)
    role = facts_of.role if facts_of is not None else ""
    order = BUY_ORDER.index(role) if role in BUY_ORDER else len(BUY_ORDER)
    new_line = facts_of is None or facts_of.name not in _lines(known, facts)
    return (role not in FIGHT_ROLES, offer.level, order, new_line, offer.cost, offer.spell_id)


def learnable(trainer: Trainer, level: int, known: Iterable[int], *,
              bar: Mapping[int, int | None] | None = None, race_id: int | None = None,
              facts: dict | None = None) -> list[Offer]:
    """What this trainer would teach a character of this level knowing `known`, and worth
    buying (`shopping`, V237), judged against `bar` (the bar's census; without one, the
    class's starting bar). Two spells for one free slot count as one.

    A rank below one the spellbook holds is known too: learning Devotion Aura rank 2 takes
    rank 1 out of the spellbook, and the rank 1 the census then lacked sent a level 10
    paladin to Brother Wilhelm for a spell he no longer offers (session 83).
    """
    have = set(known)
    ranks: dict[str, int] = {}
    for spell_id in have:
        facts_of = spell(spell_id, facts)
        if facts_of is not None and facts_of.rank:
            ranks[facts_of.name] = max(ranks.get(facts_of.name, 0), facts_of.rank)

    def held(offer: Offer) -> bool:
        if offer.spell_id in have:
            return True
        facts_of = spell(offer.spell_id, facts)
        return (facts_of is not None and bool(facts_of.rank)
                and ranks.get(facts_of.name, 0) >= facts_of.rank)

    if bar is None:
        bar = starting_bar(trainer.class_id, race_id)
    taught = [o for o in trainer.offers if o.level <= level and not held(o)]
    worth = set(shopping(taught, have, bar, facts=facts))
    return [o for o in taught if o in worth]


def trainer_due(class_id: int | None, race_id: int | None, level: int | None,
                known: Iterable[int] | None, money: int | None, map_id: int | None,
                here: tuple[float, float] | None, *, bar: Mapping[int, int | None] | None = None,
                facts: dict | None = None,
                max_yards: float = MAX_TRAINER_YARDS) -> Trainer | None:
    """The trainer worth visiting now, or `None`.

    One that teaches something worth buying (`learnable`) the character can afford, the
    most the purse can buy there first (counted cheapest first), and the nearer of two that
    sell as many. Every input unknown is `None`: an unread spellbook is not an empty one,
    and would send the character to train what it knows. An unread bar is taken to be the
    class's starting one.
    """
    if None in (class_id, race_id, level, known, money, map_id, here):
        return None
    known = set(known)
    best, best_key = None, None
    for trainer in trainers(class_id, race_id, map_id, facts):
        yards = math.dist(trainer.world[:2], here)
        if yards > max_yards * TRAINER_REACH_SPELLS:
            continue
        bought, left = 0, money
        for offer in sorted(learnable(trainer, level, known, bar=bar, race_id=race_id,
                                      facts=facts), key=lambda o: o.cost):
            if offer.cost > left:
                break
            bought, left = bought + 1, left - offer.cost
        if not bought or yards > max_yards * min(bought, TRAINER_REACH_SPELLS):
            continue
        key = (-bought, yards)
        if best_key is None or key < best_key:
            best, best_key = trainer, key
    return best


def training_cost(class_id: int | None, race_id: int | None, level: int | None,
                  known: Iterable[int] | None, map_id: int | None,
                  here: tuple[float, float] | None, *,
                  bar: Mapping[int, int | None] | None = None, facts: dict | None = None,
                  max_yards: float = MAX_TRAINER_YARDS) -> int:
    """The least purse that makes a trainer visit due (`trainer_due`): the cheapest spell
    worth buying the character could learn from a trainer within `max_yards`, or the two
    cheapest from one within twice that. 0 when no trainer in reach has anything worth
    teaching, or anything is unknown.

    What a restock keeps back (V215). The level 5 mage sold its bags for 134 copper, 34 more
    than Frostbolt or Conjure Water, and spent it on a repair and 15 waters: it had trained
    once in five levels, and the water it bought was what Conjure Water would have made.
    Nothing is kept back for a spell the bot would not buy (V237): at level 8, Polymorph.
    """
    if None in (class_id, race_id, level, known, map_id, here):
        return 0
    known = set(known)
    least = None
    for trainer in trainers(class_id, race_id, map_id, facts):
        yards = math.dist(trainer.world[:2], here)
        spells = 1 if yards <= max_yards else TRAINER_REACH_SPELLS
        if yards > max_yards * TRAINER_REACH_SPELLS:
            continue
        costs = sorted(o.cost for o in learnable(trainer, level, known, bar=bar,
                                                 race_id=race_id, facts=facts))
        if len(costs) < spells:
            continue
        cost = sum(costs[:spells])
        least = cost if least is None else min(least, cost)
    return least or 0


def _lines(spells: Iterable[int], facts: dict | None) -> dict[str, SpellFacts]:
    """The highest known rank of each spell, by name."""
    best: dict[str, SpellFacts] = {}
    for spell_id in spells:
        f = spell(spell_id, facts)
        if f is None:
            continue
        if f.name not in best or f.rank > best[f.name].rank:
            best[f.name] = f
    return best


def placements(bar: Mapping[int, int | None], known: Iterable[int], *,
               facts: dict | None = None) -> list[Placement]:
    """What to drag from the spellbook onto the main bar, in order.

    `bar` is each main-bar slot's spell id, 0 for an empty slot and `None` for an item or
    an unread slot (never overwritten). `known` is the spellbook's spell ids.
    """
    lines = _lines(known, facts)
    out: list[Placement] = []
    on_bar: dict[str, SpellFacts] = {}
    # A second copy of a spell already on the bar holds a slot for nothing: one a new spell
    # can go over, after the empty ones.
    spare: list[tuple[int, int]] = []
    for slot in range(1, BAR_SLOTS + 1):
        here = spell(bar.get(slot), facts)
        if here is None:
            continue
        if here.name in on_bar:
            spare.append((slot, here.spell_id))
            continue
        on_bar[here.name] = here
        best = lines.get(here.name)
        if best is not None and best.rank > here.rank:
            out.append(Placement(best.spell_id, slot, here.spell_id))
    roles_on_bar = {f.role for f in on_bar.values()}
    auras_on_bar = {f.aura for f in on_bar.values() if f.role == "long_buff"}
    first = _first_levels(facts)
    wanted: list[SpellFacts] = []
    for f in sorted(lines.values(), key=lambda f: (NEW_LINE_ROLES.index(f.role)
                                                   if f.role in NEW_LINE_ROLES else 99,
                                                   first.get(f.name, 0), f.spell_id)):
        if f.role not in NEW_LINE_ROLES or f.name in on_bar:
            continue
        if f.role in ONE_OF_EACH and (f.role in roles_on_bar
                                      or any(w.role == f.role for w in wanted)):
            continue
        if f.role == "long_buff" and (f.aura in auras_on_bar
                                      or any(w.role == "long_buff" and w.aura == f.aura
                                             for w in wanted)):
            continue
        wanted.append(f)
    free = [(slot, 0) for slot in range(1, BAR_SLOTS + 1) if bar.get(slot) == 0] + spare
    out.extend(Placement(f.spell_id, slot, old)
               for f, (slot, old) in zip(wanted, free, strict=False))
    return out


def _first_levels(facts: dict | None) -> dict[str, int]:
    """The level each line is first taught at, by any trainer: earlier lines come first.

    The catalog's own is worked out once: `worth_buying` asks `placements` about each
    offer at every policy look, and going over all 1,854 offers each time made a look at a
    trainer 30 ms (V237). Read only."""
    return _catalog_first_levels() if facts is None else _find_first_levels(facts)


@cache
def _catalog_first_levels() -> dict[str, int]:
    return _find_first_levels(catalog())


def _find_first_levels(facts: dict) -> dict[str, int]:
    first: dict[str, int] = {}
    for offers in facts["offers"].values():
        for o in offers:
            raw = facts["spells"].get(str(o["spell"]))
            if raw is not None:
                first[raw["name"]] = min(first.get(raw["name"], o["level"]), o["level"])
    return first
