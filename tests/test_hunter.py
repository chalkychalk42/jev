"""bet-hunter (V401-V405): a hunter's weapon kept in its hands - the rounds it fires, its ranged
weapon mended first and bought better, a rotation from range, and its pet's care out of the
repair's way. The cases are the hive's 42 hunters at 1 p.m. on 7 Oct (the character DB): 26
with the ranged weapon broken, 24 with no ammunition, all on the starting bow or gun at 9-13,
none with a pet; Wilge, a level 13 tauren, its Old Blunderbuss broken, no bullet, 2 copper;
Kosdothyt, an orc of 11, intact, 600 arrows and 969 copper - Sharp Arrows, with Rough Arrows
loaded and none of them carried, as Zhudea, a troll of 12; Krerl, an orc of 12 whose bar held
Aspect of the Monkey, Arcane Shot and Serpent Sting and never Hunter's Mark or the Hawk; and
hive-544 (Kosdothyt), whose pet's care was armed 2,286 times in 8.7 hours, 2,241 of them ended
within 0.3 s by "durability is low"."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_body import body
from test_runtime_records import seen

from jev.clients.fight import (
    DEAD_ZONE_STEPS,
    MARK_S,
    SLOT_KEYS,
    STEP_CLEAR_S,
    Fight,
)
from jev.clients.repair import MAIN_HAND, RANGED, Repaired
from jev.clients.targeting import FaceCode, FaceResult, PaintCode, PaintResult
from jev.clients.vendor import Vended
from jev.coach.policy import Context, services
from jev.coach.schema import Decision, Intent
from jev.orch.runtime import Armed
from jev.run.body import AMMO_DESIRED, AMMO_LOW, SELL_ALL
from jev.world import gear
from jev.world.combat import Role, for_class, from_bar
from jev.world.state_v1 import ArmedBy, Bags, Char, GuidePos, Pos, Sense, State, Ui, Vitals
from jev.world.training import placements, spell, worth_buying
from jev.world.vendor import (
    AMMO_STARTING,
    Merchant,
    ammo_kind,
    weapon_for_sale,
)

ORC, DWARF, TAUREN, TROLL, DRAENEI = 2, 3, 6, 8, 11
HUNTER, ROGUE, WARRIOR, MAGE = 3, 4, 1, 8
ALL = 0b111111111111


def _s(level=11, cls="hunter", race="orc", **bags) -> State:
    return State(t=1_000.0, client_id="c", char=Char(level=level, cls=cls, race=race),
                 vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                 bags=Bags(free=bags.pop("free", 10), **bags),
                 pos=Pos(zone="Durotar", zone_id=14, mx=0.5, my=0.5),
                 guide=GuidePos(step_id="grind"), ui=Ui(loot=False, modal=False),
                 sense=Sense(addon_ok=True, vision_conf=1.0))


def _hunter_body(tmp_path, *, loads=False):
    b = body()
    b.gear_memory = tmp_path / "equipped.json"
    if loads:
        loaded = []

        def load(item, values=None):
            loaded.append(item)
            return True
        b._ammo_loads = lambda: True
        b._load_ammo = load
        b.loaded = loaded
    return b


ORC_HUNTER = {"char.class_id": HUNTER, "char.race_id": ORC, "char.level": 11}


# -- the rounds the weapon fires (V401) --------------------------------------------------

def test_the_weapon_says_what_it_fires_and_creation_says_what_is_loaded():
    assert ammo_kind(ORC, weapon=2) == "arrow" and ammo_kind(DRAENEI, weapon=18) == "arrow"
    assert ammo_kind(ORC, (2512,), weapon=3) == "bullet", "the gun, whatever is carried"
    assert ammo_kind(TAUREN) == "bullet", "a tauren's Old Blunderbuss"
    assert AMMO_STARTING == {"arrow": 2512, "bullet": 2516}


def test_kosdothyts_600_sharp_arrows_fire_nothing_with_rough_arrows_loaded(tmp_path):
    """Kosdothyt and Zhudea: 600 of 2515 bought at 10 by V393, 2512 loaded, none carried."""
    b = _hunter_body(tmp_path)
    b._count_ammo({(1, 1): (2515, 200), (1, 2): (2515, 200), (1, 3): (2515, 200)}, ORC_HUNTER)
    assert b._ammo_rows == {2515: 600} and b._ammo_count == 0
    assert b.disarmed(_s(durability_min=1.0)), "nothing loaded to fire is the weapon gone"
    assert b.ammo_low(_s()) == 20, "two stacks of the Rough Arrows loaded, 10 copper each"
    assert b.fight.dry, "its fights give the shots up at once"
    b._count_ammo({(1, 1): (2512, 200)}, ORC_HUNTER)
    assert not b.fight.dry
    b._count_ammo({(1, 1): (2515, 600)}, {**ORC_HUNTER, "char.class_id": ROGUE})
    assert not b.fight.dry, "a rogue fires nothing"
    assert b._ammo_to_buy(HUNTER, ORC, 11) == (2512, 10), "the live client cannot load others"


def test_a_body_that_can_load_loads_the_best_rounds_carried(tmp_path):
    b = _hunter_body(tmp_path, loads=True)
    b._count_ammo({(1, 1): (2515, 600), (1, 2): (2512, 40)}, ORC_HUNTER)
    assert b._ammo_count == 40
    b._load_best_ammo(ORC_HUNTER)
    assert b.loaded == [2515] and b._ammo_count == 600
    assert gear.worn_item(b.gear_memory, "ammo") == 2515, "remembered loaded"
    assert b._ammo_loaded(HUNTER, ORC) == 2515 and b.ammo_low(_s()) is None
    assert b._ammo_to_buy(HUNTER, ORC, 11) == (2515, 50), "Sharp Arrows from level 10"
    assert b._ammo_to_buy(HUNTER, ORC, 9) == (2512, 10)
    b._load_best_ammo({**ORC_HUNTER, "char.level": 9})
    assert b.loaded == [2515], "nothing better at 9"


def test_rounds_of_another_kind_are_not_loaded(tmp_path):
    """A tauren's gun fires bullets: arrows it carries are not its rounds."""
    b = _hunter_body(tmp_path, loads=True)
    tauren = {"char.class_id": HUNTER, "char.race_id": TAUREN, "char.level": 13}
    b._count_ammo({(1, 1): (2515, 200), (1, 2): (2516, 30)}, tauren)
    assert b._ammo_count == 30, "Light Shot loaded"
    b._load_best_ammo(tauren)
    assert b.loaded == []


def test_the_floor_is_bought_with_the_whole_purse_and_the_fill_above_the_reserve(tmp_path,
                                                                                  monkeypatch):
    """Kosdothyt with 969 copper and R(11) = 97: two stacks to the floor whatever the reserve,
    then on to 1,000 rounds."""
    b = _hunter_body(tmp_path)
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (
        Merchant(1, "Trayexir", 1, (50, 50, 0), frozenset({2512, 2515})),))
    b._count_ammo({(1, 1): (2515, 600)}, ORC_HUNTER)
    floor, fill = b._ammo_supply(_s(), {**ORC_HUNTER, "bags.money_copper": 969})
    assert (floor.item_id, floor.desired, floor.reserve) == (2512, AMMO_LOW, 0)
    assert (fill.item_id, fill.desired, fill.reserve) == (2512, AMMO_DESIRED, 97)
    poor = b._ammo_supply(_s(), {**ORC_HUNTER, "bags.money_copper": 60})
    assert len(poor) == 1 and poor[0].desired == AMMO_LOW, "no fill on the reserve"


def test_wilge_with_2_copper_sells_its_junk_for_the_floor(tmp_path, monkeypatch):
    """Wilge, tauren 13: no bullet, 2 copper. Its junk pays for the floor's two stacks."""
    b = _hunter_body(tmp_path)
    tauren = {"char.class_id": HUNTER, "char.race_id": TAUREN, "char.level": 13}
    monkeypatch.setattr("jev.run.body.junk_prices", lambda: {4865: 5})       # Ruined Pelt
    b._count_ammo({(0, 1): (4865, 6), (0, 2): (117, 3)}, tauren)
    assert b._ammo_count == 0 and b._junk_worth == 30
    hunter = _s(level=13, race="tauren", money_copper=2)
    assert b.ammo_low(hunter) == 0, "20 copper of Light Shot, 30 of junk"
    context = Context()
    context.ammo_low = b.ammo_low
    assert services(hunter, context=context)[0].rule == "service.ammo"
    b.client.read = lambda: {**tauren, "vitals.hp": 1, "bags.money_copper": 2}
    b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE, skill="BUY_AMMO_REAGENT_FOOD",
                           abort_if=["dead"], why="ammunition", confidence=1), ArmedBy.POLICY, 0,
                  "guide", "d", "quest")
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: (
        Merchant(1, "Kennah Hawkseye", 0, (51, 50, 0), frozenset({2516})),))
    b.interact = SimpleNamespace(open_on=Mock(return_value=SimpleNamespace(opened=True)))
    calls = []

    class FakeVendor:
        detail, sold_stacks, bought_units = "", 1, 400

        def __init__(self, hid, read, visit, origin, size, eligible=None):
            calls.append({"eligible": eligible})

        def run(self, **kw):
            calls[-1].update(kw)
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    state = seen(char=Char(level=13, cls="hunter", race="tauren"),
                 bags=Bags(food_id=117, food_count=3, money_copper=2))
    assert b.execute(b.arm, state, lambda: None).outcome.value == "succeeded"
    assert calls[-1]["min_free"] == SELL_ALL and 4865 in calls[-1]["eligible"]
    (floor,) = calls[-1]["supplies"]
    assert (floor.item_id, floor.desired, floor.reserve) == (2516, AMMO_LOW, 0)


def test_no_pull_while_the_rounds_are_due(tmp_path):
    b = _hunter_body(tmp_path)
    b._count_ammo({(1, 1): (2512, 20)}, ORC_HUNTER)
    b.client.read = lambda: {**ORC_HUNTER, "vitals.hp": 1.0, "bags.durability_min": 1.0}
    b.client.recent_state = lambda age: _s(money_copper=500, durability_min=1.0)
    b.policy_context.ammo_low = b.ammo_low
    assert b._pull_refused({"bags.durability_min": 1.0}) == "ammunition is running low"
    b._count_ammo({(1, 1): (2512, 800)}, ORC_HUNTER)
    assert b._pull_refused({"bags.durability_min": 1.0}) is None


# -- the ranged weapon first (V402) --------------------------------------------------------

def _fight(values, profile):
    hid = SimpleNamespace(taps=[], holds=[])
    hid.tap = lambda key: hid.taps.append(key) or True
    hid.chord = lambda modifier, key: hid.tap(key)
    hid.hold = lambda key, seconds, **_: hid.holds.append((key, seconds)) or True
    targeting = SimpleNamespace(
        wait_for_paint=lambda: PaintResult(PaintCode.FRESH, None, values, "paint"),
        face_selected=lambda **request: FaceResult(FaceCode.FACED, "faced", 0.0, None, 0, 0.0),
        cancel_pending_spell=lambda values=None: False)
    f = Fight(hid=hid, read=lambda: values, read_frame=lambda: None, targeting=targeting)
    f.profile = profile
    return f, hid


HUNTER_BAR = {1: 6603, 2: 14260, 3: 75, 4: 13165, 5: 3044, 6: 13549, 7: 2974, 8: 1130,
              9: 5116, 11: None, 12: None}


def _hunter_profile(bar=HUNTER_BAR):
    return from_bar(bar, for_class(HUNTER, ORC))


def test_a_hunters_weapon_is_broken_when_auto_shot_greys_out():
    """26 of the 42 had the ranged weapon broken, 25 the melee one: Auto Shot tells."""
    profile = _hunter_profile()
    shot = next(a for a in profile.abilities if a.name == "Auto Shot")
    reading = {"bags.durability_min": 0.0, "vitals.power": 1.0, "vitals.power_max": 300,
               "char.class_id": HUNTER, "char.race_id": ORC}
    f, _ = _fight(reading, profile)
    assert f.disarmed({**reading, "bars.usable": ALL & ~(1 << (shot.slot - 1))}) is True
    assert f.disarmed({**reading, "bars.usable": ALL & ~0b10}) is False, \
        "Raptor Strike greyed (the melee weapon), Auto Shot whole: armed"


def test_the_weapon_the_class_fights_with_is_mended_first(tmp_path):
    b = _hunter_body(tmp_path)
    b.repair = SimpleNamespace(run=lambda: Repaired.DONE, detail="", first_slot=None)
    b._repairer_yards = lambda: None
    for cls, race, slot in (("hunter", "tauren", RANGED), ("warrior", "orc", MAIN_HAND),
                            ("rogue", "troll", MAIN_HAND), ("mage", "troll", None)):
        b._repair(_s(cls=cls, race=race, durability_min=0.0))
        assert b.repair.first_slot == slot, cls


# -- the ranged weapon: worn by score, bought better (V403) ---------------------------------

def test_a_hunters_ranged_weapon_is_gear_of_its_race_s_kind():
    hickory = gear.usable(4931, HUNTER, ORC, 9)      # Hickory Shortbow, Securing the Lines
    assert hickory is not None and hickory.slot == "ranged" and hickory.score == 60.0
    assert gear.usable(4931, HUNTER, DWARF, 9) is None, "a dwarf starts with Guns alone"
    assert gear.usable(4931, ROGUE, ORC, 9) is None and gear.usable(4931, WARRIOR, ORC, 9) is None
    assert gear.usable(5309, HUNTER, TAUREN, 9).score > 100, "the Privateer Musket"
    assert gear.usable(24441, HUNTER, DRAENEI, 5).slot == "ranged", "the Exodar Crossbow"
    assert gear.usable(2508, HUNTER, TAUREN, 1).score == pytest.approx(32.609)


def test_a_slot_never_filled_holds_the_starting_weapon():
    """Not empty: a hunter's ranged slot holds what it was created wearing, so a bow below it
    is no upgrade and is sold, not kept."""
    worn = gear.with_starting({}, HUNTER, ORC)
    assert worn == {"ranged": pytest.approx(32.609)}, "the Worn Shortbow"
    low = {"items": {"1": {"slot": "ranged", "kind": [2, 2], "level": 1, "classes": -1,
                           "races": -1, "quality": 0, "score": 20.0}},
           "proficiencies": {"2:3": [[2, 2]]}}
    assert gear.upgrades([1], gear.with_starting({}, HUNTER, ORC, facts={
        **low, "items": {**low["items"], "2504": {"score": 32.609}}}), class_id=HUNTER,
        race_id=ORC, level=5, facts=low) == []
    assert gear.upgrades([2773, 4931], worn, class_id=HUNTER, race_id=ORC,
                         level=9)[0].item_id == 4931, "the Hickory Shortbow over a grey"
    assert gear.with_starting({"ranged": 60.0}, HUNTER, ORC) == {"ranged": 60.0}
    assert gear.with_starting({}, WARRIOR, ORC) == {}


def test_a_better_bow_is_bought_from_the_merchant_the_purse_reaches():
    """An orc at 11 in Durotar: Trayexir sells the Laminated Recurve Bow (8.5 damage a second,
    17 silver 52) and the Hornwood Recurve Bow (4.5, 2s 85c); its Worn Shortbow is 3.3."""
    from jev.world.vendor import _weapon_merchants

    trayexir = next(m for m in _weapon_merchants(1, "horde") if m.name == "Trayexir")
    ask = dict(class_id=HUNTER, race_id=ORC, level=11, worn=32.609, map_id=1,
               here=trayexir.world[:2], side="horde")
    merchant, item, price = weapon_for_sale(spare=2_000, **ask)
    assert (merchant.name, item, price) == ("Trayexir", 2507, 1752)
    assert weapon_for_sale(spare=400, **ask)[1] == 2506, "the Hornwood with 4 silver"
    assert weapon_for_sale(spare=200, **ask) is None
    assert weapon_for_sale(spare=2_000, **{**ask, "level": 10})[1] == 2506
    assert weapon_for_sale(spare=2_000, **{**ask, "race_id": DWARF}) is None, "no gun there"
    orgrimmar = next(m for m in _weapon_merchants(1, "horde") if m.name == "Zendo'jian")
    assert weapon_for_sale(spare=2_000, **{**ask, "here": orgrimmar.world[:2]})[0] is orgrimmar
    far = (trayexir.world[0] - 3_000, trayexir.world[1] - 3_000)
    assert weapon_for_sale(spare=2_000, **{**ask, "here": far}) is None, "past a trainer's walk"


def test_the_weapon_service_is_the_shooters_and_keeps_what_the_purse_keeps(tmp_path):
    b = _hunter_body(tmp_path)
    offer = (Merchant(1, "Trayexir", 1, (0, 0, 0), frozenset({3026})), 3026, 1752)
    b._weapon_offer = lambda state: offer
    context = Context()
    context.weapon = b.weapon_due
    rules = [p.rule for p in services(_s(money_copper=5_000, durability_min=1.0),
                                      context=context)]
    assert rules == ["service.weapon"]
    b2 = _hunter_body(tmp_path)
    assert b2._weapon_offer(_s(cls="warrior", money_copper=50_000)) is None, \
        "a melee class's blade is not bought"


# -- the rotation from range (V404) ---------------------------------------------------------

def test_hunters_mark_and_concussive_shot_are_fight_lines_and_the_hawk_takes_the_monkeys_slot():
    """Krerl, orc 12: [Attack, Raptor Strike 2, Auto Shot, Aspect of the Monkey, Arcane Shot,
    Serpent Sting] and four slots empty; it had bought Concussive Shot, which nothing pressed."""
    assert spell(1130).role == "mark" and spell(14323).role == "mark"
    assert spell(5116).role == "slow" and spell(5116).holds_s == 4.0
    assert spell(13165).role == "aura" and spell(13163).role == "aura"
    bar = {1: 6603, 2: 14260, 3: 75, 4: 13163, 5: 3044, 6: 1978, 7: 0, 8: 0, 9: 0, 10: 0,
           11: None, 12: None}
    known = {6603, 14260, 75, 13163, 3044, 1978, 5116, 2974, 1130, 13165, 13549}
    placed = {(p.spell_id, p.slot) for p in placements(bar, known)}
    assert (13165, 4) in placed, "the Hawk over the Monkey"
    assert {1130, 5116, 2974} <= {s for s, _ in placed}
    assert worth_buying(13165, known - {13165}, bar), "none of the 42 had bought the Hawk"
    assert worth_buying(1130, known - {1130}, bar)
    paladin = {1: 6603, 2: 465, 3: 20154, 4: 0}
    assert not any(p.spell_id == 7294 for p in placements(paladin, {6603, 465, 20154, 7294})), \
        "Retribution Aura does not take Devotion Aura's slot"


def test_the_hunter_marks_once_shoots_and_slows_what_comes(clock):
    profile = _hunter_profile()
    far = {"target.has": True, "target.guid": 0x51, "target.hp": 1.0, "target.in_melee": False,
           "target.attacking_me": False, "vitals.combat": False, "vitals.hp": 1.0,
           "vitals.power": 0.9, "vitals.power_max": 300, "bars.ready": ALL, "bars.usable": ALL,
           "bars.gcd": 0.0, "bars.casting": False, "bars.out_range": 0,
           "char.class_id": HUNTER, "char.race_id": ORC}
    f, hid = _fight(far, profile)
    f._lasting["Aspect of the Hawk"] = clock[0]
    f._rotate(far)
    assert hid.taps == [SLOT_KEYS[8]], "Hunter's Mark first"
    assert f._dotted[(0x51, "Hunter's Mark")] == pytest.approx(clock[0] + MARK_S)
    f._pending_press, f._gcd_from = None, None
    clock[0] += 2.0
    f._rotate(far)
    assert hid.taps[-1] == SLOT_KEYS[3], "then Auto Shot, the mark not again"
    coming = {**far, "target.attacking_me": True, "vitals.combat": True, "target.hp": 0.8}
    f._pending_press = None
    clock[0] += 2.0
    f._rotate(coming)
    assert hid.taps[-1] == SLOT_KEYS[9], "Concussive Shot at the unit coming"
    f._pending_press = None
    clock[0] += 2.0
    f._rotate(coming)
    assert hid.taps[-1] != SLOT_KEYS[9], "not again while it lasts"


def test_a_mark_is_not_put_on_a_unit_nearly_dead(clock):
    profile = _hunter_profile()
    low = {"target.has": True, "target.guid": 0x52, "target.hp": 0.3, "target.in_melee": False,
           "target.attacking_me": True, "vitals.combat": True, "vitals.hp": 1.0,
           "vitals.power": 0.9, "vitals.power_max": 300, "bars.ready": ALL,
           "bars.usable": ALL & ~(1 << 8), "bars.gcd": 0.0, "bars.casting": False,
           "char.class_id": HUNTER, "char.race_id": ORC}
    f, hid = _fight(low, profile)
    f._lasting["Aspect of the Hawk"] = clock[0]
    f._rotate(low)
    assert SLOT_KEYS[8] not in hid.taps


def test_out_of_combat_the_aura_goes_up_for_a_class_that_is_no_caster():
    profile = _hunter_profile()
    resting = {"vitals.combat": False, "vitals.power": 1.0, "vitals.power_max": 300,
               "bars.ready": ALL, "bars.usable": ALL}
    f, hid = _fight(resting, profile)
    assert f.buff_up() == 1 and hid.taps == [SLOT_KEYS[4]], "Aspect of the Hawk"
    assert f.buff_up() == 0, "once, until a death"


def test_a_shooter_backs_out_of_the_dead_zone_from_a_unit_not_attacking_it():
    """The unit at hand attacks the pet, or runs: Auto Shot reaches it from nine yards."""
    profile = _hunter_profile()
    held = {"target.has": True, "target.hp": 0.7, "target.in_melee": True,
            "target.attacking_me": False, "vitals.combat": True, "combat.attackers": 1,
            "bars.usable": ALL, "bars.casting": False}
    f, hid = _fight(held, profile)
    assert f._dead_zone_due(profile, held)
    f._leave_dead_zone()
    assert hid.holds == [("s", STEP_CLEAR_S)] and f._dead_zone_steps == 1
    assert not f._dead_zone_due(profile, held), "not again at once"
    assert not f._dead_zone_due(profile, {**held, "target.attacking_me": True}), \
        "one attacking it follows faster than a walk backwards"
    assert not f._dead_zone_due(profile, {**held, "combat.attackers": 2})
    mage = from_bar({1: 6603, 2: 133}, for_class(MAGE, TROLL))
    assert not f._dead_zone_due(mage, held)
    f._dead_zone_at = -1e9
    f._dead_zone_steps = DEAD_ZONE_STEPS
    assert not f._dead_zone_due(profile, held)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("jev.clients.fight.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.fight.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr("jev.clients.fight.GCD_GUARD_S", 0.0)
    return now


# -- the pet out of the repair's way (V405) --------------------------------------------------

def _pet_context(need):
    context = Context()
    context.pet_due = lambda state: need
    return context


def test_hive_544s_taming_is_not_armed_while_a_repair_is_due():
    """2,286 arms in 8.7 h, 2,241 ended within 0.3 s by "durability is low": the taming's hunt
    hands back to any service, and was armed before the repair."""
    worn = _s(durability_min=0.3, money_copper=900)
    rules = [p.rule for p in services(worn, context=_pet_context("tame"))]
    assert rules == ["service.durability"], rules
    assert [p.rule for p in services(_s(durability_min=1.0, money_copper=900),
                                     context=_pet_context("tame"))] == ["service.pet"]


def test_a_taming_waits_for_every_other_service():
    context = _pet_context("tame")
    context.ammo_low = lambda state: 10
    assert [p.rule for p in services(_s(durability_min=1.0, money_copper=900),
                                     context=context)] == ["service.ammo"]


def test_a_call_or_a_revive_comes_after_the_repair_and_before_the_merchants():
    for need in ("call", "revive", "feed"):
        context = _pet_context(need)
        assert [p.rule for p in services(_s(durability_min=0.3, money_copper=900),
                                         context=context)] == ["service.durability"], need
        context.ammo_low = lambda state: 10
        assert [p.rule for p in services(_s(durability_min=1.0, money_copper=900),
                                         context=context)] == ["service.pet", "service.ammo"]
