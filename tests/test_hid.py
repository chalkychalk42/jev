"""The parts of the body that can be checked without a window.

Everything here is pure: the curve, the timing distributions, the key table and the
refusal guards. The Win32 calls themselves cannot be exercised off Windows and are not
pretended otherwise — `win32.available()` is False here and the guards say so.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from jev.clients import win32
from jev.clients.hid import EXTENDED, VK, Hid, Humaniser, bezier, held, pace


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


class _Clock:
    """Stands in for `time` inside `hid`: sleeping advances it, nothing waits."""

    def __init__(self):
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


@pytest.fixture
def timed_hid(monkeypatch):
    hid = Hid(humaniser=Humaniser(rng=random.Random(7)))
    monkeypatch.setattr("jev.clients.hid.time", _Clock())
    monkeypatch.setattr(hid, "_guard", lambda: True)
    monkeypatch.setattr(hid, "_send", lambda event: True)
    monkeypatch.setattr(hid, "_key_event", lambda key, up: (key, up))
    return hid


def test_a_hold_lasts_a_drawn_time_near_what_was_asked(timed_hid):
    """A turn that always lasts the 0.672 s its angle implies is a constant, however
    plausible the constant."""
    lasted = []
    for _ in range(300):
        assert timed_hid.hold("d", 0.5)
        lasted.append(timed_hid.last_hold_s)
    assert all(0.5 * 0.85 - 1e-9 <= s <= 0.5 * 1.15 + 1e-9 for s in lasted)
    assert len({round(s, 6) for s in lasted}) > 250, "the hold is nearly constant"
    assert sum(lasted) / len(lasted) == pytest.approx(0.5, abs=0.02)


def test_an_exact_hold_is_what_was_asked_and_a_ceiling_is_never_passed(timed_hid):
    assert timed_hid.hold("d", 0.5, exact=True)
    assert timed_hid.last_hold_s == pytest.approx(0.5, abs=1e-9)
    capped = []
    for _ in range(300):
        timed_hid.hold("w", 2.0, at_most=2.0)
        capped.append(timed_hid.last_hold_s)
    assert max(capped) <= 2.0 + 1e-9 and min(capped) >= 1.7 - 1e-9
    # Lowered, not clamped: no pile of holds at exactly the limit.
    assert sum(1 for s in capped if s > 2.0 - 1e-6) <= 1


def test_what_a_hold_actually_lasted_is_what_a_caller_learns_from(timed_hid):
    timed_hid.hold("d", 0.3)
    assert held(timed_hid, 0.3) == timed_hid.last_hold_s != 0.3
    assert held(SimpleNamespace(), 0.3) == 0.3, "a device that cannot say reports the ask"
    timed_hid.hold = Hid.hold.__get__(timed_hid)
    timed_hid.key_down = lambda key: False
    assert not timed_hid.hold("d", 0.3)
    assert held(timed_hid, 0.3) == 0.0, "a refused hold held nothing"


def test_loop_waits_are_drawn_only_behind_a_humaniser():
    """Test fakes and the simulator's device have none, so scripted tests see exactly the
    waits they scripted."""
    assert pace(SimpleNamespace(), 0.2) == 0.2
    hid = Hid(humaniser=Humaniser(rng=random.Random(3)))
    waits = [pace(hid, 0.2) for _ in range(200)]
    assert all(0.15 - 1e-9 <= w <= 0.25 + 1e-9 for w in waits)
    assert len({round(w, 6) for w in waits}) > 180


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


def test_every_printable_character_can_be_typed():
    """`type_text` refused anything with a bracket in it, which is every `/script` there
    is - so the diagnostics that would have settled four open questions about this client
    were unrunnable. Refusing was still right: the alternative was `(` arriving as `9`,
    because the shift was decided by `ch.isupper()` and punctuation is never upper."""
    import string

    from jev.clients.hid import SHIFTED, VK

    untypeable = [c for c in string.printable.strip()
                  if c.lower() not in VK and c not in SHIFTED]
    assert untypeable == [], untypeable
    assert all(target in VK for target in SHIFTED.values())


def test_a_shifted_character_is_sent_as_a_chord_not_as_its_unshifted_key():
    class _Rec(Hid):
        def __init__(self):
            super().__init__(hwnd=None, require_focus=False)
            self.events: list[str] = []

        def tap(self, key):
            self.events.append(f"tap:{key}")
            return True

        def chord(self, modifier, key):
            self.events.append(f"{modifier}+{key}")
            return True

    hid = _Rec()
    assert hid.type_text("(a)")
    assert hid.events == ["shift+9", "tap:a", "shift+0"]


@pytest.fixture
def delivered_input(monkeypatch):
    hid = Hid()
    monkeypatch.setattr(hid, "ready", lambda: True)
    monkeypatch.setattr(hid, "_sleep", lambda *_, **__: None)
    monkeypatch.setattr(hid, "_abs", lambda x, y: (x, y))
    monkeypatch.setattr(win32, "user32", SimpleNamespace(GetCursorPos=lambda _: 1))
    monkeypatch.setattr(win32, "scan_code", lambda vk: vk)
    events = []
    replies = iter([1, 0])

    def send(inputs):
        events.extend(inputs)
        return next(replies)

    monkeypatch.setattr(win32, "send_inputs", send)
    return hid, events


def test_a_partly_refused_cursor_move_cannot_click_the_wrong_position(delivered_input):
    hid, events = delivered_input
    assert hid.click(200, 300, right=True) is False
    assert hid.sent == 1
    assert hid.refused == 1
    assert len(events) == 2
    assert all(e.mi.dwFlags & win32.MOUSEEVENTF_MOVE for e in events)
    assert not hid.held_buttons
    assert "accepted 0/1" in hid.detail


def test_relative_motion_stops_at_the_first_refused_segment(delivered_input):
    hid, events = delivered_input
    assert hid.move_by(0, 100, step_px=10) is False
    assert len(events) == 2
    assert hid.sent == 1 and hid.refused == 1


def test_button_release_failure_preserves_ownership_for_cleanup(delivered_input, monkeypatch):
    hid, events = delivered_input
    assert hid.button(True, right=True)
    assert not hid.button(False, right=True)
    assert hid.held_buttons == {True}
    assert hid.sent == 1 and hid.refused == 1
    def accepted(inputs):
        events.extend(inputs)
        return len(inputs)

    monkeypatch.setattr(win32, "send_inputs", accepted)
    hid.release_all()
    assert not hid.held_buttons
    assert events[-1].mi.dwFlags == win32.MOUSEEVENTF_RIGHTUP
    assert hid.sent == len(events) - 1  # the original refused release remains uncounted


def test_hold_reports_refused_release(delivered_input):
    hid, _ = delivered_input
    assert hid.hold("w", 0) is False
    assert hid.held == {"w"}


def test_recording_failure_cannot_skip_remaining_physical_releases(delivered_input, monkeypatch):
    hid, events = delivered_input
    hid.held = {"w", "d"}
    hid.held_buttons = {True, False}
    refused_key = []

    def send(inputs):
        events.extend(inputs)
        if not refused_key:
            refused_key.append(inputs[0].ki.wScan)
            return 0
        return len(inputs)

    def broken_evidence(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(win32, "send_inputs", send)
    monkeypatch.setattr("jev.clients.hid.evidence_event", broken_evidence)
    with pytest.raises(OSError, match="disk full"):
        hid.release_all()
    assert len(events) == len(Hid.MOVEMENT_KEYS) + 2
    assert not hid.held_buttons
    assert all(VK[key] == refused_key[0] for key in hid.held)


def test_all_callers_observe_refused_cleanup(delivered_input, monkeypatch):
    hid, events = delivered_input
    hid.held = {"w"}
    hid.held_buttons = {True}

    def refused(inputs):
        events.extend(inputs)
        return 0

    monkeypatch.setattr(win32, "send_inputs", refused)
    with pytest.raises(RuntimeError, match="held inputs remain"):
        hid.release_all()
    assert len(events) == len(Hid.MOVEMENT_KEYS) + 1
    assert hid.held == {"w"} and hid.held_buttons == {True}


def test_a_long_drag_pays_the_client_stagger_once_not_per_step(monkeypatch):
    """A 2,500 px camera drag in 10 px steps took 21 s when every step paid the stagger."""
    hid = Hid(humaniser=Humaniser(stagger_ms=63.0))
    staggered = []
    monkeypatch.setattr("jev.clients.hid.time.sleep", lambda seconds: staggered.append(seconds))
    monkeypatch.setattr(hid, "_guard", lambda: True)
    monkeypatch.setattr(hid, "_send", lambda event: True)
    assert hid.move_by(0, 2000)
    assert len(staggered) == 200
    assert max(staggered) < 0.03, "a drag step waited for the client stagger"
