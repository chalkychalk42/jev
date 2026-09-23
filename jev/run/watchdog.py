"""Bounded liveness from observations, never a guessed movement or unstick heading."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from jev.clients.session import Session, credentials
from jev.learn.episode import SkillOutcome
from jev.run.supervisor import FocusLost, Result


@dataclass
class Watchdog:
    blind_grace_s: float = 20
    no_progress_s: float = 900
    reconnect_limit: int = 3
    reconnect_backoff_s: float = 60
    reconnect: Callable | None = None
    blind_since: float | None = None
    progress_at: float | None = None
    attempts: int = 0
    next_reconnect: float = 0
    reconnecting: bool = False
    failure: str | None = None
    _facts: tuple | None = None
    # The first window without progress fails the step being worked, into its own
    # `on_fail` edge, and only a second one stops the run. The grind a failed step goes to
    # earns experience, which is progress, so a run stalled on one quest carries on.
    escalate: bool = False       # a request for the supervisor, consumed by it
    escalated: bool = False
    _quests: tuple | None = None
    _level: int | None = None
    _xp: float | None = None

    def observe(self, state, now):
        if not state.sense.addon_ok:
            if self.blind_since is None:
                self.blind_since = now
            if now - self.blind_since >= self.blind_grace_s:
                if self.reconnect is None:
                    self.failure = "perception unavailable beyond grace; playhead preserved"
                elif (not self.reconnecting and self.attempts >= self.reconnect_limit
                      and now >= self.next_reconnect):
                    self.failure = "reconnect attempt budget exhausted; playhead preserved"
            return
        self.blind_since = None
        # Movement and new decisions alone are not progress: circling cannot reset this.
        if state.quests is not None:
            self._quests = tuple((q.quest_id, q.complete, tuple((o.have, o.need) for o in q.objectives))
                                 for q in state.quests)
        if state.char.level is not None:
            self._level = state.char.level
        if state.char.xp_pct is not None:
            self._xp = state.char.xp_pct
        # A fail/rib/rejoin cycle moves the playhead without earning anything. Its
        # changing step IDs must not keep a wedged route alive indefinitely.
        facts = (self._level, self._xp, self._quests)
        if facts != self._facts or self.progress_at is None:
            self._facts, self.progress_at = facts, now
            self.escalated = False
        elif now - self.progress_at >= self.no_progress_s:
            if not self.escalated:
                self.escalate = self.escalated = True
                self.progress_at = now
            else:
                self.failure = ("no quest or experience progress within watchdog window; "
                                "playhead preserved")

    def maintenance(self, now):
        if (self.failure or self.reconnecting or self.reconnect is None or self.blind_since is None
                or now - self.blind_since < self.blind_grace_s
                or now < self.next_reconnect or self.attempts >= self.reconnect_limit):
            return None
        self.attempts += 1
        self.reconnecting = True
        self.next_reconnect = now + self.reconnect_backoff_s
        return self.reconnect

    def completed(self, now):
        """The sole input worker has returned; only now can another retry be due."""
        self.reconnecting = False
        self.next_reconnect = now + self.reconnect_backoff_s


def reconnect_client(client, checkpoint, *, env_file=None) -> Result:
    """Compose the already measured Session under the supervisor's sole input worker."""
    secret = credentials(path=env_file)
    if secret is None:
        return Result(SkillOutcome.ABORTED, "reconnect credentials unavailable", "credentials")

    def guarded():
        checkpoint()
        if not client.hid.ready():
            raise FocusLost("focus lost during reconnect")

    client.hid.checkpoint = guarded
    session = Session(client.hid, client.frame, lambda: client.read() is not None,
                      client.origin, client.size, checkpoint=guarded)
    started = time.monotonic()
    def bounded():
        guarded()
        if time.monotonic() - started > 180:
            raise TimeoutError("reconnect deadline")
    session.checkpoint = bounded
    if session.sign_in(*secret):
        with client._capturing:
            client.log.reset()
        return Result(SkillOutcome.SUCCEEDED, "radio restored; resuming saved playhead", "reconnected")
    return Result(SkillOutcome.ABORTED, session.detail, "reconnect_failed")
