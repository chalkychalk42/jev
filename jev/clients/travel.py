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
from jev.guide.coords import (
    ZoneBounds,
    distance_yards,
    heading_yards,
    world_to_map,
)

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
    heading_tolerance: float = math.radians(10.0)     # docking, near the target
    cruise_tolerance: float = math.radians(22.0)      # underway, with room to absorb it
    sample_s: float = 0.04

    # How far to commit along an obstacle before re-aiming. A building corner is the
    # common case in a town and two seconds of walking clears one; less than that and the
    # loop turns straight back into the same wall.
    detour_s: float = 2.0
    max_detours: int = 8
    # Tight, and deliberately tighter than the spacing Detour produces around buildings.
    # See `follow`.
    waypoint_arrival_yards: float = 3.0

    turn_rate: float = TURN_RATE_SEED
    turns: int = field(default=0, init=False)
    detours: int = field(default=0, init=False)
    closest_yards: float | None = field(default=None, init=False)
    _detour_side: int = field(default=1, init=False)
    _pulse_ended_at: float = field(default=0.0, init=False)
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
        """Heading over the last window of **unturned** motion, or None if too little.

        The exclusion of samples from during and before a turn is the whole point, and
        leaving it out made the follower an oscillator. A pulse arcs the character, so a
        window that spans the pulse measures the arc: the heading reads past the target,
        the error flips sign, and the next tick corrects the other way. Fifty turns in
        thirty-seven seconds — a pulse every 0.7 s — with a three-point path that a person
        would have walked as two straight lines.

        So a heading is only taken once the window is full of motion made while going
        straight. Until then there is no answer, and no answer means no steering.
        """
        if len(self._track) < 2:
            return None
        newest_t = self._track[-1][0]
        floor = max(newest_t - HEADING_WINDOW_S, self._pulse_ended_at)
        window = [s for s in self._track if s[0] >= floor]
        if len(window) < 2:
            return None
        # A partial window is a partial arc. Wait for a full one rather than steer on it.
        if window[-1][0] - window[0][0] < HEADING_WINDOW_S * 0.8:
            return None
        a, b = window[0][1], window[-1][1]
        if self.distance(a, b) < MIN_TRAVEL_FOR_HEADING:
            return None
        return heading_yards(a, b, self.bounds)

    # There was a `last_heading()` here, and a `take_heading()`, and both were wrong for
    # the same reason: **there is no facing on arrival**. Heading is a 0.6 s window over
    # motion, and on arrival the character has stopped — so the window is either stale or
    # it is the last arc *into* the point. Treating it as "which way am I looking" is the
    # same shape of lie as reading a leg's remaining distance as the distance to the NPC.
    #
    # Facing is not measured. It is *arranged*: walk toward the thing and stop the instant
    # the client says you are in range. See `jev.clients.interact.approach`.

    def _deadband(self, remaining_yards: float) -> float:
        """How wrong the heading may be before it is worth a pulse.

        Ten degrees is a docking tolerance and far too tight to cruise with: the position
        readout quantises at about 0.12 yards and a heading needs 1.5 yards of travel
        behind it, so ten degrees is inside the noise on a long leg and every tick finds
        a reason to twitch. Wide while there is distance to absorb it, tight on approach.
        """
        return self.heading_tolerance if remaining_yards <= 12.0 else self.cruise_tolerance

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
           abort: Callable[[], bool] | None = None,
           allow_detour: bool = True) -> TravelResult:
        """Walk at a point.

        `allow_detour=False` on a planned leg. `_detour` is the wall heuristic for a
        straight-line walk at a raw node; on a navmesh polyline it is the follower
        arguing with the planner, and it showed up as three detours on a route that had
        already been solved. Unstick still runs — a fence or a root is a fence or a root
        — but a blocked leg is reported so the caller can ask the planner again from
        where the character actually is. That is the engine; detour-as-router is the
        band-aid it replaced.
        """
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
                    self._pulse_ended_at = now
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
                    if not allow_detour:
                        return self._result(
                            Outcome.STUCK, start, here, target,
                            time.perf_counter() - t0,
                            "blocked on a planned leg; re-plan from here")
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
                            if abs(error) > self._deadband(self.distance(here, target)):
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

    def follow(self, path, *, timeout_s: float = 300.0,
               abort: Callable[[], bool] | None = None,
               replan: Callable[[tuple[float, float]], object] | None = None,
               max_replans: int = 3) -> TravelResult:
        """Walk a planned route, one waypoint at a time.

        Sequencing only. The follower is unchanged and learns nothing new about geometry:
        each leg is the same straight walk it has always done, and the planner is what
        made the legs straight. A route around Northshire Abbey is three points, and the
        middle one is the corner — so the follower never has to know the Abbey is there.

        Intermediate waypoints get a **tight** arrival radius, not a loose one. That is
        the opposite of the obvious choice and it was learned the hard way: Detour emits
        waypoints at polygon portals, which around a building come as close as 1.9 yards
        apart, so an 8-yard "close enough" radius marked most of the route as already
        reached. The follower skipped to a distant waypoint, cut the corner, and walked
        into the Abbey it had just been routed around — eight legs of nine completed and
        the last one failing at the wall the planner existed to avoid.

        Waypoints *are* the route. Passing near one is not the same as following it.

        A blocked leg asks the planner again from where the character actually is, rather
        than turning ninety degrees and hoping. The mesh knows about the door; the
        follower does not and should not learn.
        """
        if not path.usable:
            return self._result(Outcome.STUCK, self.position(), self.position(),
                                self.position() or (0.0, 0.0), 0.0,
                                f"no usable path: {path.status.value} {path.detail}".strip())

        t0 = time.perf_counter()
        legs = [world_to_map(pt[0], pt[1], self.bounds) for pt in path.points]
        legs = [leg for leg in legs if leg is not None]
        legs = _thin(legs, self.bounds, self.waypoint_arrival_yards)
        exact = self.arrival_yards
        last: TravelResult | None = None
        replans = 0

        for i, leg in enumerate(legs[1:], start=1):     # legs[0] is where we already are
            final = i == len(legs) - 1
            self.arrival_yards = exact if final else self.waypoint_arrival_yards
            self.closest_yards = None       # per leg, or the number means nothing
            remaining = timeout_s - (time.perf_counter() - t0)
            if remaining <= 0:
                self.arrival_yards = exact
                return self._result(Outcome.TIMEOUT, legs[0], self.position(), leg,
                                    time.perf_counter() - t0,
                                    f"ran out of time on leg {i} of {len(legs) - 1}")
            last = self.to(leg, timeout_s=remaining, abort=abort, allow_detour=False)

            if last.outcome is Outcome.STUCK and replan is not None and replans < max_replans:
                # Blocked. Ask the planner from here instead of improvising: the mesh
                # knows the way round, and a follower that invents one is the thing this
                # whole file stopped doing.
                replans += 1
                position = self.position()
                fresh = replan(position) if position is not None else None
                if fresh is not None and getattr(fresh, "usable", False):
                    self.arrival_yards = exact
                    rest = self.follow(
                        fresh, timeout_s=timeout_s - (time.perf_counter() - t0),
                        abort=abort, replan=replan, max_replans=max_replans - replans,
                    )
                    return TravelResult(
                        outcome=rest.outcome, start=legs[0], end=rest.end,
                        remaining_yards=rest.remaining_yards,
                        elapsed_s=time.perf_counter() - t0, turns=self.turns,
                        stuck_events=self.stuck_events, detours=self.detours,
                        turn_rate_deg_s=rest.turn_rate_deg_s,
                        detail=f"re-planned at leg {i}: {rest.detail}".strip(),
                    )

            if last.outcome is not Outcome.ARRIVED:
                self.arrival_yards = exact
                return TravelResult(
                    outcome=last.outcome, start=legs[0], end=last.end,
                    remaining_yards=last.remaining_yards,
                    elapsed_s=time.perf_counter() - t0, turns=self.turns,
                    stuck_events=self.stuck_events, detours=self.detours,
                    turn_rate_deg_s=last.turn_rate_deg_s,
                    detail=f"leg {i} of {len(legs) - 1}: {last.detail}",
                )

        self.arrival_yards = exact
        if last is None:
            return self._result(Outcome.ARRIVED, legs[0], self.position(), legs[-1],
                                time.perf_counter() - t0, "already at the destination")
        return TravelResult(
            outcome=Outcome.ARRIVED, start=legs[0], end=last.end,
            remaining_yards=last.remaining_yards,
            elapsed_s=time.perf_counter() - t0, turns=self.turns,
            stuck_events=self.stuck_events, detours=self.detours,
            turn_rate_deg_s=last.turn_rate_deg_s, detail="",
        )

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

        # A read failure skips *this* attempt, not the whole recovery. Both extremes
        # have now cost a live run: treating None as "skip" meant one torn frame skipped
        # all five attempts instantly, and treating it as "give up" meant one torn frame
        # abandoned recovery after the first. Try them all, and only conclude nothing
        # worked when nothing has been tried successfully either.
        unreadable = 0
        for name, attempt in attempts:
            before = self.position()
            if before is None:
                unreadable += 1
                continue
            attempt()
            time.sleep(0.25)
            after = self.position()
            if after is None:
                unreadable += 1
                continue
            if self.distance(before, after) > self.stuck_step_yards:
                self.last_unstick = name
                return True
        if unreadable == len(attempts):
            self.last_unstick = "unreadable"
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


def _thin(legs: list[tuple[float, float]], bounds: ZoneBounds,
          min_gap_yards: float) -> list[tuple[float, float]]:
    """Drop waypoints closer together than the arrival radius can resolve.

    Two points a yard apart cannot both be arrived at when arrival is three yards, so
    keeping both means the second is satisfied the moment the first is and the follower
    spends its time declaring victory instead of walking. The last point always survives:
    it is the destination, and thinning it away would be arriving somewhere else.
    """
    if len(legs) <= 2:
        return legs
    kept = [legs[0]]
    for leg in legs[1:-1]:
        if distance_yards(kept[-1], leg, bounds) >= min_gap_yards:
            kept.append(leg)
    kept.append(legs[-1])
    return kept


def _wrap(a: float) -> float:
    """Signed angle difference in (-pi, pi]."""
    return (a + math.pi) % TAU - math.pi
