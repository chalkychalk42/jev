"""V555: a healing potion drunk in a fight about to be lost, a mana potion by a caster out of mana,
from the bar where the body put it. The case is hive-782's (`tests/fixtures/potion-death.json`): a
human rogue of 13 with eight Minor Healing Potions (70-90 health of its 269) went from full
health to none in 24 s against two (9 Oct 00:59), and drank none - nothing in Jev used one."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_body import body

from jev.clients.fight import Fight
from jev.world import potions

CASE = json.loads((Path(__file__).parent / "fixtures" / "potion-death.json").read_text())
ROGUE, PRIEST, HUMAN = 4, 5, 1
ALL = 0b111111111111


def test_the_potions_are_known_by_kind_level_and_what_they_restore():
    minor = potions.table()[118]
    assert (minor["kind"], minor["level"], minor["min"], minor["max"]) == ("heal", 1, 70, 90)
    assert potions.kind(2455) == "mana" and potions.kind(929) == "heal"
    assert potions.kind(159) is None, "water is no potion"
    assert potions.best({118: 8, 858: 1}, "heal", 13) == 858, "the Lesser restores more"
    assert potions.best({118: 8, 929: 1}, "heal", 11) == 118, "Healing Potion from 12"
    assert potions.best({2455: 2}, "heal", 13) is None


@pytest.mark.skipif(not Path("data/knowledge/tbc-243.sqlite").exists(), reason="local DB required")
def test_the_potions_table_regenerates_exactly(tmp_path):
    out = tmp_path / "potions.json"
    subprocess.run([sys.executable, "tools/gen_potions.py", "--out", str(out)], check=True,
                   capture_output=True)
    assert out.read_bytes() == Path("content/tbc/potions.json").read_bytes()


def _fight(potion_slots):
    hid = SimpleNamespace(taps=[])
    hid.tap = lambda key: hid.taps.append(key) or True
    fight = Fight(hid=hid, read=lambda: None, read_frame=lambda: None)
    fight.potions = dict(potion_slots)
    return fight, hid


def _look(tick, **over):
    return {"vitals.combat": tick["combat"], "vitals.hp": tick["hp"], "vitals.power": tick["power"],
            "vitals.power_type": 3, "bars.usable": ALL, "bars.ready": ALL,
            "combat.attackers": CASE["attackers"], **over}


def test_the_rogue_drinks_its_potion_once_as_the_fight_turns(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.fight.time.monotonic", lambda: now[0])
    fight, hid = _fight({"heal": 11})
    drunk = []
    for tick in CASE["ticks"]:
        now[0] = 1000.0 + tick["t"]
        if fight._potion(_look(tick)):
            drunk.append(tick)
    (first,) = drunk
    assert first["hp"] < potions.HEAL_BELOW and hid.taps == ["minus"]
    assert first["t"] < -8.0, "with eight seconds and 86 health to go: +70-90"
    assert first["target_hp"] < 0.35, "against a unit at a third of its health"


def test_no_potion_out_of_a_fight_without_one_or_while_it_cools(monkeypatch):
    low = {"combat": True, "hp": 0.2, "power": 1.0}
    fight, hid = _fight({"heal": 11})
    assert not fight._potion(_look({**low, "combat": False}))
    assert not fight._potion(_look(low, **{"bars.usable": ALL & ~(1 << 10)})), "none in the bags"
    assert not fight._potion(_look(low, **{"bars.ready": ALL & ~(1 << 10)})), "cooling"
    assert not _fight({})[0]._potion(_look(low)), "none on the bar"
    assert fight._potion(_look(low)) and not fight._potion(_look(low)), "two minutes shared"
    assert hid.taps == ["minus"]


def test_a_caster_out_of_mana_drinks_a_mana_potion_and_a_hurt_one_heals_first():
    fight, hid = _fight({"heal": 10, "mana": 9})
    caster = {"vitals.combat": True, "vitals.power_type": 0, "bars.usable": ALL, "bars.ready": ALL}
    assert fight._potion({**caster, "vitals.hp": 0.8, "vitals.power": 0.08})
    assert hid.taps == ["9"]
    fight, hid = _fight({"heal": 10, "mana": 9})
    assert fight._potion({**caster, "vitals.hp": 0.3, "vitals.power": 0.05})
    assert hid.taps == ["0"], "health first"
    fight, hid = _fight({"mana": 9})
    assert not fight._potion({**caster, "vitals.power_type": 3, "vitals.hp": 0.8,
                              "vitals.power": 0.05}), "energy is no mana"


def test_the_body_puts_the_potion_on_a_free_slot_and_the_fight_presses_it(monkeypatch):
    b = body()
    values = {"char.class_id": ROGUE, "char.race_id": HUMAN, "char.level": 13,
              "vitals.combat": False, "ui.vendor": False}
    b.client.read = lambda: values
    b._larder_rows = {118: 8}
    census = {**dict.fromkeys(range(1, 13), 0), 1: 6603, 2: 1752, 12: None}
    b._census = lambda seconds=0: (dict(census), frozenset())
    placed = []

    class Placer:
        detail = ""

        def __init__(self, *a, **kw):
            pass

        def place_item(self, item, slot):
            placed.append((item, slot))
            census[slot] = None
            return True
    monkeypatch.setattr("jev.run.body.Vendor", Placer)
    b._stock_bar()
    assert placed == [(118, 11)], "slot 11: a rogue drinks no water"
    b.fight = SimpleNamespace(profile=None, potions={})
    b.client.spells = SimpleNamespace()
    monkeypatch.setattr("jev.run.body.profile_from_bar", lambda bar, base: Mock())
    b._bar_profile()
    assert b.fight.potions == {"heal": 11}
    census[11] = 2098                       # a spell dragged over it since
    b._bar_profile()
    assert b.fight.potions == {} and 11 not in b._bar_items


def test_a_priest_keeps_its_food_and_water_slots_and_takes_the_highest_free_one(monkeypatch):
    b = body()
    b.client.read = lambda: {"char.class_id": PRIEST, "char.race_id": HUMAN, "char.level": 14,
                             "vitals.combat": False, "ui.vendor": False}
    b._larder_rows = {2070: 4, 159: 6, 118: 2, 3385: 1}
    census = {**dict.fromkeys(range(1, 11), 0), 1: 6603, 2: 585, 3: 2050, 10: 591,
              11: None, 12: None}
    b._census = lambda seconds=0: (dict(census), frozenset())
    placed = []

    class Placer:
        detail = ""

        def __init__(self, *a, **kw):
            pass

        def place_item(self, item, slot):
            placed.append((item, slot))
            census[slot] = None
            return True
    monkeypatch.setattr("jev.run.body.Vendor", Placer)
    b._stock_bar()
    assert placed == [(118, 9), (3385, 8)], "slot 10 holds Smite rank 2, 11 and 12 the meal"
