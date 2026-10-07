"""bet-kit (V394-V398): the class kit a fight has - holds, guards and a wand - on the bar and
pressed, from the real catalogs and from the hive's deaths of 7 Oct 01:25-03:50
(/tmp/strategy4-deaths): 29.6% of 2,414 deaths were fights with two attackers or more, in
which warlocks, warriors, rogues, shamans and druids pressed no control or defensive spell at
all, and on the level 12+ bars Polymorph stood on 0 of 30 mages', Fear on 0 of 25 warlocks',
Psychic Scream on 0 of 36 priests' and Evasion on 0 of 37 rogues'."""

from __future__ import annotations

import pytest

from jev.clients.fight import (
    GUARD_S,
    HOLD_TABS,
    Fight,
    Fought,
)
from jev.clients.targeting import FaceCode, FaceResult, PaintCode, PaintResult
from jev.perceive.radio_frame import name_id
from jev.world import training
from jev.world.combat import Role, for_class, from_bar
from jev.world.training import Offer, catalog, placements, shopping, spell, starting_bar

HYENA = name_id("Hecklefang Hyena")
HENCHMAN = name_id("Defias Henchman")
ALL = 0b111111111111
FACED = FaceResult(FaceCode.FACED, "faced", 0.0, None, 0, 0.0)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("jev.clients.fight.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.fight.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr("jev.clients.fight.GCD_GUARD_S", 0.0)
    return now


class _Hid:
    def __init__(self):
        self.taps = []

    def tap(self, key):
        self.taps.append(key)
        return True

    def chord(self, modifier, key):
        return self.tap(key)

    def hold(self, key, seconds, **_):
        return True

    def click(self, *a, **k):
        return True


class _Targeting:
    def __init__(self, read):
        self.read, self.faces = read, []

    def wait_for_paint(self):
        after = self.read()
        return PaintResult(PaintCode.FRESH if after else PaintCode.BLIND, None, after, "paint")

    def face_selected(self, **request):
        self.faces.append(request)
        return FACED

    def cancel_pending_spell(self, values=None):
        return False


def _fight(frames, profile):
    seq, at = list(frames), [0]

    def read():
        v = seq[min(at[0], len(seq) - 1)]
        at[0] += 1
        return v

    hid = _Hid()
    f = Fight(hid=hid, read=read, read_frame=lambda: None, targeting=_Targeting(read))
    f.profile = profile
    return f, hid


def _profile(class_id, race_id, bar):
    return from_bar(bar, for_class(class_id, race_id))


def _key(profile, name):
    from jev.clients.fight import SLOT_KEYS

    return SLOT_KEYS[next(a.slot for a in profile.abilities if a.name == name)]


# -- the catalog: what holds, what guards, what shoots (V395-V397) -------------------------


@pytest.mark.parametrize(("spell_id", "role"), [
    (5782, "cc"), (6213, "cc"), (118, "cc"), (1776, "cc"), (2637, "cc"), (339, "cc"),
    (1513, "cc"), (9484, "cc"), (8122, "guard"), (5484, "guard"), (5277, "guard"),
    (2565, "guard"), (20230, "guard"), (5019, "wand"),
    # Unchanged: a stun not broken by damage, an immune save, a slow, a seal, Recklessness
    # (more damage taken, not less).
    (853, "stun"), (498, "save"), (116, "strike"), (21084, "short_buff"),
    (1719, "short_buff")])
def test_holds_guards_and_the_wand_are_read_from_what_the_spells_do(spell_id, role):
    assert spell(spell_id).role == role


def test_a_hold_says_how_long_what_it_takes_and_whether_it_pins():
    fear, hibernate, roots, poly = spell(6213), spell(2637), spell(339), spell(118)
    assert (fear.holds_s, fear.creatures, fear.pins) == (15.0, 0, False)
    assert (hibernate.holds_s, hibernate.creatures) == (20.0, 3), "beasts and dragonkin"
    assert roots.pins and roots.holds_s == 12.0
    assert poly.creatures == 193 and poly.holds_s == 20.0
    assert spell(8122).around and not spell(5277).around
    assert spell(1715).form is False and spell(2565).form and spell(6770).form, \
        "Hamstring in Battle Stance; Shield Block in Defensive, Sap in stealth"


# -- the bar: the fight's lines first (V394) ------------------------------------------------


def _bars(class_id, race_id, levels):
    """A character trained level by level with the purse for everything, as `placements`
    puts it on the bar; a wand worn from 15 for a class that shoots one."""
    bar = starting_bar(class_id, race_id)
    known = {s for s in bar.values() if s} | ({5019} if class_id in (5, 8, 9) else set())
    offers = {}
    for t in catalog()["trainers"]:
        if t["class"] == class_id:
            for o in catalog()["offers"][t["offers"]]:
                offers[o["spell"]] = Offer(o["spell"], o["level"], o["cost"])
    seen = {}
    for level in range(2, max(levels) + 1):
        taught = [o for o in offers.values() if o.level <= level and o.spell_id not in known]
        for o in shopping(taught, known, bar):
            known.add(o.spell_id)
            known |= set(spell(o.spell_id).teaches)
        for p in placements(bar, known, wand=class_id in (5, 8, 9) and level >= 15):
            bar[p.slot] = p.spell_id
        if level in levels:
            seen[level] = {spell(s).name for s in bar.values() if s and spell(s)}
    return seen


@pytest.mark.parametrize(("class_id", "race_id", "level", "has", "lacks"), [
    # 28 of the hive's 30 mages at 12+ had full bars: Conjure Food gives way, the water stays.
    (8, 1, 12, {"Polymorph", "Frost Nova", "Conjure Water", "Frost Armor"}, {"Conjure Food"}),
    (8, 1, 16, {"Polymorph", "Frost Nova", "Arcane Explosion", "Conjure Water", "Shoot"},
     {"Attack"}),
    (9, 2, 17, {"Fear", "Shoot", "Demon Skin", "Corruption", "Immolate"}, set()),
    (5, 1, 16, {"Psychic Scream", "Shoot", "Power Word: Shield", "Inner Fire"}, set()),
    (4, 1, 12, {"Gouge", "Evasion", "Sinister Strike"}, set()),
    (11, 4, 12, {"Entangling Roots", "Mark of the Wild"}, set()),
    (3, 3, 16, {"Scare Beast"}, set()),
    # Battle Shout is pressed in a fight: a full warrior's bar keeps it at 20.
    (1, 1, 20, {"Battle Shout", "Retaliation"}, set()),
])
def test_each_class_s_bar_holds_its_kit(class_id, race_id, level, has, lacks):
    names = _bars(class_id, race_id, {level})[level]
    assert has <= names, has - names
    assert not lacks & names, lacks & names


def test_a_long_buff_kept_up_between_fights_gives_way_to_a_fight_line_and_a_shout_does_not():
    shout = spell(6673)                                  # Battle Shout: two minutes
    intellect = spell(1459)                              # Arcane Intellect: thirty
    assert shout.role == intellect.role == "long_buff"
    assert not training._yields(shout) and training._yields(intellect)
    assert not training._yields(spell(5504)), "Conjure Water: a caster's mana"
    assert training._yields(spell(587)), "Conjure Food"


def test_a_wand_s_shot_goes_over_the_melee_toggle_once_a_wand_is_worn():
    bar = {**starting_bar(5, 1), 4: 592}
    known = {6603, 585, 2050, 592, 5019}
    assert placements(bar, known) == [], "no wand worn: nowhere"
    assert [(p.spell_id, p.slot, p.replaces) for p in placements(bar, known, wand=True)] == [
        (5019, 1, 6603)]


# -- holds (V395) ----------------------------------------------------------------------------

# hive-557, an orc warlock of 17 (run 20261007T030136-7157b0, 03:10:54): a Hecklefang Hyena,
# and a second 6.6 s in; its health went 1.0 to 0 in 25 s, the first hyena's to 0.04, and Fear
# was never on the bar ("kit: not pressed - Fear").
WARLOCK_BAR = {1: 6603, 2: 705, 3: 696, 4: 707, 5: 6222, 6: 1014, 7: 6213, 11: None, 12: None}
FOUGHT = {"target.has": True, "target.guid": 0x4A1, "target.name_id": HYENA, "target.level": 15,
          "target.hp": 0.99, "target.attacking_me": True, "target.in_melee": True,
          "combat.attackers": 2, "vitals.combat": True, "vitals.hp": 0.84, "vitals.power": 0.9,
          "vitals.power_max": 700, "vitals.dead": False, "bars.ready": ALL, "bars.usable": ALL,
          "bars.gcd": 0.0, "bars.casting": False, "bars.attacking": False,
          "char.class_id": 9, "char.race_id": 2}
SECOND = {**FOUGHT, "target.guid": 0x4A2, "target.hp": 1.0}


def test_hive_557_fears_the_second_hyena_and_fights_the_first():
    """V395: the warlock's Corruption is on the first hyena, which would break its Fear; the
    second is found by Tab, faced, Feared, and the fight ends: the next takes the first."""
    warlock = _profile(9, 2, WARLOCK_BAR)
    fear = next(a for a in warlock.abilities if a.name == "Fear")
    assert fear.role is Role.CC and fear.holds_s == 15.0
    casting = {**SECOND, "bars.casting": True}
    landed = {**SECOND, "vitals.power": 0.84}             # Fear's share of base mana gone
    f, hid = _fight([SECOND, casting, casting, landed], warlock)
    f._dotted[(0x4A1, "Corruption")] = 1e9
    assert f._hold_wanted(warlock, FOUGHT)
    assert f._hold(warlock, FOUGHT) is Fought.HELD
    assert hid.taps == ["tab", _key(warlock, "Fear")]
    assert set(f._holding) == {0x4A2} and f._after_hold
    assert f._acceptable(None, defend=True, values=SECOND) is False, "held, though it 'attacks'"
    assert f._acceptable(None, defend=True, values=FOUGHT) is True


def test_the_next_fight_does_not_take_the_held_unit_back_and_wakes_it_only_when_alone(clock):
    """The hive's mage Polymorphed a unit and began its next fight on it at once, Fire Blast
    breaking it (7 Oct): a held selection is not the fight. With nothing else to take, the
    held one is (V395)."""
    warlock = _profile(9, 2, WARLOCK_BAR)
    f, _ = _fight([SECOND], warlock)
    f._holding[0x4A2] = clock[0] + 15.0
    asked = []

    def acquire(name, *, defend=False):
        asked.append((defend, f._waking))
        return Fought.NO_TARGET

    f.acquire = acquire
    assert f._fight(None, 5.0) is Fought.NO_TARGET
    assert asked == [(True, False), (True, True)], "not engaged on the held one; woken alone"


def test_with_no_other_attacker_in_front_a_touched_unit_is_not_held_and_the_fight_goes_on():
    warlock = _profile(9, 2, WARLOCK_BAR)
    f, hid = _fight([FOUGHT], warlock)
    f._dotted[(0x4A1, "Corruption")] = 1e9
    assert f._hold(warlock, FOUGHT) is None
    assert hid.taps == ["tab"] * HOLD_TABS and f._holding == {}
    assert not f._hold_wanted(warlock, FOUGHT), "one try a fight"


def test_an_untouched_selected_unit_is_held_when_tab_finds_no_other():
    """V287's hold, kept for a unit nothing of ours has touched: an add 0.4 s in (the mage
    hive-445, Silithid Swarmers)."""
    warlock = _profile(9, 2, WARLOCK_BAR)
    fresh = {**FOUGHT, "target.hp": 1.0}
    f, hid = _fight([fresh, fresh, fresh, fresh, fresh, {**fresh, "bars.casting": True},
                     {**fresh, "vitals.power": 0.84}], warlock)
    assert f._hold(warlock, fresh) is Fought.HELD
    assert set(f._holding) == {0x4A1}


def test_a_search_that_leaves_a_bystander_selected_ends_the_fight_unpulled():
    warlock = _profile(9, 2, WARLOCK_BAR)
    bystander = {**FOUGHT, "target.guid": 0x777, "target.attacking_me": False}
    f, hid = _fight([bystander], warlock)
    assert f._hold(warlock, FOUGHT) is Fought.LOST
    assert _key(warlock, "Fear") not in hid.taps and _key(warlock, "Shadow Bolt") not in hid.taps


def test_hibernate_takes_beasts_and_a_root_only_what_is_not_at_hand():
    druid = _profile(11, 4, {1: 6603, 2: 5178, 3: 5187, 4: 2637, 5: 1062, 11: None, 12: None})
    hibernate = next(a for a in druid.abilities if a.name == "Hibernate")
    roots = next(a for a in druid.abilities if a.name == "Entangling Roots")
    coming = {**SECOND, "target.in_melee": False}
    assert Fight._holds(hibernate, SECOND), "a hyena is a beast"
    assert not Fight._holds(hibernate, {**SECOND, "target.name_id": HENCHMAN})
    assert not Fight._holds(roots, SECOND), "at hand it bites on, rooted"
    assert Fight._holds(roots, coming)


def test_a_rogue_stops_swinging_and_gouges_the_second_attacker(clock):
    """hive-210, a rogue of 19 (run 20261007T012758-8a5005): a second Gangled Cannibal 14.5 s
    in, Attack its only press. Melee auto-attack goes off first - a swing breaks a Gouge -
    and the Gouge lands by its cooldown."""
    rogue = _profile(4, 1, {1: 6603, 2: 1759, 3: 6761, 4: 2764, 5: 1777, 6: 5277, 12: None})
    gouge = next(a for a in rogue.abilities if a.name == "Gouge")
    swinging = {**FOUGHT, "bars.attacking": True, "char.class_id": 4, "char.race_id": 1,
                "vitals.power": 1.0, "vitals.power_max": 100}
    other = {**swinging, "target.guid": 0x4A2, "target.hp": 1.0}
    answered = {**other, "bars.ready": ALL & ~(1 << (gouge.slot - 1))}
    f, hid = _fight([other, answered], rogue)
    assert f._hold(rogue, swinging) is Fought.HELD
    assert hid.taps == ["1", "tab", "5"], "the swing off, then Tab, then Gouge"
    assert f._holding[0x4A2] == pytest.approx(clock[0] + gouge.holds_s) and gouge.holds_s == 4.0


# -- guards (V396) ---------------------------------------------------------------------------

PRIEST_BAR = {1: 6603, 2: 598, 3: 2053, 4: 592, 5: 8122, 6: 8102, 7: 594, 11: None, 12: None}
PAIR = {**FOUGHT, "char.class_id": 5, "char.race_id": 1, "vitals.hp": 0.7,
        "vitals.power": 0.6, "vitals.power_max": 600}


def test_hive_375_screams_with_two_kolkar_at_hand_and_heals_under_it():
    """hive-375, a priest of 14 (run 20261007T011321-b63e06): a second Kolkar Stormer 4 s in;
    Power Word: Shield at 18 s, a heal at 20 s, dead at 23. A scream clears the way for the
    heal as a save does."""
    priest = _profile(5, 1, PRIEST_BAR)
    f, hid = _fight([PAIR], priest)
    f._rotate(PAIR)
    assert hid.taps == [_key(priest, "Psychic Scream")]
    f._pending_press = None
    f._rotate({**PAIR, "combat.attackers": 1})
    assert hid.taps[-1] == _key(priest, "Lesser Heal"), "the heal next, under the scream"


def test_one_guard_at_a_time_and_no_scream_round_a_held_unit(clock):
    priest = _profile(5, 1, PRIEST_BAR)
    f, hid = _fight([PAIR], priest)
    f._guarded_until = clock[0] + GUARD_S
    f._rotate({**PAIR, "vitals.hp": 0.9})
    assert _key(priest, "Psychic Scream") not in hid.taps
    g, ghid = _fight([PAIR], priest)
    g._holding[0x999] = clock[0] + 10.0
    g._race = [(clock[0] - 4.0, 0.9, 0.9, False, 0x4A1), (clock[0], 0.5, 0.88, False, 0x4A1)]
    g._rotate({**PAIR, "vitals.hp": 0.9})
    assert _key(priest, "Psychic Scream") not in ghid.taps, "a scream would take the held one"


def test_evasion_comes_when_health_falls_one_and_a_half_times_as_fast_as_the_unit_s(clock):
    rogue = _profile(4, 1, {1: 6603, 2: 1759, 3: 6761, 4: 2764, 5: 1777, 6: 5277, 12: None})
    alone = {**FOUGHT, "combat.attackers": 1, "char.class_id": 4, "vitals.power": 1.0,
             "vitals.power_max": 100, "vitals.hp": 0.62, "target.hp": 0.82,
             "bars.attacking": True}
    for ours, theirs, pressed in ((0.62, 0.82, True), (0.62, 0.62, False)):
        f, hid = _fight([alone], rogue)
        f._race = [(clock[0] - 4.0, 0.9, 0.9, False, 0x4A1)]
        f._rotate({**alone, "vitals.hp": ours, "target.hp": theirs})
        assert (_key(rogue, "Evasion") in hid.taps) is pressed, (ours, theirs)


# -- the wand (V397) -------------------------------------------------------------------------

WAND_BAR = {**PRIEST_BAR, 1: 5019}


def test_a_priest_shoots_its_wand_at_a_unit_nearly_dead_and_lets_it_repeat():
    priest = _profile(5, 1, WAND_BAR)
    assert priest.caster and any(a.name == "Shoot" for a in priest.abilities)
    low = {**PAIR, "combat.attackers": 1, "vitals.hp": 0.9, "target.hp": 0.2}
    f, hid = _fight([low], priest)
    f._dotted[(0x4A1, "Shadow Word: Pain")] = 1e9
    f._rotate(low)
    assert hid.taps == ["1"], "Shoot, not Smite"
    f._rotate(low)
    assert hid.taps == ["1"], "pressed again it would stop"
    f._rotate({**low, "vitals.hp": 0.3})
    assert hid.taps[-1] == _key(priest, "Power Word: Shield") and f._shooting_at is None, \
        "the shield before the heal, and either stops the wand"


def test_a_healthy_unit_gets_spells_and_a_priest_out_of_mana_shoots():
    priest = _profile(5, 1, WAND_BAR)
    healthy = {**PAIR, "combat.attackers": 1, "vitals.hp": 0.9, "target.hp": 0.9}
    f, hid = _fight([healthy], priest)
    f._rotate(healthy)
    assert hid.taps and hid.taps[0] != "1"
    dry = {**healthy, "vitals.power": 0.02, "bars.usable": 0b1}        # only Shoot usable
    g, ghid = _fight([dry], priest)
    g._rotate(dry)
    assert ghid.taps == ["1"], "the wand where it stood, staff in hand"


def test_a_wand_at_hand_goes_on_shooting():
    """Auto Shot stops in the dead zone (V358); a wand has none."""
    priest = _profile(5, 1, WAND_BAR)
    f, _ = _fight([PAIR], priest)
    f._shooting_at = 99.0
    f._watch_shots(priest, {**PAIR, "target.in_melee": True})
    assert f._shooting_at == 99.0


# -- wands worn, chosen and bought (V397, V398) ----------------------------------------------


def test_a_wand_is_gear_a_caster_wears_from_its_level():
    from jev.world.gear import upgrades, usable

    assert usable(5208, 5, 1, 15).slot == "ranged"
    assert usable(5208, 5, 1, 14) is None and usable(5208, 1, 1, 20) is None, "a warrior"
    assert usable(12296, 5, 1, 10) is not None, "Spark of the People's Militia, quest 14"
    picked = upgrades([5208, 12296], {}, class_id=5, race_id=1, level=15)
    assert [(p.item_id, p.slot) for p in picked] == [(5208, "ranged")]


def test_a_wand_is_for_sale_in_a_capital_from_15_within_a_trainer_s_walk():
    from jev.world.vendor import wand_for_sale

    goldshire, sentinel_hill, brill = (-9460.0, 80.0), (-10510.0, 1050.0), (2250.0, 260.0)
    merchant, item, price = wand_for_sale(5, 1, 15, 5000, 0.0, 0, goldshire, "alliance")
    assert (merchant.name, item, price) == ("Ardwyn Cailen", 5208, 3340)
    assert wand_for_sale(5, 1, 15, 5000, 0.0, 0, sentinel_hill, "alliance") is None, "1,660 yd"
    assert wand_for_sale(5, 1, 14, 5000, 0.0, 0, goldshire, "alliance") is None
    assert wand_for_sale(5, 1, 15, 3000, 0.0, 0, goldshire, "alliance") is None, "the purse"
    assert wand_for_sale(5, 1, 15, 5000, 200.0, 0, goldshire, "alliance") is None, "worn better"
    assert wand_for_sale(1, 1, 20, 9000, 0.0, 0, goldshire, "alliance") is None, "a warrior"
    assert wand_for_sale(5, 5, 16, 5000, 0.0, 0, goldshire, "horde") is None, "Stormwind's"
    assert wand_for_sale(5, 5, 16, 5000, 0.0, 0, brill, "horde")[1] == 5209, "Gloom Wand"


def test_the_policy_walks_to_a_wand_only_when_nothing_else_is_due(state):
    from jev.coach.policy import Context, services

    context = Context()
    context.wand = lambda s: True
    plans = services(state, context=context)
    assert [p.decision.skill for p in plans][-1:] == ["BUY_WAND"]
    assert plans[-1].decision.params == {"service": "wand"}
    fighting = state.model_copy(update={"vitals": state.vitals.model_copy(
        update={"combat": True})})
    assert services(fighting, context=context) == []
    context.wand = lambda s: 1 / 0
    assert not context.can_buy_wand(state), "a question that fails is a no"


def test_the_body_asks_for_a_wand_with_what_the_purse_keeps_kept(state, tmp_path):
    from types import SimpleNamespace

    from jev.coach.policy import Context
    from jev.guide.coords import ZoneBounds
    from jev.run.body import BAG_SPARE_COPPER, LiveBody
    from jev.world.state_v1 import Char

    elwynn = ZoneBounds(area_id=12, map_id=0, left=1535.42, right=-1935.42, top=-7939.58,
                        bottom=-10254.17)
    kept = Context()
    kept.reserve = lambda s: 1000
    body = SimpleNamespace(client=SimpleNamespace(bounds=elwynn), gear_memory=tmp_path / "worn",
                           policy_context=kept, _side="alliance")
    at_goldshire = state.model_copy(update={
        "char": Char(cls="priest", race="human", level=15, faction="alliance"),
        "pos": state.pos.model_copy(update={"mx": 0.41933, "my": 0.65689}),
        "bags": state.bags.model_copy(update={"money_copper": 3340 + 1000 + BAG_SPARE_COPPER})})
    offer = LiveBody._wand_offer(body, at_goldshire)
    assert offer is not None and offer[1:] == (5208, 3340)
    poorer = at_goldshire.model_copy(update={"bags": at_goldshire.bags.model_copy(
        update={"money_copper": 3340 + 999 + BAG_SPARE_COPPER})})
    assert LiveBody._wand_offer(body, poorer) is None, "the trainer's due kept"


def test_a_priest_keeping_its_heal_s_mana_shoots_rather_than_stands():
    """A priest presses no spell that would leave less than a heal and its save (the
    fight's mana reserve): with a wand it shoots then, where it stood."""
    priest = _profile(5, 1, WAND_BAR)
    keeping = {**PAIR, "combat.attackers": 1, "vitals.hp": 0.9, "target.hp": 0.9,
               "vitals.power": 0.2, "vitals.power_max": 600}      # 120 mana: Smite would cut in
    f, hid = _fight([keeping], priest)
    f._rotate(keeping)
    assert hid.taps == ["1"]
