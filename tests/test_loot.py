"""Taking what the corpse is holding, and knowing whether anything came off it."""

from __future__ import annotations

from jev.clients.loot import Loot, Looted
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


HAVE = {"bags.free": 8, "ui.loot": False, "bags.money_silver": 3}


def test_full_bags_close_a_persisting_loot_frame_before_service(monkeypatch):
    monkeypatch.setattr("jev.clients.loot.time.sleep", lambda _: None)
    hid = _Hid()
    skill = _loot([{**HAVE, "bags.free": 0, "ui.loot": True}], hid=hid)
    assert skill.run() is Looted.BAGS_FULL
    assert not hid.clicks
    assert hid.taps == ["esc"]


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


def test_it_aims_at_the_ring_because_a_corpse_lies_on_it():
    """A living unit stands on its ring, so the fight aims above it to hit the body. A
    dead one lies on it, and aiming above a corpse clicks the empty air it used to
    occupy - first live attempt was `killed` then `loot: nothing`, on a wolf with an
    eighty percent quest drop and a counter that did not move."""
    hid = _Hid()
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}], hid=hid)
    skill.run(settle_s=1.0)
    x, y, right = hid.clicks[0]
    assert right is True
    assert x == 10 + 700
    lift = 38 + 500 - y
    assert 0 <= lift < RING.h, f"aimed {lift}px up; a corpse is not standing"


def test_bags_falling_is_what_counts_as_having_looted():
    """Not "we clicked"."""
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert skill.took == 1


def test_a_loot_frame_is_not_a_take():
    """A frame means a corpse was opened, not that anything came out of it. Only auto
    loot makes the two coincide, and that is a client setting this code cannot see: if it
    were ever off, every empty wolf in the zone would report a take."""
    skill = _loot([HAVE, {**HAVE, "ui.loot": True}, {**HAVE, "ui.loot": True}])
    assert skill.run(settle_s=0.8) is Looted.NOTHING


def test_the_objective_counter_is_believed_before_the_bags():
    """The server's own tally, and the only signal that answers the question actually
    being asked. It is also the one that survives stacking."""
    counts = iter([3, 4])
    have = [3]

    def progress():
        have[0] = next(counts, have[0])
        return (have[0], 8)

    # Bags never move: meat 2 through 8 land on the stack meat 1 made.
    skill = _loot([HAVE, HAVE, HAVE])
    outcome = skill.run(settle_s=1.0, progress=progress)
    assert outcome is Looted.TOOK, "a full stack landed and the bags said nothing"
    assert "objective" in skill.detail


def test_money_counts_when_the_bags_cannot_see_it():
    """A copper or two comes off almost everything, and it fills no slot."""
    skill = _loot([HAVE, {**HAVE, "bags.money_silver": 5}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert "2 silver" in skill.detail


def test_stacked_loot_is_not_reported_as_an_empty_corpse():
    """`bags.free` alone is why a collect quest reads as a dry camp."""
    skill = _loot([HAVE, HAVE, HAVE])
    assert skill.run(settle_s=0.8) is Looted.NOTHING, "nothing moved, so nothing took"


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
    took = {**open_frame, "bags.free": 7}
    skill = _loot([HAVE, took, took, took], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert hid.taps == ["esc"]


def test_an_empty_corpse_still_gets_its_window_shut():
    """A left-open window swallows the next click whether or not anything came out."""
    hid = _Hid()
    open_frame = {**HAVE, "ui.loot": True}
    skill = _loot([HAVE, open_frame, open_frame, open_frame, open_frame], hid=hid)
    assert skill.run(settle_s=0.8) is Looted.NOTHING
    assert hid.taps == ["esc"]
