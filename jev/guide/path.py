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
to Rut'theran, which no walk on the tree reaches.

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


@dataclass(frozen=True)
class Path:
    """Waypoints in **world yards**, start first, destination last.

    Through a teleport (V305), `points[:jump]` walk into its trigger and `points[jump:]`
    walk on from where it puts the character: the leg between is the jump, not a walk."""

    status: PathStatus
    points: tuple[Point, ...] = ()
    source: str = ""
    detail: str = ""
    teleport: Teleport | None = None
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
        self._lock = threading.Lock()

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

    def _proc(self, map_id: int) -> subprocess.Popen | None:
        with self._lock:
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
            self._procs[map_id] = proc
            return proc

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        try:
            proc = self._proc(map_id)
        except (OSError, ValueError) as exc:
            return Path(PathStatus.UNAVAILABLE, source="mmap", detail=str(exc))
        if proc is None:
            return Path(PathStatus.UNAVAILABLE, source="mmap",
                        detail=f"no sidecar at {self.binary} for map {map_id}")
        try:
            with self._lock:
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
        with self._lock:
            for proc in self._procs.values():
                self._dispose(proc)
            self._procs.clear()


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

    def __init__(self, inner: PathQuery, teleports: Iterable[Teleport]) -> None:
        self.inner = inner
        self.teleports = tuple(teleports)
        self.on_map: dict[int, tuple[Teleport, ...]] = {}
        for teleport in self.teleports:
            self.on_map[teleport.map_id] = (*self.on_map.get(teleport.map_id, ()), teleport)
        self._walks_on: dict[tuple, tuple[float, Path]] = {}    # (map, trigger, end) -> kept

    def estimate(self):
        """The planner for a walk's cost (`jev.run.client.Client.plan_to`): the one below's
        estimate, through the same teleports."""
        estimate = getattr(self.inner, "estimate", None)
        return TeleportQuery(estimate(), self.teleports) if callable(estimate) else self

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        direct = self.inner.path(map_id, start, end)
        teleports = self.on_map.get(map_id, ())
        # A point asked for itself is a floor looked for, not a walk.
        if not teleports or math.dist(start, end) < LINK_ROOM_YARDS:
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
                best = (length, teleport, walk_in, walk_on)
        if best is None:
            return direct
        _, teleport, walk_in, walk_on = best
        return Path(PathStatus.COMPLETE, walk_in.points + walk_on.points, walk_in.source,
                    f"through areatrigger {teleport.trigger_id} ({teleport.name})",
                    teleport=teleport, jump=len(walk_in.points))

    def _walk_on(self, map_id: int, teleport: Teleport, end: Point) -> Path:
        """The walk on from `teleport`'s exit to `end`, kept `WALK_ON_KEEP_S`."""
        now = time.monotonic()
        key = (map_id, teleport.trigger_id, tuple(end))
        kept = self._walks_on.get(key)
        if kept is not None and now - kept[0] <= WALK_ON_KEEP_S:
            return kept[1]
        walk_on = self.inner.path(map_id, teleport.exit, end)
        self._walks_on = {k: v for k, v in self._walks_on.items()
                          if now - v[0] <= WALK_ON_KEEP_S}
        self._walks_on[key] = (now, walk_on)
        return walk_on

    def close(self) -> None:
        self.inner.close()
