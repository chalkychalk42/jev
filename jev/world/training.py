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
- no new heal and no new short buff: the starting bar has its heal and its seal already,
  and a second seal would only replace the first.
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
MAX_TRAINER_YARDS = 1500.0

# Roles worth a new bar slot, in the order free slots are handed out.
ONE_OF_EACH = ("aura", "save", "stun", "last_resort")
NEW_LINE_ROLES = ("aura", "long_buff", "strike", "save", "stun", "last_resort")
BAR_SLOTS = 12


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
                      aura=raw.get("aura"))


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


def learnable(trainer: Trainer, level: int, known: Iterable[int], *,
              facts: dict | None = None) -> list[Offer]:
    """What this trainer would teach a character of this level knowing `known`.

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

    return [o for o in trainer.offers if o.level <= level and not held(o)]


def trainer_due(class_id: int | None, race_id: int | None, level: int | None,
                known: Iterable[int] | None, money: int | None, map_id: int | None,
                here: tuple[float, float] | None, *, facts: dict | None = None,
                max_yards: float = MAX_TRAINER_YARDS) -> Trainer | None:
    """The trainer worth visiting now, or `None`.

    One that teaches something the character can afford, the most the purse can buy
    there first (counted cheapest first), and the nearer of two that sell as many. Every
    input unknown is `None`: an unread spellbook is not an empty one, and would send the
    character to train what it knows.
    """
    if None in (class_id, race_id, level, known, money, map_id, here):
        return None
    known = set(known)
    best, best_key = None, None
    for trainer in trainers(class_id, race_id, map_id, facts):
        yards = math.dist(trainer.world[:2], here)
        if yards > max_yards:
            continue
        bought, left = 0, money
        for offer in sorted(learnable(trainer, level, known, facts=facts), key=lambda o: o.cost):
            if offer.cost > left:
                break
            bought, left = bought + 1, left - offer.cost
        if not bought:
            continue
        key = (-bought, yards)
        if best_key is None or key < best_key:
            best, best_key = trainer, key
    return best


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
    """The level each line is first taught at, by any trainer: earlier lines come first."""
    facts = facts if facts is not None else catalog()
    first: dict[str, int] = {}
    for offers in facts["offers"].values():
        for o in offers:
            raw = facts["spells"].get(str(o["spell"]))
            if raw is not None:
                first[raw["name"]] = min(first.get(raw["name"], o["level"]), o["level"])
    return first
