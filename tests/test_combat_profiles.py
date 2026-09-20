"""The generated combat profiles: roles derived from what a button does."""

from __future__ import annotations

import json
import pathlib

from jev.world.combat import (
    GENERIC,
    PROFILES,
    PROFILES_PATH,
    Role,
    for_class,
)

PALADIN, WARRIOR, HUMAN = 2, 1, 1


def test_every_class_has_a_profile_and_they_are_generated():
    assert PROFILES, "the generated file is missing; run tools/gen_combat_profiles.py"
    raw = json.loads(pathlib.Path(PROFILES_PATH).read_text(encoding="utf-8"))
    assert len(raw) >= 40, "one entry per race and class the game ships"
    # The **game's** class ids, which is what the source table is keyed by: shaman is 7,
    # mage 8, warlock 9, druid 11. An earlier compact 1-N table of our own agreed with
    # this only for warrior through priest, and for human paladins by coincidence.
    classes = {entry["class"] for entry in raw.values()}
    assert classes == {1, 2, 3, 4, 5, 7, 8, 9, 11}


def test_a_role_is_what_the_button_does_not_what_it_is_called():
    """First effect 10 is a heal, 6 applies an aura, 78 is melee auto-attack. Nothing
    here matches on a spell's name."""
    paladin = for_class(PALADIN, HUMAN)
    roles = {a.role: a for a in paladin.abilities}
    assert roles[Role.HEAL].name == "Holy Light"
    assert roles[Role.BUFF].name == "Seal of Righteousness"
    assert roles[Role.ATTACK].name == "Attack"


def test_food_and_drink_are_told_apart_by_what_they_restore():
    """Both are item class 0, subclass 5, and neither says which it is in its name. The
    only honest discriminator is the aura: 84 restores health, 85 restores mana. A
    constant saying 'food is slot 11' had `Rest` pressing the water and reporting the
    character was out of food with a wheel of cheese in the bar."""
    paladin = for_class(PALADIN, HUMAN)
    food, drink = paladin.first(Role.FOOD), paladin.first(Role.DRINK)
    assert food is not None and drink is not None
    assert food.name == "Darnassian Bleu" and food.slot == 12
    assert drink.name == "Refreshing Spring Water" and drink.slot == 11


def test_a_buff_carries_its_own_duration():
    """25 seconds is 30 less a margin, read from `dbc_SpellDuration`, not chosen."""
    seal = for_class(PALADIN, HUMAN).first(Role.BUFF)
    assert seal is not None and seal.every_s == 25.0


def test_melee_auto_attack_is_marked_as_a_toggle():
    attack = for_class(PALADIN, HUMAN).first(Role.ATTACK)
    assert attack is not None and attack.toggle is True


def test_a_class_without_a_heal_simply_has_no_heal_row():
    """This is what 'healing is a role, not a module' means in practice."""
    assert for_class(WARRIOR, HUMAN).first(Role.HEAL) is None
    assert for_class(WARRIOR, HUMAN).first(Role.ATTACK) is not None


def test_the_ids_are_the_games_own_not_a_table_of_our_own():
    """Human is 1 and paladin is 2 under either numbering, which is exactly why a compact
    table of our own looked fine: the character this was built against matched by luck,
    and a warlock would have been handed a druid's action bar."""
    from jev.perceive.radio_frame import CLASS_BY_ID, RACE_BY_ID

    assert CLASS_BY_ID[7] == "shaman" and CLASS_BY_ID[11] == "druid"
    assert RACE_BY_ID[2] == "orc" and RACE_BY_ID[11] == "draenei"
    assert for_class(7, HUMAN).name == "shaman"
    assert for_class(8, HUMAN).first(Role.HEAL) is None, "a mage grew a heal"


def test_an_unknown_class_falls_back_rather_than_refusing_to_fight():
    assert for_class(None) is GENERIC
    assert for_class(99) is GENERIC
    assert for_class(6) is GENERIC, "6 is death knight, which TBC does not have"


def test_class_alone_is_enough_when_the_race_is_unknown():
    """Races differ in their starting bread, not in which slot holds the seal."""
    assert for_class(PALADIN).name == "paladin"
    assert for_class(PALADIN, 999).name == "paladin"
