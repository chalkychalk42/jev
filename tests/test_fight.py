"""Killing one unit: select by Tab, face by clicking, and do not claim a kill."""

from __future__ import annotations

import inspect

from jev.clients.fight import (
    DEAD_HP,
    FLEE_HP,
    LOST_HP,
    MAX_SELECTS,
    MIN_START_HP,
    SLOT_KEYS,
    Fight,
    Fought,
)
from jev.world.combat import GENERIC, PROFILES, Ability, for_class


class _Hid:
    def __init__(self):
        self.taps = []
        self.clicks = []

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
    assert hid.taps == ["1"]


def test_a_slot_the_client_says_is_not_ready_is_not_pressed():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "bars.ready": 0b110})     # slot 1 on cooldown
    assert hid.taps == ["2"], "ignored bars.ready"


def test_a_seal_is_not_re_pressed_every_tick():
    """`bars.ready` says a self-buff is pressable on every single tick, so without a
    worth-pressing interval the character stands there re-sealing and never swings."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate(ALIVE)
    f._rotate(ALIVE)
    assert hid.taps == ["1", "2"], "slot 1 was re-pressed while still up"
    assert PROFILES[2].abilities[0].every_s > 0


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
    f.close_in = lambda: True
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


def test_a_target_that_never_takes_damage_is_given_up_not_waited_out():
    """A live run spent ninety seconds pressing abilities at a full-health kobold it had
    engaged and never reached. Eight bursts of walking with nothing landing is the
    answer already; the rotation cannot improve on it."""
    f = _fight([ALIVE])
    f.acquire = lambda name_id: None
    f.engage = lambda: True
    f.close_in = lambda: False
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
