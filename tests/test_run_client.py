"""The wiring: the readers every skill is handed, and what `None` is allowed to mean."""

from __future__ import annotations

import time

from jev.run.client import STALE_AFTER_S, Client


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

    def fake_read(_pixels, prev_seq=None):
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
