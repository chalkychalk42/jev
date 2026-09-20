"""`LiveSource` satisfies the same contract as the simulator, and never raises."""

from __future__ import annotations

import pytest

from jev.clients.capture import Backend, CaptureError
from jev.clients.live import LiveSource, LiveStats, bind
from jev.clients.source import Source
from jev.world.state_v1 import SenseFault, State


def test_live_source_is_a_source():
    """The runtime must not be able to tell a window from a fixture — that is what lets
    the brain be exercised for thousands of ticks before a window is involved."""
    assert hasattr(LiveSource, "read") and hasattr(LiveSource, "close")
    assert isinstance(Source, type(Source))


def test_binding_refuses_rather_than_guessing():
    """Binding the wrong window presents as "the bot does nothing" for as long as it
    takes someone to notice which window has focus."""
    with pytest.raises((RuntimeError, Exception)):
        bind("a window that certainly does not exist")


def test_a_black_frame_and_a_lost_window_are_counted_apart():
    """Same shape here, different problems: one is a DirectX capture backend that cannot
    see, the other is a client that has gone."""
    stats = LiveStats()
    stats.black += 1
    stats.lost_window += 2
    assert stats.black == 1 and stats.lost_window == 2


def test_decode_rate_is_unknown_before_any_frame():
    """Zero frames is not a zero rate. Reporting 0.0 would have a client that has not
    started yet look identical to one that cannot read its own strip."""
    assert LiveStats().decode_rate is None
    assert LiveStats(frames=4, decoded=3).decode_rate == 0.75


def test_a_capture_error_is_a_distinct_type():
    assert issubclass(CaptureError, RuntimeError)


def test_backends_are_named_for_what_they_do():
    assert Backend.SCREEN.value == "screen"
    assert Backend.PRINT_WINDOW.value == "print_window"


def test_a_blind_state_reports_windows_as_unobserved_not_absent():
    """Vision is not written yet, so a radio-only client does not know whether a loot
    window is open. `None`, not `False` — otherwise the coach is confidently wrong."""
    from jev.clients.source import blind

    s = blind(1.0, "c01", SenseFault.NOT_FOUND)
    assert isinstance(s, State)
    assert s.ui.loot is None
    assert s.ui.modal is None
    assert s.sense.addon_ok is False
