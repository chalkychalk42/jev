"""V563: a two-hander is worn where it beats what the main and off hand hold together.

Two-handers were left out of the gear catalog ("one would take the shield off"), so a warrior of
14 carried a Barbaric Battle Axe (11.9 damage a second) and fought with a Billy Club (3.9) on 9
Oct, and the hive's mages never held a staff."""

from jev.world.gear import TWO_HAND, Piece, load_worn, save_worn, upgrades, usable

DWARF, HUMAN = 3, 1
WARRIOR, MAGE = 1, 8
SEVERING_AXE = 4562          # a two-handed axe of level 5, 70.3: a dwarf warrior starts with the skill
BILLY_CLUB = 4563            # a one-handed mace


def test_a_two_hander_beats_a_club_and_a_shield_it_is_worth_more_than():
    axe = usable(SEVERING_AXE, WARRIOR, DWARF, 14)
    assert axe is not None and axe.slot == TWO_HAND
    worn = {"main_hand": 39.0, "off_hand": 20.0}
    chosen = upgrades([SEVERING_AXE, BILLY_CLUB], worn, class_id=WARRIOR, race_id=DWARF, level=14)
    assert [p.slot for p in chosen] == [TWO_HAND]


def test_a_one_hander_and_a_shield_worth_more_than_the_two_hander_stay():
    worn = {"main_hand": 60.0, "off_hand": 55.0}           # 115 against the axe's 70
    chosen = upgrades([SEVERING_AXE], worn, class_id=WARRIOR, race_id=DWARF, level=14)
    assert chosen == []


def test_a_two_hander_worn_takes_both_hands_and_a_hand_s_piece_takes_it_off(tmp_path):
    path = tmp_path / "equipped.json"
    save_worn(path, [Piece(1, "main_hand", 39.0), Piece(2, "off_hand", 20.0)])
    save_worn(path, [Piece(SEVERING_AXE, TWO_HAND, 70.3)])
    assert load_worn(path) == {TWO_HAND: 70.3}
    # A one-hander must beat the two-hander worn by itself, the off hand being empty.
    assert upgrades([BILLY_CLUB], load_worn(path), class_id=WARRIOR, race_id=DWARF, level=14) == []
    save_worn(path, [Piece(9, "main_hand", 90.0)])
    assert load_worn(path) == {"main_hand": 90.0}


def test_no_class_without_the_skill_wields_one():
    assert usable(SEVERING_AXE, MAGE, HUMAN, 14) is None
