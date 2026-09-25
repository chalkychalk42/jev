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

from jev.clients.hid import Hid, pace
from jev.guide.coords import (
    ZoneBounds,
    distance_yards,
    heading_yards,
    map_to_world,
    world_to_map,
)

TAU = 2 * math.pi
# Within arrival of the walk's destination on any leg is arrival: the route is a way
# there, not a tour. Inside the Lion's Pride Inn a character passed 3.9 yards from William
# Pestle and then walked two more minutes of a re-planned route round the building, and
# the hand-in ran out of time (session 108).
EARLY = "at the destination before the route's end"
# How much farther than the arrival radius from a complete route's end a walk may stop and
# still have arrived: the re-planned route's end is the same destination snapped afresh.
ARRIVAL_SLACK_YARDS = 1.0
# Early arrival (`EARLY`) only on a leg whose ends are within this of the destination's
# height. Positions are map x and y alone, and Goldtooth's spawn lies 30 yards under the
# field over Fargodeep Mine: the walk to it "arrived" crossing the field above, and the hunt
# found nothing there to fight (session 113).
EARLY_HEIGHT_YARDS = 3.0
# And, when the caller can ask the planner (`reach`), only where the planned walk from there
# is within the arrival radius and this: William Pestle stands 2.9 yards from the next room
# of the Lion's Pride Inn and 13.8 yards' walk round its wall, and the hand-ins "arrived"
# in that room, facing the wall, until the step failed over (session 114).
EARLY_WALK_YARDS = 3.0

# A seed, not a calibration. Measured once on a level-3 human in Northshire.
TURN_RATE_SEED = math.radians(134.0)

# A heading is taken across a window of recent motion. Long enough that the readout's
# quantisation is small against it, short enough that it describes where the character is
# going *now* rather than where it was going a second ago.
HEADING_WINDOW_S = 0.6
MIN_TRAVEL_FOR_HEADING = 1.5      # yards; below this the angle is noise

MIN_PULSE_S = 0.05
MAX_PULSE_S = 0.45                # one correction, not a whole swing

# Near the point, a large heading error is turned off standing still. Turned while walking,
# a pulse is an arc of three yards before the new heading can even be measured, and within
# a few yards of the point the arcs close into a circle round it. Simulated: a point four
# to eight yards off at sixty to ninety degrees was circled until the walk timed out, 16
# walks of 72, moving the whole time so no stuck test fired; a character following a
# learned passage circled one for 295 s.
PIVOT_YARDS = 10.0
PIVOT_ERROR = math.radians(60.0)

# A detour whose own walk covered less than this found a dead end on its side.
DETOUR_BLOCKED_YARDS = 3.0


class Outcome(StrEnum):
    ARRIVED = "arrived"
    STUCK = "stuck"
    TIMEOUT = "timeout"
    LOST = "lost"          # position stopped being readable
    REFUSED = "refused"    # the window was not focused; nothing was ever pressed
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
    # Indoors nothing is learned (`RouteMemory`): in a hall's tight corners an escape is
    # wherever an unstick move happened to land, and the Lion's Pride Inn's learned points
    # bent every route to William Pestle past the foot of its stairs, and up them
    # (session 109).
    indoors: Callable[[], bool] | None = None

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
    # Forward held this long without a heading's worth of travel is stuck. Measured on a
    # clean run: the longest frozen stretch was 0.06s and the median step 0.19 yards, so
    # a second and a half is far outside normal and still quick to recover from.
    #
    # The distance is the travel a heading needs, not "frozen". A character pressed into a
    # fence two degrees off square slides along it at a quarter of a yard a second: past a
    # 0.3-yard "frozen" test, and far too slow to take a heading from, so nothing steered
    # it and nothing unstuck it. Simulated, that walk pressed into the fence for its whole
    # two minutes without a single stuck event; drawn timing made the angle common.
    stuck_after_s: float = 1.5
    stuck_step_yards: float = MIN_TRAVEL_FOR_HEADING
    # Moving is not getting anywhere. A slope too steep to climb lets the character walk
    # up it and slide back for as long as forward is held: 147 s below a hillside on the
    # way to Northshire, never still long enough for the test above, until the step's
    # clock failed it (run 20260924T052148-85c63f). A leg whose end has come no closer by
    # `no_progress_yards` in `no_progress_s` is stuck as well.
    no_progress_s: float = 12.0
    no_progress_yards: float = 2.0
    # How many headings to try the recovery from, and how far to turn between them. Eight
    # would be a full circle; four covers a corner, and each one costs five attempts.
    unstick_headings: int = 4
    unstick_turn_s: float = 0.45
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
    # The search for the way round one obstacle: this side's allowance and what is left.
    _sweep: int = field(default=1, init=False)
    _sweep_left: int = field(default=1, init=False)
    _pulse_ended_at: float = field(default=0.0, init=False)
    stuck_events: int = field(default=0, init=False)
    last_unstick: str = field(default="", init=False)
    # Where the last `_detour` left the character: the escape a blocked spot is learned by.
    last_detour_end: tuple[float, float] | None = field(default=None, init=False)
    # Where the last stuck event stopped the character, before anything moved it.
    last_stuck_at: tuple[float, float] | None = field(default=None, init=False)
    # Positions of the escape in progress since its last stuck event, while one is traced.
    _trace: list | None = field(default=None, init=False)
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
           allow_detour: bool = True, destination=None,
           destination_yards: float | None = None,
           destination_reach: Callable[[tuple[float, float]], float | None] | None = None,
           ) -> TravelResult:
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
        refused_at_start = getattr(self.hid, "refused", 0)
        start = self.position()
        here = start
        self._track.clear()
        pulse_until = 0.0
        pulse_key: str | None = None
        pulse_started_heading: float | None = None
        pulse_len = 0.0
        early_refused = False

        best, best_at = None, t0
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
                    if self._trace is not None:
                        self._trace.append(p)

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
                if (destination is not None and destination_yards is not None
                        and not early_refused
                        and self.distance(here, destination) <= destination_yards):
                    walk = None if destination_reach is None else destination_reach(here)
                    if destination_reach is None or (
                            walk is not None and walk <= destination_yards + EARLY_WALK_YARDS):
                        return self._result(Outcome.ARRIVED, start, here, destination,
                                            elapsed, EARLY)
                    early_refused = True             # a wall between: this leg walks on
                if best is None or remaining < best - self.no_progress_yards:
                    best, best_at = remaining, now

                moved = self._moved_since(self.stuck_after_s)
                if (now - best_at > self.no_progress_s
                        or (moved is not None and moved < self.stuck_step_yards)):
                    # Not moving and not pressing are different problems with the same
                    # symptom. `Hid` refuses whenever the game window is not focused, so
                    # a character that was never sent a keystroke looks exactly like one
                    # wedged against a tree — and a whole live run was spent unsticking a
                    # character that was standing still because a console window had
                    # stolen the foreground.
                    if getattr(self.hid, "refused", 0) > refused_at_start:
                        return self._result(
                            Outcome.REFUSED, start, here, target,
                            time.perf_counter() - t0,
                            "the game window lost focus; nothing was pressed")
                    self.stuck_events += 1
                    self.last_stuck_at = here
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
                    if self._trace is not None:
                        self._trace = []        # only the attempt that works is learned
                    self._detour(here)
                    self._track.clear()
                    best, best_at = None, time.perf_counter()   # the detour starts afresh
                    self.hid.key_down("w")
                    time.sleep(pace(self.hid, self.sample_s))
                    continue

                # Steer, but only while actually moving: a heading taken from a character
                # pinned against a wall points wherever the last real step went.
                if pulse_key is None:
                    heading = self._heading_now()
                    if heading is not None:
                        want = self.bearing(here, target)
                        if want is not None:
                            error = _wrap(want - heading)
                            if abs(error) > PIVOT_ERROR and remaining < PIVOT_YARDS:
                                self._pivot(error)
                                continue
                            if abs(error) > self._deadband(self.distance(here, target)):
                                pulse_len = min(MAX_PULSE_S,
                                                abs(error) / max(self.turn_rate, 0.1))
                                if pulse_len >= MIN_PULSE_S:
                                    pulse_key = "d" if error > 0 else "a"
                                    pulse_started_heading = heading
                                    pulse_until = now + pulse_len
                                    self.hid.key_down(pulse_key)
                                    self.turns += 1

                time.sleep(pace(self.hid, self.sample_s))
        finally:
            self.hid.release_all()

    def follow(self, path, *, timeout_s: float = 300.0,
               abort: Callable[[], bool] | None = None,
               replan: Callable[[tuple[float, float]], object] | None = None,
               max_replans: int = 3, memory=None, rounds: int = 2,
               reach: Callable[[tuple[float, float]], float | None] | None = None,
               ) -> TravelResult:
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

        What the mesh does not know is learned instead (`memory`, a `RouteMemory`): a
        spot where a leg was blocked and only the follower's own detour got past it -
        a tree gap, a rail fence, a ledge the server's creature mesh calls open ground -
        is remembered with the point the detour reached, and every later route passing
        that spot goes by the point.
        """
        if not path.usable:
            return self._result(Outcome.STUCK, self.position(), self.position(),
                                self.position() or (0.0, 0.0), 0.0,
                                f"no usable path: {path.status.value} {path.detail}".strip())

        if memory is not None:
            path = memory.patch(self.bounds.map_id, path)
        t0 = time.perf_counter()
        placed = [(world_to_map(pt[0], pt[1], self.bounds), pt[2] if len(pt) > 2 else None)
                  for pt in path.points]
        heights = {leg: z for leg, z in placed if leg is not None}
        legs = [leg for leg, _ in placed if leg is not None]
        legs = _thin(legs, self.bounds, self.waypoint_arrival_yards)
        end_z = heights.get(legs[-1]) if legs else None
        exact = self.arrival_yards
        complete = str(getattr(path, "status", "")) == "complete"
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
            level = all(end_z is None or heights.get(end) is None
                        or abs(heights[end] - end_z) <= EARLY_HEIGHT_YARDS
                        for end in (legs[i - 1], leg))
            last = self.to(leg, timeout_s=remaining, abort=abort, allow_detour=False,
                           destination=None if final or not level else legs[-1],
                           destination_yards=exact, destination_reach=reach)
            if last.outcome is Outcome.ARRIVED and last.detail == EARLY:
                self.arrival_yards = exact
                return self._result(Outcome.ARRIVED, legs[0], self.position(), legs[-1],
                                    time.perf_counter() - t0, "")

            if last.outcome is Outcome.STUCK:
                stuck_at = self.last_stuck_at
                # Blocked. Ask the planner from here instead of improvising: the mesh
                # knows the way round, and a follower that invents one is the thing this
                # whole file stopped doing.
                position = self.position()
                fresh = None
                asked = replan is not None and replans < max_replans
                if asked:
                    replans += 1
                    fresh = replan(position) if position is not None else None

                # ...unless the planner has nothing new to say. A re-plan is only worth
                # anything if its answer changed, and the mesh does not know about the
                # obstacle: a ghost against a Northshire fence got the same 199.0-yard
                # path back every time, four waypoints and all, then re-planned into it
                # three more times per pass. So when the fresh route still starts by
                # walking into the same place, hand the leg to the wall heuristic **once**
                # - the only thing here that learns from the world rather than from the
                # mesh - and the planner has the route back as soon as it clears.
                #
                # Comparing answers rather than positions, because a leg that walks eighty
                # yards and *then* wedges has moved, and that one spins just as happily.
                #
                # Nor when it can no longer be asked. The re-plans are a budget for the
                # walk, and a blocked leg with none left used to end it: a 366-yard walk to
                # Echo Ridge Mine spent all three on the Abbey's corners, met a pit prop at
                # the mine's mouth, and stopped 95 yards short without trying the wall
                # heuristic once (run 20260923T184413-a386ff).
                if not asked or self._same_answer(fresh, position, leg):
                    # A fresh obstacle, not the last one continued.
                    self.detours = 0
                    self._sweep = self._sweep_left = 1
                    # Round it now. Walking the same leg first only found the same
                    # obstacle again: measured in simulation, a whole second stuck cycle
                    # per fence before the first detour.
                    self.last_detour_end = None
                    self._trace = []
                    try:
                        if position is not None:
                            self._detour(position)
                        last = self.to(leg, abort=abort, allow_detour=True,
                                       timeout_s=timeout_s - (time.perf_counter() - t0))
                        if last.outcome is Outcome.ARRIVED:
                            self._learn(memory, position, leg, path)
                            continue           # past it; the planner has the route back
                    finally:
                        self._trace = None
                    # Nor did the wall heuristic get past: a spot with no way round a
                    # detour can find, like the pocket under Echo Ridge Mine's pit prop.
                    # Remember it as blocked and ask the planner once more, for a route
                    # that stays clear of it (`AvoidingQuery`).
                    if (last.outcome is Outcome.STUCK and memory is not None
                            and replan is not None and rounds > 0 and stuck_at is not None):
                        rounded = self._round_blocked(
                            path, legs[i - 1], leg, stuck_at, memory, replan,
                            timeout_s - (time.perf_counter() - t0), abort,
                            max_replans - replans, rounds - 1, reach)
                        if rounded is not None:
                            self.arrival_yards = exact
                            outcome, short = self._short_of(rounded.outcome, rounded.end,
                                                            legs, exact, complete)
                            return TravelResult(
                                outcome=outcome, start=legs[0], end=rounded.end,
                                remaining_yards=self._short_by(rounded.end, legs),
                                elapsed_s=time.perf_counter() - t0, turns=self.turns,
                                stuck_events=self.stuck_events, detours=self.detours,
                                turn_rate_deg_s=rounded.turn_rate_deg_s,
                                detail=f"round a blocked spot at leg {i}: {rounded.detail}".strip()
                                + short)
                    self.arrival_yards = exact
                    return self._result(last.outcome, legs[0], last.end, leg,
                                        time.perf_counter() - t0,
                                        f"leg {i} of {len(legs) - 1}: {last.detail}")
                if fresh is not None and getattr(fresh, "usable", False):
                    self.arrival_yards = exact
                    rest = self.follow(
                        fresh, timeout_s=timeout_s - (time.perf_counter() - t0),
                        abort=abort, replan=replan, max_replans=max_replans - replans,
                        memory=memory, rounds=rounds, reach=reach,
                    )
                    outcome, short = self._short_of(rest.outcome, rest.end, legs, exact,
                                                    complete)
                    return TravelResult(
                        outcome=outcome, start=legs[0], end=rest.end,
                        remaining_yards=self._short_by(rest.end, legs),
                        elapsed_s=time.perf_counter() - t0, turns=self.turns,
                        stuck_events=self.stuck_events, detours=self.detours,
                        turn_rate_deg_s=rest.turn_rate_deg_s,
                        detail=f"re-planned at leg {i}: {rest.detail}".strip() + short,
                    )

            if last.outcome is not Outcome.ARRIVED:
                self.arrival_yards = exact
                return TravelResult(
                    outcome=last.outcome, start=legs[0], end=last.end,
                    remaining_yards=self._short_by(last.end, legs),
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
            remaining_yards=self._short_by(last.end, legs),
            elapsed_s=time.perf_counter() - t0, turns=self.turns,
            stuck_events=self.stuck_events, detours=self.detours,
            turn_rate_deg_s=last.turn_rate_deg_s, detail="",
        )

    def _learn(self, memory, blocked, leg, path=None) -> None:
        """Remember where a leg was blocked, and the widest point of the escape that worked.

        The escape traced since its last stuck event is the one that got past; its point
        farthest from the blocked line is where it went round - the end of a fence, the
        far side of a trunk. The end of the first detour is not: along a long fence it
        is still in front of the fence.
        """
        trace = self._trace or ([self.last_detour_end] if self.last_detour_end else [])
        if memory is None or blocked is None or not trace or self._inside():
            return
        bx, by = _yards(blocked, self.bounds)
        lx, ly = _yards(leg, self.bounds)
        length = math.hypot(lx - bx, ly - by) or 1.0

        def lateral(point):
            px, py = _yards(point, self.bounds)
            return abs((lx - bx) * (py - by) - (ly - by) * (px - bx)) / length

        widest = max(trace, key=lateral)
        stuck = map_to_world(blocked[0], blocked[1], self.bounds)
        via = map_to_world(widest[0], widest[1], self.bounds)
        if stuck is not None and via is not None:
            # The route's height where it was stopped, along its segment as `patch` reads
            # it: the floor the passage belongs to. The nearest waypoint's was 6 yards off
            # on a sloped leg, and the passage never matched its own route (review).
            from jev.guide.route_memory import nearest_height

            near = nearest_height(getattr(path, "points", None) or (), stuck[:2])
            memory.learn(self.bounds.map_id, stuck, via, z=near[1] if near else None)

    def _inside(self) -> bool:
        return self.indoors is not None and self.indoors() is True

    def _round_blocked(self, path, previous, leg, stuck_at, memory, replan, timeout_s,
                       abort, max_replans, rounds, reach=None) -> TravelResult | None:
        """Block the spot where the route met what stopped it, and follow a way round.

        The spot is on the route, not where the character ended up: pressed into the log
        at Echo Ridge Mine it slid three yards along it into the pocket before the stuck
        test fired. So it is the point of the blocked leg nearest where it stopped, at the
        route's own height there. `None` when the planner has no other way to offer.
        """
        a = map_to_world(previous[0], previous[1], self.bounds)
        b = map_to_world(leg[0], leg[1], self.bounds)
        at = map_to_world(stuck_at[0], stuck_at[1], self.bounds)
        if a is None or b is None or at is None:
            return None
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((at[0] - a[0]) * dx
                                                        + (at[1] - a[1]) * dy) / length2))
        spot = (a[0] + t * dx, a[1] + t * dy)
        z = min(path.points, key=lambda p: math.dist(p[:2], spot))[2] if path.points else 0.0
        if not self._inside():
            memory.block(self.bounds.map_id, (spot[0], spot[1], z), heading=(dx, dy))
        position = self.position()
        fresh = replan(position) if position is not None else None
        if (fresh is None or not getattr(fresh, "usable", False)
                or self._same_answer(fresh, position, leg)):
            return None
        return self.follow(fresh, timeout_s=timeout_s, abort=abort, replan=replan,
                           max_replans=max_replans, memory=memory, rounds=rounds,
                           reach=reach)

    def _same_answer(self, fresh, position, leg) -> bool:
        """Would following `fresh` walk straight back into the leg that just blocked?

        A re-plan is new information only if it heads somewhere else. `fresh` is in world
        yards and `leg` is a map fraction, so the comparison happens where both can be
        said: the first waypoint the fresh route would actually travel to, against the
        leg the character just failed to reach.
        """
        if fresh is None or not getattr(fresh, "usable", False):
            return True                    # no answer at all is not a different answer
        if position is None:
            return False
        ahead = [w for w in (world_to_map(pt[0], pt[1], self.bounds)
                             for pt in fresh.points) if w is not None]
        # Already there by the tight radius, not the leg's: a final leg arrives within
        # talking distance, and a way round that starts four yards off is a new way.
        near = min(self.arrival_yards, self.waypoint_arrival_yards)
        ahead = [w for w in ahead if self.distance(w, position) > near]
        if not ahead:
            return True                    # nowhere left to go, so nothing new to try
        return self.distance(ahead[0], leg) <= self.waypoint_arrival_yards

    def _detour(self, here) -> None:
        """Turn off the direct line and walk along the obstacle for a while.

        Which way round an obstacle nobody has mapped is a search, done by doubling: one
        detour one way, then two the other - one to come back, one of new ground - then
        four, and so on, which bounds the walk for an end at any distance on either side.
        Two earlier rules both oscillated. Alternating on every detour took the character
        from ten yards out to twenty-nine, each detour undoing the last. Flipping whenever
        a detour ended farther from the target is the same thing round anything long,
        because going round a wall moves away from the target before it moves toward it:
        simulated on a 60-yard fence, it walked back and forth along the fence until the
        walk timed out.

        A detour whose own walk is blocked has found a dead end on its side, and the rest
        of the search goes the other way.
        """
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
        self.last_detour_end = after_pos
        if self._trace is not None:
            self._trace.append(after_pos)
        self._sweep_left -= 1
        if self.distance(here, after_pos) < DETOUR_BLOCKED_YARDS:
            self._detour_side *= -1
            self._sweep = self._sweep_left = self.max_detours
        elif self._sweep_left <= 0:
            self._detour_side *= -1
            self._sweep *= 2
            self._sweep_left = self._sweep

    def _pivot(self, error: float) -> None:
        """Stop, turn the whole error standing still, and walk on to measure it afresh."""
        key = "d" if error > 0 else "a"
        self.hid.key_up("w")
        self.hid.hold(key, abs(error) / max(self.turn_rate, 0.1), tick_s=self.sample_s)
        self._track.clear()                  # the heading it had is not the one it has
        self.turns += 1
        self.hid.key_down("w")

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

        **Every attempt above acts along the current facing**, which is why they are tried
        again after turning. A character wedged in a corner can leave along exactly one
        heading, and a recovery that only ever pushes one way reports "could not free the
        character" while a quarter-turn would have done it. Measured on a ghost pinned
        against a tree in Northshire that survived two full corpse runs, a relog, and
        every attempt at its original heading: it came free on the **third** heading, on
        jump-forward, after turning twice.
        """
        self.hid.release_all()
        self.last_unstick = ""

        attempts = (
            ("jump-forward", lambda: (self.hid.tap("space"), time.sleep(pace(self.hid, 0.15)),
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
        unreadable = tried = 0
        for heading in range(self.unstick_headings):
            if heading:
                self.hid.hold("d", self.unstick_turn_s)
            for name, attempt in attempts:
                before = self.position()
                if before is None:
                    unreadable += 1
                    continue
                tried += 1
                attempt()
                time.sleep(pace(self.hid, 0.25))
                after = self.position()
                if after is None:
                    unreadable += 1
                    continue
                if self.distance(before, after) > self.stuck_step_yards:
                    self.last_unstick = name if not heading else f"{name} (turned {heading}x)"
                    return True
        if tried == 0 and unreadable:
            self.last_unstick = "unreadable"
        return False

    def _short_of(self, outcome: Outcome, end, legs, exact: float,
                  complete: bool) -> tuple[Outcome, str]:
        """`outcome`, unless it is an arrival short of a complete route's end: then stuck.

        A route taken up part way - re-planned, or round a blocked spot - can be partial,
        and reaching its end is not reaching the destination. From the Lion's Pride Inn,
        where a plan can start on the wrong floor, the re-plans were a few yards of hall:
        two walks to a grind 1,550 yards off were called "arrived, 1209.6 yards left"
        (session 110), the grind was begun indoors, and the rule that takes the hearthstone
        after wedged walks was reset by each. A partial route's own end is all a caller
        asked for, so only a complete one is held to its destination.
        """
        short = self._short_by(end, legs)
        if (outcome is Outcome.ARRIVED and complete and short is not None
                and short > exact + ARRIVAL_SLACK_YARDS):
            return Outcome.STUCK, f" (the route ended {short:.1f} yards short)"
        return outcome, ""

    def _short_by(self, end, legs) -> float | None:
        """How far the character stopped from where it was going.

        Against `legs[-1]`, never against the leg it happened to be on. `to()` measures
        the leg because that is what it is steering at, but a caller of `follow()` asks
        one question — did we get there — and a leg-relative answer lies in the most
        convincing way available: a run that gave up 46 yards from Marshal McBride
        reported "3.1 yards left", because 3.1 yards was all that remained of a waypoint
        in the middle of the courtyard.
        """
        return None if end is None else self.distance(end, legs[-1])

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


def _yards(point: tuple[float, float], bounds: ZoneBounds) -> tuple[float, float]:
    """A map position in the map-aligned yard frame `heading_yards` measures in."""
    return point[0] * abs(bounds.left - bounds.right), point[1] * abs(bounds.top - bounds.bottom)


def _wrap(a: float) -> float:
    """Signed angle difference in (-pi, pi]."""
    return (a + math.pi) % TAU - math.pi
