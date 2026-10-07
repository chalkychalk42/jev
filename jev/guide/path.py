"""`PathQuery` — how to get there, as opposed to where it is.

`ARCHITECTURE.md` §8 splits the job: the world DB generates **destinations**, and the
routes between them come from somewhere else. `Travel` is a follower and knows nothing
about geometry — it walks at a point and reports honestly when a wall stops it. This is
the piece it was missing.

    PathQuery(map_id, start, end) -> Path
        Path = waypoints in world yards | PARTIAL | NOPATH

Backends, in the order they are tried:

1. **`MmapQuery`** — the navmesh the server already uses. Those tiles are how CMaNGOS
   walks its own NPCs around Northshire Abbey, they were extracted when the server was
   built, and Detour is already compiled as `libDetour.a`. Asking them is cheaper and
   more honest than any mesh we could build, and far cheaper than teaching a follower to
   see buildings — which would not find a door on the north face of the Abbey anyway.
2. **`RecordedQuery`** — polylines walked by hand, for the legs a navmesh gets wrong:
   doors, boats, indoor stairs, anything scripted. Same `Path` type, so nothing above
   notices which answered.

If neither has an answer the result is `NOPATH` and the caller fails with the sentence
`Travel` already prints. That is the design: a missing route is a known gap, not a crash.

Over them all, **`TeleportQuery`** (V305): where walking does not get there, a route may walk
into a teleport's trigger and on from where it puts the character - the Darnassus portal down
to Rut'theran, which no walk on the tree reaches. And through lifts (V382): a platform that goes
up and down between two stops, Thunder Bluff's between its mesas and the ground, ridden by a
follower that can ride one.

Measured on the first real query, Northshire courtyard to Marshal McBride — the leg that
defeated straight-line travel nine times:

    {"status":"complete","points":[[-8944.8,-120.2,83.3],
                                   [-8913.1,-141.3,82.4],
                                   [-8902.6,-162.6,82.9]]}

Three points, and the middle one is the corner of the Abbey.
"""

from __future__ import annotations

import json
import math
import pathlib
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

from jev.guide.coords import _as_float

Point = tuple[float, float, float]

# Teleports (V305). A direct route fails when it ends farther than this from where it was
# asked to go: a walk's own arrival test (`jev.run.client.ARRIVED_NEAR_YARDS`). From Darnassus
# the mesh's way toward Rut'theran ends at the edge of the tree, 1,227 yards over it.
LINK_NEAR_YARDS = 15.0
# ...or when it is this many times the straight line: from Rut'theran the mesh's way toward
# Darnassus is 5,594 yards for a straight line of 2,403, and ends at the foot of the tree.
LINK_FAR_LONGER = 2.0
# What going through costs over its two walks, in yards of walking: stopping in the trigger
# and the server's turn to move the character.
LINK_COST_YARDS = 30.0
# A failed route tries only the teleports whose straight lines in and out are within this
# many times its own straight line and `LINK_SLACK_YARDS` more: plans are asked at up to 34
# start heights (`jev.run.client.START_HEIGHTS`), and a teleport a continent away helps none.
LINK_REACH = 3.0
LINK_SLACK_YARDS = 200.0
# A walk into a trigger stops at least this far inside its edge, in x and y.
LINK_ROOM_YARDS = 2.0
# The walk on from a teleport's exit is the same whatever height a plan starts at, and one
# to a place no walk reaches was asked again at every start height: kept this long.
WALK_ON_KEEP_S = 5.0
# The radio paints no height, so a jump is seen on the map alone: a teleport whose exit lies
# within this of its trigger's edge in x and y is not taken. The Wizard's Sanctum's way down
# (areatrigger 704) lands 6 yards from its middle, under it.
JUMP_SEEN_YARDS = 10.0


class PathStatus(StrEnum):
    COMPLETE = "complete"        # reaches the destination
    PARTIAL = "partial"          # the mesh allows only part of the way
    NOPATH = "nopath"            # no route, or an endpoint is off the mesh
    UNAVAILABLE = "unavailable"  # the backend could not be asked at all


@dataclass(frozen=True)
class Teleport:
    """An areatrigger that moves whoever walks into it to `exit`, on its own map (V305): a
    row of `world_areatrigger_teleport` in the shape `AreaTrigger.dbc` gives it - a sphere of
    `radius` round `at`, or with no radius a box of `box` (its x, y and z extents, and its
    turn about z)."""

    trigger_id: int
    name: str
    map_id: int
    at: Point
    radius: float
    box: tuple[float, float, float, float]
    exit: Point

    def room(self, point: Point) -> float:
        """How far `point` may move in x and y and stay inside, at its own height; below
        zero it is outside. The server's test, `IsPointInAreaTriggerZone`."""
        dx, dy, dz = (p - c for p, c in zip(point, self.at, strict=True))
        if self.radius > 0:
            if abs(dz) >= self.radius:
                return -abs(dz)
            return math.sqrt(self.radius ** 2 - dz ** 2) - math.hypot(dx, dy)
        turn = 2 * math.pi - self.box[3]
        rx = dx * math.cos(turn) - dy * math.sin(turn)
        ry = dy * math.cos(turn) + dx * math.sin(turn)
        if abs(dz) > self.box[2] / 2:
            return self.box[2] / 2 - abs(dz)
        return min(self.box[0] / 2 - abs(rx), self.box[1] / 2 - abs(ry))

    def reach(self) -> float:
        """How far from `at` its edge lies, at most, in x and y."""
        return self.radius if self.radius > 0 else math.hypot(self.box[0], self.box[1]) / 2


@lru_cache(maxsize=4)
def load_teleports(db_path: str) -> tuple[Teleport, ...]:
    """The teleports a walk may take (V305), from the world database: those that land on the
    map they stand on and ask nothing of the character - no level, item, quest or condition,
    which no plan can see - with an exit far enough from the trigger for the jump to show on
    the map (`JUMP_SEEN_YARDS`). Of the 186 in `world_areatrigger_teleport`, 32 land on their
    own map, and of those Rut'theran's and Darnassus's portals are the ones a character under
    20 walks through. None when the database cannot be read. Opened as the live bot opens it
    from Windows, where the checkout is a UNC path (`readonly_uri`)."""
    from jev.play.world_knowledge import readonly_uri

    try:
        con = sqlite3.connect(readonly_uri(pathlib.Path(db_path)), uri=True, timeout=1)
        try:
            rows = con.execute(
                "select t.id, t.name, t.target_map, t.target_position_x, t.target_position_y,"
                " t.target_position_z, t.required_level, t.required_item, t.required_item2,"
                " t.required_quest_done, t.condition_id,"
                " d.c1, d.c2, d.c3, d.c4, d.c5, d.c6, d.c7, d.c8, d.c9"
                " from world_areatrigger_teleport t join dbc_AreaTrigger d on d.id = t.id"
                " order by t.id").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ()
    out = []
    for (trigger_id, name, target_map, tx, ty, tz, level, item, item2, quest, condition,
         map_id, x, y, z, radius, bx, by, bz, turn) in rows:
        if target_map != map_id or (level or 0) > 1 or item or item2 or quest or condition:
            continue
        teleport = Teleport(
            trigger_id=trigger_id, name=name or "", map_id=map_id,
            at=(_as_float(x), _as_float(y), _as_float(z)), radius=_as_float(radius),
            box=(_as_float(bx), _as_float(by), _as_float(bz), _as_float(turn)),
            exit=(float(tx), float(ty), float(tz)))
        if math.dist(teleport.exit[:2], teleport.at[:2]) - teleport.reach() >= JUMP_SEEN_YARDS:
            out.append(teleport)
    return tuple(out)


# Decks beside lifts' stops, found once a process (`TeleportQuery._decks`): (map, stop) -> spots.
_DECKS: dict[tuple, list] = {}


# Lifts (V382): a platform that goes straight up and down between two stops, a `gameobject` of
# type 11 (GAMEOBJECT_TYPE_TRANSPORT) whose path is its rows of `TransportAnimation.dbc`. Thunder
# Bluff's four Mesa Elevators are the only way between its mesas and the ground, as the
# Undercity's three are between its city and the ruins over it; the navmesh has neither, and
# 27 of the hive's 315 bots stood on the mesas at once with no way down (6 Oct). Its stops are
# at least this far apart in height...
LIFT_RISE_YARDS = 10.0
# ...and the platform moves no more than this in x and y: a tram or a cart is no lift.
LIFT_FLAT_YARDS = 1.0
# A stop is where the platform stands this long at least; it leaves once a cycle.
LIFT_DWELL_MS = 1000
# The decks beside a stop: the navmesh's floors within these heights of it, looked for on
# rings round the shaft. The shaft has no floor of its own; at the foot of Thunder Bluff's
# lifts the ground under the platform's bottom stop is 9 yards below it (measured on the map 1
# tiles, 6 Oct), and the deck round it a small island of its own.
LIFT_DECK_ABOVE = 3.0
LIFT_DECK_BELOW = 10.0
LIFT_DECK_RINGS = (0.0, 3.0, 6.0, 9.0, 12.0)
LIFT_DECK_BEARINGS = 12
LIFT_DECKS = 3                     # floors tried at a stop, nearest its height first
LIFT_SAME_FLOOR = 2.0              # two decks closer than this in height are one floor
# A walk to a lift ends this near its deck spot, in x and y.
LIFT_BOARD_YARDS = 1.5
# What a ride costs a plan, in yards: its wait and the ride itself, at running pace.
LIFT_YARDS_PER_S = 7.0
# A failed plan tries only the lifts whose boarding stop is this near its start, in x and y, or
# whose other stop is this near its end: a lift helps a walk that begins or ends by the cliff it
# climbs, and each one tried is a planner ask at every start height (`START_HEIGHTS`). Elder
# Rise's south edge, where 14 of the hive's bots stood, is 310 yards from the nearest.
LIFT_NEAR_YARDS = 600.0
# Of two platforms in one shaft the one that goes farther is the lift (the Gnomeregan Vator's
# Plunger rides 12 yards over it).
LIFT_SHAFT_YARDS = 3.0


@dataclass(frozen=True)
class LiftStop:
    """Where a lift's platform stands, and when in its cycle it comes and goes."""

    z: float
    arrives_ms: int
    leaves_ms: int


@dataclass(frozen=True)
class Lift:
    """One platform (V382): its shaft at (`x`, `y`) on `map_id`, its two stops, lowest first,
    and its cycle. The server moves it by the time of day alone (`ElevatorTransport::Update`:
    the server's clock modulo the cycle), which nothing a client reads shows."""

    guid: int
    entry: int
    name: str
    map_id: int
    x: float
    y: float
    period_ms: int
    stops: tuple[LiftStop, LiftStop]

    def ride_s(self, frm: int, to: int) -> float:
        """From leaving stop `frm` to standing at stop `to`."""
        return ((self.stops[to].arrives_ms - self.stops[frm].leaves_ms) % self.period_ms) / 1000.0

    def wait_s(self) -> float:
        """The wait at a stop for the platform to leave it, on the average: it leaves once a
        cycle, and a character comes to the stop at any moment of it."""
        return self.period_ms / 2000.0

    def legs(self) -> tuple[LiftLeg, LiftLeg]:
        return (LiftLeg(self, 0, 1), LiftLeg(self, 1, 0))


@dataclass(frozen=True)
class LiftLeg:
    """A ride on `lift` from stop `frm` to stop `to`: walked in to `at`, a deck beside the shaft
    at `frm`'s height, and walked on from `exit`, beside it at `to`'s. A plan's own leg has
    them; the leg `Lift.legs` gives has its stops' middles."""

    lift: Lift
    frm: int
    to: int
    at: Point | None = None
    exit: Point | None = None

    @property
    def board(self) -> Point:
        """The platform's middle at the stop it is boarded at."""
        return (self.lift.x, self.lift.y, self.lift.stops[self.frm].z)

    @property
    def alight(self) -> Point:
        """...and at the stop it is left at."""
        return (self.lift.x, self.lift.y, self.lift.stops[self.to].z)

    @property
    def up(self) -> bool:
        return self.to > self.frm

    @property
    def wait_s(self) -> float:
        return self.lift.wait_s()

    @property
    def ride_s(self) -> float:
        return self.lift.ride_s(self.frm, self.to)

    @property
    def label(self) -> str:
        return (f"the {self.lift.name} {'up' if self.up else 'down'} at "
                f"({self.lift.x:.0f}, {self.lift.y:.0f})")

    def cost_yards(self) -> float:
        return (self.wait_s + self.ride_s) * LIFT_YARDS_PER_S

    def placed(self, at: Point, exit: Point) -> LiftLeg:
        return LiftLeg(self.lift, self.frm, self.to, tuple(at), tuple(exit))


def lift_stops(nodes: Iterable[tuple[int, float]]) -> tuple[int, list[LiftStop]] | None:
    """A platform's cycle and its stops from its path's (time, height) nodes: where it stands
    `LIFT_DWELL_MS` or more, the stand that ends the cycle and the one that begins it being one
    (the server moves it by its clock modulo the last node's time)."""
    nodes = sorted(nodes)
    if len(nodes) < 2 or nodes[-1][0] <= 0:
        return None
    period = nodes[-1][0]
    runs: list[list[float]] = []                 # [arrives, leaves, z]
    for t, z in nodes:
        if runs and abs(runs[-1][2] - z) < 0.05:
            runs[-1][1] = t
        else:
            runs.append([t, t, z])
    if (len(runs) > 1 and abs(runs[0][2] - runs[-1][2]) < 0.05 and runs[0][0] == 0
            and runs[-1][1] == period):
        first, last = runs.pop(0), runs.pop()
        runs.append([last[0], first[1] + period, first[2]])
    stops = [LiftStop(z=r[2], arrives_ms=int(r[0]) % period, leaves_ms=int(r[1]) % period)
             for r in runs if r[1] - r[0] >= LIFT_DWELL_MS]
    return period, stops


@lru_cache(maxsize=4)
def load_lifts(db_path: str) -> tuple[Lift, ...]:
    """The lifts a walk may ride (V382), from the world database: every spawned `gameobject`
    of type 11 whose path in `TransportAnimation.dbc` goes straight up and down (`LIFT_FLAT_
    YARDS`) between two stops `LIFT_RISE_YARDS` apart or more. Thunder Bluff's four Mesa
    Elevators and the Undercity's three Undervators among them; the doors of the Undercity's
    lift shafts move 5 and 8 yards and are none. None when the database cannot be read."""
    from jev.play.world_knowledge import readonly_uri

    try:
        con = sqlite3.connect(readonly_uri(pathlib.Path(db_path)), uri=True, timeout=1)
        try:
            spawns = con.execute(
                "select g.guid, g.id, t.name, g.map, g.position_x, g.position_y, g.position_z"
                " from world_gameobject g join world_gameobject_template t on t.entry = g.id"
                " where t.type = 11 order by g.guid").fetchall()
            paths: dict[int, list] = {}
            for entry, t, x, y, z in con.execute(
                    "select c1, c2, c3, c4, c5 from dbc_TransportAnimation order by c1, c2"):
                paths.setdefault(entry, []).append((t, _as_float(x), _as_float(y), _as_float(z)))
        finally:
            con.close()
    except sqlite3.Error:
        return ()
    found = []
    for guid, entry, name, map_id, x, y, z in spawns:
        path = paths.get(entry)
        if not path or max(math.hypot(px, py) for _, px, py, _ in path) > LIFT_FLAT_YARDS:
            continue
        cycle = lift_stops((t, pz) for t, _, _, pz in path)
        if cycle is None:
            continue
        period, stops = cycle
        if len(stops) != 2:
            continue
        stops.sort(key=lambda s: s.z)
        if stops[1].z - stops[0].z < LIFT_RISE_YARDS:
            continue
        z0 = float(z)
        found.append(Lift(guid=guid, entry=entry, name=name or "lift", map_id=map_id,
                          x=float(x), y=float(y), period_ms=period,
                          stops=tuple(LiftStop(z0 + s.z, s.arrives_ms, s.leaves_ms)
                                      for s in stops)))
    rise = {lift.guid: lift.stops[1].z - lift.stops[0].z for lift in found}
    return tuple(lift for lift in found
                 if not any(other.guid != lift.guid and other.map_id == lift.map_id
                            and math.dist((other.x, other.y), (lift.x, lift.y)) <= LIFT_SHAFT_YARDS
                            and rise[other.guid] > rise[lift.guid] for other in found))


def lift_decks(query: PathQuery, map_id: int, stop: Point) -> list[Point]:
    """The navmesh's floors beside a lift's stop (`LIFT_DECK_ABOVE`, `LIFT_DECK_BELOW`), the
    nearest the shaft on each, nearest the stop's height first, `LIFT_DECKS` at most."""
    x, y, z = stop
    floors: list[tuple[float, float, Point]] = []        # (|dz|, distance, spot)
    for radius in LIFT_DECK_RINGS:
        for k in range(LIFT_DECK_BEARINGS if radius else 1):
            angle = 2 * math.pi * k / LIFT_DECK_BEARINGS
            probe = (x + radius * math.cos(angle), y + radius * math.sin(angle))
            for height in (z, z - LIFT_DECK_BELOW / 2, z - LIFT_DECK_BELOW):
                snapped = query.path(map_id, (*probe, height), (*probe, height))
                if (snapped.status not in (PathStatus.COMPLETE, PathStatus.PARTIAL)
                        or not snapped.points):
                    continue
                spot = tuple(snapped.points[0])
                if (math.dist(spot[:2], probe) > 1.0
                        or not -LIFT_DECK_BELOW <= spot[2] - z <= LIFT_DECK_ABOVE):
                    continue
                gap = math.dist(spot[:2], (x, y))
                same = [i for i, f in enumerate(floors) if abs(f[2][2] - spot[2]) < LIFT_SAME_FLOOR]
                if not same:
                    floors.append((abs(spot[2] - z), gap, spot))
                elif gap < floors[same[0]][1]:
                    floors[same[0]] = (abs(spot[2] - z), gap, spot)
    floors.sort(key=lambda f: f[0])
    return [f[2] for f in floors[:LIFT_DECKS]]


@dataclass(frozen=True)
class Path:
    """Waypoints in **world yards**, start first, destination last.

    Through a teleport (V305), `points[:jump]` walk into its trigger and `points[jump:]`
    walk on from where it puts the character: the leg between is the jump, not a walk."""

    status: PathStatus
    points: tuple[Point, ...] = ()
    source: str = ""
    detail: str = ""
    teleport: Teleport | LiftLeg | None = None   # or a lift's ride (V382)
    jump: int = 0
    # A walk refused through a death camp (`route_memory.CAMP_REFUSED`): where the camp's
    # death lies, (x, y), for whoever waits for it to end (V334).
    camp: tuple[float, float] | None = None

    @property
    def usable(self) -> bool:
        """Worth walking? A partial path is: it goes most of the way, and the follower
        finds out about the rest by arriving and re-planning."""
        return self.status in (PathStatus.COMPLETE, PathStatus.PARTIAL) and len(self.points) >= 2

    def length_yards(self) -> float:
        """Along the route, walked: a jump through a teleport is no yards of walking."""
        return sum(
            math.dist(a, b) for i, (a, b) in enumerate(zip(self.points, self.points[1:],
                                                           strict=False))
            if self.teleport is None or i != self.jump - 1
        )

    def walk_in(self) -> Path:
        """The walk into the teleport's trigger: the route as far as the jump."""
        if self.teleport is None:
            return self
        return Path(self.status, self.points[:self.jump], self.source, self.detail)


def stop_short_of(path: Path, yards: float) -> Path | None:
    """The same route ending `yards` before its end, measured along it: where a caster
    stands to cast at what waits at the end (V167). `None` when the route is no longer
    than that: the start is within `yards` of the end already. A route through a teleport
    is not shortened: where to stand is planned from where the teleport puts the character
    (V305)."""
    if yards <= 0 or path.teleport is not None:
        return path
    points = list(path.points)
    left = yards
    while len(points) >= 2:
        last, before = points[-1], points[-2]
        leg = math.dist(before, last)
        if leg > left:
            f = (leg - left) / leg
            end = tuple(b + (a - b) * f for a, b in zip(last, before, strict=True))
            return Path(path.status, (*points[:-1], end), path.source,
                        (path.detail + "; " if path.detail else "") + f"{yards:.0f} yards short")
        left -= leg
        points.pop()
    return None


class PathQuery(Protocol):
    def path(self, map_id: int, start: Point, end: Point) -> Path: ...
    def close(self) -> None: ...


class MmapQuery:
    """Talks to the `jevpath` sidecar, one long-lived process per map.

    Long-lived because loading is the expensive part and querying is not: map 0 is 513
    tiles and 560 MB, which takes 2.2 seconds and 577 MB resident to load and then
    answers in microseconds. A process per query would pay that every time.

    The sidecar prints a `ready` line once its tiles are in, so start-up is waited on
    rather than guessed at.
    """

    def __init__(self, binary: str | pathlib.Path, mmaps_dir: str | pathlib.Path,
                 timeout_s: float = 60.0, launcher: tuple[str, ...] = (),
                 checkpoint: Callable[[], None] | None = None) -> None:
        if timeout_s <= 0:
            raise ValueError("sidecar timeout must be positive")
        self.binary = str(binary)
        self.mmaps_dir = str(mmaps_dir)
        self.timeout_s = timeout_s
        self.checkpoint = checkpoint
        # A prefix to run the sidecar somewhere else. The client is a Windows process and
        # the navmesh, the tiles and the compiler all live on the Linux side, so the
        # planner is reached through `wsl.exe` rather than cross-compiled. Planning is a
        # once-per-leg step, not a hot-loop one, so the boundary costs nothing that
        # matters.
        self.launcher = tuple(launcher)
        self._procs: dict[int, subprocess.Popen] = {}
        # One lock a map (V338): a map's sidecar answers one request at a time, and the maps'
        # are separate processes. One lock for all of them, in a farm process of about 80 bots
        # sharing this query (`hive.farm`), queued every plan behind every other map's, and
        # behind a map's tiles loading at its sidecar's start (2.2 s for map 0). `_guard`
        # keeps the maps' locks and sidecars.
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}

    @staticmethod
    def _dispose(proc: subprocess.Popen, *, close_stdout: bool = True) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader = getattr(proc, "_jev_reader", None)
        if close_stdout and reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)          # it reads to the end the kill made
        close_stdout = close_stdout and (reader is None or not reader.is_alive())
        for stream in (proc.stdin, proc.stdout if close_stdout else None):
            if stream is not None:
                stream.close()

    @staticmethod
    def _lines(proc: subprocess.Popen) -> queue.Queue:
        """The sidecar's lines as it writes them, read by one thread for the life of the
        process. A thread started for each reply, as before, was a thread started and joined
        per planner query, and a farm process of 79 bots asks thousands a minute: its reads
        were 9.4% of its samples (py-spy, 29 Sep). Ends at the end of the output, as a kill
        makes; `""` is the end, an exception one the read raised."""
        lines = getattr(proc, "_jev_lines", None)
        if lines is None:
            lines = queue.Queue()

            def pump():
                try:
                    while True:
                        line = proc.stdout.readline()
                        lines.put(line)
                        if not line:
                            return
                except Exception as exc:
                    lines.put(exc)

            reader = threading.Thread(target=pump, name="jevpath-read", daemon=True)
            proc._jev_lines, proc._jev_reader = lines, reader
            reader.start()
        return lines

    def _readline(self, proc: subprocess.Popen) -> str:
        # Windows pipes cannot be passed to select(). A single bounded reader works on
        # both sides of the WSL boundary; cancellation kills the process to unblock it.
        lines = self._lines(proc)
        reader = proc._jev_reader
        deadline = time.monotonic() + self.timeout_s
        try:
            while True:
                if self.checkpoint is not None:
                    self.checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"sidecar did not answer within {self.timeout_s:g}s")
                try:
                    value = lines.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    if not reader.is_alive() and lines.empty():
                        return ""           # read to its end before: gone, as a read says
                    continue
                if isinstance(value, Exception):
                    raise value
                return value
        except BaseException:
            self._dispose(proc, close_stdout=False)
            raise
        finally:
            if proc.poll() is not None:
                reader.join(timeout=1)
                if not reader.is_alive():
                    proc.stdout.close()

    def _lock(self, map_id: int) -> threading.Lock:
        """The lock of `map_id`'s sidecar (V338)."""
        with self._guard:
            lock = self._locks.get(map_id)
            if lock is None:
                lock = self._locks[map_id] = threading.Lock()
            return lock

    def _proc(self, map_id: int) -> subprocess.Popen | None:
        """`map_id`'s sidecar, started if it is not running; the map's lock held."""
        with self._guard:
            proc = self._procs.get(map_id)
        if proc is not None and proc.poll() is None:
            return proc
        if not self.launcher and not pathlib.Path(self.binary).exists():
            return None
        # CREATE_NO_WINDOW, because the planner must not steal focus from the game.
        # Launched through `wsl.exe` on Windows it opens a console, Windows raises
        # that console, and the next keypress the bot sends is refused by its own
        # focus guard — which presents as "could not type /target" a full minute
        # later, in a completely different part of the run.
        flags = 0x08000000 if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [*self.launcher, self.binary, self.mmaps_dir, str(map_id)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=flags,
        )
        ready = self._readline(proc)
        if '"ready"' not in ready:
            self._dispose(proc)
            return None
        with self._guard:
            self._procs[map_id] = proc
        return proc

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        with self._lock(map_id):
            try:
                proc = self._proc(map_id)
            except (OSError, ValueError) as exc:
                return Path(PathStatus.UNAVAILABLE, source="mmap", detail=str(exc))
            if proc is None:
                return Path(PathStatus.UNAVAILABLE, source="mmap",
                            detail=f"no sidecar at {self.binary} for map {map_id}")
            try:
                proc.stdin.write(
                    f"{start[0]:.3f} {start[1]:.3f} {start[2]:.3f} "
                    f"{end[0]:.3f} {end[1]:.3f} {end[2]:.3f}\n"
                )
                proc.stdin.flush()
                line = self._readline(proc)
            except (OSError, ValueError) as exc:
                return Path(PathStatus.UNAVAILABLE, source="mmap", detail=str(exc))

        if not line:
            return Path(PathStatus.UNAVAILABLE, source="mmap", detail="sidecar went away")
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            return Path(PathStatus.UNAVAILABLE, source="mmap",
                        detail=f"unparseable reply: {line[:120]}")

        status = doc.get("status", "error")
        if status not in {s.value for s in PathStatus}:
            return Path(PathStatus.NOPATH, source="mmap", detail=doc.get("detail", status))
        return Path(
            status=PathStatus(status),
            points=tuple(tuple(p) for p in doc.get("points", [])),
            source="mmap",
            detail=doc.get("detail", ""),
        )

    def close(self) -> None:
        with self._guard:
            maps = list(self._procs)
        for map_id in maps:
            with self._lock(map_id), self._guard:
                proc = self._procs.pop(map_id, None)
            if proc is not None:
                self._dispose(proc)


class RecordedQuery:
    """Polylines walked by hand (`DECISIONS.md` V5), for legs a navmesh gets wrong.

    Deliberately a seam with nothing behind it yet. The recorder is real work and the
    navmesh may make most of it unnecessary — which is exactly why the backend order
    matters, and why this is not being filled in speculatively. It exists so that when a
    door or a boat needs one, there is a place to put it that nothing above has to know
    about.
    """

    def __init__(self, routes_dir: str | pathlib.Path) -> None:
        self.routes_dir = pathlib.Path(routes_dir)

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        return Path(PathStatus.UNAVAILABLE, source="recorded",
                    detail="no recorded routes yet")

    def close(self) -> None:
        return


class FirstAvailable:
    """Ask each backend in turn and take the first usable answer.

    Order is the point (`DECISIONS.md` V19): the navmesh first because it is free and
    already correct for most ground, recorded routes second because they cost human time
    and only exist where the mesh is wrong.
    """

    def __init__(self, *backends: PathQuery) -> None:
        self.backends = backends

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        last = Path(PathStatus.NOPATH, detail="no backends")
        for backend in self.backends:
            result = backend.path(map_id, start, end)
            if result.usable:
                return result
            last = result
        return last

    def close(self) -> None:
        for backend in self.backends:
            backend.close()


class TeleportQuery:
    """The planner, through teleports where walking does not get there (V305).

    The route asked for is kept unless it fails - it ends farther than `LINK_NEAR_YARDS`
    from where it was to go, or does not end at all - or is `LINK_FAR_LONGER` times the
    straight line. Then each teleport on the map is tried: a walk into its trigger's middle,
    stopping at least `LINK_ROOM_YARDS` inside, and a walk on from its exit, both complete.
    The shortest such way is taken, if it is shorter than a route that got there. Only one
    teleport a plan, and only those that land on the map they stand on (`load_teleports`):
    where the first puts the character, the walk is planned again (`jev.run.client`).

    From Darnassus the mesh's way to Rut'theran ends at the edge of the tree, 1,227 yards
    over the village; through the portal (areatrigger 527) it is a walk of 455 yards to the
    portal and 97 on from where it puts the character to Nessa Shadowsong (28 Sep).
    """

    def __init__(self, inner: PathQuery, teleports: Iterable[Teleport],
                 lifts: Iterable[Lift] = (), can_ride: Callable[[], bool] | None = None,
                 rideable: Callable[[LiftLeg], bool] | None = None) -> None:
        self.inner = inner
        # Which legs the follower rides (V407): the live follower's keys only by a platform's
        # times and between decks at its stops' heights; `None`, every leg.
        self.rideable = rideable
        self.teleports = tuple(teleports)
        self.on_map: dict[int, tuple[Teleport, ...]] = {}
        for teleport in self.teleports:
            self.on_map[teleport.map_id] = (*self.on_map.get(teleport.map_id, ()), teleport)
        # Lifts (V382), ridden only where the walk's follower can ride one (`can_ride`): the
        # live client's keys cannot see a platform, and its radio paints no height.
        self.lifts = tuple(lifts)
        self.can_ride = can_ride
        self.legs_on_map: dict[int, tuple[LiftLeg, ...]] = {}
        for lift in self.lifts:
            self.legs_on_map[lift.map_id] = (*self.legs_on_map.get(lift.map_id, ()), *lift.legs())
        self._walks_on: dict[tuple, tuple[float, Path]] = {}    # (map, trigger, end) -> kept

    def estimate(self):
        """The planner for a walk's cost (`jev.run.client.Client.plan_to`): the one below's
        estimate, through the same teleports and lifts."""
        estimate = getattr(self.inner, "estimate", None)
        return (TeleportQuery(estimate(), self.teleports, self.lifts, self.can_ride,
                              self.rideable)
                if callable(estimate) else self)

    def _legs(self, map_id: int) -> tuple[LiftLeg, ...]:
        legs = self.legs_on_map.get(map_id, ())
        if not legs or self.can_ride is None or not self.can_ride():
            return ()
        return legs if self.rideable is None else tuple(leg for leg in legs if self.rideable(leg))

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        direct = self.inner.path(map_id, start, end)
        teleports = self.on_map.get(map_id, ())
        legs = self._legs(map_id)
        # A point asked for itself is a floor looked for, not a walk.
        if (not teleports and not legs) or math.dist(start, end) < LINK_ROOM_YARDS:
            return direct
        origin = direct.points[0] if direct.points else start
        straight = math.dist(origin, end)
        limit = None
        if direct.usable and direct.status is PathStatus.COMPLETE:
            limit = direct.length_yards()
            if limit <= LINK_FAR_LONGER * straight:
                return direct
        elif direct.usable and math.dist(direct.points[-1], end) <= LINK_NEAR_YARDS:
            return direct
        best = None
        for teleport in teleports:
            least = math.dist(origin, teleport.at) + math.dist(teleport.exit, end)
            if (least + LINK_COST_YARDS >= limit if limit is not None
                    else least > LINK_REACH * straight + LINK_SLACK_YARDS):
                continue
            walk_on = self._walk_on(map_id, teleport, end)
            if walk_on.status is not PathStatus.COMPLETE or not walk_on.points:
                continue
            walk_in = self.inner.path(map_id, start, teleport.at)
            if (walk_in.status is not PathStatus.COMPLETE or len(walk_in.points) < 2
                    or teleport.room(walk_in.points[-1]) < LINK_ROOM_YARDS):
                continue
            length = walk_in.length_yards() + walk_on.length_yards() + LINK_COST_YARDS
            if (limit is None or length < limit) and (best is None or length < best[0]):
                best = (length, teleport, walk_in.points, walk_on)
        for leg in legs:
            if (math.dist(origin[:2], leg.board[:2]) > LIFT_NEAR_YARDS
                    and math.dist(end[:2], leg.alight[:2]) > LIFT_NEAR_YARDS):
                continue
            least = math.dist(origin, leg.board) + math.dist(leg.alight, end) + leg.cost_yards()
            if (least >= limit if limit is not None
                    else least > LINK_REACH * straight + LINK_SLACK_YARDS + leg.cost_yards()):
                continue
            ridden = self._ride(map_id, leg, start, end)
            if ridden is None:
                continue
            placed, walk_in, walk_on = ridden
            length = Path(PathStatus.COMPLETE, walk_in).length_yards() + walk_on.length_yards() \
                + leg.cost_yards()
            if (limit is None or length < limit) and (best is None or length < best[0]):
                best = (length, placed, walk_in, walk_on)
        if best is None:
            return direct
        _, link, walk_in, walk_on = best
        label = (link.label if isinstance(link, LiftLeg)
                 else f"areatrigger {link.trigger_id} ({link.name})")
        return Path(PathStatus.COMPLETE, tuple(walk_in) + walk_on.points, walk_on.source,
                    f"through {label}", teleport=link, jump=len(walk_in))

    def _ride(self, map_id: int, leg: LiftLeg, start: Point, end: Point):
        """The way through a lift (V382): a walk to a deck at the stop it is boarded at and a
        walk on from one at the stop it is left at, both complete; the deck nearest each stop's
        height first (`lift_decks`). `None` when there is no such way."""
        decks = self._decks(map_id, leg.board)
        walks_in: list = []                      # each deck's walk in, planned once, lazily

        def walk_in(i: int):
            while len(walks_in) <= i:
                at = decks[len(walks_in)]
                walk = self.inner.path(map_id, start, at)
                last = walk.points[-1] if walk.points else None
                ok = (walk.status is PathStatus.COMPLETE and last is not None
                      and math.dist(last[:2], at[:2]) <= LIFT_BOARD_YARDS
                      and abs(last[2] - at[2]) <= LIFT_DECK_ABOVE)
                walks_in.append((walk.points if len(walk.points) >= 2 else (last, last)) if ok
                                else None)
            return walks_in[i]

        for exit in self._decks(map_id, leg.alight):
            walk_on = self._walk_on(map_id, (leg.lift.guid, leg.to, exit), end, exit)
            if walk_on.status is not PathStatus.COMPLETE or not walk_on.points:
                continue
            for i, at in enumerate(decks):
                points = walk_in(i)
                if points is None:
                    continue
                placed = leg.placed(at, exit)
                if self.rideable is None or self.rideable(placed):
                    return placed, points, walk_on
            # Every follower boards and leaves at any deck but the live keys (V407): the first
            # deck walked on from is the leg's, as before.
            if self.rideable is None or not any(w is not None for w in walks_in):
                return None
        return None

    def _decks(self, map_id: int, stop: Point) -> list[Point]:
        key = (map_id, tuple(round(v, 1) for v in stop))
        decks = _DECKS.get(key)
        if decks is None:
            decks = _DECKS[key] = lift_decks(self.inner, map_id, stop)
        return decks

    def _walk_on(self, map_id: int, teleport, end: Point, start: Point | None = None) -> Path:
        """The walk on from `teleport`'s exit (or `start`) to `end`, kept `WALK_ON_KEEP_S`."""
        now = time.monotonic()
        key = (map_id, getattr(teleport, "trigger_id", teleport), tuple(end))
        kept = self._walks_on.get(key)
        if kept is not None and now - kept[0] <= WALK_ON_KEEP_S:
            return kept[1]
        walk_on = self.inner.path(map_id, teleport.exit if start is None else start, end)
        self._walks_on = {k: v for k, v in self._walks_on.items()
                          if now - v[0] <= WALK_ON_KEEP_S}
        self._walks_on[key] = (now, walk_on)
        return walk_on

    def close(self) -> None:
        self.inner.close()
