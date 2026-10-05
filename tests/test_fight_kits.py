"""Class kits in the fight: a shooter's Auto Shot (V358)."""

from __future__ import annotations

from test_fight import ALIVE, _fight, _Hid, _rotate_answered, combat_clock  # noqa: F401

from jev.clients.fight import SHOT_GIVE_UP, SHOT_SILENT_S, Fought
from jev.world.combat import Ability, CombatProfile, Role, for_class, reach, repeats

HUNTER = for_class(3, 3)            # a dwarf hunter: Attack, Raptor Strike, Auto Shot
SHOOTING = {**ALIVE, "char.class_id": 3, "char.race_id": 3, "target.in_melee": False,
            "target.melee_range": False, "vitals.combat": True, "vitals.power": 1.0,
            "vitals.power_max": 120, "char.level": 6, "char.xp_pct": 0.1,
            # Raptor Strike does not reach a unit out of melee; Auto Shot does.
            "bars.out_range": 0b010, "bars.in_range": 0b100, "bars.attacking": False}
AT_HAND = {**SHOOTING, "target.in_melee": True, "target.melee_range": True,
           "target.attacking_me": True, "bars.out_range": 0b100, "bars.in_range": 0b010}


def _hunter(frames, hid=None, profile=HUNTER):
    f = _fight(frames, hid=hid)
    f.profile = profile
    return f


def _look(f, values):
    """One pass of the fight's loop as far as the rotation: the shot watched, then pressed."""
    f._watch_shots(f.profile, values)
    _rotate_answered(f, values)


def test_a_hunter_is_a_shooter_and_no_other_class_is():
    """Auto Shot repeats (the spell's own AttributesEx2): a hunter shoots from range. A
    rogue's Throw does not repeat, and a mage casts."""
    shot = next(a for a in HUNTER.abilities if a.name == "Auto Shot")
    assert repeats(shot) and HUNTER.shooter and not HUNTER.caster
    for class_id, race_id in ((4, 1), (8, 1), (2, 1), (1, 1), (5, 1), (9, 1)):
        assert not for_class(class_id, race_id).shooter, (class_id, race_id)


def test_the_shots_cast_time_is_read_as_instant():
    """Arcane Shot, Serpent Sting and Concussive Shot are stored as -1,000,000 ms, which read
    unsigned made each a 4,293,967 s cast."""
    for spell_id in (3044, 1978, 5116):
        assert reach(spell_id).instant, spell_id


def test_a_shooter_opens_from_range_with_one_shot_and_never_swings():
    """Pressed again while it repeats, the client's Auto Shot stops: one press, and the
    blade's toggle is for the unit at hand."""
    hid = _Hid()
    f = _hunter([SHOOTING], hid=hid)
    for _ in range(4):
        _look(f, SHOOTING)
    assert hid.taps == ["3"], hid.taps
    assert f._ranged_ready(HUNTER, SHOOTING)


def test_at_hand_the_shooter_swings_and_shoots_again_once_clear():
    """The server stops Auto Shot in the dead zone, and the client marks it out of range:
    there the hunter fights with its blade (Attack, Raptor Strike); out of it, it shoots
    again, once."""
    hid = _Hid()
    f = _hunter([SHOOTING], hid=hid)
    _look(f, SHOOTING)
    assert not f._ranged_ready(HUNTER, AT_HAND)
    for _ in range(3):
        _look(f, AT_HAND)
    assert "1" in hid.taps and "2" in hid.taps and hid.taps.count("3") == 1, hid.taps
    for _ in range(3):
        _look(f, SHOOTING)
    assert hid.taps.count("3") == 2, hid.taps


def test_an_arcane_shot_follows_the_auto_shot_from_range():
    """A strike that reaches from range is pressed beside the repeating shot, which takes no
    global cooldown."""
    profile = CombatProfile(name="hunter", abilities=(
        *HUNTER.abilities,
        Ability(slot=4, role=Role.ATTACK, name="Arcane Shot", mana=25, spell_id=3044)))
    values = {**SHOOTING, "bars.ready": 0b1111, "bars.usable": 0b1111,
              "bars.in_range": 0b1100}
    hid = _Hid()
    f = _hunter([values], hid=hid, profile=profile)
    for _ in range(3):
        _look(f, values)
    assert hid.taps[:2] == ["3", "4"], hid.taps


def test_a_silent_shot_is_pressed_again_then_given_up_for_the_blade(combat_clock):
    """No damage for `SHOT_SILENT_S` after the press: the shot is taken as stopped (no ammo,
    the press lost) and pressed again; after `SHOT_GIVE_UP` silences the fight is melee's."""
    hid = _Hid()
    f = _hunter([SHOOTING], hid=hid)
    f._damage_at = 0.0
    _look(f, SHOOTING)
    for _ in range(SHOT_GIVE_UP):
        combat_clock[0] += SHOT_SILENT_S + 0.1
        _look(f, SHOOTING)
    assert hid.taps.count("3") == SHOT_GIVE_UP, hid.taps
    assert f._no_shots and not f._ranged_ready(HUNTER, SHOOTING)


def test_a_hunter_fight_is_shot_from_where_it_stands(combat_clock):
    """The hive's hunters walked into melee and pressed Auto Shot 0.06 times a kill."""
    hurt = {**SHOOTING, "target.hp": 0.4}
    dead = {**SHOOTING, "target.hp": 0.0, "char.xp_pct": 0.2}
    hid = _Hid()
    f = _hunter([SHOOTING, SHOOTING, hurt, hurt, dead], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    assert [key for key, _ in hid.holds if key == "w"] == [], "not a step toward it"
    assert hid.taps.count("3") == 1 and "1" not in hid.taps, hid.taps
    assert f.ended_far, "the corpse lies out there: the loot walks to it"


def test_the_reach_generator_reads_a_negative_cast_time_as_instant():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "tools/gen_spell_reach.py"
    spec = importlib.util.spec_from_file_location("gen_spell_reach", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._signed(4293967296) == -1_000_000 and module._signed(1500) == 1500


# -- attacks by value, damage over time once a unit (V360) ---------------------------------

from jev.clients.fight import Fight  # noqa: E402
from jev.world.combat import by_value, instant_blow, lingers, per_s  # noqa: E402

SMITE = Ability(slot=2, role=Role.ATTACK, name="Smite", mana=20, spell_id=585)
MIND_BLAST = Ability(slot=5, role=Role.ATTACK, name="Mind Blast", mana=50, spell_id=8092)
SHADOW_BOLT = Ability(slot=2, role=Role.ATTACK, name="Shadow Bolt", mana=25, spell_id=686)
IMMOLATE = Ability(slot=4, role=Role.ATTACK, name="Immolate", mana=25, spell_id=348)
ATTACK = Ability(slot=1, role=Role.ATTACK, name="Attack", toggle=True, spell_id=6603)
CASTING = {**ALIVE, "char.class_id": 5, "char.race_id": 1, "target.in_melee": False,
           "target.melee_range": False, "target.attacking_me": True, "vitals.combat": True,
           "vitals.power": 1.0, "vitals.power_max": 300, "target.guid": 7,
           "bars.ready": 0b11111, "bars.usable": 0b11111}


def _caster(abilities, hid):
    f = _fight([CASTING], hid=hid)
    f.profile = CombatProfile(name="caster", abilities=abilities, caster=True)
    return f


def test_damage_a_second_is_read_from_the_spells_own_data():
    """Smite 13-17 in 1.5 s, Mind Blast 39-43 in 1.5 s; Immolate 8 at once and 20 over 15 s
    in 2 s, most of it over time; a weapon blow's and a finisher's are not read."""
    assert per_s(SMITE) == 10.0 and round(per_s(MIND_BLAST), 1) == 27.3
    assert per_s(IMMOLATE) == 14.0 and lingers(IMMOLATE) and not lingers(SHADOW_BOLT)
    raptor = Ability(slot=2, role=Role.ATTACK, name="Raptor Strike", spell_id=2973)
    eviscerate = Ability(slot=3, role=Role.ATTACK, name="Eviscerate", spell_id=2098)
    assert per_s(raptor) is None and per_s(eviscerate) is None and per_s(ATTACK) is None


def test_attacks_take_one_anothers_places_by_damage_a_second():
    """A weapon blow keeps its place; those the data says take each other's."""
    sinister = Ability(slot=3, role=Role.ATTACK, name="Sinister Strike", spell_id=1752)
    order = by_value((ATTACK, SMITE, sinister, MIND_BLAST))
    assert [a.name for a in order] == ["Attack", "Mind Blast", "Sinister Strike", "Smite"]


def test_mind_blast_is_not_starved_by_smite():
    """The priests pressed Mind Blast in 1% of their kills: Smite, slot 2, was always ready."""
    hid = _Hid()
    f = _caster((ATTACK, SMITE, MIND_BLAST), hid)
    _rotate_answered(f, CASTING)
    assert hid.taps == ["5"], hid.taps
    f._gcd_from = None
    _rotate_answered(f, {**CASTING, "bars.ready": 0b01111})   # Mind Blast cooling
    assert hid.taps == ["5", "2"], hid.taps


def test_damage_over_time_goes_on_a_unit_once_while_it_lasts(combat_clock):
    """Immolate once, then Shadow Bolt; again once it has run out, and at once on another unit.
    The warlocks pressed Immolate in 3-4% of their kills, Shadow Bolt in slot 2 first."""
    hid = _Hid()
    f = _caster((ATTACK, SHADOW_BOLT, IMMOLATE), hid)
    f._dotted = {}

    def look(values):
        f._gcd_from = None
        _rotate_answered(f, values)

    look(CASTING)
    look(CASTING)
    assert hid.taps == ["4", "2"], hid.taps
    combat_clock[0] += 15.5
    look(CASTING)
    look({**CASTING, "target.guid": 8})
    assert hid.taps == ["4", "2", "4", "4"], hid.taps


def test_damage_over_time_the_client_never_answered_is_not_taken_as_on(combat_clock):
    hid = _Hid()
    f = _caster((ATTACK, SHADOW_BOLT, IMMOLATE), hid)
    f._rotate(CASTING)                                   # Immolate pressed, no answer
    combat_clock[0] += 0.9                               # past PRESS_ANSWER_S, no answer
    assert f._press_answered(CASTING)                    # unanswered: undone
    assert not f._dotted_now(IMMOLATE, combat_clock[0])


def test_a_next_swing_blow_keeps_its_rage_from_hamstring(combat_clock):
    """Hamstring, slot 3, ate the rage Heroic Strike, slot 2, had been pressed to spend at its
    swing: 3.09 Hamstrings a warrior's kill. With the blow pressed, another attack leaves its
    cost."""
    heroic = Ability(slot=2, role=Role.ATTACK, name="Heroic Strike", mana=150, spell_id=78)
    hamstring = Ability(slot=3, role=Role.ATTACK, name="Hamstring", mana=100, spell_id=1715)
    warrior = CombatProfile(name="warrior", abilities=(ATTACK, heroic, hamstring))
    rage = {**ALIVE, "char.class_id": 1, "target.in_melee": True, "vitals.combat": True,
            "vitals.power": 0.2, "vitals.power_max": 1000, "bars.attacking": True,
            "bars.ready": 0b111, "bars.usable": 0b111}
    hid = _Hid()
    f = _fight([rage], hid=hid)
    f.profile = warrior
    f._rotate(rage)
    assert hid.taps == ["2"]
    combat_clock[0] += 0.3
    f._rotate(rage)
    assert hid.taps == ["2"], "200 rage less 100 would leave under Heroic Strike's 150"
    f._rotate({**rage, "vitals.power": 0.3})
    assert hid.taps == ["2", "3"], hid.taps


def test_the_mages_order_is_its_own_at_its_bar():
    """V165 stays: the mage's bars (Fireball, Frostbolt, Fire Blast, Arcane Missiles, at 16
    ranks 3, 3, 2 and 1; at 8 ranks 2, 2, 1 and 1) are ordered exactly as before, at contact,
    opening and while the unit comes on: its instant blow keeps its place for contact, the
    channel its own, and Fireball outdoes Frostbolt rank for rank."""
    for ranks in ((145, 837, 2137, 5143), (143, 205, 2136, 5143), (133, 116, 2136, 5143)):
        fireball, frostbolt, fire_blast, missiles = (
            Ability(slot=slot, role=Role.ATTACK, name=name, spell_id=spell_id)
            for slot, name, spell_id in zip((2, 6, 7, 8), ("Fireball", "Frostbolt", "Fire Blast",
                                                           "Arcane Missiles"), ranks, strict=True))
        attacks = (fireball, frostbolt, fire_blast, missiles)

        def order(values, attacks=attacks):
            return [a.name for a in Fight._caster_order(by_value(attacks, keep=instant_blow),
                                                        values)]

        near = {"target.in_melee": True, "target.attacking_me": True}
        opening = {"target.in_melee": False, "target.attacking_me": False}
        coming = {"target.in_melee": False, "target.attacking_me": True}
        assert order(near) == ["Fire Blast", "Fireball", "Frostbolt", "Arcane Missiles"], ranks
        assert order(opening) == ["Frostbolt", "Fireball", "Fire Blast", "Arcane Missiles"]
        assert order(coming) == ["Fireball", "Frostbolt", "Fire Blast", "Arcane Missiles"]


# -- the kit's new roles in the fight (V361) ----------------------------------------------

from jev.world.combat import from_bar  # noqa: E402


def test_a_dot_on_the_bar_is_an_attack_put_on_once():
    """Corruption, utility before, is an attack on the bar, and one that lingers."""
    warlock = from_bar({1: 6603, 2: 686, 3: 687, 4: 172}, for_class(9, 1))
    corruption = next(a for a in warlock.abilities if a.name == "Corruption")
    assert corruption.role is Role.ATTACK and lingers(corruption) and not corruption.toggle


def test_a_shield_that_lasts_is_not_pressed_again_while_it_does(combat_clock):
    """Power Word: Shield before the heal, then not again for its 25 s, across fights: its
    Weakened Soul would refuse it for 15."""
    priest = from_bar({1: 6603, 2: 585, 3: 2050, 4: 17}, for_class(5, 1))
    shield = next(a for a in priest.abilities if a.name == "Power Word: Shield")
    assert shield.role is Role.SAVE and shield.friendly and shield.every_s == 25.0
    low = {**CASTING, "vitals.hp": 0.2, "target.in_melee": True, "bars.ready": 0b1111,
           "bars.usable": 0b1111}
    hid = _Hid()
    f = _fight([low], hid=hid)
    f.profile = priest
    _rotate_answered(f, low)
    assert hid.taps == ["4"], "the shield before the heal"
    f._saved_at = None
    f._pending_heal = None
    combat_clock[0] += 10.0
    f._gcd_from = None
    _rotate_answered(f, low)
    assert hid.taps == ["4", "3"], "within its 25 s, the heal without another shield"


def test_the_kit_line_is_said_once_for_a_spellbook():
    from jev.run.body import LiveBody

    said = []

    class Stub:
        _kit_said = None

        def say(self, text):
            said.append(text)

    stub = Stub()
    LiveBody._say_kit(stub, {6603, 78, 100})
    LiveBody._say_kit(stub, {6603, 78, 100})
    assert said == ["  kit: not pressed - Charge"]
    LiveBody._say_kit(stub, {6603, 78, 100, 1454})
    assert len(said) == 2 and "Life Tap" in said[1]
