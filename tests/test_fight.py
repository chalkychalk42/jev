"""Killing one unit: select by Tab, face by clicking, and do not claim a kill."""

from __future__ import annotations

import inspect
import time

from jev.clients.fight import (
    DEAD_HP,
    FLEE_HP,
    LOST_HP,
    MAX_CLOSE_BURSTS,
    MAX_SELECTS,
    MIN_START_HP,
    SLOT_KEYS,
    Fight,
    Fought,
)
from jev.world.combat import GENERIC, Ability, Role, for_class


class _Hid:
    def __init__(self):
        self.taps = []
        self.clicks = []
        self.holds = []

    def hold(self, key, seconds, **_):
        self.holds.append((key, seconds))
        return True

    def tap(self, key):
        self.taps.append(key)
        return True

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))


A_FRAME = object()   # stands in for pixels; `find` is not what these tests exercise


def _fight(frames, hid=None, frame=A_FRAME):
    """`frames` is a list of decoded readings, served one per read."""
    seq = list(frames)
    state = {"i": 0}

    def read():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    return Fight(hid=hid or _Hid(), read=read, read_frame=lambda: frame,
                 window_origin=(10, 38))


ALIVE = {"target.has": True, "target.hp": 1.0, "target.name_id": 1161,
         "bars.ready": 0b111, "bars.usable": 0b111, "bars.gcd": 0.0,
         "bars.casting": False, "char.class_id": 2, "vitals.dead": False}


def test_selection_is_tab_and_never_chat():
    """A bot that can talk is a bot that can say the wrong thing — see `interact`. Tab is
    a keybind, the client picks the nearest attackable unit, and the radio says what
    answered."""
    src = inspect.getsource(Fight)
    assert "slash(" not in src and "type_text(" not in src
    hid = _Hid()
    assert _fight([ALIVE], hid=hid).select(1161) is None
    assert hid.taps == ["tab"]


def test_a_corpse_is_not_a_fight():
    """Corpses stay selectable. Tabbing onto one and 'fighting' it burns the timeout."""
    hid = _Hid()
    dead = {**ALIVE, "target.hp": 0.0}
    assert _fight([dead, dead, dead, dead], hid=hid).select(None) is Fought.NO_TARGET
    assert hid.taps == ["tab"] * MAX_SELECTS


def test_the_wrong_unit_costs_a_selection_and_tab_moves_on():
    hid = _Hid()
    other = {**ALIVE, "target.name_id": 999}
    f = _fight([other, other, ALIVE], hid=hid)
    assert f.select(1161) is None
    assert hid.taps == ["tab"] * 3, "gave up, or accepted the wrong unit"


def test_engaging_refuses_when_the_unit_cannot_be_seen():
    """2.4.3 has no facing API — `GetPlayerFacing` is 3.0 and `pos.facing` reads None on
    every live frame — so clicking the model is the only way to aim the character. No
    sighting means no way to face it, and swinging anyway hits whatever the camera is
    pointed at."""
    hid = _Hid()
    f = Fight(hid=hid, read=lambda: ALIVE, read_frame=lambda: None)
    assert f.engage() is False
    assert hid.clicks == []
    assert "face" in f.detail


def test_a_vanishing_target_at_full_health_is_not_a_kill():
    """Vanishing is one observation with two causes. Claiming the kill is how a counter
    that never moves looks like a working fight."""
    f = _fight([ALIVE, {**ALIVE, "target.has": False}])
    f.last_hp = 1.0
    assert f._settle() is Fought.LOST
    f.last_hp = 0.0
    assert f._settle() is Fought.KILLED
    assert LOST_HP > DEAD_HP


def test_the_rotation_respects_the_global_cooldown_and_casting():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "bars.gcd": 0.7})
    f._rotate({**ALIVE, "bars.casting": True})
    assert hid.taps == [], "pressed through the GCD or through a cast"
    f._rotate(ALIVE)
    assert hid.taps == ["2"], "a lapsed buff outranks a swing"


def test_a_slot_the_client_says_is_not_ready_is_not_pressed():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "bars.ready": 0b101})     # the buff in slot 2 is on cooldown
    assert hid.taps == ["1"], "ignored bars.ready"


def test_a_seal_is_not_re_pressed_every_tick():
    """`bars.ready` says a self-buff is pressable on every single tick, so without a
    worth-pressing interval the character stands there re-sealing and never swings. The
    interval is the spell's own duration less a margin, read from the world database."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate(ALIVE)
    f._rotate(ALIVE)
    assert hid.taps == ["2", "1"], "the seal was re-pressed while still up"
    seal = for_class(2, 1).first(Role.BUFF)
    assert seal is not None and seal.every_s == 25.0   # 30s duration, 5s margin


def test_melee_auto_attack_is_a_toggle_and_is_pressed_once():
    """Spell 6603 toggles the swing: pressing it while already swinging stops it. The
    first live rotation pressed `[1, 2, 2, ..., 1, 2, ...]` and turned the character's
    attack on and off all fight."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    for _ in range(4):
        f._rotate(ALIVE)
    assert hid.taps.count("1") == 1, "auto-attack was toggled more than once"
    attack = for_class(2, 1).first(Role.ATTACK)
    assert attack is not None and attack.toggle


def test_a_toggle_is_not_pressed_once_damage_is_already_landing():
    """Health coming off the target means the swing is already going; pressing the toggle
    then is how it stops."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.last_hp = 0.6
    for _ in range(3):
        f._rotate(ALIVE)
    assert "1" not in hid.taps


def test_healing_is_a_role_not_a_class():
    """No `if paladin`. The engine asks for a row with role=heal and presses it if the
    bars say it is ready; a warrior is the same list with one fewer row."""
    import inspect

    # Code, not prose: the docstrings name paladins on purpose, to say why there is no
    # module for them.
    code = "".join(ln.split("#")[0] for ln in inspect.getsource(Fight).splitlines()
                   if not ln.strip().startswith(("#", '"', "'")))
    for branch in ("class_id ==", "class_id in", "PaladinHeal", '== "paladin"'):
        assert branch not in code, f"{branch}: the engine is branching on class"

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
               "vitals.power": 0.9, "vitals.power_max": 100})
    assert hid.taps == ["3"], "stood there at 20% with a heal on the bar"
    assert for_class(1, 1).first(Role.HEAL) is None, "a warrior grew a heal"


def test_a_heal_is_not_started_without_the_mana_to_finish_it():
    """Out of mana falls through to swinging, and the caller falls through to food, a
    vendor, or breaking off. There is deliberately no drinking inside a fight."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    dying = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
             "vitals.power": 0.02, "vitals.power_max": 100}
    f._rotate(dying)
    assert "3" not in hid.taps


def test_a_heal_is_confirmed_by_the_client_not_by_having_tapped_a_key():
    """A press the client ignored looks identical to one that worked if nobody checks,
    and what it hides is a picker predicate that never fires."""
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt])
    f._rotate(hurt)
    assert f._pending_heal is not None
    f._watch_heal({**hurt, "vitals.hp": 0.5}, hurt["bars.ready"])
    assert f.heals_landed == 1 and f.heals_ignored == 0

    f2 = _fight([hurt])
    f2._rotate(hurt)
    f2._pending_heal = (0.2, time.monotonic() - 5.0)
    f2._watch_heal(hurt, hurt["bars.ready"])
    assert f2.heals_ignored == 1, "a press nothing happened after was counted as a heal"


def test_slot_keys_cover_the_bar_including_food_and_water():
    """10-12 are `0`, `minus`, `equals` — and 11 and 12 are where a fresh character's food
    and water sit, so getting them wrong is not academic."""
    assert SLOT_KEYS[9] == "9" and SLOT_KEYS[10] == "0"
    assert SLOT_KEYS[11] == "minus" and SLOT_KEYS[12] == "equals"
    assert len(SLOT_KEYS) == 12


def test_a_class_with_no_profile_still_fights():
    assert for_class(None) is GENERIC
    assert for_class(2).name == "paladin"
    assert all(isinstance(a, Ability) for a in for_class(7).abilities)


# -- not dying ----------------------------------------------------------------------

def test_a_fight_is_not_started_on_low_health():
    """Both live deaths were pulls taken at health that could not absorb a mistake."""
    hurt = {**ALIVE, "vitals.hp": 0.2}
    f = _fight([hurt])
    assert f.run(timeout_s=1) is Fought.TOO_HURT
    assert MIN_START_HP > FLEE_HP


def test_a_losing_fight_is_broken_off_rather_than_finished():
    """A fight is usually lost several seconds before the character falls over, and
    finishing it standing up costs a two-hundred-yard corpse run."""
    sinking = {**ALIVE, "vitals.hp": 0.1}
    f = _fight([ALIVE, sinking])
    f.acquire = lambda name_id: None
    f.engage = lambda: True
    assert f.run(timeout_s=5) is Fought.LOSING
    assert "broke off" in f.detail


def test_an_isolated_nameplate_is_preferred_over_a_central_one():
    """A pull in the middle of a camp is what killed this character twice, and a plate
    with no neighbour is the best evidence available that a mob has none either."""
    from jev.perceive.units import Plate, RingColour

    def plate(cx):
        return Plate(cx=cx, cy=300.0, w=140, colour=RingColour.YELLOW)

    crowd_a, crowd_b, alone = plate(790.0), plate(830.0), plate(1400.0)
    f = Fight(hid=_Hid(), read=lambda: ALIVE,
              read_frame=lambda: None, window_centre_x=800)
    import jev.clients.fight as mod

    real, mod.find_plates = mod.find_plates, lambda _f: [crowd_a, crowd_b, alone]
    try:
        assert f._candidates(object())[0] is alone, "picked the one with company"
    finally:
        mod.find_plates = real


def test_resting_stops_for_combat_and_says_when_there_is_no_food():
    from jev.clients.rest import Rest, Rested

    hid = _Hid()
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 0.4, "vitals.combat": True}).until() \
        is Rested.INTERRUPTED
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 0.4, "vitals.combat": False,
                                       "bars.usable": 0b111}).until() is Rested.NO_FOOD
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 1.0}).until() is Rested.HEALTHY


def test_walking_at_it_and_swinging_at_it_are_the_same_loop():
    """Closing used to be a gate: walk until the target takes damage, *then* start the
    rotation. Damage comes from swinging, swinging is the rotation, and the rotation was
    behind the gate — so a live run reported `unreachable pressed [] closed 8` eight
    times over, having walked at a kobold without once pressing anything at it."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.acquire = lambda name_id: None
    f.engage = lambda: True
    f.run(timeout_s=2)
    assert f.pressed, "walked at it and never swung"
    assert f.closed > 0, "swung at it and never closed"


def test_a_target_that_never_takes_damage_is_given_up_not_waited_out():
    """Out of bursts, nothing pressed and nothing landed is the answer already; more
    seconds cannot improve on it. A live run spent ninety on exactly that."""
    hid = _Hid()
    f = _fight([{**ALIVE, "bars.ready": 0, "bars.usable": 0}], hid=hid)
    f.acquire = lambda name_id: None
    f.engage = lambda: True
    f.closed = MAX_CLOSE_BURSTS
    assert f.run(timeout_s=5) is Fought.UNREACHABLE
    assert "cannot reach" in f.detail


def test_in_combat_the_name_filter_comes_off():
    """Something already hitting us does not have to be the quest mob. A Kobold Worker
    beat this character to 27% while every attempt refused to fight anything but a Kobold
    Vermin, selected nothing, and reported "not visible" twenty times running."""
    seen = []
    f = _fight([{**ALIVE, "vitals.combat": True, "target.has": False}])
    f.acquire = lambda name_id: seen.append(name_id) or Fought.NO_TARGET
    f.run(1161, timeout_s=1)
    assert seen == [None], "refused to defend itself against the wrong species"

    seen.clear()
    f2 = _fight([{**ALIVE, "vitals.combat": False, "target.has": False}])
    f2.acquire = lambda name_id: seen.append(name_id) or Fought.NO_TARGET
    f2.run(1161, timeout_s=1)
    assert seen == [1161], "picked a fight with something that was not the objective"


def test_a_guard_does_not_freeze_a_fight_already_started():
    """Breaking off is only a choice when nothing is hitting us. In combat it is not a
    choice, it is standing still: this returned LOSING on the first iteration, before the
    rotation, so the caller rested, was interrupted because something was attacking,
    tried again, and got LOSING again - eight times, pressing nothing, while health went
    29, 27, 21, 18, 18, 15, 9, 6, dead."""
    hid = _Hid()
    sinking = {**ALIVE, "vitals.hp": 0.1, "vitals.combat": True}
    f = _fight([sinking], hid=hid)
    f.acquire = lambda name_id: None
    f.engage = lambda: True
    f.run(timeout_s=1)
    assert f.pressed, "stood at 10% health in combat and pressed nothing"

    # Out of combat it is a real choice, and still taken.
    free = {**ALIVE, "vitals.hp": 0.1, "vitals.combat": False}
    g = _fight([ALIVE, free])
    g.acquire = lambda name_id: None
    g.engage = lambda: True
    assert g.run(timeout_s=5) is Fought.LOSING


def test_a_heal_is_not_pressed_again_until_the_last_one_answers():
    """Holy Light is a 2.5 second cast. A live fight pressed it fifteen times in a row,
    none of which healed anything, because nothing stopped the next tick from pressing
    again."""
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    for _ in range(5):
        f._rotate(hurt)
    assert hid.taps.count("3") == 1, "spammed the heal without waiting for an answer"

    # Once it answers, the next one is allowed.
    f._watch_heal({**hurt, "vitals.hp": 0.5}, hurt["bars.ready"])
    f._rotate(hurt)
    assert hid.taps.count("3") == 2
