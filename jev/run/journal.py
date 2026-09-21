"""The flight recorder, wired to the runtime.

`jev.learn.episode` has had a complete store since the beginning - ticks, decisions,
grades, skill results, reward and grading functions, its own tests - and nothing ever
called it. The first successful slice, accept through ten kills to turn-in, was recorded
nowhere. Its own docstring says why that is the expensive kind of missing:

    Written from the first run or distillation is impossible later.

A run can be repeated; the corpus of a run cannot be recovered afterwards. So this exists
to make recording the default rather than a thing somebody remembers to do.

What it is not
--------------
Not a learner, not a policy, not a teacher. It writes down what happened and who decided
it. `armed_by` is `TRACKER` for everything at the moment, because everything *is* armed
mechanically by the playhead - and writing that down honestly now is what makes it
possible to tell later which rows came from a policy instead.

Rate
----
2 Hz, plus the runtime's own decision points. `Heartbeat` does the 2 Hz half on its own
thread; this file is what both of them write through.

An earlier version of this note argued against a fixed rate, on the grounds that "claiming
a clean 2 Hz by resampling would be inventing rows that were never observed". That is a
good argument against resampling and not one against sampling: a thread that genuinely
reads the strip twice a second observes every row it writes. The distinction mattered,
because event-only ticks record the moments the runtime *decided* something and a wedge is
precisely the period in which it decides nothing. Three minutes of a ghost turning 180
times against a fence produced no rows at all.

Both threads write here, so the recorder is taken under a lock. It is not thread-safe and
a torn parquet row is unrecoverable in the same way the missing ones were.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field

from jev.coach.situation import situation_key
from jev.learn.episode import Recorder, SkillOutcome, SkillResultRow, TickRow
from jev.world.state_v1 import ArmedBy, State


@dataclass
class Journal:
    """Writes ticks and skill outcomes for one client. Never raises at a call site.

    A recorder that throws takes the run with it, and a run is worth more than a row.
    Failures are counted and reported at the end rather than propagated.
    """

    recorder: Recorder
    client_id: str = "run"
    ticks: int = field(default=0, init=False)
    skills: int = field(default=0, init=False)
    dropped: int = field(default=0, init=False)
    _writing: threading.RLock = field(default_factory=threading.RLock, init=False,
                                      repr=False)

    @property
    def run_id(self) -> str:
        return self.recorder.run_id

    def tick(self, state: State | None, *, skill: str | None = None,
             intent: str | None = None, armed_by: ArmedBy = ArmedBy.TRACKER,
             keys: list[str] | None = None) -> int | None:
        """One observation. `None` when there was nothing readable to record.

        A blind tick is not written: absence of a reading is not an observation of the
        world, and a row whose state is empty would be indistinguishable later from a
        moment when the world genuinely held nothing.
        """
        if state is None:
            return None
        try:
            with self._writing:
                tick_id = self.recorder.next_tick_id()
                self.recorder.tick(TickRow(
                    run_id=self.run_id, tick_id=tick_id, t=state.t,
                    client_id=self.client_id, state=state.model_dump(mode="json"),
                    situation_key=situation_key(state),
                    armed_skill=skill, armed_intent=intent, armed_by=armed_by,
                    keys=list(keys or ()),
                ))
                self.ticks += 1
            return tick_id
        except Exception:
            self.dropped += 1
            return None

    def skill(self, name: str, outcome: SkillOutcome, *, started_at: float,
              state: State | None = None, step_id: str | None = None,
              detail: str | None = None,
              armed_by: ArmedBy = ArmedBy.TRACKER) -> None:
        """How one armed skill ended, and how long it took.

        `started_at` is a `time.monotonic()` reading and the duration is measured against
        the same clock. Mixing it with `time.time()` produced a first row claiming a
        skill took fifty-six years, which is the kind of number a corpus keeps forever.
        The row's `t` stays wall-clock, because that is what joins to everything else.
        """
        try:
            with self._writing:
                self.recorder.skill_result(SkillResultRow(
                    run_id=self.run_id, client_id=self.client_id, t=time.time(),
                    tick_id=self.recorder._tick_id, skill=name, armed_by=armed_by,
                    outcome=outcome, duration_s=max(0.0, time.monotonic() - started_at),
                    situation_key=situation_key(state) if state is not None else "",
                    step_id=step_id, detail=detail,
                ))
                self.skills += 1
        except Exception:
            self.dropped += 1

    def summary(self) -> str:
        return (f"{self.ticks} ticks, {self.skills} skill outcomes"
                + (f", {self.dropped} dropped" if self.dropped else "")
                + f" -> {self.recorder.dir}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.recorder.close()


def outcome_of(ok: bool, *, timed_out: bool = False,
               aborted: bool = False) -> SkillOutcome:
    """Map a skill's own verdict onto the store's vocabulary.

    `UNKNOWN` is deliberately absent here: a caller that knows whether the thing worked
    should say so, and a caller that does not should not be guessing at this layer.
    """
    if ok:
        return SkillOutcome.SUCCEEDED
    if timed_out:
        return SkillOutcome.TIMED_OUT
    if aborted:
        return SkillOutcome.ABORTED
    return SkillOutcome.ABORTED
