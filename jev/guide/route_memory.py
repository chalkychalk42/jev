"""Where walking got stuck, and the way round that worked. Learned, kept, applied.

The planner's navmesh is the server's, built for creatures, and it treats some tree gaps,
fences and ledges as open ground: one polygon of Northshire spans both sides of a rail
fence. A character stops at such a spot every time a route passes it, and no re-plan can
help, because the mesh has nothing to route around (measured 23 September: marking the
fence's polygon expensive changed nothing, since start and destination lay in it).

So the follower's own experience is the map of those spots. When a planned leg is blocked
and the follower's detour gets the character past it, the spot and the point the detour
reached are remembered; every later route whose segment passes the spot goes by that point
instead. Learned once, then never walked into again. World yards, keyed by map, so a
passage learned from one zone's frame applies in another's.

Some spots have no way round for a detour to find. At Echo Ridge Mine the mesh steps from
a grass slope onto the mine's platform across a log the client does not let a character
over, and the slope beside it ends in a pocket under the platform's pit prop: every detour
from there met rock, log or prop (run 20260923T184413-a386ff). Such a spot is remembered
as **blocked**, and `AvoidingQuery` asks the planner for routes that stay clear of it: by
a point on a ring round the spot, both halves of the route passing wide of it. The mesh
knows the platform's open front; it only had to be asked for a route that uses it.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path as FilePath

from jev.guide.path import Path, PathStatus, Point
from jev.persist import atomic_json

# Two stuck spots this close are the same obstacle, and the newer escape replaces the older.
MERGE_YARDS = 3.0
# A route segment this close to a stuck spot would walk into it again.
PASS_YARDS = 2.0
# Bounded: the oldest passages go first once a map has this many.
MAX_PER_MAP = 400
# Blocked spots: two this close are one, and a route passing within `AVOID_YARDS` of one,
# at a height within `AVOID_HEIGHT` of it, would walk into it again. Tight, because the
# way round at Echo Ridge passes two yards from the crossing that is blocked.
BLOCK_MERGE_YARDS = 1.0
# Blocked this many times before plans avoid it. Once is weak evidence: inside Northshire
# Abbey a detour cannot get past anything, so every bump in its halls became a "blocked"
# spot, and plans routed round the Abbey's only doorway - ten minutes of wandering and a
# failed quest step (run 20260924T001027-84b25c). A log across a crossing blocks every
# time; a hall blocks once.
BLOCK_CONFIRM_HITS = 2
AVOID_YARDS = 1.5
AVOID_HEIGHT = 4.0
# Where to look for a way round a blocked spot: rings about it, twelve bearings each.
VIA_RINGS = (4.0, 8.0, 12.0, 18.0)
VIA_BEARINGS = 12


@dataclass
class Passage:
    map_id: int
    x: float            # where the character stopped
    y: float
    via_x: float        # where the detour that got past it took the character
    via_y: float
    hits: int = 1
    updated: float = 0.0
    # The stopped spot's height: a passage is only taken on its own floor. Beside William
    # Pestle in the Lion's Pride Inn an escape learned on one floor bent every route to
    # him twelve yards towards the stairs, and the character walked the floor above him
    # (session 95). `None` for passages learned before heights were kept: `backfill` gives
    # them the navmesh's floor there when there is only one (`floors`); over several they
    # are not taken.
    z: float | None = None
    floors: int | None = None


@dataclass
class Block:
    map_id: int
    x: float            # where a planned leg met something the mesh does not know
    y: float
    z: float | None = None
    hits: int = 1
    updated: float = 0.0
    # The way the blocked leg was going, as a unit vector. A route near the spot is only
    # walking into it again when it heads that way: from the spot itself, back the way
    # the character came is the way out.
    dx: float | None = None
    dy: float | None = None


def nearest_height(points, xy: tuple[float, float]) -> tuple[float, float] | None:
    """How far a route passes from `xy`, and its height there - interpolated along the
    segment, as `RouteMemory.patch` compares it, not the nearest waypoint's."""
    best = None
    for a, b in pairwise(points):
        near = _nearest_on_segment(xy, a, b)
        distance = math.dist(near[:2], xy)
        if best is None or distance < best[0]:
            best = (distance, near[2])
    return best


def _segment_distance(p: tuple[float, float], a: Point, b: Point) -> float:
    return math.dist(p, _nearest_on_segment(p, a, b)[:2])


def _nearest_on_segment(p: tuple[float, float], a: Point, b: Point) -> tuple[float, float, float]:
    """The point of segment `a`-`b` nearest `p`, with the height there (`a`'s if unknown)."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    za = a[2] if len(a) > 2 else 0.0
    zb = b[2] if len(b) > 2 else za
    return ax + t * dx, ay + t * dy, za + t * (zb - za)


class RouteMemory:
    """Learned passages for one installation, persisted as JSON."""

    def __init__(self, file: str | FilePath | None = None):
        self.file = FilePath(file) if file is not None else None
        self.passages: list[Passage] = []
        self.blocked: list[Block] = []
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            self.passages = [Passage(**row) for row in document.get("passages", ())]
            self.blocked = [Block(**row) for row in document.get("blocked", ())]

    def block(self, map_id: int, spot: Point,
              heading: tuple[float, float] | None = None) -> Block:
        """Remember a spot a planned leg could not pass and no detour got round, and the
        way the leg was going when it met it."""
        now = time.time()
        z = spot[2] if len(spot) > 2 else None
        dx = dy = None
        if heading is not None and math.hypot(*heading) > 0:
            dx, dy = (v / math.hypot(*heading) for v in heading)
        for known in self.blocked:
            if known.map_id == map_id and math.dist((known.x, known.y), spot[:2]) <= BLOCK_MERGE_YARDS:
                known.hits += 1
                known.updated = now
                self._save()
                return known
        found = Block(map_id, spot[0], spot[1], z, updated=now, dx=dx, dy=dy)
        self.blocked.append(found)
        same_map = [b for b in self.blocked if b.map_id == map_id]
        if len(same_map) > MAX_PER_MAP:
            self.blocked.remove(min(same_map, key=lambda b: b.updated))
        self._save()
        return found

    def blocks(self, map_id: int) -> list[Block]:
        """The confirmed blocked spots on a map: those walking was stopped at repeatedly."""
        return [b for b in self.blocked if b.map_id == map_id and b.hits >= BLOCK_CONFIRM_HITS]

    def learn(self, map_id: int, stuck: tuple[float, float], via: tuple[float, float],
              z: float | None = None) -> Passage:
        """Remember that the character stopped at `stuck` (at height `z`) and got past by
        `via`."""
        now = time.time()
        for known in self.passages:
            if (known.map_id == map_id and math.dist((known.x, known.y), stuck) <= MERGE_YARDS
                    and (z is None or known.z is None or abs(known.z - z) < AVOID_HEIGHT)):
                known.x, known.y = stuck
                known.via_x, known.via_y = via
                if z is not None:
                    known.z, known.floors = z, None
                known.hits += 1
                known.updated = now
                self._save()
                return known
        passage = Passage(map_id, stuck[0], stuck[1], via[0], via[1], updated=now, z=z)
        self.passages.append(passage)
        same_map = [p for p in self.passages if p.map_id == map_id]
        if len(same_map) > MAX_PER_MAP:
            oldest = min(same_map, key=lambda p: p.updated)
            self.passages.remove(oldest)
        self._save()
        return passage

    def patch(self, map_id: int, path: Path) -> Path:
        """The same route, going by the learned point wherever a segment meets a known spot.

        A spot at the route's own start is not patched: the character is already there, and
        the follower's recovery is what gets it out.
        """
        if not path.usable:
            return path
        points = list(path.points)
        used: set[int] = set()
        out = [points[0]]
        for a, b in pairwise(points):
            for index, known in enumerate(self.passages):
                if (index in used or known.map_id != map_id or known.z is None
                        or math.dist((known.x, known.y), points[0][:2]) <= PASS_YARDS):
                    continue
                near = _nearest_on_segment((known.x, known.y), a, b)
                if (math.dist(near[:2], (known.x, known.y)) <= PASS_YARDS
                        and abs(near[2] - known.z) < AVOID_HEIGHT):
                    used.add(index)
                    out.append((known.via_x, known.via_y, near[2]))
            out.append(b)
        if len(out) == len(points):
            return path
        return Path(path.status, tuple(out), path.source,
                    (path.detail + "; " if path.detail else "") + f"{len(out) - len(points)} learned passage(s)")

    def backfill(self, map_id: int, surfaces: Callable[[float, float], list[float]]) -> int:
        """Give this map's passages learned without a height the navmesh's floor under
        their spot, where there is one floor (`surfaces` lists the heights found); over
        several they stay untaken. Each is looked up once, the answer kept."""
        changed = 0
        for known in self.passages:
            if known.map_id != map_id or known.z is not None or known.floors is not None:
                continue
            found = surfaces(known.x, known.y)
            known.floors = len(found)
            if len(found) == 1:
                known.z = found[0]
            changed += 1
        if changed:
            self._save()
        return changed

    def _save(self) -> None:
        if self.file is None:
            return
        atomic_json(self.file, {"format": 1, "passages": [asdict(p) for p in self.passages],
                                "blocked": [asdict(b) for b in self.blocked]})


def passes(path: Path, block: Block) -> bool:
    """Does the route go within `AVOID_YARDS` of the blocked spot, at its height, heading
    the way that was blocked there?"""
    for a, b in pairwise(path.points):
        x, y, z = _nearest_on_segment((block.x, block.y), a, b)
        if math.dist((x, y), (block.x, block.y)) >= AVOID_YARDS:
            continue
        if block.z is not None and abs(z - block.z) >= AVOID_HEIGHT:
            continue
        if block.dx is None or block.dy is None:
            return True
        if (b[0] - a[0]) * block.dx + (b[1] - a[1]) * block.dy > 0:
            return True
    return False


class AvoidingQuery:
    """The planner, asked for routes that stay clear of the spots walking found blocked.

    The first answer is kept whenever it passes none of them. Otherwise the planner is
    asked again by a point on rings round the first spot it passes, and the shortest pair
    of halves that both stay clear of every spot wins. When nothing does, the first answer
    is returned all the same: the follower's own recovery is still there, and a way that
    might be walked beats none. A spot the route ends on is not avoided; one it starts on
    is, when it knows which way was blocked - the way out is back the way it came.
    """

    def __init__(self, inner, memory: RouteMemory):
        self.inner, self.memory = inner, memory

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        direct = self.inner.path(map_id, start, end)
        if not direct.usable:
            return direct
        # A spot the route ends on cannot be avoided. One it starts on can, when the spot
        # knows which way was blocked; without that, every route from it would pass it.
        spots = [b for b in self.memory.blocks(map_id)
                 if math.dist((b.x, b.y), end[:2]) > AVOID_YARDS
                 and (b.dx is not None or math.dist((b.x, b.y), start[:2]) > AVOID_YARDS)]
        hit = next((b for b in spots if passes(direct, b)), None)
        if hit is None:
            return direct
        best: tuple[float, Path] | None = None
        z = hit.z if hit.z is not None else start[2]
        for radius in VIA_RINGS:
            for k in range(VIA_BEARINGS):
                angle = 2 * math.pi * k / VIA_BEARINGS
                via = (hit.x + radius * math.cos(angle), hit.y + radius * math.sin(angle), z)
                first = self.inner.path(map_id, start, via)
                if first.status is not PathStatus.COMPLETE or len(first.points) < 2:
                    continue
                if any(passes(first, b) for b in spots):
                    continue
                second = self.inner.path(map_id, first.points[-1], end)
                if not second.usable or (second.status is not direct.status):
                    continue
                if any(passes(second, b) for b in spots):
                    continue
                length = first.length_yards() + second.length_yards()
                if best is None or length < best[0]:
                    best = (length, Path(direct.status, first.points + second.points[1:],
                                         direct.source, f"round {len(spots)} blocked spot(s)"))
        if best is None:
            return Path(direct.status, direct.points, direct.source,
                        (direct.detail + "; " if direct.detail else "")
                        + "no way round a blocked spot")
        return best[1]

    def close(self) -> None:
        self.inner.close()
