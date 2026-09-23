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
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path as FilePath

from jev.guide.path import Path, Point
from jev.persist import atomic_json

# Two stuck spots this close are the same obstacle, and the newer escape replaces the older.
MERGE_YARDS = 3.0
# A route segment this close to a stuck spot would walk into it again.
PASS_YARDS = 2.0
# Bounded: the oldest passages go first once a map has this many.
MAX_PER_MAP = 400


@dataclass
class Passage:
    map_id: int
    x: float            # where the character stopped
    y: float
    via_x: float        # where the detour that got past it took the character
    via_y: float
    hits: int = 1
    updated: float = 0.0


def _segment_distance(p: tuple[float, float], a: Point, b: Point) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


class RouteMemory:
    """Learned passages for one installation, persisted as JSON."""

    def __init__(self, file: str | FilePath | None = None):
        self.file = FilePath(file) if file is not None else None
        self.passages: list[Passage] = []
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            self.passages = [Passage(**row) for row in document.get("passages", ())]

    def learn(self, map_id: int, stuck: tuple[float, float], via: tuple[float, float]) -> Passage:
        """Remember that the character stopped at `stuck` and got past by `via`."""
        now = time.time()
        for known in self.passages:
            if known.map_id == map_id and math.dist((known.x, known.y), stuck) <= MERGE_YARDS:
                known.x, known.y = stuck
                known.via_x, known.via_y = via
                known.hits += 1
                known.updated = now
                self._save()
                return known
        passage = Passage(map_id, stuck[0], stuck[1], via[0], via[1], updated=now)
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
                if (index in used or known.map_id != map_id
                        or math.dist((known.x, known.y), points[0][:2]) <= PASS_YARDS):
                    continue
                if _segment_distance((known.x, known.y), a, b) <= PASS_YARDS:
                    used.add(index)
                    z = (a[2] + b[2]) / 2
                    out.append((known.via_x, known.via_y, z))
            out.append(b)
        if len(out) == len(points):
            return path
        return Path(path.status, tuple(out), path.source,
                    (path.detail + "; " if path.detail else "") + f"{len(out) - len(points)} learned passage(s)")

    def _save(self) -> None:
        if self.file is None:
            return
        atomic_json(self.file, {"format": 1, "passages": [asdict(p) for p in self.passages]})
