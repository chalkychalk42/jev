"""Closed-loop walking. No navmesh, no absolute facing, no trusted constants.

2.4.3 gives an addon no facing (`DECISIONS.md` V17), so heading cannot be read — only
*measured*, from where the character actually went. That turns out to be the better shape
anyway: a loop that measures its own result does not need the constants to be right.

**Forward is held for the whole journey and turns are pulsed into it.** The first version
of this stopped to take a heading before and after every turn, which meant each correction
dragged the character nine yards in whatever direction it happened to be facing. Live, on
a 33-yard walk to Marshal McBride: 11 turns, **17 stuck events**, and a timeout with ten
yards still to go. Measuring and moving are not separate activities here — the measurement
*is* the movement — so the loop now samples a sliding window of the motion it is already
making, and a turn is a pulse inside a continuous walk. That is also how the character
arcs toward a target instead of pirouetting toward it.

The turn rate is **estimated and then corrected from observation**. A first guess starts
it moving; every pulse compares the heading change it asked for against the one it got.
The number below is a seed, not a calibration.

Everything is in yards along the map's axes (`coords.to_yards`). Raw map fractions are
stretched — 1.5:1 in Elwynn — and an angle taken in that space is wrong by up to eleven
degrees, which quietly contaminated the first turn-rate measurement taken here.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.clients.hid import Hid
from jev.guide.coords import ZoneBounds, distance_yards, heading_yards

TAU = 2 * math.pi

# A seed, not a calibration. Measured once on a level-3 human in Northshire.
TURN_RATE_SEED = math.radians(134.0)

# A heading is taken across a window of recent motion. Long enough that the readout's
# quantisation is small against it, short enough that it describes where the character is
# going *now* rather than where it was going a second ago.
HEADING_WINDOW_S = 0.6
MIN_TRAVEL_FOR_HEADING = 1.5      # yards; below this the angle is noise

MIN_PULSE_S = 0.05
MAX_PULSE_S = 0.45                # one correction, not a whole swing


class Outcome(StrEnum):
    ARRIVED = "arrived"
    STUCK = "stuck"
    TIMEOUT = "timeout"
    LOST = "lost"          # position stopped being readable
    ABORTED = "aborted"    # a caller-supplied abort fired


@dataclass
class TravelResult:
    outcome: Outcome
    start: tuple[float, float] | None
    end: tuple[float, float] | None
    remaining_yards: float | None
    elapsed_s: float
    turns: int
    stuck_events: int
    detours: int
    turn_rate_deg_s: float
    detail: str = ""


@dataclass
class Travel:
    """Walk to a map position, correcting as it goes, without ever stopping."""

    hid: Hid
    bounds: ZoneBounds
    read_pos: Callable[[], tuple[float, float] | None]

    arrival_yards: float = 5.0
    # A single failed decode is not a lost position. Frames get captured mid-paint and
    # tear, and the strip then fails its checksum for exactly one tick — which is the
    # decoder working correctly, not the character disappearing.
    #
    # This cost a whole debugging session. `_unstick` skipped an attempt whenever its
    # "before" read came back None, so one torn frame skipped *all five* recovery
    # attempts in about no time at all, and the run reported "could not free the
    # character" 3.7 seconds in without having tried anything. The loop must distinguish
    # "could not read this frame" from "cannot read at all".
    read_retries: int = 4
    # Frozen for this long with forward held is stuck. Measured on a clean run: the
    # longest frozen stretch was 0.06s and the median step 0.19 yards, so a second and a
    # half is far outside normal and still quick to recover from.
    stuck_after_s: float = 1.5
    stuck_step_yards: float = 0.3
    heading_tolerance: float = math.radians(10.0)
    sample_s: float = 0.04

    # How far to commit along an obstacle before re-aiming. A building corner is the
    # common case in a town and two seconds of walking clears one; less than that and the
    # loop turns straight back into the same wall.
    detour_s: float = 2.0
    max_detours: int = 8

    turn_rate: float = TURN_RATE_SEED
    turns: int = field(default=0, init=False)
    detours: int = field(default=0, init=False)
    closest_yards: float | None = field(default=None, init=False)
    _detour_side: int = field(default=1, init=False)
    stuck_events: int = field(default=0, init=False)
    last_unstick: str = field(default="", init=False)
    _track: deque = field(default_factory=lambda: deque(maxlen=64), init=False)

    # -- geometry ------------------------------------------------------------

    def bearing(self, here, there) -> float | None:
        return heading_yards(here, there, self.bounds)

    def distance(self, a, b) -> float:
        return distance_yards(a, b, self.bounds)

    def position(self) -> tuple[float, float] | None:
        """A position, retried past transient decode failures. `None` means really gone."""
        for attempt in range(self.read_retries):
            p = self.read_pos()
            if p is not None:
                return p
            if attempt + 1 < self.read_retries:
                time.sleep(self.sample_s)
        return None

    def _heading_now(self) -> float | None:
        """Heading over the last `HEADING_WINDOW_S` of motion, or None if too little."""
        if len(self._track) < 2:
            return None
        newest_t = self._track[-1][0]
        window = [s for s in self._track if newest_t - s[0] <= HEADING_WINDOW_S]
        if len(window) < 2:
            return None
        a, b = window[0][1], window[-1][1]
        if self.distance(a, b) < MIN_TRAVEL_FOR_HEADING:
            return None
        return heading_yards(a, b, self.bounds)

    def _moved_since(self, seconds: float) -> float | None:
        """Yards covered over the last `seconds`, or None if the window is not full yet."""
        if not self._track:
            return None
        newest_t = self._track[-1][0]
        window = [s for s in self._track if newest_t - s[0] <= seconds]
        if len(window) < 2 or newest_t - window[0][0] < seconds * 0.8:
            return None
        return self.distance(window[0][1], window[-1][1])

    # -- the loop ------------------------------------------------------------

    def to(self, target, *, timeout_s: float = 90.0,
           abort: Callable[[], bool] | None = None) -> TravelResult:
        t0 = time.perf_counter()
        start = self.position()
        here = start
        self._track.clear()
        pulse_until = 0.0
        pulse_key: str | None = None
        pulse_started_heading: float | None = None
        pulse_len = 0.0

        self.hid.key_down("w")
        try:
            while True:
                now = time.perf_counter()
                elapsed = now - t0
                if elapsed > timeout_s:
                    return self._result(Outcome.TIMEOUT, start, here, target, elapsed,
                                        "ran out of time")
                if abort is not None and abort():
                    return self._result(Outcome.ABORTED, start, here, target, elapsed,
                                        "caller aborted")

                # Single-shot here: the loop samples continuously, so one
                # torn frame simply means no sample this tick.
                p = self.read_pos()
                if p is not None:
                    here = p
                    self._track.append((now, p))

                # Close out a turn pulse and learn from what it did.
                if pulse_key is not None and now >= pulse_until:
                    self.hid.key_up(pulse_key)
                    pulse_key = None
                    observed = self._heading_now()
                    if (observed is not None and pulse_started_heading is not None
                            and pulse_len > 0.15):
                        delta = abs(_wrap(observed - pulse_started_heading))
                        if delta > math.radians(3):
                            self.turn_rate += 0.4 * (delta / pulse_len - self.turn_rate)

                if here is None:
                    return self._result(Outcome.LOST, start, None, target, elapsed,
                                        "position stopped being readable")

                remaining = self.distance(here, target)
                if self.closest_yards is None or remaining < self.closest_yards:
                    self.closest_yards = remaining
                if remaining <= self.arrival_yards:
                    return self._result(Outcome.ARRIVED, start, here, target, elapsed, "")

                moved = self._moved_since(self.stuck_after_s)
                if moved is not None and moved < self.stuck_step_yards:
                    self.stuck_events += 1
                    if pulse_key is not None:
                        self.hid.key_up(pulse_key)
                        pulse_key = None
                    if not self._unstick():
                        return self._result(Outcome.STUCK, start, here, target,
                                            time.perf_counter() - t0,
                                            "could not free the character")
                    # Freed, but pointing at whatever stopped us. Aiming straight at the
                    # target again walks into it again — which is what happened at
                    # Northshire Abbey: the node is Marshal McBride's spawn, the Abbey is
                    # between us and it, and a straight line meets a wall nine times in
                    # forty seconds.
                    #
                    # So go *around*. Turn away from the obstacle and commit to a stretch
                    # before re-aiming. Alternating sides means a dead end on one side is
                    # tried from the other rather than repeated. This is deliberately not
                    # a navmesh (`DECISIONS.md` V5): it clears a corner, and when it
                    # cannot, it fails honestly and says the node needs a recorded route.
                    if self.detours >= self.max_detours:
                        return self._result(
                            Outcome.STUCK, start, here, target,
                            time.perf_counter() - t0,
                            f"{self.detours} detours did not get around it "
                            f"(closest {self.closest_yards:.1f} yards); "
                            "this node needs a recorded route")
                    self._detour(here, target)
                    self._track.clear()
                    self.hid.key_down("w")
                    time.sleep(self.sample_s)
                    continue

                # Steer, but only while actually moving: a heading taken from a character
                # pinned against a wall points wherever the last real step went.
                if pulse_key is None:
                    heading = self._heading_now()
                    if heading is not None:
                        want = self.bearing(here, target)
                        if want is not None:
                            error = _wrap(want - heading)
                            if abs(error) > self.heading_tolerance:
                                pulse_len = min(MAX_PULSE_S,
                                                abs(error) / max(self.turn_rate, 0.1))
                                if pulse_len >= MIN_PULSE_S:
                                    pulse_key = "d" if error > 0 else "a"
                                    pulse_started_heading = heading
                                    pulse_until = now + pulse_len
                                    self.hid.key_down(pulse_key)
                                    self.turns += 1

                time.sleep(self.sample_s)
        finally:
            self.hid.release_all()

    def _detour(self, here, target) -> None:
        """Turn off the direct line and walk along the obstacle for a while.

        The side is kept until it stops helping. The first version alternated on every
        detour, which is not "try the other way", it is "oscillate": eight detours took
        the character from ten yards out to twenty-nine, each one undoing the last. A
        detour that makes the distance worse flips the side; one that helps is repeated.
        """
        before = self.distance(here, target)
        self.detours += 1
        key = self.hid.TURN_RIGHT if self._detour_side > 0 else self.hid.TURN_LEFT
        # About a right angle at the current estimated rate: enough to clear a wall face
        # rather than scrape along it.
        self.hid.hold(key, min(0.9, (math.pi / 2) / max(self.turn_rate, 0.1)),
                      tick_s=self.sample_s)
        self.hid.hold("w", self.detour_s, tick_s=self.sample_s)

        after_pos = self.position()
        if after_pos is None:
            return
        if self.distance(after_pos, target) > before:
            self._detour_side *= -1

    def _unstick(self) -> bool:
        """Try the things that actually free a character, cheapest first.

        Measured on a character genuinely pinned in Northshire:

            forward        0.00 yards   (blocked)
            back           3.91 yards
            turn left      0.00 yards   (A turns; it does not strafe)
            turn right     0.00 yards
            jump + forward 3.39 yards   <- cleared it

        So jump-forward leads: most of what stops a character in this game is a step, a
        root or a fence edge, and a jump clears all three without giving up the ground
        already gained. Backing off is second because it always works and always costs
        progress. Strafing is last and uses **Q/E** — the earlier version strafed with D,
        which turns rather than strafes and therefore moved the character nowhere at all
        while reporting a failed recovery.

        Each attempt is measured, and the first one that moves us wins.
        """
        self.hid.release_all()
        self.last_unstick = ""

        attempts = (
            ("jump-forward", lambda: (self.hid.tap("space"), time.sleep(0.15),
                                      self.hid.hold("w", 0.8, tick_s=self.sample_s))),
            ("back", lambda: self.hid.hold("s", 0.5, tick_s=self.sample_s)),
            ("strafe-right", lambda: self.hid.hold(self.hid.STRAFE_RIGHT, 0.6,
                                                   tick_s=self.sample_s)),
            ("strafe-left", lambda: self.hid.hold(self.hid.STRAFE_LEFT, 0.6,
                                                  tick_s=self.sample_s)),
            ("back-and-turn", lambda: (self.hid.hold("s", 0.5, tick_s=self.sample_s),
                                       self.hid.hold("d", 0.4, tick_s=self.sample_s))),
        )

        for name, attempt in attempts:
            before = self.position()
            if before is None:
                return False            # genuinely cannot see; recovery is not the fix
            attempt()
            time.sleep(0.25)
            after = self.position()
            if after is None:
                return False
            if self.distance(before, after) > self.stuck_step_yards:
                self.last_unstick = name
                return True
        return False

    def _result(self, outcome: Outcome, start, end, target, elapsed: float,
                detail: str) -> TravelResult:
        return TravelResult(
            outcome=outcome, start=start, end=end,
            remaining_yards=None if end is None else self.distance(end, target),
            elapsed_s=elapsed, turns=self.turns, stuck_events=self.stuck_events,
            detours=self.detours,
            turn_rate_deg_s=math.degrees(self.turn_rate), detail=detail,
        )


def _wrap(a: float) -> float:
    """Signed angle difference in (-pi, pi]."""
    return (a + math.pi) % TAU - math.pi
