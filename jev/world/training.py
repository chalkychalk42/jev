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
- every hold (`cc`: Polymorph, Fear, Gouge, V395) and every guard (Evasion, Psychic Scream,
  V396), and a wand's shot once a wand is worn, over the melee toggle (V397);
- no new heal and no new short buff: the starting bar has its heal and its seal already,
  and a second seal would only replace the first.

The fight's lines take the twelve slots first (V394): a free slot goes to what is pressed in
a fight before what is kept up or made between fights, and a full bar gives a fight line
waiting the slot of what no fight presses - first a spell cast only in a form the character
never takes (a druid's Maul), then a conjure that makes no drink, then the newest long buff
kept up between fights (ten minutes or more: not Battle Shout); a conjure of water never, a
caster's mana being its damage. Nothing presses a spell that is not on the bar: a buff or a
conjure given up is not cast.

What is worth buying follows from the same rules (V237): a spell whose role the fight code
presses, which would go on the bar - a new rank where the old one is, or a new spell the
bar takes. A dispel, Slow Fall or a second save is never bought, and a trainer
is not walked to, nor copper kept back, for one. Among the spells worth buying, what acts
in a fight comes before what is kept up or made between fights (`buy_order`).
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache, lru_cache

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

# Roles worth a new bar slot, in the order free slots are handed out: what a fight presses
# first (V394). Of the 30 hive mages at level 12 and over on 7 Oct, 28 had full bars, with
# Conjure Water, Conjure Food, Frost Armor and Arcane Intellect, and none Polymorph.
ONE_OF_EACH = ("aura", "save", "stun", "last_resort")
NEW_LINE_ROLES = ("aura", "strike", "dot", "mark", "slow", "save", "guard", "stun",
                  "last_resort", "root", "cc", "area", "wand", "long_buff", "conjure")
BAR_SLOTS = 12

# The roles the fight code presses (`jev.world.combat.TRAINED_ROLES`), in the order a spell
# is worth buying (V237). First what a fight is won with: damage (a strike, a seal), then
# control of what is fought (a root, a stun), then what keeps the character standing in one
# (a save, a last resort, a heal, an aura). Then what is kept up or made between fights: a
# conjure, then a long buff. A spell of any other role - a dispel, a passive - is pressed by
# nothing. Polymorph is pressed since V287 (`cc`): it holds one of two attackers out of the
# fight. The mage with a spell's money a visit bought Conjure Water before Frostbolt at
# level 5 and Conjure Food before Fire Blast at 6, in the stock window's order.
# Damage over time is damage (V361): Corruption, Shadow Word: Pain, Serpent Sting, Rend.
# A guard is pressed in a fight against more than one or one being lost (V396), a wand's shot
# when the mana is spent or the unit nearly dead (V397).
# A mark as the fight opens and a slow at a unit coming (V404): Hunter's Mark, Concussive Shot.
FIGHT_ROLES = ("strike", "dot", "mark", "slow", "short_buff", "root", "stun", "cc", "guard",
               "area", "save", "last_resort", "heal", "aura", "attack", "wand")
BETWEEN_ROLES = ("conjure", "long_buff")
BUY_ORDER = FIGHT_ROLES + BETWEEN_ROLES
# Passives worth their price though nothing presses them, bought first at their level (V562,
# amends V237): Parry (3127), Dual Wield (674), a warrior's Stance Mastery (12678) and a
# paladin's Spiritual Attunement (31785). Bought by none - "pressed by nothing" - not one of the
# hive's 116 warriors, paladins, hunters and rogues online on 9 Oct 01:00 had Parry, offered at
# 1-12 for a silver or eight, nor one of its 32 rogues Dual Wield, three silver at 10.
PASSIVES_BOUGHT = frozenset({3127, 674, 12678, 31785})
# Suspended (V564): the spellbook census does not show a passive once learned, so a passive
# bought stayed "not known" and every trainer visit bought it again - the hive's class-training
# decisions rose 3.6 times in the hour after V562 (63 to 225 a 600 runs). Bought again only
# with learned skills remembered (bet-arms' V590).
PASSIVES_SUSPENDED = True
# What holds off more than one attacker, bought before the oldest gap (V242): a root, a stun,
# and a guard (V396) - Psychic Scream scatters what is round the priest. A hold takes one,
# and is bought by its level, as the strikes are (V287).
CONTROL_ROLES = ("root", "stun", "guard")
# A long buff kept up between fights, which a full bar gives up to a fight line (V394): one
# lasting ten minutes or more, pressed again five seconds before it lapses (Arcane Intellect,
# Inner Fire). A shorter one is pressed in the fight (Battle Shout, Blessing of Might).
KEPT_UP_S = 595.0
# Auras that raise the damage the character deals (V404): damage done (13), attack power (99)
# and ranged attack power (124). One takes the bar's aura slot from one that only guards - a
# hunter's Aspect of the Hawk (124, from level 10) from its Aspect of the Monkey (49, dodge,
# from 4), which held it on every hunter's bar in the hive (7 Oct), none of 42 having bought
# the Hawk: an aura was worth buying only as the first of its kind. A paladin's auras, armour
# (22) and Retribution's damage shield (15), raise neither, and keep their slot.
DAMAGE_AURAS = (13, 99, 124)


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
    # A trainer's spell that teaches others (V359): Judgement is sold as 10321, which teaches
    # 20271 and a Seal of Righteousness; these facts are the first one's.
    teaches: tuple[int, ...] = ()
    # A hold's or a guard's (V395, V396): how long it lasts, the creature types it takes (0:
    # any), a root that pins its unit where it stands, a fear round the caster.
    holds_s: float = 0.0
    creatures: int = 0
    pins: bool = False
    around: bool = False
    # Cast only in a form or stance the character does not take (V394): a druid's Claw, a
    # rogue's Sap, a warrior's Shield Block.
    form: bool = False

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
                      creates=raw.get("creates"), teaches=tuple(raw.get("teaches") or ()),
                      holds_s=float(raw.get("holds_s", 0.0)),
                      creatures=int(raw.get("creatures") or 0), pins=bool(raw.get("pins")),
                      around=bool(raw.get("around")), form=bool(raw.get("form")))


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
    if spell_id in PASSIVES_BOUGHT:
        # No slot: it works unpressed (V562); suspended until a learned one is remembered (V564).
        return not PASSIVES_SUSPENDED and spell_id not in set(known)
    if facts_of is None or facts_of.role not in BUY_ORDER:
        return False
    known = set(known)
    after = {p.spell_id for p in placements(bar, {*known, spell_id}, facts=facts)}
    if spell_id not in after:
        return False
    lines = {spell(s, facts).name for s in after}
    # A fight line may take what a buff or a conjure would have had (V394), nothing else.
    fighting = facts_of.role in FIGHT_ROLES
    return all(spell(p.spell_id, facts).name in lines
               for p in placements(bar, known, facts=facts)
               if not (fighting and spell(p.spell_id, facts).role in BETWEEN_ROLES))


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
    # Control of more than one attacker before any level's gap (V242): 15 of the level 8
    # mage's 17 deaths in sessions 206-213 had two to four attackers, Mangy Wolves and
    # murlocs below its level among them, and a mage behind on its spells would have
    # bought Frostbolt and both level 8 spells before Frost Nova.
    # A passive worth buying goes with the control of more than one: first at its level, for a
    # silver or a few (V562).
    passive = offer.spell_id in PASSIVES_BOUGHT
    return (role not in FIGHT_ROLES and not passive, role not in CONTROL_ROLES and not passive,
            offer.level, order, new_line, offer.cost, offer.spell_id)


def learnable(trainer: Trainer, level: int, known: Iterable[int], *,
              bar: Mapping[int, int | None] | None = None, race_id: int | None = None,
              facts: dict | None = None) -> list[Offer]:
    """What this trainer would teach a character of this level knowing `known`, and worth
    buying (`shopping`, V237), judged against `bar` (the bar's census; without one, the
    class's starting bar). Two spells for one free slot count as one.

    A rank below one the spellbook holds is known too: learning Devotion Aura rank 2 takes
    rank 1 out of the spellbook, and the rank 1 the census then lacked sent a level 10
    paladin to Brother Wilhelm for a spell he no longer offers (session 83).

    Asked at every policy look, of a spellbook and a bar that change once a level: kept
    for the catalog's own facts, where working it out took up to 12 ms a look.
    """
    if facts is None:
        held_bar = None if bar is None else tuple(sorted(bar.items()))
        return list(_learnable(trainer, level, frozenset(known), held_bar, race_id))
    return _find_learnable(trainer, level, known, bar, race_id, facts)


@lru_cache(maxsize=256)
def _learnable(trainer: Trainer, level: int, known: frozenset[int],
               bar: tuple | None, race_id: int | None) -> tuple[Offer, ...]:
    return tuple(_find_learnable(trainer, level, known, None if bar is None else dict(bar),
                                 race_id, None))


def _find_learnable(trainer: Trainer, level: int, known: Iterable[int],
                    bar: Mapping[int, int | None] | None, race_id: int | None,
                    facts: dict | None) -> list[Offer]:
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
        if facts_of is not None and facts_of.teaches and facts_of.teaches[0] in have:
            return True                     # what it teaches is in the spellbook (V359)
        return (facts_of is not None and bool(facts_of.rank)
                and ranks.get(facts_of.name, 0) >= facts_of.rank)

    taught = [o for o in trainer.offers if o.level <= level and not held(o)]
    # A rank past the first is taught only once the rank before it is known, or is itself
    # taught here: a talent's later ranks (Pyroblast rank 2 at 24, Ice Barrier's) never are,
    # and counted, one would take a slot in the shopping from a spell that can be bought.
    by_rank: dict[tuple[str, int], Offer] = {}
    for offer in taught:
        facts_of = spell(offer.spell_id, facts)
        if facts_of is not None:
            by_rank[(facts_of.name, facts_of.rank)] = offer

    def reachable(offer: Offer) -> bool:
        facts_of = spell(offer.spell_id, facts)
        if (facts_of is None or facts_of.rank <= 1
                or ranks.get(facts_of.name, 0) >= facts_of.rank - 1):
            return True
        before = by_rank.get((facts_of.name, facts_of.rank - 1))
        return before is not None and reachable(before)

    taught = [o for o in taught if reachable(o)]
    if bar is None:
        bar = starting_bar(trainer.class_id, race_id)
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


def unpressed(known: Iterable[int], *, facts: dict | None = None) -> tuple[list[str], list[int]]:
    """What the character knows that nothing presses (V361), for the session's one line: the
    spell lines whose role the fight has no use for (`utility`: Charge, Life Tap, Hunter's
    Mark, a totem), by name, passives left out; and the spell ids the catalog does not know
    at all (a pet's, Tame Beast, a racial's)."""
    known = set(known)
    lines = _lines(known, facts)
    names = sorted(f.name for f in lines.values()
                   if f.role not in BUY_ORDER and f.role != "passive")
    unknown = sorted(s for s in known if spell(s, facts) is None)
    return names, unknown


def _lines(spells: Iterable[int], facts: dict | None) -> dict[str, SpellFacts]:
    """The highest known rank of each spell, by name."""
    best: dict[str, SpellFacts] = {}
    for spell_id in spells:
        f = spell(spell_id, facts)
        if f is None:
            continue
        here = best.get(f.name)
        # The spell itself before the trainer's spell that teaches it (V359): the one is in
        # the spellbook and goes on the bar, the other only on the trainer's list.
        if (here is None or f.rank > here.rank
                or (f.rank == here.rank and here.teaches and not f.teaches)):
            best[f.name] = f
    return best


def placements(bar: Mapping[int, int | None], known: Iterable[int], *,
               facts: dict | None = None, wand: bool = False) -> list[Placement]:
    """What to drag from the spellbook onto the main bar, in order.

    `bar` is each main-bar slot's spell id, 0 for an empty slot and `None` for an item or
    an unread slot (never overwritten). `known` is the spellbook's spell ids. `wand`: one is
    worn, and its shot goes over the melee toggle (V397); without one it goes nowhere.
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
        if best is not None and best.rank > here.rank and fought_with(best):
            out.append(Placement(best.spell_id, slot, here.spell_id))
    # An aura that raises the damage dealt takes the slot of one on the bar that does not
    # (`DAMAGE_AURAS`, V404): Aspect of the Hawk over Aspect of the Monkey.
    held = next(((slot, f) for slot, f in _on_bar_slots(bar, facts)
                 if f.role == "aura" and f.aura not in DAMAGE_AURAS
                 and not any(p.slot == slot for p in out)), None)
    hitting = next((f for f in sorted(lines.values(), key=lambda f: f.spell_id)
                    if f.role == "aura" and f.aura in DAMAGE_AURAS and f.name not in on_bar),
                   None)
    if held is not None and hitting is not None:
        out.append(Placement(hitting.spell_id, held[0], held[1].spell_id))
    roles_on_bar = {f.role for f in on_bar.values()}
    auras_on_bar = {f.aura for f in on_bar.values() if f.role == "long_buff"}
    first = _first_levels(facts)
    wanted: list[SpellFacts] = []
    for f in sorted(lines.values(), key=lambda f: (NEW_LINE_ROLES.index(f.role)
                                                   if f.role in NEW_LINE_ROLES else 99,
                                                   first.get(f.name, 0), f.spell_id)):
        if (f.role not in NEW_LINE_ROLES or f.name in on_bar or (f.role == "wand" and not wand)
                or not fought_with(f)):
            continue
        if f.role in ONE_OF_EACH and (f.role in roles_on_bar
                                      or any(w.role == f.role for w in wanted)):
            continue
        if f.role == "long_buff" and (f.aura in auras_on_bar
                                      or any(w.role == "long_buff" and w.aura == f.aura
                                             for w in wanted)):
            continue
        wanted.append(f)
    # A wand's shot over the melee toggle (V397): a caster with a wand shoots where it would
    # have swung its staff.
    shot = next((f for f in wanted if f.role == "wand"), None)
    toggle = next(((slot, f) for slot, f in _on_bar_slots(bar, facts) if f.role == "attack"
                   and not any(p.slot == slot for p in out)), None)
    if shot is not None and toggle is not None:
        out.append(Placement(shot.spell_id, toggle[0], toggle[1].spell_id))
        wanted.remove(shot)
    free = [(slot, 0) for slot in range(1, BAR_SLOTS + 1) if bar.get(slot) == 0] + spare
    out.extend(Placement(f.spell_id, slot, old)
               for f, (slot, old) in zip(wanted, free, strict=False))
    # A full bar gives a fight line waiting the slot of a line nothing presses in a fight
    # (V394), the least needed first (`_yield_order`): at 10 a mage's Frost Nova goes over
    # Conjure Food, where since V287 it went over Polymorph. A conjure of water is kept.
    # A line given up takes its new rank with it: Conjure Food 2 is not bought for a slot
    # Polymorph is to have.
    waiting = [f for f in wanted[len(free):] if f.role in FIGHT_ROLES and not f.form]
    yielding = sorted(((slot, f) for slot, f in _on_bar_slots(bar, facts) if _yields(f)),
                      key=lambda pair: _yield_order(pair[1], facts))
    for f, (slot, old) in zip(waiting, yielding, strict=False):
        out = [p for p in out if p.slot != slot]
        out.append(Placement(f.spell_id, slot, old.spell_id))
    return out


def fought_with(f: SpellFacts) -> bool:
    """Whether a fight can press the spell at all (V501): not one cast only in a form or stance
    the character never takes (`SpellFacts.form`: a rogue's Sap and Ambush, out of a Stealth
    nothing presses; a warrior's Revenge and Shield Block, out of a Defensive Stance it never
    takes), nor a blow struck only from behind its unit (`jev.world.combat.behind`: Backstab),
    whose unit faces the character it fights. Neither goes on the bar, so neither is worth
    buying (`worth_buying`): of the hive's 32 cohort rogues on 8 Oct, 26 had bought Sap and 32
    Backstab, which the server refused "not behind" 2,862 times in 26 hours; a rogue spends 71
    silver on them by 20 (Backstab 1, 2 and 3, Sap, Ambush), a warrior 35 on Revenge and
    Shield Block by 16."""
    from jev.world.combat import reach

    facts = reach(f.spell_id)
    return not f.form and not (facts is not None and facts.behind)


def _yields(f: SpellFacts) -> bool:
    """A line a full bar gives up to a fight line (V394): one cast only in a form the
    character does not take (a druid's Maul, a warrior's Shield Block), which nothing can
    press; a long buff kept up between fights (`KEPT_UP_S`); or a conjure that makes no drink
    (food, a healthstone, a soulstone). Water is a caster's mana, and its rests are its
    downtime."""
    if f.form:
        return True
    if f.role == "long_buff":
        return f.every_s >= KEPT_UP_S
    if f.role != "conjure":
        return False
    from jev.world.vendor import consumable_role

    return consumable_role(f.creates) not in ("drink", "both")


def _yield_order(f: SpellFacts, facts: dict | None) -> tuple:
    """Which given-up line goes first: one nothing can press, then a conjure, then a long
    buff; of each the newest - by the level its first rank is taught at, a line a class
    starts with (Frost Armor) the oldest - and of two as new, the later spell."""
    return (not f.form, f.role != "conjure", -_rank_one_levels(facts).get(f.name, 0),
            -f.spell_id)


def _rank_one_levels(facts: dict | None) -> dict[str, int]:
    """The level each line's first rank is taught at; a line whose first rank no trainer
    teaches is a class's from the start."""
    return _catalog_rank_one_levels() if facts is None else _find_rank_one_levels(facts)


@cache
def _catalog_rank_one_levels() -> dict[str, int]:
    return _find_rank_one_levels(catalog())


def _find_rank_one_levels(facts: dict) -> dict[str, int]:
    first: dict[str, int] = {}
    for offers in facts["offers"].values():
        for o in offers:
            raw = facts["spells"].get(str(o["spell"]))
            if raw is not None and raw.get("rank", 0) <= 1:
                first[raw["name"]] = min(first.get(raw["name"], o["level"]), o["level"])
    return first


def _on_bar_slots(bar: Mapping[int, int | None], facts: dict | None):
    """Each slot's spell on the main bar, its first copy only."""
    seen: set[str] = set()
    for slot in range(1, BAR_SLOTS + 1):
        here = spell(bar.get(slot), facts)
        if here is not None and here.name not in seen:
            seen.add(here.name)
            yield slot, here


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
