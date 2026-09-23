"""Home by hearthstone, found in the bag census the way selling finds junk."""

from __future__ import annotations

from jev.clients.hearth import HEARTHSTONE, Hearth, Hearthed


class _Hid:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))
        return True


class _World:
    """A bag census that paints one slot per read, and a character that may move."""

    def __init__(self, slots, *, bag_open=False, moves_after=None, combat=False):
        self.slots, self.bag_open, self.reads = slots, bag_open, 0
        self.moves_after, self.combat, self.pressed_at = moves_after, combat, None
        self.hid = _Hid()

    def read(self):
        self.reads += 1
        if self.hid.clicks and not self.bag_open and not self.hid.clicks[-1][2]:
            self.bag_open = True                          # the opener was clicked
        if any(c[2] for c in self.hid.clicks) and self.pressed_at is None:
            self.pressed_at = self.reads
        moved = (self.moves_after is not None and self.pressed_at is not None
                 and self.reads - self.pressed_at >= self.moves_after)
        item = self.slots[self.reads % len(self.slots)]
        v = {"pos.mx": 0.5 if moved else 0.4, "pos.my": 0.35 if moved else 0.7,
             "vitals.combat": self.combat, "inventory.item_id": item,
             "inventory.open_x": None if self.bag_open else 0.95,
             "inventory.open_y": None if self.bag_open else 0.97}
        if self.bag_open:
            v.update({"inventory.x": 0.8, "inventory.y": 0.6})
        return v


def _hearth(world):
    return Hearth(world.hid, world.read, sleep=lambda s: None,
                  monotonic=iter(i * 0.25 for i in range(10_000)).__next__)


def test_the_stone_in_a_closed_bag_is_shown_then_used_and_the_bag_closed_again():
    world = _World([117, HEARTHSTONE, 2070], moves_after=3)
    assert _hearth(world).run() is Hearthed.HOME
    opened, used, closed = world.hid.clicks
    assert not opened[2] and used[2] and not closed[2], "open, right-click the stone, close"
    assert used[:2] == (round(0.8 * 1600), round(0.6 * 900))


def test_no_stone_in_the_census_is_reported_not_pressed():
    world = _World([117, 2070])
    assert _hearth(world).run() is Hearthed.NO_STONE
    assert world.hid.clicks == []


def test_a_stone_that_does_not_move_the_character_is_not_home():
    world = _World([HEARTHSTONE], bag_open=True)
    assert _hearth(world).run() is Hearthed.NOT_READY


def test_the_cast_is_not_tried_in_combat():
    world = _World([HEARTHSTONE], bag_open=True, combat=True)
    assert _hearth(world).run() is Hearthed.IN_COMBAT
    assert world.hid.clicks == []
