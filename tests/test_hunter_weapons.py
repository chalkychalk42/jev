"""bet-hunter3 (V492, V493): a hunter mends its main hand first, then its ranged weapon, and
reads either one broken as disarmed. The cases are recorded play
(`tests/fixtures/hunter-weapons-broken.json`): hive-698 (Naramin, a draenei hunter of 9,
everything worn at 0%) at its repairer on 8 Oct 02:51 with 22 copper, V402's ranged weapon first
(the Light Crossbow, 68 copper, refused) and Repair All spending 11 on the belt, the Worn
Shortsword's 15 short; it then lost 53 of 59 fights and sat at 11 copper for four hours.
hive-732 (Telunti, a tauren of 9) at 09:50 with 121 copper: its gun mended first for 45, the
armour after it, the Stone Tomahawk left broken, and at hand in its next fight with Raptor
Strike greyed and Auto Shot whole."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_hunter import _fight, _hunter_body, _s

from jev.clients.repair import MAIN_HAND, RANGED, Repair, Repaired
from jev.world.combat import for_class, from_bar

FIXTURE = Path(__file__).parent / "fixtures" / "hunter-weapons-broken.json"


def _case() -> dict:
    return json.loads(FIXTURE.read_text())


def _mend(purse: int, kit: list[dict], first: tuple[int, ...], discount: float):
    """The server's arithmetic for a repair at a merchant: each of `first` mended alone while the
    purse pays for it (the hive bridge's `repair <npc> <item>`), then Repair All, item by item in
    slot order, each mended whole or not at all as the purse pays (`Player::DurabilityRepair`,
    `DurabilityRepairAll`). The purse left and the slots mended."""
    owed = {item["slot"]: int(item["cost"] * discount) for item in kit}
    mended = []
    for slot in (*first, *sorted(owed)):
        price = owed.get(slot)
        if slot in mended or price is None or price > purse:
            continue
        purse -= price
        mended.append(slot)
    return purse, mended


def _order(tmp_path, cls: str, race: str) -> tuple[int, ...]:
    """The slots the body asks to be mended first, as `LiveBody._repair` sets them."""
    b = _hunter_body(tmp_path)
    b.repair = SimpleNamespace(run=lambda: Repaired.DONE, detail="", first_slots=None)
    b._repairer_yards = lambda: None
    b._repair(_s(cls=cls, race=race, durability_min=0.0))
    return b.repair.first_slots


def test_hive_698s_22_copper_mended_the_belt_where_its_sword_was_due(tmp_path):
    """Replayed with V402's order (the ranged weapon alone first), the server's arithmetic gives
    what was recorded: the crossbow refused, 11 copper on the belt, the sword broken. With the
    order the body now asks (the main hand, then the ranged weapon) the sword is mended."""
    case = _case()["hive-698"]
    kit, discount = case["kit"], case["discount"]
    assert case["first"][0]["slot"] == RANGED and case["first"][0]["paid"] == 0
    purse, mended = _mend(case["purse_before"], kit, (RANGED,), discount)
    assert purse == case["purse_after"] == 11, "the record: 22 copper, 11 left"
    assert MAIN_HAND not in mended and RANGED not in mended
    order = _order(tmp_path, "hunter", "draenei")
    assert order == (MAIN_HAND, RANGED)
    purse, mended = _mend(case["purse_before"], kit, order, discount)
    assert mended[0] == MAIN_HAND and purse == 7, "the Worn Shortsword's 15 paid first"


def test_hive_732s_121_copper_mends_both_weapons_before_its_armour(tmp_path):
    """Its gun was mended for 45 and the armour took the rest, 12 left and the tomahawk broken.
    Asked in the body's order, both weapons are mended (22 and 45) before any armour."""
    case = _case()["hive-732"]
    assert case["first"][0] == {"slot": RANGED, "entry": 2509, "paid": 45, "ok": True}
    order = _order(tmp_path, "hunter", "tauren")
    purse, mended = _mend(case["purse_before"], case["weapons"], order, case["discount"])
    assert mended == [MAIN_HAND, RANGED] and purse == 121 - 22 - 45


def test_the_weapons_a_class_fights_with_are_mended_first(tmp_path):
    for cls, race, slots in (("hunter", "dwarf", (MAIN_HAND, RANGED)),
                             ("warrior", "orc", (MAIN_HAND,)), ("rogue", "troll", (MAIN_HAND,)),
                             ("mage", "troll", ())):
        assert _order(tmp_path, cls, race) == slots, cls


def test_a_bridge_from_before_reads_the_first_slot_alone():
    """The hive's `ServerRepair` from before V492 reads `first_slot`, and its tests set it: the
    main hand for a hunter now, the one the purse pays for first."""
    repair = Repair(hid=None, read=lambda: None, visit=lambda: True)
    assert repair.first_slot is None and repair.first_slots == ()
    repair.first_slots = (MAIN_HAND, RANGED)
    assert repair.first_slot == MAIN_HAND
    repair.first_slot = RANGED
    assert repair.first_slots == (RANGED,)
    repair.first_slot = None
    assert repair.first_slots == ()


def test_hive_732_at_hand_with_its_tomahawk_broken_is_disarmed():
    """Its strip in the fight after the gun was mended: Raptor Strike greyed (the tomahawk at 0%),
    Auto Shot usable, the unit at hand. V402 read the gun alone and called it armed."""
    fight = _case()["hive-732"]["fight_after"]
    values = fight["sample"]
    assert fight["usable_every_sample"] == [values["bars.usable"]] and values["target.in_melee"]
    profile = from_bar({int(s): spell for s, spell in fight["bar"].items()},
                       for_class(values["char.class_id"], values["char.race_id"]))
    raptor = next(a for a in profile.abilities if a.name == "Raptor Strike")
    shot = next(a for a in profile.abilities if a.name == "Auto Shot")
    usable = values["bars.usable"]
    assert not usable & (1 << (raptor.slot - 1)) and usable & (1 << (shot.slot - 1))
    f, _ = _fight(values, profile)
    assert f.disarmed(values) is True
    whole = {**values, "bars.usable": usable | (1 << (raptor.slot - 1))}
    assert f.disarmed(whole) is False, "both weapons usable: armour is the broken thing"
    assert f.disarmed({**whole, "bars.usable": usable & ~(1 << (shot.slot - 1))
                       | (1 << (raptor.slot - 1))}) is True, "the gun broken (V402)"
