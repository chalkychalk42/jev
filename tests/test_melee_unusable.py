"""A blow struck only from behind, and a spell cast only in a form or stance never taken, are
neither pressed nor placed nor bought (V501).

`tests/fixtures/melee-unusable-spells.json`: every cohort warrior's and rogue's training (the
bridge's `trained` events, 7 Oct 14:43 - 8 Oct 17:00), the server's "not behind" refusals of
each rogue's Backstab, and hive-785's bar as its session placed it (Jefferso, undead rogue 11,
coached by Jev: 985 refusals).
"""

from __future__ import annotations

import json
import pathlib

from test_fight import _fight, _Hid, combat_clock  # noqa: F401

from jev.world.combat import behind, for_class, from_bar, reach
from jev.world.training import catalog, placements, spell, starting_bar, worth_buying

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "melee-unusable-spells.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
NAMES = ("Backstab", "Sap", "Ambush", "Garrote", "Revenge", "Shield Block")


def _cost(spell_id):
    return min(o["cost"] for offs in catalog()["offers"].values() for o in offs
               if o["spell"] == spell_id)


def test_every_cohort_rogue_bought_what_it_could_never_press():
    """All 32 rogues bought Backstab or Sap; the five Jev coaches pressed Backstab and the server
    refused it "not behind" 2,870 times; the local coaches never reached it (Sinister Strike
    comes first in the bar's order)."""
    rogues = DATA["rogues"]
    assert len(rogues) == 32 and all(r["unusable_bought"] for r in rogues)
    refused = {r["guid"]: r["backstab_not_behind"] for r in rogues if r["backstab_not_behind"]}
    assert sum(refused.values()) == 2870 and len(refused) == 5
    assert all(r["coach"] == "jev" for r in rogues if r["guid"] in refused)
    spent = sum(_cost(s) for r in rogues for s in r["unusable_bought"])
    assert spent == sum(r["unusable_copper"] for r in rogues) and spent / 32 > 600


def test_the_spell_data_says_which_blows_ask_for_the_back():
    """The server's own test (`Spell::CheckTarget`): Backstab, Ambush and Garrote from behind; a
    druid's Pounce not (its facing limit went in 2.0.1); Sinister Strike and Eviscerate not."""
    from jev.world.combat import Ability, Role

    def ability(spell_id):
        return Ability(slot=2, role=Role.ATTACK, spell_id=spell_id)

    for spell_id in (53, 2589, 2590, 8676, 703):
        assert behind(ability(spell_id)), spell_id
    for spell_id in (1752, 1757, 2098, 6760, 1776, 78, 1715):
        assert not behind(ability(spell_id)), spell_id
    assert reach(9005) is None or not reach(9005).behind      # Pounce


def test_the_recorded_bar_loses_backstab_and_sap():
    """hive-785's session placed Backstab in slot 5 and Sap in 9; placed again from the same
    spellbook, neither is."""
    recorded = {int(k): v for k, v in DATA["hive_785_bar"].items()}
    assert recorded[5] == 53 and recorded[9] == 6770
    bar = starting_bar(4, 5)
    known = {s for s in bar.values() if s} | set(recorded.values())
    for p in placements(bar, known):
        bar[p.slot] = p.spell_id
    placed = {spell(s).name for s in bar.values() if s and spell(s)}
    assert "Backstab" not in placed and "Sap" not in placed, placed
    assert {"Sinister Strike", "Eviscerate", "Gouge", "Evasion", "Slice and Dice"} <= placed


def test_none_of_them_is_bought():
    """Backstab 2 at 12, Sap at 10, Ambush at 18; a warrior's Revenge at 14 and Shield Block at
    16: not worth buying. Gouge's and Eviscerate's next ranks, and Heroic Strike's, are."""
    rogue_known = {6603, 1757, 6760, 2764, 1776, 5277, 5171}
    rogue_bar = starting_bar(4, 5)
    for spell_id in (2589, 6770, 8676, 703):
        assert not worth_buying(spell_id, rogue_known, rogue_bar), spell_id
    assert worth_buying(1777, rogue_known, rogue_bar) and worth_buying(6761, rogue_known, rogue_bar)
    warrior_known = {6603, 284, 1715, 6546, 6343, 5242, 7384}
    warrior_bar = starting_bar(1, 1)
    for spell_id in (6572, 2565):
        assert not worth_buying(spell_id, warrior_known, warrior_bar), spell_id
    assert worth_buying(285, warrior_known, warrior_bar)


def test_jev_is_never_offered_a_blow_from_behind(combat_clock):
    """With Backstab on a live bar already, the rotation neither presses it nor offers it to
    Jev: hive-785's recorded bar, energy for anything."""
    recorded = {int(k): v for k, v in DATA["hive_785_bar"].items()}
    census = {slot: recorded.get(slot, 0) for slot in range(1, 13)}
    profile = from_bar(census, for_class(4, 5))
    assert any(a.name == "Backstab" for a in profile.abilities)

    class Judge:
        offered: list = []

        def take(self, options):
            self.offered.append(tuple(options))
            return "Backstab" if "Backstab" in options else None

        def ask(self, values, options):
            self.offered.append(tuple(options))

    hid = _Hid()
    f = _fight([{}], hid=hid)
    f.profile, f.judge, f._toggled = profile, Judge(), True
    values = {"target.has": True, "target.hp": 0.8, "target.in_melee": True,
              "target.attacking_me": True, "vitals.combat": True, "vitals.hp": 0.9,
              "vitals.power": 1.0, "vitals.power_max": 100, "vitals.power_type": 3,
              "bars.ready": 4095, "bars.usable": 0b111111111, "bars.gcd": 0.0,
              "bars.casting": False, "bars.attacking": True, "char.class_id": 4,
              "combat.attackers": 1}
    f._rotate(values)
    assert hid.taps and hid.taps[0] != "5", hid.taps
    assert all("Backstab" not in options for options in f.judge.offered), f.judge.offered
