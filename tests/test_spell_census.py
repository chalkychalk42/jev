"""The bar and spellbook censuses: whole under one revision, or unread."""

from __future__ import annotations

from jev.perceive.spellbook import SpellCensus


def _bar(slot, spell, revision=1):
    return {"bars.revision": revision, "bars.slot": slot, "bars.slot_spell": spell}


def _book(index, spell, total=3, revision=1, passive=False):
    return {"spells.revision": revision, "spells.total": total, "spells.index": index,
            "spells.id": spell, "spells.passive": passive}


def test_a_partial_bar_is_unread_and_a_whole_one_is_kept():
    census = SpellCensus()
    for slot in range(1, 12):
        census.observe(_bar(slot, slot * 10))
    assert census.bar is None
    census.observe(_bar(12, 0))
    assert census.bar[1] == 10 and census.bar[12] == 0
    census.observe(_bar(1, 999, revision=2))            # a drag: a new census begins
    assert census.bar[1] == 10                         # the last whole one stands meanwhile


def test_a_new_revision_throws_the_half_built_bar_away():
    census = SpellCensus()
    for slot in range(1, 7):
        census.observe(_bar(slot, 1))
    for slot in range(7, 13):
        census.observe(_bar(slot, 2, revision=2))
    assert census.bar is None


def test_forgetting_the_bar_waits_for_a_whole_new_census():
    census = SpellCensus()
    for slot in range(1, 13):
        census.observe(_bar(slot, slot))
    census.forget_bar()
    assert census.bar is None
    for slot in range(1, 13):
        census.observe(_bar(slot, slot + 100))
    assert census.bar[1] == 101


def test_the_spellbook_is_known_only_whole_and_a_learned_spell_restarts_it():
    census = SpellCensus()
    census.observe(_book(1, 6603))
    census.observe(_book(2, 20154))
    assert census.known is None
    census.observe(_book(3, 635))
    assert census.known == {6603, 20154, 635}
    census.observe(_book(1, 6603, total=4, revision=2))
    assert census.known == {6603, 20154, 635}           # the last whole one, until the next
    for index, spell in ((2, 20154), (3, 635), (4, 465)):
        census.observe(_book(index, spell, total=4, revision=2))
    assert census.known == {6603, 20154, 635, 465}


def test_an_empty_spellbook_is_known_empty_and_an_old_addon_is_unread():
    census = SpellCensus()
    census.observe({"spells.revision": 0, "spells.total": 0})
    assert census.known == frozenset()
    old = SpellCensus()
    old.observe({"quests.count": 2})
    assert old.known is None and old.bar is None
