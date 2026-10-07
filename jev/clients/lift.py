"""A lift ridden by keys (V407): walked to its deck, the platform waited for, stepped onto,
stood on for the ride, walked off, and the ride confirmed by the zone changing.

The server moves a lift's platform by its own clock alone (`ElevatorTransport::Update`: the
world's milliseconds since the server started, modulo the platform's cycle), and nothing the
client reads shows where the platform is: the strip paints no height, a platform has no plate
and no tooltip, and a ride changes no x or y. A step into the shaft at the top while the
platform is at the bottom is a fall of 61 to 71 yards at Thunder Bluff and 96 at the
Undercity; and a walk off the platform before it stands at its stop is a walk into the open air
under Thunder Bluff's upper decks (the map 1 tiles: under each upper deck the navmesh's next
floor is the ground). So the live follower rides only by the platform's times (`LiftClock`):
when it stands at each stop, learned from a sighting with its time, and kept while rides keep
confirming it. Without them it rides nothing, and the planner offers it no lift (V382).

    at the deck            the plan's walk ends beside the shaft, at the boarding stop's height
    wait for the platform  until the clock says it surely stands at the stop, long enough to
                           step on (`STEP_MARGIN_S` spare)
    step on                a straight walk by keys to the platform's middle, turned from the
                           walk's last heading
    stand the ride's time  until the clock says it surely stands at the other stop
    walk off               a straight walk to the other deck
    confirmed              by the zone the strip paints changing (Thunder Bluff's mesas are its
                           city's map, the ground Mulgore's; the Undercity's lifts climb from
                           its map to Tirisfal's). Nothing else confirms one: at the foot of
                           Thunder Bluff's lifts a walk toward the upper deck's x and y drops
                           nine yards onto the ground under it, there in x and y. A ride not
                           confirmed forgets the platform's times.

A deck the navmesh puts nine yards under a stop - the ground under Thunder Bluff's lower docks
- is no deck for keys: a ride by keys boards and leaves only at a deck at its stop's height
(`LIFT_DECK_ABOVE`).
"""

from __future__ import annotations

import contextlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jev.guide.coords import heading_yards, map_to_world, world_to_map
from jev.guide.path import LIFT_DECK_ABOVE, Lift, LiftLeg
from jev.persist import atomic_json

Point = tuple[float, float, float]

# A platform's times are kept this long after the last sighting or confirmed ride: the clock is
# the server's since it started, and a restart moves it.
KEEP_S = 4 * 3600.0
# A step onto or off the platform: the deck is 4 to 9 yards from the shaft's middle, at a run
# of seven yards a second, and the step is begun only with this much of the platform's stand
# to spare after it.
RUN_YARDS_PER_S = 7.0
STEP_MARGIN_S = 1.0
STEP_EXTRA_S = 0.3
# On the platform: within this of its middle, in x and y.
ON_BOARD_YARDS = 1.5
# A turn smaller than this is not made before a step.
TURN_MIN = math.radians(5.0)
WAIT_LOOK_S = 0.1


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


@dataclass
class LiftClock:
    """When each lift's platform arrives at its lowest stop, by the wall clock, as a window
    (`lo`, `hi`): it arrived there at some moment between them, a cycle apart ever since. A
    sighting of the platform at a stop gives it; a confirmed ride keeps it; a ride not
    confirmed, a reconnect or `KEEP_S` without either forgets it."""

    path: Path | None = None
    keep_s: float = KEEP_S
    wall: Callable[[], float] = time.time
    _lifts: dict[int, dict] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
            for guid, entry in (raw.get("lifts") or {}).items():
                self._lifts[int(guid)] = {k: float(entry[k]) for k in ("lo", "hi", "seen")}

    def _save(self) -> None:
        if self.path is None:
            return
        with contextlib.suppress(OSError):
            atomic_json(Path(self.path), {"format": 1, "lifts": {
                str(g): e for g, e in sorted(self._lifts.items())}})

    @staticmethod
    def _offset_s(lift: Lift, stop: int) -> float:
        """When the platform arrives at `stop` after it arrives at the lowest, in seconds."""
        return ((lift.stops[stop].arrives_ms - lift.stops[0].arrives_ms) % lift.period_ms) / 1000.0

    @staticmethod
    def dwell_s(lift: Lift, stop: int) -> float:
        s = lift.stops[stop]
        return ((s.leaves_ms - s.arrives_ms) % lift.period_ms) / 1000.0

    def _entry(self, lift: Lift) -> dict | None:
        self._load()
        entry = self._lifts.get(lift.guid)
        if entry is None:
            return None
        if self.wall() - entry["seen"] > self.keep_s:
            self.forget(lift)
            return None
        return entry

    def saw(self, lift: Lift, stop: int, at: float, *, arrived_within_s: float | None = None) -> None:
        """The platform stood at `stop` at wall time `at`, having arrived there no more than
        `arrived_within_s` before (at most its stand there). A second sighting narrows the
        window; one that does not meet it starts it afresh (the server restarted)."""
        dwell = self.dwell_s(lift, stop)
        within = dwell if arrived_within_s is None else min(max(arrived_within_s, 0.0), dwell)
        lo = at - within - self._offset_s(lift, stop)
        hi = at - self._offset_s(lift, stop)
        old = self._entry(lift)
        if old is not None:
            period = lift.period_ms / 1000.0
            shift = round((lo - old["lo"]) / period) * period
            o_lo, o_hi = old["lo"] + shift, old["hi"] + shift
            if max(lo, o_lo) <= min(hi, o_hi):
                lo, hi = max(lo, o_lo), min(hi, o_hi)
        self._lifts[lift.guid] = {"lo": lo, "hi": hi, "seen": self.wall()}
        self._save()

    def kept(self, lift: Lift) -> None:
        """A ride the times foretold, confirmed: they stand `KEEP_S` more."""
        entry = self._entry(lift)
        if entry is not None:
            entry["seen"] = self.wall()
            self._save()

    def forget(self, lift: Lift | None = None) -> None:
        """Forget a lift's times, or every lift's (`None`): a reconnect may be a restart."""
        self._load()
        if lift is None:
            self._lifts.clear()
        else:
            self._lifts.pop(lift.guid, None)
        self._save()

    def known(self, lift: Lift) -> bool:
        return self._entry(lift) is not None

    def window(self, lift: Lift, stop: int, after: float, need_s: float) -> tuple[float, float] | None:
        """The first stretch of wall time from `after` through which the platform surely
        stands at `stop`, `need_s` long at least, as (start, end); `None` with its times not
        known, or not known well enough for `need_s`."""
        entry = self._entry(lift)
        if entry is None:
            return None
        period = lift.period_ms / 1000.0
        offset, dwell = self._offset_s(lift, stop), self.dwell_s(lift, stop)
        first, last = entry["hi"] + offset, entry["lo"] + offset + dwell   # one cycle's sure stand
        if last - first < need_s:
            return None
        k = math.ceil((after + need_s - last) / period)
        start, end = max(first + k * period, after), last + k * period
        if end - start < need_s:
            k += 1
            start, end = first + k * period, last + k * period
        return start, end


@dataclass
class LiftRide:
    """The live follower's ride by keys (`jev.clients.travel.Travel.ride`)."""

    hid: object
    bounds: object
    read: Callable[[], dict | None]
    read_pos: Callable[[], tuple[float, float] | None]
    heading: Callable[[], float | None]
    turn_rate: Callable[[], float]
    clock: LiftClock
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    detail: str | None = field(default=None, init=False)

    # -- what can be ridden --------------------------------------------------------

    def rideable(self, leg: LiftLeg) -> bool:
        """A leg the keys can ride: its platform's times known, and its decks, where the plan
        has placed them, at their stops' heights."""
        if leg.at is not None and abs(leg.at[2] - leg.board[2]) > LIFT_DECK_ABOVE:
            return False
        if leg.exit is not None and abs(leg.exit[2] - leg.alight[2]) > LIFT_DECK_ABOVE:
            return False
        return self.clock.known(leg.lift)

    # -- the ride ------------------------------------------------------------------

    def ride(self, leg: LiftLeg) -> Point | None:
        """Ride `leg` from its deck: where it left the character, or `None` (`detail` says why).
        Nothing is stepped onto or off but in the clock's sure stand of the platform."""
        self.detail = None
        if leg.at is None or leg.exit is None or not self.rideable(leg):
            self.detail = "the platform's times are not known" if leg.at is not None \
                else "no deck placed"
            return None
        here = self._here()
        if here is None:
            self.detail = "no position"
            return None
        zone = self._zone()
        facing = self.heading()
        if facing is None:
            self.detail = "no heading known at the deck"
            return None
        step_on = math.dist(here[:2], leg.board[:2]) / RUN_YARDS_PER_S + STEP_EXTRA_S
        board = self.clock.window(leg.lift, leg.frm, self.wall(), step_on + STEP_MARGIN_S)
        if board is None:
            self.detail = "the platform's times are not known well enough to step on"
            return None
        if not self._wait_until(board[0]):
            return None
        facing = self._step(leg.board, facing, step_on, past=False)
        here = self._here()
        if facing is None or here is None or math.dist(here[:2], leg.board[:2]) > ON_BOARD_YARDS:
            self.detail = "the step onto the platform did not reach its middle"
            return None
        step_off = math.dist(leg.board[:2], leg.exit[:2]) / RUN_YARDS_PER_S + STEP_EXTRA_S
        off = self.clock.window(leg.lift, leg.to, self.wall(), step_off + STEP_MARGIN_S)
        if off is None or not self._wait_until(off[0]):
            self.detail = self.detail or "the platform's times went unknown aboard"
            return None
        self._step(leg.exit, facing, step_off, past=True)
        here = self._here()
        if here is None:
            self.detail = "no position after the ride"
            return None
        after = self._zone()
        if zone is None or after is None or after == zone:
            self.clock.forget(leg.lift)
            self.detail = (f"the zone as before, {math.dist(here[:2], leg.exit[:2]):.1f} yards "
                           "from the other deck: no ride; the platform's times forgotten")
            return None
        self.clock.kept(leg.lift)
        return tuple(leg.exit)

    # -- keys ----------------------------------------------------------------------

    def _here(self) -> Point | None:
        p = self.read_pos()
        return None if p is None else map_to_world(p[0], p[1], self.bounds)

    def _zone(self):
        return (self.read() or {}).get("pos.zone_id")

    def _wait_until(self, moment: float) -> bool:
        checkpoint = getattr(self.hid, "checkpoint", None)
        while True:
            if callable(checkpoint):
                checkpoint()
            left = moment - self.wall()
            if left <= 0:
                return True
            self.sleep(min(WAIT_LOOK_S, left))

    def _step(self, target: Point, facing: float, seconds: float, *, past: bool) -> float | None:
        """Turn from `facing` toward `target` and walk straight at it for `seconds` at most, by
        keys held for exactly as long as asked: onto the platform's middle and no farther,
        off it on past the deck (`past`). The facing walked, from the step's own displacement
        where it moved enough to read one, else the one turned to; `None` where a key was
        refused."""
        here = self.read_pos()
        aim = world_to_map(target[0], target[1], self.bounds)
        if here is None or aim is None:
            return None
        want = heading_yards(here, aim, self.bounds)
        if want is None:
            return facing
        error = _wrap(want - facing)
        if abs(error) >= TURN_MIN:
            key = "d" if error > 0 else "a"
            if not self.hid.hold(key, abs(error) / max(self.turn_rate(), 0.1), exact=True):
                return None
        walk = (math.dist(map_to_world(*here, self.bounds)[:2], target[:2]) / RUN_YARDS_PER_S
                + (STEP_EXTRA_S if past else 0.0))
        if not self.hid.hold("w", min(seconds, walk), exact=True):
            return None
        after = self.read_pos()
        moved = heading_yards(here, after, self.bounds) if after is not None else None
        return want if moved is None else moved
