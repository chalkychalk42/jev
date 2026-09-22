"""`LiveSource` — a real window as a `Source`.

    capture -> radio decode -> state_v1

The runtime cannot tell this apart from `ScriptedSource` or `ReplaySource`, which is the
whole point of the protocol: everything above here has already been exercised for
thousands of simulated ticks before a window was ever involved.

What this file deliberately does *not* do is fail. A window that vanished, a black frame,
a strip that could not be located, a checksum that did not hold — each returns a state
that says so. "I could not see" is an observation the coach has rules for; an exception
is a client that stops, and a client that stops mid-run takes its corpus tail with it.

Vision is not here yet. Until it is, `LiveSource` is radio-only: numbers come from the
strip and windows (loot, gossip, vendor, a blocking modal) are simply unobserved rather
than assumed absent. That is the honest shape — `ui.loot` is `None`, not `False` — and it
means the coach treats an addon-off client as blind instead of confidently wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from jev.clients import win32
from jev.clients.capture import Backend, CaptureError, WindowCapture
from jev.clients.source import blind
from jev.perceive import radio_frame
from jev.perceive.questlog import QuestLog
from jev.world.state_v1 import SenseFault, State
from jev.world.state_v1 import Source as StateSource


@dataclass
class LiveStats:
    """Enough to tell a perception problem from a game problem, without a debugger."""

    frames: int = 0
    decoded: int = 0
    log_cycles: int = 0
    black: int = 0
    not_found: int = 0
    checksum: int = 0
    stale: int = 0
    lost_window: int = 0

    @property
    def decode_rate(self) -> float | None:
        return None if not self.frames else self.decoded / self.frames


@dataclass
class LiveSource:
    """One bound game window, read at whatever rate the caller ticks it."""

    hwnd: int
    client_id: str = "c01"
    backend: Backend = Backend.SCREEN
    stats: LiveStats = field(default_factory=LiveStats)
    _cap: WindowCapture | None = field(default=None, init=False)
    _prev_seq: int | None = field(default=None, init=False)
    # The strip paints one log entry per frame, so the log exists across frames rather
    # than in any one of them. This is the only thing that turns those into a log.
    _log: QuestLog = field(default_factory=QuestLog, init=False)

    def __post_init__(self) -> None:
        self._cap = WindowCapture(self.hwnd, backend=self.backend)

    # -- the Source protocol -------------------------------------------------

    def read(self) -> State:
        t = time.time()
        self.stats.frames += 1

        try:
            frame = self._cap.grab()
        except CaptureError as exc:
            # Black frames and a vanished window are different problems with the same
            # shape here, so the fault says which and the counters keep them apart.
            if "black" in str(exc):
                self.stats.black += 1
                return blind(t, self.client_id, SenseFault.NOT_FOUND)
            self.stats.lost_window += 1
            return blind(t, self.client_id, SenseFault.NOT_FOUND)

        reading = radio_frame.read(frame.rgb, prev_seq=self._prev_seq)
        seq = reading.values.get("seq") if reading.values else None
        if seq is not None:
            self._prev_seq = seq

        if not reading.ok:
            match reading.fault:
                case SenseFault.NOT_FOUND:
                    self.stats.not_found += 1
                case SenseFault.CHECKSUM:
                    self.stats.checksum += 1
                case SenseFault.STALE:
                    self.stats.stale += 1
            # A stale strip still carries its last painted values, and those are evidence
            # about where the addon hung. `to_state` keeps them; the sense block says
            # not to trust them.
            if reading.values:
                return radio_frame.to_state(reading, t=t, client_id=self.client_id)
            return blind(t, self.client_id, reading.fault)

        self.stats.decoded += 1
        state = radio_frame.to_state(reading, t=t, client_id=self.client_id)
        # `to_state` refuses to build a log from one frame and says `None` for anything
        # non-empty. Here there is history, so the assembled log — complete or not yet —
        # replaces it.
        return state.model_copy(update={
            "quests": self._log.observe(reading.values),
            "sense": state.sense.model_copy(update={"source": StateSource.RADIO}),
        })

    @property
    def quest_log_progress(self) -> tuple[int, int | None]:
        """Slots seen against slots expected, so a postmortem can say whether the log was
        ever actually read rather than leaving it to be inferred."""
        return self._log.progress

    def close(self) -> None:
        if self._cap is not None:
            self._cap.close()


def bind(title: str = "World of Warcraft", client_id: str = "c01",
         index: int | None = None, backend: Backend = Backend.SCREEN) -> LiveSource:
    """Find the game window and bind to it.

    Raises rather than guessing when there is no window or more than one and no index was
    chosen. Binding the wrong window is a failure that presents as "the bot does nothing"
    for as long as it takes someone to notice which window has focus.
    """
    hwnd = win32.game_window(title, index=index)
    return LiveSource(hwnd=hwnd, client_id=client_id, backend=backend)
