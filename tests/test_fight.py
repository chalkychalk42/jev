"""Killing one unit: select by Tab, face by clicking, and do not claim a kill."""

from __future__ import annotations

import inspect

from jev.clients.fight import DEAD_HP, LOST_HP, MAX_SELECTS, SLOT_KEYS, Fight, Fought
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
