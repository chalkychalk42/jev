"""Taking what the corpse is holding, and knowing whether anything came off it."""

from __future__ import annotations

from jev.clients.loot import ABOVE_RING_PX, Loot, Looted
from jev.perceive.units import Ring, RingColour

A_FRAME = object()
RING = Ring(cx=700.0, cy=500.0, w=60, h=20, colour=RingColour.YELLOW, area=300)


class _Hid:
    def __init__(self):
        self.clicks = []
        self.taps = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))

    def tap(self, key):
        self.taps.append(key)


def _loot(readings, hid=None, ring=RING):
    import jev.clients.loot as mod

    seq = list(readings)
    state = {"i": 0}

    def read():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    skill = Loot(hid=hid or _Hid(), read=read, read_frame=lambda: A_FRAME,
                 window_origin=(10, 38))
    mod._find_ring = lambda _f: ring
    return skill


HAVE = {"bags.free": 8, "ui.loot": False}


def test_a_full_bag_is_not_clicked_at():
    """Looting into a full bag silently takes nothing, and the fix is a vendor rather
    than another click."""
    hid = _Hid()
    skill = _loot([{**HAVE, "bags.free": 0}], hid=hid)
    assert skill.run() is Looted.BAGS_FULL
    assert hid.clicks == []


def test_nothing_selected_is_not_a_corpse():
    hid = _Hid()
    skill = _loot([HAVE], hid=hid, ring=None)
    assert skill.run() is Looted.NO_CORPSE
    assert hid.clicks == []


def test_it_aims_just_above_the_ring_because_a_unit_stands_on_its_own():
    """The same primitive the fight uses to face something, reused. A unit stays selected
    after it dies, so the ring is still drawn under the corpse."""
    hid = _Hid()
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}], hid=hid)
    skill.run(settle_s=1.0)
    x, y, right = hid.clicks[0]
    assert right is True
    assert x == 10 + 700
    assert y == 38 + 500 - max(ABOVE_RING_PX, RING.h)


def test_bags_falling_is_what_counts_as_having_looted():
    """Not "we clicked"."""
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert skill.took == 1


def test_a_loot_frame_also_counts():
    skill = _loot([HAVE, {**HAVE, "ui.loot": True}, {**HAVE, "ui.loot": False}])
    assert skill.run(settle_s=1.0) is Looted.TOOK


def test_an_empty_corpse_is_not_a_failure():
    """A skill that treats an empty wolf as an error retires itself on a good camp."""
    skill = _loot([HAVE])
    outcome = skill.run(settle_s=0.8)
    assert outcome is Looted.NOTHING
    assert outcome.ok, "an empty corpse must not count against the skill"


def test_a_loot_window_that_stays_open_is_closed():
    """Auto-loot usually closes itself; when it does not, a left-open window swallows the
    next click. Escape is pressed against a screen the radio has measured."""
    hid = _Hid()
    open_frame = {**HAVE, "ui.loot": True}
    skill = _loot([HAVE, open_frame, open_frame, open_frame], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert hid.taps == ["esc"]
