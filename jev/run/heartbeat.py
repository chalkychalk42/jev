"""Ticks at 2 Hz, on their own thread, whatever the runtime happens to be doing.

ARCH §4 asks for `ticks` at *2 Hz + events* and the runtime only ever supplied the
events. That sounds like a rate detail and is not one: an event tick records a moment the
runtime **decided** something, and a wedge is precisely the period in which it decides
nothing. Three minutes of a ghost turning 180 times against a Northshire fence produced a
single row — the one written before it set off — so the corpus contains every decision
the bot has ever made and almost nothing about the thing it is worst at.

The corpus cannot be backfilled. A run can be repeated; what a run saw cannot.

What a heartbeat tick carries
-----------------------------
The same row an event tick carries: `state_v1`, `situation_key`, the armed skill and
`armed_by`, and the keys held at that instant. The last is what turns "the character did
not move" into "`w` was held for six seconds and the character did not move", which is
the difference between an observation and a wedge.

`arm()` is how the loop says what it is doing, so heartbeat rows are attributed to the
same skill as the events around them rather than to nothing.

Sampling, not resampling
------------------------
This reads the strip twice a second and writes what it read. It does not interpolate
between event ticks, which would be inventing rows that were never observed — the
objection the journal used to raise against a fixed rate, and a correct objection to a
different design.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.run.journal import Journal
from jev.world.state_v1 import ArmedBy

HZ = 2.0

# A sample that takes longer than this is a sign the capture is contended, not that the
# machine is slow: worth counting rather than worth chasing every tick.
SLOW_SAMPLE_S = 0.5


@dataclass
class Heartbeat:
    """Samples and records on a thread. Start it once, stop it in a `finally`."""

    journal: Journal
    observe: Callable[[], object | None]
    keys_down: Callable[[], list[str]] | None = None
    hz: float = HZ

    ticks: int = field(default=0, init=False)
    slow: int = field(default=0, init=False)
    _skill: str | None = field(default=None, init=False)
    _intent: str | None = field(default=None, init=False)
    _armed_by: ArmedBy = field(default=ArmedBy.TRACKER, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def arm(self, skill: str | None, intent: str | None = None,
            armed_by: ArmedBy = ArmedBy.TRACKER) -> None:
        """What the runtime is doing now, so the rows in between are attributed to it."""
        with self._lock:
            self._skill, self._intent, self._armed_by = skill, intent, armed_by

    def start(self) -> Heartbeat:
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jev-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def __enter__(self) -> Heartbeat:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _run(self) -> None:
        period = 1.0 / max(0.1, self.hz)
        due = time.monotonic()
        while not self._stop.is_set():
            due += period
            self._sample()
            # Against the schedule rather than sleeping a fixed period, so a slow sample
            # does not push every later one back with it. A missed beat is skipped, not
            # queued: catching up would bunch rows at a moment nothing happened.
            wait = due - time.monotonic()
            if wait <= 0:
                due = time.monotonic()
                continue
            self._stop.wait(wait)

    def _sample(self) -> None:
        started = time.monotonic()
        try:
            state = self.observe()
        except Exception:
            return                          # a blind sample is not worth taking the run
        if time.monotonic() - started > SLOW_SAMPLE_S:
            self.slow += 1
        with self._lock:
            skill, intent, armed_by = self._skill, self._intent, self._armed_by
        keys = None
        if self.keys_down is not None:
            try:
                keys = self.keys_down()
            except Exception:
                keys = None
        if self.journal.tick(state, skill=skill, intent=intent, armed_by=armed_by,
                             keys=keys) is not None:
            self.ticks += 1
