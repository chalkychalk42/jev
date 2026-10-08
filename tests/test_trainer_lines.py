"""The training line of the class trainers whose gossip menu the world database leaves empty (V491).

The cohort's blood elf hunters visited their five trainers 88 times on 7-8 October and trained
nothing: menu 6652 has no option, so the gossip opened on a text with nothing to choose and
`_open_trainer` found no line to pick. The hive's database gains each menu its class's usual line
(JevHive's server/sql/trainers.sql); the catalog names the same line.
"""

import sqlite3

from jev.world.training import catalog
from tools import gen_trainer_catalog as gen

# Each trainer the world database leaves without a line, and its menu.
EMPTY = {15513: 6652, 16270: 6652, 16672: 6652, 16673: 6652, 16674: 6652,   # blood elf hunters
         4146: 4008, 5479: 4482, 11406: 3642, 27704: 9580}


def test_every_trainer_on_an_empty_menu_has_its_class_line_in_the_catalog():
    by_entry = {t["entry"]: t for t in catalog()["trainers"]}
    for entry, menu in EMPTY.items():
        assert by_entry[entry]["gossip"] == gen.EMPTY_MENU_LINES[menu], by_entry[entry]["name"]


def test_the_blood_elf_hunters_trainers_all_open_on_the_hunters_line():
    horde_hunters = [t for t in catalog()["trainers"] if t["class"] == 3 and "horde" in t["sides"]
                     and t["entry"] in EMPTY]
    assert {t["name"] for t in horde_hunters} == {"Ranger Sallina", "Hannovia", "Tana", "Oninath",
                                                  "Zandine"}
    assert {t["gossip"] for t in horde_hunters} == {"I seek training in the ways of the Hunter."}


def _world(rows):
    db = sqlite3.connect(":memory:")
    db.execute("create table world_gossip_menu_option (menu_id, id, option_text, option_id)")
    db.executemany("insert into world_gossip_menu_option values (?, ?, ?, ?)", rows)
    return db


def test_the_generator_takes_the_database_line_first_and_the_empty_menus_line_after():
    db = _world([(4648, 0, "I seek training in the ways of the Hunter.", 5),
                 (4648, 1, "I wish to unlearn my talents.", 1),
                 (6652, 9, "Not a training line.", 1)])
    assert gen._gossip(db, 4648) == "I seek training in the ways of the Hunter."
    assert gen._gossip(db, 6652) == "I seek training in the ways of the Hunter."
    assert gen._gossip(db, 4482) == "I require warrior training."
    assert gen._gossip(db, 1234) is None, "a menu with no line, not one the data leaves empty"
    assert gen._gossip(db, 0) is None
