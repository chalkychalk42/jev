"""Class training: the catalog's roles, the trainer chosen, and what goes on the bar."""

from __future__ import annotations

from jev.world import training
from jev.world.training import Placement, placements, spell, trainer_due

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
    # Weapon blows and drains are strikes too, whatever the class.
    assert {spell(sid).role for sid in (78, 1752, 75, 2973, 28734)} == {"strike"}
    assert spell(19740).self_cast and not spell(20154).self_cast
    assert spell(19740).every_s == 595.0


def test_judgement_is_taught_by_a_spell_that_learns_it():
    """The trainer's entry is 10321, whose effect is "learn spell 20271"."""
    sammuel = next(t for t in training.trainers(2, 1, 0) if t.name == "Brother Sammuel")
    assert 20271 in {o.spell_id for o in sammuel.offers}
    assert 10321 not in {o.spell_id for o in sammuel.offers}


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
    # What the purse buys, not what it could buy one at a time: 510 copper is all six of
    # Brother Sammuel's and six of Brother Wilhelm's nine, so the nearer one.
    assert trainer_due(2, 1, 8, known, 510, 0, NORTHSHIRE).name == "Brother Sammuel"


def test_a_new_rank_goes_where_the_old_one_is_and_new_spells_on_free_slots():
    known = {6603, 20154, 635, 639, 465, 20271, 19740, 498, 21082, 853, 1152, 3127, 20600}
    plan = placements(STARTING_BAR, known)
    assert plan[0] == Placement(639, 3, 635)
    assert [(p.spell_id, p.slot) for p in plan[1:]] == [
        (465, 4), (19740, 5), (20271, 6), (498, 7), (853, 8)]
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
