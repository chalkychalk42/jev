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

Measured on the first real query, Northshire courtyard to Marshal McBride — the leg that
defeated straight-line travel nine times:

    {"status":"complete","points":[[-8944.8,-120.2,83.3],
                                   [-8913.1,-141.3,82.4],
                                   [-8902.6,-162.6,82.9]]}

Three points, and the middle one is the corner of the Abbey.
"""

from __future__ import annotations

import json
import pathlib
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

Point = tuple[float, float, float]


class PathStatus(StrEnum):
    COMPLETE = "complete"        # reaches the destination
    PARTIAL = "partial"          # the mesh allows only part of the way
    NOPATH = "nopath"            # no route, or an endpoint is off the mesh
    UNAVAILABLE = "unavailable"  # the backend could not be asked at all


@dataclass(frozen=True)
class Path:
    """Waypoints in **world yards**, start first, destination last."""

    status: PathStatus
    points: tuple[Point, ...] = ()
    source: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Worth walking? A partial path is: it goes most of the way, and the follower
        finds out about the rest by arriving and re-planning."""
        return self.status in (PathStatus.COMPLETE, PathStatus.PARTIAL) and len(self.points) >= 2

    def length_yards(self) -> float:
        import math

        return sum(
            math.dist(a, b) for a, b in zip(self.points, self.points[1:], strict=False)
        )


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
        close_stdout = close_stdout and (reader is None or not reader.is_alive())
        for stream in (proc.stdin, proc.stdout if close_stdout else None):
            if stream is not None:
                stream.close()

    def _readline(self, proc: subprocess.Popen) -> str:
        # Windows pipes cannot be passed to select(). A single bounded reader works on
        # both sides of the WSL boundary; cancellation kills the process to unblock it.
        reply: queue.Queue = queue.Queue(maxsize=1)

        def read():
            try:
                reply.put(proc.stdout.readline())
            except Exception as exc:
                reply.put(exc)

        reader = threading.Thread(target=read, name="jevpath-read", daemon=True)
        proc._jev_reader = reader
        reader.start()
        deadline = time.monotonic() + self.timeout_s
        try:
            while True:
                if self.checkpoint is not None:
                    self.checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"sidecar did not answer within {self.timeout_s:g}s")
                try:
                    value = reply.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if isinstance(value, Exception):
                    raise value
                return value
        except BaseException:
            self._dispose(proc, close_stdout=False)
            raise
        finally:
            reader.join(timeout=1)
            if proc.poll() is not None and not reader.is_alive():
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


def default_query(root: str | pathlib.Path = ".",
                  mmaps_dir: str | pathlib.Path = "~/cmangos/run/bin/mmaps") -> FirstAvailable:
    root = pathlib.Path(root)
    return FirstAvailable(
        MmapQuery(root / "tools" / "jevpath" / "jevpath",
                  pathlib.Path(mmaps_dir).expanduser()),
        RecordedQuery(root / "content" / "routes"),
    )
