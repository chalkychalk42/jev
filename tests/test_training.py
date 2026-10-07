"""Class training: the catalog's roles, the trainer chosen, and what goes on the bar."""

from __future__ import annotations

from jev.world import training
from jev.world.training import Placement, placements, spell, trainer_due, training_cost

# Northshire Abbey's door, and Testvvi's purse at level 8 (6 silver 26 copper).
NORTHSHIRE = (-8914.0, -210.0)
STARTING_BAR = {1: 6603, 2: 20154, 3: 635, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0, 9: 0, 10: 0,
                11: None, 12: None}


def test_the_catalog_reads_each_paladin_spell_by_what_it_does():
    roles = {sid: spell(sid).role for sid in (6603, 20154, 635, 639, 465, 20271, 19740, 498,
                                              853, 633, 1022, 1152, 3127, 25780, 879, 21082)}
    assert roles == {6603: "attack", 20154: "short_buff", 635: "heal", 639: "heal",
                     465: "aura", 20271: "strike", 19740: "long_buff", 498: "save",
                     853: "stun", 633: "last_resort", 1022: "save", 1152: "utility",
                     3127: "passive", 25780: "utility", 879: "utility", 21082: "short_buff"}
    assert spell(20271).spends                      # Judgement releases the seal
    # Weapon blows are strikes too, whatever the class.
    assert {spell(sid).role for sid in (78, 1752, 75, 2973)} == {"strike"}
    assert spell(28734).role == "utility"           # Mana Tap drains, it does not strike (V288)
    assert spell(19740).self_cast and not spell(20154).self_cast
    assert spell(19740).every_s == 595.0


def test_judgement_is_sold_as_the_trainer_spell_that_teaches_it():
    """The trainer's entry is 10321, whose effect is "learn spell 20271" (V359): it is sold by
    that id, which the server's trainer list holds, and its facts are Judgement's. The hive's
    paladins asked the server for 20271, "not trained", at every visit, and never had it."""
    sammuel = next(t for t in training.trainers(2, 1, 0) if t.name == "Brother Sammuel")
    assert 10321 in {o.spell_id for o in sammuel.offers}
    assert 20271 not in {o.spell_id for o in sammuel.offers}
    sold = spell(10321)
    assert sold.name == "Judgement" and sold.role == "strike" and sold.teaches == (20271, 21084)


def test_judgement_is_bought_once_and_goes_on_the_bar():
    """Worth buying while its spell is not in the spellbook; once it is, not for sale, and
    Judgement itself (20271) goes on the bar, never the trainer's 10321."""
    sammuel = next(t for t in training.trainers(2, 1, 0) if t.name == "Brother Sammuel")
    bar = training.starting_bar(2, 1)
    known = {6603, 20154, 635}
    assert 10321 in {o.spell_id for o in training.learnable(sammuel, 4, known, bar=bar)}
    taught = known | {20271, 21084}
    assert 10321 not in {o.spell_id for o in training.learnable(sammuel, 4, taught, bar=bar)}
    assert [p.spell_id for p in placements(bar, taught)] == [20271]
    assert [p.spell_id for p in placements(bar, taught | {10321})] == [20271]


def test_a_paladin_is_trained_by_its_own_side_on_its_own_map():
    alliance = {t.name for t in training.trainers(2, 1, 0)}
    assert {"Brother Sammuel", "Brother Wilhelm"} <= alliance
    assert "Champion Cyssa Dawnrose" not in alliance            # the Undercity's, Horde
    assert "Brother Karman" not in alliance                     # Kalimdor
    assert training.trainers(2, None, 0) == []


def test_the_trainer_teaching_most_of_what_is_affordable_is_chosen():
    """At level 8 Goldshire's Brother Wilhelm teaches Hammer of Justice as well as all that
    Northshire's Brother Sammuel does; with ten copper, both teach only Devotion Aura and
    the nearer one is chosen."""
    known = {6603, 20154, 635}
    assert trainer_due(2, 1, 8, known, 626, 0, NORTHSHIRE).name == "Brother Wilhelm"
    assert trainer_due(2, 1, 8, known, 50, 0, NORTHSHIRE).name == "Brother Sammuel"
    assert trainer_due(2, 1, 8, known, 5, 0, NORTHSHIRE) is None
    everything = known | {465, 20271, 19740, 498, 639, 21082, 853, 1152, 3127}
    assert trainer_due(2, 1, 8, everything, 626, 0, NORTHSHIRE) is None
    assert trainer_due(2, 1, 8, None, 626, 0, NORTHSHIRE) is None     # spellbook unread
    assert trainer_due(2, 1, 8, known, 626, 0, NORTHSHIRE, max_yards=100).name == "Brother Sammuel"
    # What the purse buys, not what it could buy one at a time: 410 copper is all five of
    # Brother Sammuel's spells worth buying and five of Brother Wilhelm's six, so the nearer
    # one. Seal of the Crusader, Purify and Parry are not worth buying (V237).
    assert trainer_due(2, 1, 8, known, 410, 0, NORTHSHIRE).name == "Brother Sammuel"
    assert trainer_due(2, 1, 8, known, 510, 0, NORTHSHIRE).name == "Brother Wilhelm"


def test_a_new_rank_goes_where_the_old_one_is_and_new_spells_on_free_slots():
    known = {6603, 20154, 635, 639, 465, 20271, 19740, 498, 21082, 853, 1152, 3127, 20600}
    plan = placements(STARTING_BAR, known)
    assert plan[0] == Placement(639, 3, 635)
    # What a fight presses before the blessing kept up between fights (V394).
    assert [(p.spell_id, p.slot) for p in plan[1:]] == [
        (465, 4), (20271, 5), (498, 6), (853, 7), (19740, 8)]
    # Nothing for Purify, Parry, a second seal or a racial: no role worth a slot.
    assert not {1152, 3127, 21082, 20600} & {p.spell_id for p in plan}


def test_items_and_unread_slots_are_never_placed_over():
    bar = {slot: None for slot in range(1, 13)}
    bar[1], bar[3] = 6603, 635
    assert placements(bar, {6603, 635, 639, 465}) == [Placement(639, 3, 635)]


def test_one_aura_one_save_and_a_long_buff_per_aura_kind():
    bar = {**STARTING_BAR, **{4: 465, 5: 19740}}
    known = {6603, 20154, 635, 465, 7294, 19740, 19742, 498, 1022, 633}
    plan = placements(bar, known)
    names = [spell(p.spell_id).name for p in plan]
    assert "Retribution Aura" not in names          # Devotion Aura is on the bar already
    assert "Blessing of Wisdom" in names            # mana, where Might is attack power
    assert names.count("Divine Protection") + names.count("Blessing of Protection") == 1
    assert "Lay on Hands" in names


def test_a_placed_spell_is_not_placed_again():
    bar = {**STARTING_BAR, **{3: 639, 4: 465, 5: 19740, 6: 20271, 7: 498, 8: 853}}
    known = {6603, 20154, 635, 639, 465, 20271, 19740, 498, 853}
    assert placements(bar, known) == []


def test_new_spells_stop_when_the_bar_is_full():
    bar = {**STARTING_BAR, **{slot: None for slot in range(4, 10)}}    # items
    plan = placements(bar, {6603, 20154, 635, 465, 20271, 19740})
    assert [p.slot for p in plan] == [10]


def test_a_second_copy_on_the_bar_makes_room_for_a_new_spell():
    """Devotion Aura, placed three times while the bar could not read it (session 57): the
    copies' slots take new spells, after the empty ones."""
    bar = {**STARTING_BAR, **{4: 465, 5: 19740, 6: 465, 7: 465, 8: 0, 9: 0, 10: 0}}
    bar[10] = 20154
    known = {6603, 20154, 635, 465, 19740, 498, 853, 633}
    plan = placements(bar, known)
    assert [(p.spell_id, p.slot, p.replaces) for p in plan] == [
        (498, 8, 0), (853, 9, 0), (633, 6, 465)]


def test_every_spell_a_trainer_spell_teaches_is_known_with_its_successors():
    """Judgement's trainer spell teaches Judgement and a Seal of Righteousness (21084) that
    the client put in the starting seal's place on the bar (session 62)."""
    seal = spell(21084)
    assert seal is not None and seal.name == "Seal of Righteousness" and seal.role == "short_buff"
    assert spell(10290) is not None and spell(10290).role == "aura"   # Devotion Aura 2


def test_a_rank_below_one_the_spellbook_holds_is_not_for_sale():
    """Session 83: Devotion Aura rank 2 took rank 1 out of the spellbook, and the census
    that lacked it sent the character to Brother Wilhelm for nothing."""
    trainer = next(t for t in training.trainers(2, 1, 0) if t.name == "Brother Wilhelm")
    known = [81, 107, 498, 633, 635, 639, 853, 1022, 1152, 3127, 6603, 10290, 19740, 20271,
             20287, 20597, 20598, 20599, 20600, 20864, 21082, 21084]
    assert training.learnable(trainer, 10, known) == []
    sale = {o.spell_id for o in training.learnable(trainer, 10, [465])}
    assert 10290 in sale and 465 not in sale, "rank 1 held: rank 2 is still for sale"
    assert 465 not in {o.spell_id for o in training.learnable(trainer, 10, [10290])}


def test_the_mages_spells_have_roles_and_the_paladins_stay_as_they_were():
    """V165: Frostbolt's first effect is its slow and Arcane Missiles' its periodic missile,
    so both read as utility and never reached the bar; Frost Nova roots, Polymorph
    transforms, the conjures make items. No paladin spell changed role."""
    from jev.world.training import spell

    mage = {133: "strike", 2136: "strike", 116: "strike", 5143: "strike", 122: "root",
            118: "cc", 5504: "conjure", 587: "conjure"}
    assert {sid: spell(sid).role for sid in mage} == mage
    assert spell(116).slows and not spell(133).slows
    paladin = {635: "heal", 20154: "short_buff", 20271: "strike", 465: "aura",
               19740: "long_buff", 498: "save", 853: "stun", 633: "last_resort",
               879: "utility", 26573: "utility", 7294: "aura"}
    assert {sid: spell(sid).role for sid in paladin} == paladin


def test_a_mage_puts_its_conjures_on_the_bar():
    """V166: Conjure Water and Food go on free slots like a strike."""
    from jev.world.training import placements

    bar = {1: 6603, 2: 133, 3: 168, 11: None, 12: None, **{s: 0 for s in range(4, 11)}}
    placed = {p.spell_id for p in placements(bar, frozenset({6603, 133, 168, 5504, 587, 116}))}
    assert {5504, 587, 116} <= placed


def test_a_trainer_teaching_more_is_worth_a_longer_walk():
    """V168: from Moonbrook, Brother Wilhelm is about 2,070 yards; worth it for two spells
    or more, not for one."""
    moonbrook = (-11000.0, 1500.0)
    known = {6603, 20154, 635}
    assert trainer_due(2, 1, 14, known, 100_000, 0, moonbrook).name == "Brother Wilhelm"
    cheapest = min(o.cost for t in training.trainers(2, 1, 0) if t.name == "Brother Wilhelm"
                   for o in training.learnable(t, 14, known))
    assert trainer_due(2, 1, 14, known, cheapest, 0, moonbrook) is None


def test_a_restock_keeps_the_least_purse_that_makes_a_trainer_visit_due():
    """V215: the level 5 mage in Northshire keeps 100 copper, Frostbolt's or Conjure Water's
    price; from Moonbrook, where one spell is not worth the walk, a paladin keeps two."""
    mage = {6603, 133, 168, 1459}
    kept = training_cost(8, 1, 5, mage, 0, NORTHSHIRE)
    assert kept == 100
    assert trainer_due(8, 1, 5, mage, kept, 0, NORTHSHIRE) is not None
    assert trainer_due(8, 1, 5, mage, kept - 1, 0, NORTHSHIRE) is None
    assert training_cost(8, 1, 5, mage | {116, 5504}, 0, NORTHSHIRE) == 0, "all learned"
    assert training_cost(8, 1, 5, None, 0, NORTHSHIRE) == 0, "spellbook unread"
    assert training_cost(8, 1, 5, mage, 0, None) == 0, "position unknown"
    moonbrook = (-11000.0, 1500.0)
    known = {6603, 20154, 635}
    kept = training_cost(2, 1, 14, known, 0, moonbrook)
    assert trainer_due(2, 1, 14, known, kept, 0, moonbrook) is not None
    assert trainer_due(2, 1, 14, known, kept - 1, 0, moonbrook) is None


# --- what is worth buying, and in what order (V237) --------------------------------------

from collections import defaultdict  # noqa: E402

from jev.world.training import buy_order, starting_bar, worth_buying  # noqa: E402

ZALDIMAR = next(t for t in training.trainers(8, 1, 0) if t.name == "Zaldimar Wefhellt")
MAGE_START = frozenset({6603, 133, 168})
# The mage as it stood on 26 September, one spell bought a visit in the window's order.
MAGE_LIVE = frozenset({6603, 133, 168, 1459, 5504, 587, 2136})
MAGE_LIVE_BAR = {1: 6603, 2: 133, 3: 168, 4: 1459, 5: 5504, 6: 587, 7: 2136, 8: 0, 9: 0,
                 10: 0, 11: None, 12: None}


def _names(spell_ids):
    return [f"{spell(s).name} {spell(s).rank}" if spell(s).rank else spell(s).name
            for s in spell_ids]


def test_a_spell_nothing_presses_or_the_bar_will_not_hold_is_not_worth_buying():
    """Parry (passive), Slow Fall (a new short buff), Flash of Light (a new heal) and
    Blessing of Protection beside Divine Protection (a second save) are never pressed: not
    worth a copper. A new rank of a spell on the bar, a strike, a root, Polymorph (V287),
    damage round the caster (Arcane Explosion, V277) or a conjure is."""
    mage_bar = starting_bar(8, 1)
    for spell_id in (130,):
        assert not worth_buying(spell_id, MAGE_START, mage_bar), spell(spell_id).name
    for spell_id in (116, 143, 122, 118, 1449, 5504, 7300, 5143):
        assert worth_buying(spell_id, MAGE_START, mage_bar), spell(spell_id).name
    assert worth_buying(5505, MAGE_START | {5504}, mage_bar), "Conjure Water rank 2"
    paladin_bar = starting_bar(2, 1)
    paladin = frozenset({6603, 20154, 635, 498})
    for spell_id in (3127, 19750, 1022, 21082):
        assert not worth_buying(spell_id, paladin, paladin_bar), spell(spell_id).name
    for spell_id in (639, 20287, 853, 633, 465):
        assert worth_buying(spell_id, paladin, paladin_bar), spell(spell_id).name


def test_a_rank_of_a_spell_off_the_bar_or_a_spell_with_no_slot_left_is_not_worth_buying():
    """Seal of the Crusader, bought in the window's order, never went on the bar: its rank 2
    (10 silver at level 12) would not either. A new spell with no slot left for it is as
    unused as Polymorph: the mage's full bar at 12 takes no Dampen Magic, and its Fireball
    rank 3 still goes over rank 2."""
    paladin = frozenset({6603, 20154, 635, 21082})
    assert not worth_buying(20162, paladin, starting_bar(2, 1)), "Seal of the Crusader 2"
    full = {1: 6603, 2: 143, 3: 7300, 4: 1459, 5: 205, 6: 5505, 7: 2136, 8: 587, 9: 5143,
            10: 122, 11: None, 12: None}
    known = set(full.values()) - {None}
    assert not worth_buying(604, known, full), "Dampen Magic: no slot"
    assert worth_buying(145, known, full), "Fireball 3 over Fireball 2"


def test_polymorph_sends_the_mage_to_a_trainer_and_keeps_its_copper_back():
    """V215's reserve and the visit itself count only what would be bought. Since V287 the
    fight presses Polymorph, holding one of two attackers: a level 8 mage with everything
    else learned is walked to Zaldimar for it, and keeps its 200 copper."""
    goldshire = (-9460.0, 60.0)
    known = MAGE_START | {1459, 116, 205, 5504, 587, 2136, 143, 5143}
    assert [o.spell_id for o in training.learnable(ZALDIMAR, 8, known, race_id=1)] == [118]
    assert trainer_due(8, 1, 8, known, 10_000, 0, goldshire) is not None
    assert training_cost(8, 1, 8, known, 0, goldshire) == 200
    assert [o.spell_id for o in training.learnable(ZALDIMAR, 8, known | {118},
                                                   race_id=1)] == []


def test_a_talent_s_later_rank_is_not_counted_as_for_sale():
    """Pyroblast rank 2 is on Zaldimar's list at 24, its rank 1 a talent the bot never
    takes: the stock window never teaches it. Counted, it took the last free slot in the
    shopping of a mage new at 24. Fireball rank 2's rank 1 is a starting spell, and counts.
    The bar's free slots go to what a fight is won with first (V237): with Arcane Explosion
    one of those (V277), a mage new at 24 has none left for Conjure Water."""
    sale = {o.spell_id for o in training.learnable(ZALDIMAR, 24, MAGE_START, race_id=1)}
    assert 12505 not in sale, "Pyroblast 2"
    assert 143 in sale, "Fireball 2"
    assert 1449 in sale, "Arcane Explosion 1"


def test_what_acts_in_a_fight_is_bought_before_what_is_kept_up_between_fights():
    """At 10: Frost Nova before Conjure Water 2 and Frost Armor 2, which the window sells
    first. At 12: Fireball 3 before Conjure Food 2. At 8, a new rank of a strike the bar
    holds before a new strike, and at any level the oldest gap before a newer spell."""
    def ordered(level, known):
        offers = [o for o in ZALDIMAR.offers if o.level == level
                  and worth_buying(o.spell_id, known, starting_bar(8, 1))]
        return _names(o.spell_id for o in sorted(offers, key=lambda o: buy_order(o, known)))

    steady = MAGE_START | {1459, 116, 5504, 143, 587, 2136, 205, 5143}
    assert ordered(10, steady) == ["Frost Nova 1", "Conjure Water 2", "Frost Armor 2"]
    assert ordered(12, steady | {122, 5505, 7300})[:2] == ["Fireball 3", "Conjure Food 2"]
    # Polymorph holds one of two attackers (V287), bought by its level as a strike is.
    assert ordered(8, MAGE_START | {116, 1459}) == ["Frostbolt 2", "Arcane Missiles 1",
                                                    "Polymorph 1"]
    frostbolt, fireball = (next(o for o in ZALDIMAR.offers if o.spell_id == s)
                           for s in (116, 143))
    assert buy_order(frostbolt, MAGE_LIVE) < buy_order(fireball, MAGE_LIVE)


def test_a_mage_behind_on_its_spells_buys_frost_nova_first_at_ten():
    """V242: 15 of the level 8 mage's 17 deaths had two to four attackers; behind on its
    spells, it would have bought Frostbolt and both level 8 spells before Frost Nova."""
    bought, _, _ = _visit(10, MAGE_LIVE, MAGE_LIVE_BAR, 10**7)
    assert _names(bought[:1]) == ["Frost Nova 1"]


def test_every_role_the_fight_code_presses_is_ranked_for_buying():
    from jev.world.combat import TRAINED_ROLES

    assert set(training.BUY_ORDER) == set(TRAINED_ROLES)
    assert {"utility", "passive"}.isdisjoint(training.BUY_ORDER)
    assert "cc" in training.BUY_ORDER, "Polymorph holds one of two attackers (V287)"


def _visit(level, known, bar, money):
    """What the desk buys at Zaldimar with `money`, as `TrainerDesk._choose` does: of the
    rows the stock window marks learnable now (a rank only once the rank before it is
    known), the first the purse pays for on the shopping list of them all."""
    known, bought = set(known), []

    def learnable_now(offer):
        facts = spell(offer.spell_id)
        before = [o for o in ZALDIMAR.offers if (f := spell(o.spell_id)) is not None
                  and f.name == facts.name and f.rank == facts.rank - 1]
        return not before or any(o.spell_id in known for o in before)

    while True:
        rows = [o for o in ZALDIMAR.offers if o.level <= level and o.spell_id not in known
                and learnable_now(o)]
        best = next((o for o in training.shopping(rows, known, bar) if o.cost <= money), None)
        if best is None:
            return bought, known, money
        bought.append(best.spell_id)
        known.add(best.spell_id)
        money -= best.cost


def _placed(bar, known):
    bar = dict(bar)
    for p in placements(bar, known):
        bar[p.slot] = p.spell_id
    return bar


def test_a_mage_with_one_spell_s_money_buys_what_it_fights_with():
    """A mage that kept up, with one spell's money at 8, 10, 12 and 14: Frostbolt 2, Frost
    Nova (over Polymorph's slot when the bar is full, V287), Fireball 3, Frostbolt 3. In the
    window's order (skill line, then name) the Arcane rows come first: Arcane Missiles and
    Polymorph at 8, Conjure Water 2 at 10, Conjure Food 2, Dampen Magic and Slow Fall at 12,
    Arcane Explosion at 14. The mage as it stood on 26 September buys Frostbolt and Fireball
    rank 2 at 8 with 200 copper, the two it lacked."""
    known, bar, first = set(MAGE_START), starting_bar(8, 1), {}
    for level in range(2, 16, 2):
        price = max((o.cost for o in ZALDIMAR.offers if o.level == level), default=0)
        if level in (8, 10, 12, 14):
            first[level] = _names(_visit(level, known, bar, price)[0][:1])
        _, known, _ = _visit(level, known, bar, 10**7)
        bar = _placed(bar, known)
    assert first == {8: ["Frostbolt 2"], 10: ["Frost Nova 1"], 12: ["Fireball 3"],
                     14: ["Frostbolt 3"]}
    bought, _, left = _visit(8, MAGE_LIVE, MAGE_LIVE_BAR, 200)
    assert (_names(bought), left) == (["Frostbolt 1", "Fireball 2"], 0)


def test_a_full_bar_gives_a_conjures_place_to_a_new_fight_line_and_keeps_water():
    """V394: a new mage's bar took Polymorph at 8 into its last free slot; at 10 Frost Nova
    goes over Conjure Food, where since V287 it went over Polymorph, and none of the 30 hive
    mages at 12 and over kept Polymorph (7 Oct). A long buff does not push a fight line off
    (V237); Conjure Water gives way to nothing."""
    full = {1: 6603, 2: 143, 3: 168, 4: 1459, 5: 116, 6: 5504, 7: 2136, 8: 587, 9: 5143,
            10: 118, 11: None, 12: None}
    known = {i for i in full.values() if i}
    placed = training.placements(full, known | {122})
    assert [(p.spell_id, p.slot, p.replaces) for p in placed] == [(122, 8, 587)]
    assert worth_buying(122, known, full), "Frost Nova, over Conjure Food"
    with_nova = {**full, 8: 122}
    assert training.placements(with_nova, known | {122, 604}) == [], \
        "Dampen Magic pushes no fight line off"
    # At 14 Arcane Explosion: the newest long buff next - Arcane Intellect, taught at 1, over
    # Frost Armor, which a mage starts with - never the water.
    more = training.placements(with_nova, known | {122, 1449})
    assert [(p.spell_id, p.slot, p.replaces) for p in more] == [(1449, 4, 1459)]


def test_every_trainer_s_offers_are_told_apart_by_name_and_rank():
    """The desk knows a row by its name hash and rank. Across every trainer, the only two
    offers alike in both are two spells of one name (a Shattrath portal and teleport for
    each side, two Cure Diseases), none of them worth buying."""
    from jev.perceive.radio_frame import name_id

    alike = []
    for offers in training.catalog()["offers"].values():
        by_row = defaultdict(set)
        for o in offers:
            if (f := spell(o["spell"])) is not None:
                by_row[(name_id(f.name), f.rank)].add(o["spell"])
        alike += [ids for ids in by_row.values() if len(ids) > 1]
    assert len(alike) == 3
    assert all(len({spell(s).name for s in ids}) == 1 for ids in alike), "a hash collision"
    assert all(spell(s).role == "utility" for ids in alike for s in ids)


# -- the classifier reads damage over time, shouts, shields and aspects (V361) ------------

def test_damage_over_time_shouts_shields_and_aspects_have_their_roles():
    """Read from the spell's own data, never its name: periodic damage on the enemy is a dot,
    damage beside an interrupt cast with mana a strike, Battle Shout a long buff on the party
    round the warrior, Power Word: Shield a save, the hunter's aspects kept auras."""
    roles = {sid: spell(sid).role for sid in (172, 589, 1978, 772, 980, 8042, 6673, 17,
                                              13163, 13165)}
    assert roles == {172: "dot", 589: "dot", 1978: "dot", 772: "dot", 980: "dot",
                     8042: "strike", 6673: "long_buff", 17: "save", 13163: "aura",
                     13165: "aura"}
    assert spell(17).every_s == 25.0 and spell(17).self_cast


def test_what_the_fight_cannot_press_stays_utility():
    """A form, stealth, speed, tracking, a one-school ward, a stealth opener, a finisher, a
    channel, a rage or energy interrupt, Charge and Life Tap: nothing presses them yet."""
    # Hunter's Mark (1130) is a mark since V404, pressed as a fight opens.
    for sid in (1784, 5118, 1494, 703, 1943, 689, 1766, 72, 100, 1454):
        assert spell(sid).role == "utility", (sid, spell(sid).name, spell(sid).role)
    assert spell(543).role == "short_buff"                       # Fire Ward: one school


def test_a_priest_buys_and_places_its_shield_and_shadow_word_pain():
    """Power Word: Shield was a new short buff, never placed nor bought; Shadow Word: Pain
    utility. A level 6 priest's trainer has both."""
    bar = training.starting_bar(5, 1)
    known = {6603, 585, 2050}
    for sid in (589, 17):
        assert worth_buying(sid, known, bar), sid
    plan = {p.spell_id for p in placements(bar, known | {589, 17})}
    assert plan == {589, 17}


def test_a_warrior_buys_and_places_battle_shout_and_rend():
    bar = training.starting_bar(1, 1)
    known = {6603, 78}
    for sid in (6673, 772):
        assert worth_buying(sid, known, bar), sid
    assert {p.spell_id for p in placements(bar, known | {6673, 772})} == {6673, 772}


def test_what_nothing_presses_is_said():
    """V361: a spell nobody classified was a spell nobody pressed, and nothing said so."""
    names, unknown = training.unpressed({6603, 78, 100, 6673, 772, 2687, 999999})
    assert names == ["Bloodrage", "Charge"] and unknown == [999999]
