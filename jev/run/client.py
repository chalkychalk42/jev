"""One live client, wired up. The composition, in one place.

`tools/probe_slice.py` grew this by accretion — window, capture, a decoded read, a frame
read, a position read, an assembled quest log, a follower, and a planner-backed approach —
and it was right to. Every one of them is needed to do anything at all.

It lives here because the second thing that needs it is a second probe, and copying ninety
lines of wiring is how two clients start disagreeing about what "read the strip" means.
Nothing here is a skill: no clicking, no walking decisions, no graph. It hands out the
readers and the one composed action — *stand on a world point* — that every skill above
needs and none of them should know how to build.

The readers are plain callables rather than an interface, because that is what the skills
already take, and it keeps them testable with a lambda.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.clients import win32
from jev.clients.capture import Backend, WindowCapture
from jev.clients.hid import Hid, Humaniser
from jev.clients.travel import Outcome, Travel
from jev.guide.coords import ZoneBounds, map_to_world
from jev.guide.path import PathQuery
from jev.perceive import radio_frame
from jev.perceive.questlog import QuestLog

# Taking the window back: short waits first, doubling, capped.
#
# A flat twenty attempts two seconds apart is forty seconds of standing still inside a
# run, and most focus losses clear on the first try. The long patience is still available
# to whoever is starting up, where a Windows notification panel can genuinely hold the
# foreground for half a minute.
FOCUS_FIRST_WAIT_S = 0.25
FOCUS_MAX_WAIT_S = 4.0
FOCUS_PATIENCE_S = 40.0        # starting up
FOCUS_QUICK_S = 6.0            # mid-run, where standing still costs stations

# How long the strip may repeat a sequence number before it counts as frozen.
#
# Generous, because repeats are normal: the addon paints on a timer and this captures
# faster than it paints, so the same frame is read several times over. Frozen is a
# different thing entirely — the addon has stopped, usually on a Lua error — and it is
# worth naming because **a frozen strip is indistinguishable from a pinned character**.
# Both look like a position that never changes. An hour went into terrain that was never
# the problem before anyone looked at `seq`.
STALE_AFTER_S = 4.0


class NotRunning(RuntimeError):
    """No client window, or it would not come to the foreground."""


@dataclass
class Client:
    """Readers and one composed action for a single game window."""

    hwnd: int
    hid: Hid
    cap: WindowCapture
    origin: tuple[int, int]
    size: tuple[int, int]
    log: QuestLog = field(default_factory=QuestLog)
    _seq: int | None = field(default=None, init=False)
    _seq_at: float = field(default=0.0, init=False)
    travel: Travel | None = field(default=None, init=False)
    query: PathQuery | None = field(default=None, init=False)
    bounds: ZoneBounds | None = field(default=None, init=False)
    on_path: Callable[[str], None] | None = field(default=None, init=False)
    # One window, one capture, one set of GDI handles. `WindowCapture` creates its device
    # context and bitmap once and reuses them, so two threads grabbing at the same time
    # tear each other's frame in half. The heartbeat samples on its own thread, so every
    # path that touches the capture takes this first.
    _capturing: threading.RLock = field(default_factory=threading.RLock, init=False,
                                        repr=False)

    # -- readers -------------------------------------------------------------

    def reading(self, tries: int = 6) -> radio_frame.RadioReading | None:
        """A whole decoded reading, or `None`. Feeds the quest log on the way past.

        `None` also means **frozen**, not only unreadable, and that is deliberate: every
        caller already treats `None` as "cannot see", which is the honest answer for a
        strip whose numbers stopped changing. Returning the last painted values would let
        a follower conclude the character is stuck when the addon is what stopped.
        """
        for _ in range(tries):
            with self._capturing:
                r = radio_frame.read(self.cap.grab().rgb)
            if r.ok:
                self._note_seq(r.values.get("seq"))
                if self.frozen_for() > STALE_AFTER_S:
                    return None
                self.log.observe(r.values)
                return r
            time.sleep(0.05)
        return None

    def _note_seq(self, seq: int | None) -> None:
        now = time.monotonic()
        if seq != self._seq or self._seq_at == 0.0:
            self._seq, self._seq_at = seq, now

    def frozen_for(self) -> float:
        """Seconds since the strip's sequence number last advanced."""
        return 0.0 if self._seq_at == 0.0 else time.monotonic() - self._seq_at

    def read(self) -> dict | None:
        r = self.reading()
        return None if r is None else r.values

    def frame(self):
        try:
            with self._capturing:
                return self.cap.grab().rgb
        except Exception:
            return None

    def position(self) -> tuple[float, float] | None:
        v = self.read()
        if v is None or v.get("pos.mx") is None:
            return None
        return (v["pos.mx"], v["pos.my"])

    def quest_ids(self, tries: int = 40) -> tuple[int, ...] | None:
        """The assembled log, or `None` while the cycle is still partial.

        A partial cycle is unread, never a short log — see `jev.perceive.questlog`.
        """
        for _ in range(tries):
            self.read()
            assembled = self.log.complete
            if assembled is not None:
                return tuple(q.quest_id for q in assembled)
            time.sleep(0.08)
        return None

    def state(self):
        """A `State` carrying the assembled log, which is what the tracker needs.

        `to_state` will not call one frame a log and is right not to, so the accumulated
        one is handed in. Without this the tracker sees an empty log on every tick.
        """
        r = self.reading()
        if r is None:
            return None
        return radio_frame.to_state(r, t=time.time(), client_id="run",
                                    quests=self.log.complete)

    # -- the one composed action ---------------------------------------------

    def approach(self, world: tuple[float, float, float], *,
                 timeout_s: float = 180.0) -> bool:
        """Plan from here to a world point and follow it.

        The planner is the only thing that knows about terrain, and the skills above know
        only where to click. Nine yards of blind walking finds a fence the mesh had
        already routed around.
        """
        if self.travel is None or self.query is None or self.bounds is None:
            return False
        here = self.travel.position()
        if here is None:
            self._say("  cannot read a position")
            return False
        hw = map_to_world(here[0], here[1], self.bounds)
        path = self.query.path(self.bounds.map_id, (hw[0], hw[1], world[2]), world)
        self._say(f"  {path.status.value}: {len(path.points)} waypoints, "
                  f"{path.length_yards():.1f} yards")
        if not path.usable:
            return False

        def replan(here_map):
            w = map_to_world(here_map[0], here_map[1], self.bounds)
            return self.query.path(self.bounds.map_id, (w[0], w[1], world[2]), world)

        result = self.travel.follow(path, timeout_s=timeout_s, replan=replan)
        if result.outcome is Outcome.REFUSED:
            # Nothing was pressed because the window was not focused - a notification
            # panel, or anything else that takes the foreground. `Hid` is right to refuse,
            # and `Travel` is right to say so rather than call it stuck, but somebody has
            # to take the window back. A live run made three kills and then spent twelve
            # stations refused, walking nowhere.
            self._say("  the window lost focus; taking it back")
            if self.focused(FOCUS_QUICK_S):
                result = self.travel.follow(path, timeout_s=timeout_s, replan=replan)
        remaining = ("unknown" if result.remaining_yards is None
                     else f"{result.remaining_yards:.1f} yards")
        self._say(f"  {result.outcome.value}, {remaining} left, {result.turns} turns, "
                  f"{result.stuck_events} stuck"
                  + (f" - {result.detail}" if result.detail else ""))
        return result.outcome.value == "arrived"

    def focused(self, patience_s: float = FOCUS_PATIENCE_S) -> bool:
        """Bring the window forward, and say whether it actually came.

        Not assumed. A Windows notification panel holds the foreground and refuses to give
        it up, and `Hid` correctly declines to type into whatever is focused instead — so
        the caller needs to know the difference between "slow" and "blocked".
        """
        deadline = time.monotonic() + patience_s
        wait = FOCUS_FIRST_WAIT_S
        while True:
            if win32.focus(self.hwnd) and win32.is_foreground(self.hwnd):
                return True
            if time.monotonic() + wait >= deadline:
                return win32.is_foreground(self.hwnd)
            time.sleep(wait)
            wait = min(FOCUS_MAX_WAIT_S, wait * 2)

    def close(self) -> None:
        if self.query is not None:
            self.query.close()
        self.cap.close()

    def _say(self, line: str) -> None:
        if self.on_path is not None:
            self.on_path(line)


def attach(client_id: str = "run", *, title: str = "World of Warcraft",
           backend: Backend = Backend.SCREEN) -> Client:
    """Find the window and wire it up. Raises `NotRunning` rather than returning `None`."""
    if not win32.available():
        raise NotRunning("this needs Windows Python; there is no window on this platform")
    hwnds = win32.find_windows(title)
    if not hwnds:
        raise NotRunning(f"no window matching {title!r}")
    hwnd = hwnds[0]
    ox, oy, w, h = win32.client_rect(hwnd)
    return Client(
        hwnd=hwnd,
        hid=Hid(hwnd=hwnd, humaniser=Humaniser.for_client(client_id)),
        cap=WindowCapture(hwnd, backend=backend),
        origin=(ox, oy),
        size=(w, h),
    )


def with_travel(client: Client, bounds: ZoneBounds, query: PathQuery, *,
                arrival_yards: float, say: Callable[[str], None] | None = None) -> Client:
    """Give a client the ability to walk. Separate because reading needs no planner."""
    client.bounds = bounds
    client.query = query
    client.on_path = say
    client.travel = Travel(hid=client.hid, bounds=bounds,
                           read_pos=client.position, arrival_yards=arrival_yards)
    return client
