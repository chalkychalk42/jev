"""Getting the gear working again, and knowing whether it actually came back."""

from __future__ import annotations

from jev.clients.repair import BROKEN, REPAIR_BELOW, Repair, Repaired


class _Hid:
    def __init__(self):
        self.clicks = []
        self.taps = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))

    def tap(self, key):
        self.taps.append(key)


def _repair(readings, *, visit=True, hid=None):
    """`readings` is consumed one per read, then the last one repeats."""
    seq = list(readings)
    state = {"i": 0}

    def read():
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    visits = []

    def _visit():
        visits.append(True)
        return visit

    r = Repair(hid=hid or _Hid(), read=read, visit=_visit)
    r.visits = visits
    return r


FINE = {"bags.durability_min": 1.0, "ui.vendor": False}
WORN = {"bags.durability_min": 0.1, "ui.vendor": False}
AT_VENDOR = {"bags.durability_min": 0.1, "ui.vendor": True,
             "ui.advance_x": 0.5, "ui.advance_y": 0.5}
FIXED = {"bags.durability_min": 1.0, "ui.vendor": True,
         "ui.advance_x": 0.5, "ui.advance_y": 0.5}


def test_good_gear_is_not_a_trip_to_town():
    r = _repair([FINE])
    assert r.run() is Repaired.NOT_NEEDED
    assert r.visits == [], "walked to a vendor with nothing to repair"


def test_nothing_equipped_with_durability_is_not_broken_gear():
    """`nil` is no observation. A naked character would otherwise read as ruined and
    spend the rest of the run walking to merchants about it."""
    r = _repair([{"bags.durability_min": None, "ui.vendor": False}])
    assert r.run() is Repaired.NOT_NEEDED
    assert r.visits == []


def test_a_broken_weapon_is_repaired_and_believed_by_the_durability():
    hid = _Hid()
    r = _repair([WORN, AT_VENDOR, FIXED], hid=hid)
    assert r.run() is Repaired.DONE
    assert r.visits == [True]
    assert hid.clicks, "Repair All was never pressed"
    assert "esc" in hid.taps, "the merchant frame was left open"
    assert r.before == 0.1 and r.after == 1.0


def test_pressing_repair_with_no_money_is_reported_as_being_poor():
    """The button is painted whether or not the copper is there, so a click proves
    nothing. 27 copper does not go far, and a caller that believes the click walks back
    to the same merchant forever instead of going to sell something."""
    r = _repair([WORN, AT_VENDOR, AT_VENDOR])
    assert r.run() is Repaired.TOO_POOR
    assert "not enough money" in r.detail


def test_a_merchant_who_does_not_repair_is_not_a_failed_repair():
    """No painted button *is* the test for "does this one repair": the client only draws
    Repair All on a merchant that can. A cheese seller is a wrong node, not a bug."""
    cheese = {"bags.durability_min": 0.1, "ui.vendor": True,
              "ui.advance_x": None, "ui.advance_y": None}
    r = _repair([WORN, cheese, cheese])
    assert r.run() is Repaired.NO_BUTTON


def test_a_merchant_that_never_opens_is_not_a_merchant():
    r = _repair([WORN, WORN, WORN])
    assert r.run() is Repaired.NO_VENDOR

    unreachable = _repair([WORN], visit=False)
    assert unreachable.run() is Repaired.NO_VENDOR


def test_the_walk_is_taken_before_the_weapon_is_broken():
    """Arriving at a vendor already at zero means the fights that got it there were
    already being lost. The band is above broken on purpose."""
    assert REPAIR_BELOW > BROKEN
    assert _repair([{"bags.durability_min": REPAIR_BELOW, "ui.vendor": False}]).needed()
    assert not _repair([{"bags.durability_min": 1.0, "ui.vendor": False}]).needed()


def test_an_unreadable_strip_is_not_a_reason_to_go_shopping():
    assert _repair([None]).run() is Repaired.BLIND
