"""The wiring: the readers every skill is handed, and what `None` is allowed to mean."""

from __future__ import annotations

import time

import pytest

from jev.run.client import STALE_AFTER_S, Client


def test_focus_backoff_is_cancellable_before_another_focus_attempt(monkeypatch):
    from jev.clients import win32
    from jev.run.supervisor import Cancelled

    calls = []
    monkeypatch.setattr(win32, "focus", lambda _: calls.append("focus") or False)
    monkeypatch.setattr(win32, "is_foreground", lambda _: False)
    client = Client(hwnd=1, hid=None, cap=_Cap(), origin=(0, 0), size=(1600, 900))
    def checkpoint():
        if calls:
            raise Cancelled("operator stop")
    with pytest.raises(Cancelled, match="operator stop"):
        client.focused(checkpoint=checkpoint)
    assert calls == ["focus"]


class _Cap:
    """A capture that always hands back the same pixels."""

    def __init__(self):
        self.grabs = 0

    def grab(self):
        self.grabs += 1
        return self

    @property
    def rgb(self):
        return None

    def close(self):
        pass


def _client(seqs):
    """A client whose decoder yields these sequence numbers in order."""
    from jev.perceive import radio_frame

    c = Client(hwnd=1, hid=None, cap=_Cap(), origin=(0, 0), size=(1600, 900))
    seq = list(seqs)
    state = {"i": 0}

    def fake_read(_pixels, prev_seq=None, grid=None):
        n = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return radio_frame.RadioReading(values={"seq": n}, ok=True,
                                        fault=radio_frame.SenseFault.NONE, seq=n)

    radio_frame_read, radio_frame.read = radio_frame.read, fake_read
    c._restore = lambda: setattr(radio_frame, "read", radio_frame_read)
    return c


def test_a_repeated_sequence_is_normal_and_not_frozen():
    """The addon paints on a timer and this captures faster than it paints, so reading
    the same frame several times over is the ordinary case."""
    c = _client([7, 7, 7])
    try:
        assert c.reading() is not None
        assert c.reading() is not None
        assert c.frozen_for() < STALE_AFTER_S
    finally:
        c._restore()


def test_a_frozen_strip_reads_as_blind_not_as_a_stuck_character():
    """The expensive one. A frozen addon and a pinned character look identical — a
    position that never changes — and a follower that believes the stale values reports
    STUCK and sends everyone looking at terrain."""
    c = _client([7])
    try:
        assert c.reading() is not None
        c._seq_at = time.monotonic() - (STALE_AFTER_S + 1.0)
        assert c.reading() is None, "stale values were handed out as if they were current"
    finally:
        c._restore()


def test_a_sequence_that_advances_clears_the_clock():
    c = _client([7, 8])
    try:
        c.reading()
        c._seq_at = time.monotonic() - (STALE_AFTER_S + 1.0)
        assert c.reading() is not None, "a fresh sequence still read as frozen"
        assert c.frozen_for() < 1.0
    finally:
        c._restore()


def test_a_refused_walk_takes_the_window_back_and_retries():
    """`Hid` refuses when the window is not focused and `Travel` reports REFUSED rather
    than calling it stuck - but somebody has to take the window back. A live run made
    three kills and then spent twelve stations refused, walking nowhere."""
    import inspect

    from jev.run.client import Client as _C

    body = inspect.getsource(_C.approach)
    assert "Outcome.REFUSED" in body
    assert body.index("Outcome.REFUSED") < body.rindex("self.focused(")
    assert body.count("self.travel.follow(") == 2, "it reports the refusal and gives up"


def test_taking_the_window_back_backs_off_rather_than_waiting_flat():
    """Twenty attempts two seconds apart is forty seconds of standing still inside a run,
    and most focus losses clear on the first try. The long patience stays available for
    starting up, where a notification panel can hold the foreground for half a minute."""
    from jev.run.client import (
        FOCUS_FIRST_WAIT_S,
        FOCUS_MAX_WAIT_S,
        FOCUS_PATIENCE_S,
        FOCUS_QUICK_S,
    )

    assert FOCUS_FIRST_WAIT_S <= 0.5, "the first retry should be immediate-ish"
    assert FOCUS_FIRST_WAIT_S < FOCUS_MAX_WAIT_S, "it does not back off at all"
    assert FOCUS_QUICK_S < FOCUS_PATIENCE_S, "mid-run is as patient as start-up"

    import inspect

    from jev.run.client import Client as _C

    body = inspect.getsource(_C.approach)
    assert "FOCUS_QUICK_S" in body, "a refused walk waits the start-up patience"


def test_the_strip_grid_is_remembered_across_runs(tmp_path, monkeypatch):
    """A session's first read rests on the grid the last one read the strip on, not on the
    locator, which the scenery behind the strip misled for a whole session (145)."""
    from jev.perceive import radio_frame

    asked = []
    good = radio_frame.Grid(x0=1.0, y0=2.0, dx=3.0, dy=3.0, cell_w=3.0, cell_h=3.0)

    def fake_read(_pixels, prev_seq=None, grid=None):
        asked.append(grid)
        return radio_frame.RadioReading(values={"seq": len(asked)}, ok=True,
                                        fault=radio_frame.SenseFault.NONE,
                                        seq=len(asked), grid=good)

    monkeypatch.setattr(radio_frame, "read", fake_read)
    memory = tmp_path / "radio-grid.json"
    first = Client(hwnd=1, hid=None, cap=_Cap(), origin=(0, 0), size=(1600, 900),
                   grid_memory=memory)
    assert first.reading() is not None and asked == [None]
    assert first.reading() is not None and asked[-1] == good, "remembered within the run"
    second = Client(hwnd=1, hid=None, cap=_Cap(), origin=(0, 0), size=(1600, 900),
                    grid_memory=memory)
    assert second.reading() is not None and asked[-1] == good, "and across runs"
    memory.write_text("not json")
    third = Client(hwnd=1, hid=None, cap=_Cap(), origin=(0, 0), size=(1600, 900),
                   grid_memory=memory)
    assert third.reading() is not None and asked[-1] is None, "a bad file is no memory"
