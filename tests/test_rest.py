"""A meal (`jev.clients.rest`): from the bar's slot, else from the bags (V166)."""

from __future__ import annotations

import pytest

from jev.clients.rest import Rest, Rested
from jev.world.combat import Role, for_class


class _Hid:
    def __init__(self):
        self.taps = []

    def tap(self, key):
        self.taps.append(key)
        return True


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.rest.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.rest.time.sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def _mana(power, *, water=False):
    usable = (1 << 1) | ((1 << 10) if water else 0)          # Fireball; slot 11 if any water
    return {"vitals.power": power, "vitals.hp": 1.0, "vitals.combat": False,
            "bars.usable": usable, "char.class_id": 8, "char.race_id": 1}


def test_an_empty_drink_slot_drinks_from_the_bags(clock):
    looks = iter([_mana(0.3), _mana(0.5), _mana(0.8), _mana(0.96)])
    used = []
    hid = _Hid()
    rest = Rest(hid=hid, read=lambda: next(looks), profile=for_class(8, 1),
                use_item=lambda role: used.append(role) or True)
    assert rest.until(0.95, role=Role.DRINK) is Rested.HEALTHY
    assert used == [Role.DRINK] and hid.taps == [], "from the bags, not the empty slot"


def test_with_nothing_in_the_bags_either_it_says_so(clock):
    rest = Rest(hid=_Hid(), read=lambda: _mana(0.3), profile=for_class(8, 1),
                use_item=lambda role: False)
    assert rest.until(0.95, role=Role.DRINK) is Rested.NO_FOOD


def test_a_drink_on_the_bar_is_pressed_as_before(clock):
    looks = iter([_mana(0.3, water=True), _mana(0.96, water=True)])
    hid = _Hid()
    rest = Rest(hid=hid, read=lambda: next(looks), profile=for_class(8, 1),
                use_item=lambda role: pytest.fail("the bar's water comes first"))
    assert rest.until(0.95, role=Role.DRINK) is Rested.HEALTHY
    assert hid.taps == ["minus"]
