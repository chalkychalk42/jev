"""Whether a person is using the desktop: the one thing the bot must never compete with.

Windows keeps the time of the last input from anyone (`GetLastInputInfo`), on the same
millisecond tick as `GetTickCount`. The bot's own input moves it too, so every injection is
stamped (`stamp`): in this process, and in a small file the next session reads, since a
session starts knowing nothing of the one before it. Input newer than the bot's own last by
more than `MARGIN_MS` came from somebody else.

Before this, the bot could not tell a person's input from its own: in the minute before an
operator stop on 24 September it pulled the game back to the foreground eight times while
the operator was trying to use the machine. Now a person's input pauses it - no input sent,
no window raised - until `QUIET_S` has passed without any.
"""

from __future__ import annotations

import atexit
import os
import time
from collections.abc import Callable
from pathlib import Path

# Windows stamps each event at its own tick; ours lands within a few ms of the call.
MARGIN_MS = 300
# How long a person's input keeps the bot paused. Long, because a person at the desk
# reads, thinks and types in bursts; `JEV_OPERATOR_QUIET_S` shortens it for a live test.
QUIET_S = float(os.environ.get("JEV_OPERATOR_QUIET_S", "600"))
# The file stamp is refreshed at most this often; the in-process one every injection.
STAMP_EVERY_S = 0.5
# A person is `CONFIRM_INPUTS` inputs that are not the bot's within `CONFIRM_MS`. One
# alone can be the machine's: a lone input 391 ms after the bot's own, then nothing for
# minutes with nobody at the desk, paused session 91 mid-fight and the character died
# (24 September), and session 102 saw three more, 650-750 ms after the bot's own, each
# alone. A hand on a mouse or a keyboard makes many.
CONFIRM_MS = 10_000
CONFIRM_INPUTS = 3
WRAP = 1 << 32


def _stamp_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return root / "Jev" / "bot-input.tick"


class Operator:
    def __init__(self, *, last_input_tick: Callable[[], int | None],
                 tick_now: Callable[[], int], stamp_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, quiet_s: float | None = None,
                 say: Callable[[str], None] | None = None):
        self._last_input_tick, self._tick_now = last_input_tick, tick_now
        self._say = say
        self._path, self._clock = stamp_path, clock
        self.quiet_s = QUIET_S if quiet_s is None else quiet_s
        self._ours: int | None = self._read_stamp()
        if self._ours is not None and (tick_now() - self._ours) % WRAP >= WRAP // 2:
            self._ours = None            # ahead of the clock: written before a restart
        # The file is written at most every `STAMP_EVERY_S`, so the last session's final
        # input can be up to that much newer than its stamp: allowed for until ours arrives.
        self._slack_ms = STAMP_EVERY_S * 1000
        if self._ours is None:
            # Nothing known of earlier input: what came before this process is not a person's.
            self._ours, self._slack_ms = tick_now(), 0
        self._human: int | None = None
        self._odd: int | None = None     # the last input seen that was not the bot's
        self._odds: list[int] = []       # the recent ones, for `CONFIRM_INPUTS`
        self._last_label = ""            # what the bot last sent, for the log
        self._written = -float("inf")
        self._unwritten = False

    def _read_stamp(self) -> int | None:
        if self._path is None:
            return None
        try:
            return int(self._path.read_text().strip()) % WRAP
        except (OSError, ValueError):
            return None

    def stamp(self, label: str = "") -> None:
        """The bot has just injected input (`label`: what, for the log)."""
        self._ours, self._slack_ms = self._tick_now(), 0
        self._last_label = label
        now = self._clock()
        self._unwritten = True
        if now - self._written >= STAMP_EVERY_S:
            self._written = now
            self.flush()

    def flush(self) -> None:
        """Write the latest stamp for the next session, if one is unwritten."""
        if self._path is None or not self._unwritten:
            return
        self._unwritten = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(str(self._ours))
        except OSError:
            pass

    def age_s(self) -> float | None:
        """Seconds since input that was not the bot's; `inf` if none seen; `None` unread."""
        last = self._last_input_tick()
        if last is None:
            return None
        ours = self._ours if self._ours is not None else last
        after = (last - ours) % WRAP
        if MARGIN_MS + self._slack_ms < after < WRAP // 2 and last != self._odd:
            self._odds = [t for t in self._odds if (last - t) % WRAP <= CONFIRM_MS] + [last]
            confirmed = len(self._odds) >= CONFIRM_INPUTS
            present = (self._human is not None
                       and (self._tick_now() - self._human) % WRAP < self.quiet_s * 1000)
            if self._say is not None and not present:
                self._say(f"operator: {time.strftime('%H:%M:%S')} input {after} ms after the "
                          f"bot's own ({self._last_label or 'unlabelled'}); "
                          + ("a person" if confirmed
                             else f"{len(self._odds)} of {CONFIRM_INPUTS}, not yet a person"))
            if confirmed:
                self._human = last
            self._odd = last
        if self._human is None:
            return float("inf")
        return ((self._tick_now() - self._human) % WRAP) / 1000

    def active(self) -> bool:
        """A person has used the desktop within `quiet_s`."""
        age = self.age_s()
        return age is not None and age < self.quiet_s


_default: Operator | None = None


def default() -> Operator | None:
    """This process's operator watch, on Windows; `None` elsewhere."""
    global _default
    if _default is None:
        from jev.clients import win32

        if not win32.available():
            return None
        _default = Operator(last_input_tick=win32.last_input_tick, tick_now=win32.tick_now,
                            stamp_path=_stamp_path(), say=print)
        atexit.register(_default.flush)
    return _default


def stamp(label: str = "") -> None:
    watch = default()
    if watch is not None:
        watch.stamp(label)


def active() -> bool:
    watch = default()
    return watch is not None and watch.active()
