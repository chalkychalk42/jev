"""Ticks at 2 Hz, including through the stretches where the runtime decides nothing."""

from __future__ import annotations

import threading
import time

from jev.run.heartbeat import Heartbeat
from jev.world.state_v1 import ArmedBy


class _Journal:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()

    def tick(self, state, *, skill=None, intent=None, armed_by=None, keys=None):
        if state is None:
            return None
        with self.lock:
            self.rows.append({"state": state, "skill": skill, "intent": intent,
                              "armed_by": armed_by, "keys": keys})
            return len(self.rows)


def _beat(journal=None, observe=None, keys_down=None, hz=50.0):
    return Heartbeat(journal=journal or _Journal(),
                     observe=observe or (lambda: "state"),
                     keys_down=keys_down, hz=hz)


def _until(predicate, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_rows_arrive_while_the_runtime_does_nothing_at_all():
    """The whole point. A wedge is the stretch in which nothing decides anything, so
    event-driven ticks record everything except the thing the bot is worst at."""
    j = _Journal()
    with _beat(j):
        assert _until(lambda: len(j.rows) >= 3), "no rows without a decision to hang on"


def test_the_armed_skill_is_carried_onto_heartbeat_rows():
    """Otherwise the 2 Hz rows are attributed to nothing and cannot be joined to the
    skill that was running when they were taken."""
    j = _Journal()
    beat = _beat(j)
    beat.arm("TRAVEL", "walk to McBride", ArmedBy.TRACKER)
    with beat:
        assert _until(lambda: j.rows)
    row = j.rows[0]
    assert row["skill"] == "TRAVEL" and row["intent"] == "walk to McBride"
    assert row["armed_by"] is ArmedBy.TRACKER


def test_keys_held_are_recorded_because_that_is_what_makes_a_wedge_visible():
    """"The character did not move" is worth little; "`w` was held and it did not move"
    is a wedge."""
    j = _Journal()
    with _beat(j, keys_down=lambda: ["w", "d"]):
        assert _until(lambda: j.rows)
    assert j.rows[0]["keys"] == ["w", "d"]


def test_an_unreadable_sample_is_skipped_rather_than_written():
    """Absence of a reading is not an observation, and a blind row is indistinguishable
    later from a moment the world held nothing."""
    j = _Journal()
    with _beat(j, observe=lambda: None):
        time.sleep(0.15)
    assert j.rows == []


def test_a_sampler_that_throws_does_not_take_the_run_with_it():
    j = _Journal()
    calls = [0]

    def flaky():
        calls[0] += 1
        if calls[0] % 2:
            raise RuntimeError("capture lost")
        return "state"

    with _beat(j, observe=flaky):
        assert _until(lambda: len(j.rows) >= 2)


def test_stopping_is_idempotent_and_joins_the_thread():
    beat = _beat()
    beat.start()
    beat.start()                      # a second start must not leave a thread orphaned
    beat.stop()
    beat.stop()
    assert beat._thread is None
    assert not any(t.name == "jev-heartbeat" and t.is_alive()
                   for t in threading.enumerate())


def test_a_slow_sample_does_not_queue_up_the_beats_behind_it():
    """Catching up would bunch rows at a moment nothing happened. A missed beat is
    skipped, and counted."""
    j = _Journal()
    beat = Heartbeat(journal=j, observe=lambda: (time.sleep(0.05), "state")[1], hz=100.0)
    with beat:
        assert _until(lambda: len(j.rows) >= 3, seconds=3.0)
    assert beat.slow == 0, "50ms is not slow; SLOW_SAMPLE_S guards against noise"
    assert len(j.rows) < 100, "beats were queued rather than skipped"
