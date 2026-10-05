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
