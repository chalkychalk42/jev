"""The upgrade decision: what a character can wear, and what beats what it wears."""

from __future__ import annotations

from jev.world import gear
from jev.world.gear import Piece, load_worn, save_worn, upgrades, usable

FACTS = {
    "items": {
        "36": {"slot": "main_hand", "kind": [2, 4], "level": 1, "classes": -1, "races": -1,
               "quality": 1, "score": 10.5},
        "5580": {"slot": "main_hand", "kind": [2, 4], "level": 0, "classes": -1, "races": -1,
                 "quality": 1, "score": 19.6},
        "7000": {"slot": "main_hand", "kind": [2, 7], "level": 0, "classes": -1, "races": -1,
                 "quality": 1, "score": 30.0},
        "6078": {"slot": "off_hand", "kind": [4, 6], "level": 0, "classes": -1, "races": -1,
                 "quality": 1, "score": 55.0},
        "2645": {"slot": "hands", "kind": [4, 3], "level": 2, "classes": -1, "races": -1,
                 "quality": 0, "score": 48.0},
        "9000": {"slot": "hands", "kind": [4, 1], "level": 12, "classes": -1, "races": -1,
                 "quality": 2, "score": 90.0},
        "9001": {"slot": "chest", "kind": [4, 1], "level": 1, "classes": 8, "races": -1,
                 "quality": 1, "score": 40.0},
    },
    "proficiencies": {"1:2": [[2, 4], [4, 0], [4, 1], [4, 2], [4, 3], [4, 6]]},
}
PALADIN = dict(class_id=2, race_id=1, level=8, facts=FACTS)


def test_a_fresh_paladin_takes_the_hammer_the_shield_and_the_gloves():
    """A level 8 paladin fought the night with its Worn Mace and bare hands while its bags
    held all three (run 20260924T090629-93a85b)."""
    chosen = upgrades([36, 5580, 6078, 2645], {}, **PALADIN)
    assert [p.item_id for p in chosen] == [2645, 5580, 6078]


def test_what_it_cannot_use_is_left_in_the_bags():
    assert usable(7000, 2, 1, 8, FACTS) is None, "a one-handed sword: no skill"
    assert usable(9000, 2, 1, 8, FACTS) is None, "level 12"
    assert usable(9001, 2, 1, 8, FACTS) is None, "a rogue's chest (class mask 8)"
    assert usable(1, 2, 1, 8, FACTS) is None, "not in the catalog"


def test_what_is_remembered_worn_is_not_swapped_for_worse():
    worn = {"main_hand": 19.6}
    assert upgrades([36], worn, **PALADIN) == [], "the old mace back on"
    assert [p.item_id for p in upgrades([5580, 36], {"main_hand": 10.5}, **PALADIN)] == [5580]


def test_the_worn_memory_round_trips(tmp_path):
    path = tmp_path / "character.equipped.json"
    assert load_worn(path) == {}
    save_worn(path, [Piece(5580, "main_hand", 19.6)])
    save_worn(path, [Piece(6078, "off_hand", 55.0)])
    assert load_worn(path) == {"main_hand": 19.6, "off_hand": 55.0}


def test_the_generated_catalog_knows_the_paladin_and_its_hammer():
    facts = gear.catalog()
    assert [2, 4] in facts["proficiencies"]["1:2"], "one-handed maces"
    assert usable(5580, 2, 1, 8) == Piece(5580, "main_hand", facts["items"]["5580"]["score"])



def test_bag_gear_is_kept_only_when_it_beats_what_is_worn_now_or_later():
    from jev.world.gear import keep

    facts = {"items": {"1": {"classes": -1, "kind": [4, 1], "level": 1, "races": -1,
                             "score": 5.0, "slot": "legs"},
                       "2": {"classes": -1, "kind": [4, 1], "level": 1, "races": -1,
                             "score": 2.0, "slot": "legs"},
                       "3": {"classes": -1, "kind": [4, 1], "level": 20, "races": -1,
                             "score": 9.0, "slot": "legs"},
                       "4": {"classes": -1, "kind": [2, 8], "level": 1, "races": -1,
                             "score": 9.0, "slot": "main_hand"}},
             "proficiencies": {"1:2": [[4, 1]]}}
    kept = keep([1, 2, 3, 4, 5], {"legs": 3.0}, class_id=2, race_id=1, facts=facts)
    assert kept == {1, 3}, "better now, better later; not worse, not unwearable, not unknown"
