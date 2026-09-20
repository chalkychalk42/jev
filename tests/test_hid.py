"""The parts of the body that can be checked without a window.

Everything here is pure: the curve, the timing distributions, the key table and the
refusal guards. The Win32 calls themselves cannot be exercised off Windows and are not
pretended otherwise — `win32.available()` is False here and the guards say so.
"""

from __future__ import annotations

import random

import pytest

from jev.clients import win32
from jev.clients.hid import EXTENDED, VK, Hid, Humaniser, bezier


def test_win32_imports_anywhere_and_admits_where_it_is():
    """The whole test suite imports this module on Linux. It must not pretend a window
    exists, and it must not fail to import either."""
    assert win32.available() is (win32.IS_WINDOWS)
    if not win32.available():
        with pytest.raises(win32.Unavailable):
            win32.find_windows("anything")


def test_an_unavailable_platform_is_not_a_caller_error():
    """"There is no window here" is a fact about the machine, not a failure of the thing
    that asked, and code running in both places has to tell them apart."""
    assert issubclass(win32.Unavailable, RuntimeError)


# --- the curve --------------------------------------------------------------

def test_a_mouse_path_always_lands_exactly():
    """However the curve rounds, the click has to happen on the pixel asked for."""
    rng = random.Random(0)
    for _ in range(50):
        end = (rng.randint(0, 1900), rng.randint(0, 1000))
        path = bezier((5, 5), end, rng.randint(2, 20), rng)
        assert path[-1] == end


def test_a_mouse_path_is_not_a_straight_line():
    """A cursor travelling in a perfect line at a constant rate is the single easiest
    synthetic-input signature there is."""
    rng = random.Random(7)
    path = bezier((0, 0), (400, 0), 16, rng)
    # A straight horizontal move would keep y at 0 the whole way.
    assert any(y != 0 for _, y in path[:-1]), "the path never left the straight line"


def test_a_degenerate_move_does_not_produce_a_fake_journey():
    """Two pixels apart is a jump, not a swoop. Inventing a curve for it is as unnatural
    as never curving at all."""
    rng = random.Random(1)
    assert bezier((10, 10), (10, 10), 12, rng) == [(10, 10)]
    assert bezier((10, 10), (300, 300), 1, rng) == [(300, 300)]


def test_longer_moves_bow_more():
    """The control point is displaced by a fraction of the distance, so the shape of a
    long move is not a scaled-up short one."""
    rng = random.Random(3)

    def deviation(dist: int) -> float:
        path = bezier((0, 0), (dist, 0), 20, random.Random(3))
        return max(abs(y) for _, y in path)

    assert deviation(800) > deviation(80)
    assert rng is not None


# --- timing -----------------------------------------------------------------

def test_no_delay_is_ever_zero():
    """PLAN §2.1: do not look human in the planner and then act on a 16 ms grid."""
    h = Humaniser(rng=random.Random(2))
    assert all(h.gap() > 0 for _ in range(200))
    assert all(h.hold() > 0 for _ in range(200))


def test_delays_are_drawn_not_fixed():
    """A constant 50 ms is a signature exactly as clean as 0 ms — just a different one."""
    h = Humaniser(rng=random.Random(4))
    gaps = {round(h.gap(), 6) for _ in range(100)}
    assert len(gaps) > 90, "the gap distribution is nearly constant"


def test_clients_are_staggered_against_each_other():
    """Ten bots pressing the same key on the same millisecond is a pattern no amount of
    per-event jitter hides."""
    staggers = {Humaniser.for_client(f"c{i:02d}").stagger_ms for i in range(10)}
    assert len(staggers) > 1, "every client got the same offset"


def test_a_client_reproduces_its_own_timing():
    a = Humaniser.for_client("c03", seed=11)
    b = Humaniser.for_client("c03", seed=11)
    assert [a.gap() for _ in range(5)] == [b.gap() for _ in range(5)]


# --- guards -----------------------------------------------------------------

def test_input_refuses_when_the_window_is_not_focused():
    """A bot pressing 1 into the wrong window has not failed to attack. It has typed into
    somebody's chat, and the failure surfaces somewhere unrelated much later."""
    hid = Hid(hwnd=1234, require_focus=True)
    assert not hid.ready(), "nothing is focused on a machine with no windows"
    assert hid.tap("1") is False
    assert hid.refused == 1
    assert hid.sent == 0


def test_refusals_are_counted_not_swallowed():
    hid = Hid(hwnd=1)
    for _ in range(4):
        hid.tap("w")
    assert hid.refused == 4


# --- the key table ----------------------------------------------------------

def test_the_keys_a_leveling_bot_presses_are_all_there():
    for key in ("1", "0", "w", "a", "s", "d", "esc", "enter", "space", "tab", "f1"):
        assert key in VK, f"{key} is not in the key table"


def test_arrow_keys_carry_the_extended_flag():
    """Without it the game reads a different key entirely, and it fails silently."""
    assert {"up", "down", "left", "right"} == EXTENDED
    assert all(k in VK for k in EXTENDED)
