"""Where a `State` comes from.

One protocol, several implementations, and the runtime cannot tell them apart. That is
deliberate: it means the whole brain — tracker, coach, verifier, recorder, teacher queue —
runs with no game, no capture and no Windows, which is the difference between a project
you can work on and one you can only work on at the machine with the client open.

    ClientSource    capture -> radio decode (`jev.run.client`)      (needs the game)
    ReplaySource    a recorded run, played back                     (needs a run)
    ScriptedSource  a hand-written sequence of states               (needs nothing)

`Source.read()` returns the best `State` it can and says how much of it to believe. It
never raises for a bad frame: a frame it could not read is a `State` with `addon_ok=False`
and a fault, because "I could not see" is an observation the coach can act on, while an
exception is a client that stops.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from jev.world.state_v1 import Sense, SenseFault, State
from jev.world.state_v1 import Source as StateSource


class Source(Protocol):
    """Anything that can produce a state when asked."""

    def read(self) -> State: ...
    def close(self) -> None: ...


class ScriptedSource:
    """A fixed sequence of states, for tests and for developing the brain with no game.

    Repeats its last state once exhausted rather than raising. A source that runs out is
    not an error — a real one never does — and ending the run on it would make every test
    about the length of its fixture.
    """

    def __init__(self, states: Sequence[State]) -> None:
        if not states:
            raise ValueError("a scripted source needs at least one state")
        self._states = list(states)
        self._i = 0

    def read(self) -> State:
        state = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return state

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._states)

    def close(self) -> None:
        return


class ReplaySource:
    """Replays the tick stream of a recorded run.

    The point is not nostalgia: a bug that only appears after forty minutes of live play
    is unfixable if the only way to reach it is forty minutes of live play.
    """

    def __init__(self, rows: Iterator[dict]) -> None:
        self._rows = rows
        self._last: State | None = None

    def read(self) -> State:
        row = next(self._rows, None)
        if row is None:
            if self._last is None:
                raise ValueError("replay is empty")
            return self._last
        self._last = State.model_validate(row["state"])
        self._last = self._last.model_copy(update={
            "sense": self._last.sense.model_copy(update={"source": StateSource.REPLAY}),
        })
        return self._last

    def close(self) -> None:
        return


def blind(t: float, client_id: str, fault: SenseFault,
          source: StateSource = StateSource.VISION) -> State:
    """A state that admits to having seen nothing.

    Returned instead of raising when a frame cannot be read. Every field stays `None`, so
    nothing downstream mistakes a failed read for a negative observation — the coach sees
    a blind character and has rules for that, which is far better than a client that
    stops on an exception.
    """
    return State(
        t=t, client_id=client_id,
        sense=Sense(addon_ok=False, fault=fault, source=source, vision_conf=0.0),
    )
