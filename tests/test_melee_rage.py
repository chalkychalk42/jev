"""Rage waits for the blow ranked first (V500): two recorded warrior fights of the hive's cohort.

`tests/fixtures/warrior-rage-starved.json` holds each fight's looks as the strip painted them
(`combat.observed`: health, rage, `bars.usable`, `bars.ready`, the unit's health, attackers) and
the slots pressed (`ability.request`), with the bar the session placed (`placements` at once
from the class's starting bar, as `character-*.bar.json` holds it).

- hive-683 (Brun, orc 15, 8 Oct 15:37, run 20261008T153612-9959de): Battle Shout, Rend, then
  Hamstring three times, every press at 12.9-13.2 rage, Heroic Strike (15) never.
- hive-728 (Orentino, human 12, 14:51, run 20261008T145145-6de222): three attackers, Thunder
  Clap on the bar; Rend, then Hamstring six times refused at 10.8-13.1 rage while it died.

The replay keeps the rage each look gained in the recording (what the swings and the blows taken
brought, the cost of a recorded press that spent its rage added back, a refused one's not) and
spends the cost of what the rotation presses now, a next-swing blow at its press.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from test_fight import _fight, _Hid, combat_clock  # noqa: F401

from jev.world.combat import for_class, from_bar, reach
from jev.world.training import spell

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "warrior-rage-starved.json"
FIGHTS = json.loads(FIXTURE.read_text(encoding="utf-8"))["fights"]
POOL = 1000
# Revenge (Defensive Stance) and Overpower (after a dodge) are greyed for more than rage: their
# bits are the recording's.
CONDITIONAL = {"Revenge", "Overpower"}


def _bar(fight):
    return {int(k): v for k, v in fight["bar"].items()}


def _profile(fight, race_id):
    census = {slot: _bar(fight).get(slot, 0) for slot in range(1, 13)}
    return from_bar(census, for_class(1, race_id))


def _cost(spell_id):
    facts = spell(spell_id)
    return facts.mana if facts is not None else 0


def _values(fight, look, rage, *, gcd=0.0):
    t, hp, _, usable, ready, target_hp, in_melee, attackers = look
    bits = far = 0
    for slot, spell_id in _bar(fight).items():
        facts = spell(spell_id)
        bit = 1 << (slot - 1)
        if facts is None:
            continue
        facts_reach = reach(spell_id)
        if facts_reach is not None and facts_reach.max_yd >= 20:
            far |= bit                           # Throw: out of range at hand
        if facts.name in CONDITIONAL:
            ok = bool(usable & bit)
        else:
            ok = _cost(spell_id) <= rage
        if ok and (facts.name != "Revenge"):
            bits |= bit
    return {"target.has": True, "target.hp": target_hp, "target.name_id": 1161,
            "target.in_melee": in_melee, "target.attacking_me": True,
            "combat.attackers": attackers, "vitals.hp": hp, "vitals.combat": True,
            "vitals.power": rage / POOL, "vitals.power_max": POOL, "vitals.power_type": 1,
            "bars.usable": bits | 1, "bars.ready": 4095, "bars.gcd": gcd, "bars.casting": False,
            "bars.out_range": far,
            "bars.attacking": True, "char.class_id": 1, "vitals.dead": False}


def _replay(fight, race_id, clock, *, rule=True):
    """The fight's recorded rage income through the rotation: the slots pressed, in order."""
    hid = _Hid()
    f = _fight([{}], hid=hid)
    f.profile = _profile(fight, race_id)
    f._toggled = True
    bar = _bar(fight)
    recorded = {round(t, 2): slot for t, slot in fight["presses"]}
    looks = fight["looks"]
    rage = looks[0][2] * POOL
    pressed: list[int] = []
    if not rule:
        f._rage_wanted = lambda *a, **k: None
    for prev, look in zip(looks, looks[1:]):
        # What the recorded presses spent: a press the server refused spent nothing.
        spent_then = sum(_cost(bar[s]) for t, s in recorded.items()
                         if prev[0] <= t < look[0] and s != 1)
        if (prev[2] - look[2]) * POOL < 0.5 * spent_then:
            spent_then = 0
        rage = max(0.0, min(POOL, rage + max(0.0, (look[2] - prev[2]) * POOL + spent_then)))
        clock[0] = look[0]
        before = len(hid.taps)
        f._rotate(_values(fight, look, rage))
        for key in hid.taps[before:]:
            slot = int(key) if key.isdigit() else {"0": 10}.get(key, 0)
            pressed.append(slot)
            if slot != 1:
                rage = max(0.0, rage - _cost(bar[slot]))
        if f._pending_press is not None:
            clock[0] = look[0] + 0.1
            f._rotate({**_values(fight, look, rage), "bars.gcd": 0.5})
    return pressed


def _names(fight, slots):
    bar = _bar(fight)
    return [spell(bar[s]).name for s in slots if s in bar]


def test_the_recorded_rotation_spent_heroic_strikes_rage_on_hamstring(combat_clock):
    """Brun's recorded presses: at 12.9 rage, Heroic Strike unpaid, the rotation fell through to
    Hamstring - the rule's case. Replayed without the rule, the same: Battle Shout, Rend, two
    Hamstrings, and one Heroic Strike in the last seconds, where the replay's Rend ran out."""
    brun = FIGHTS[0]
    names = _names(brun, [s for _, s in brun["presses"] if s != 1])
    assert names == ["Battle Shout", "Rend", "Hamstring", "Hamstring", "Hamstring"], names
    old = _names(brun, _replay(brun, 2, combat_clock, rule=False))
    assert old[:4] == ["Battle Shout", "Rend", "Hamstring", "Hamstring"], old
    assert old.count("Heroic Strike") <= 1, old


def test_rage_waits_for_heroic_strike_and_spends_it_there(combat_clock):
    """With the rule, the same rage income buys Heroic Strikes, not Hamstrings; Battle Shout and
    Rend, its dot not yet on the unit, are pressed in their turn as before."""
    brun = FIGHTS[0]
    new = _names(brun, _replay(brun, 2, combat_clock))
    assert "Hamstring" not in new, new
    assert new.count("Heroic Strike") >= 2, new
    assert new[:2] == ["Battle Shout", "Rend"], new


def test_the_rage_buys_more_damage(combat_clock):
    """Heroic Strike rank 2 adds 21 to the swing (the spell's 20 + 1), Hamstring deals 5
    (`world_spell_template`): the replay's rage, spent as the rule spends it, deals 63 more than
    the swings alone, against 36 the old way (two Hamstrings and a late Heroic Strike)."""
    brun = FIGHTS[0]
    extra = {"Heroic Strike": 21, "Hamstring": 5}
    old = sum(extra.get(n, 0) for n in _names(brun, _replay(brun, 2, combat_clock, rule=False)))
    combat_clock[0] = 0.0
    new = sum(extra.get(n, 0) for n in _names(brun, _replay(brun, 2, combat_clock)))
    assert new > 1.5 * old, (old, new)


def test_against_three_rage_waits_for_thunder_clap(combat_clock):
    """Orentino's death: three attackers, Thunder Clap on the bar and never pressed, Hamstring
    six times at 10.8-13.1 rage. With the rule nothing below Thunder Clap spends its 20 while
    the crowd is at hand, and no Hamstring is pressed."""
    orentino = FIGHTS[1]
    recorded = _names(orentino, [s for _, s in orentino["presses"] if s != 1])
    assert recorded.count("Hamstring") == 6 and "Thunder Clap" not in recorded, recorded
    new = _names(orentino, _replay(orentino, 1, combat_clock))
    assert "Hamstring" not in new, new


def test_mana_and_energy_are_not_held(combat_clock):
    """Only rage waits: a rogue's energy and a paladin's mana come back on their own."""
    f = _fight([{}])
    f.profile = for_class(4, 1)
    rogue = {"vitals.power_type": 3, "vitals.power": 0.4, "vitals.power_max": 100,
             "bars.ready": 4095}
    assert f._rage_wanted(f.profile.abilities, rogue, lambda a: False, lambda a: False,
                          0.0) is None


@pytest.mark.parametrize("name, spends", [("Hamstring", True), ("Rend", False),
                                          ("Overpower", False), ("Mocking Blow", False),
                                          ("Attack", False)])
def test_what_spends_the_rage_waited_for(name, spends):
    """A dot (in its turn, once a unit) and a blow its own cooldown gates are pressed while
    rage waits; a filler with neither is not."""
    from jev.clients.fight import Fight
    from jev.world.combat import Ability, Role

    ids = {"Hamstring": 1715, "Rend": 6546, "Overpower": 7384, "Mocking Blow": 694,
           "Attack": 6603}
    ability = Ability(slot=3, role=Role.ATTACK, name=name, mana=_cost(ids[name]),
                      toggle=name == "Attack", spell_id=ids[name])
    assert Fight._spends_waited(ability) is spends
