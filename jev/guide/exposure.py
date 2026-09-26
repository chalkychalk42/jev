"""Routes that pass fewer units that attack on sight (V248).

A walk is attacked where it passes within reach of a hostile spawn: 103 of the 110 attacks on
the walking mage in sessions 195-219 began within 20 yards of one, about 4.3 a minute of
walking there, 0.4 at 20 to 30 yards and almost none beyond, and 23 of its 34 deaths came on
a walk - to a hunt's station, a merchant, a quest giver - in fights that began that way. The
spawns are the world's (`jev.world.hostiles`), those worth experience at the character's
level; the ones round the walk's start and end are its own business (a hunt walks to its
camp, a character stands where it stands).

Of the direct route and ways round the passed spawns (via points on rings round their
middle), the one that costs least is walked: its length, at a run, and `EXPOSURE_COST_S` for
each spawn passed within `EXPOSED_YARDS`. Anything that goes wrong here plans as before.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from jev.guide.path import Path, PathStatus

# A spawn this near a route is passed within its unit's reach.
EXPOSED_YARDS = 20.0
# A spawn this near the walk's start or end is not the route's to avoid.
END_YARDS = 30.0
# What passing one costs, in seconds of walking: some 0.4 attacks (about 6 s within 20 yards
# at 4.3 a minute), each a fight of about 15 s, and a share of a death's two minutes.
EXPOSURE_COST_S = 10.0
RUN_YARDS_PER_S = 7.0
# A way round at most this many times the direct route's length.
EXPOSURE_DETOUR = 1.6
# Via points: this many bearings on rings this far beyond the passed spawns' spread.
EXPOSURE_BEARINGS = 8
EXPOSURE_MARGINS = (15.0, 40.0)
# The route is looked along at points this far apart, for spawns this far round each.
SAMPLE_YARDS = 30.0


def _samples(points, step: float):
    """Points along a polyline, `step` apart, its ends included (x, y)."""
    if not points:
        return
    yield points[0][:2]
    for a, b in zip(points, points[1:], strict=False):
        length = math.dist(a[:2], b[:2])
        n = int(length // step)
        for i in range(1, n + 1):
            f = i * step / length
            yield (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        yield b[:2]


def _distance_to(points, p: tuple[float, float]) -> float:
    """How near a polyline passes `p`."""
    if len(points) == 1:
        return math.dist(points[0][:2], p)
    best = math.inf
    for a, b in zip(points, points[1:], strict=False):
        ax, ay, bx, by = a[0], a[1], b[0], b[1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy)
                                                  / length2))
        best = min(best, math.dist((ax + t * dx, ay + t * dy), p))
    return best


class ExposureQuery:
    """The planner, asked for routes that pass fewer hostile spawns. `hostile(map_id, x, y,
    radius)` gives the spawns round a point that attack this character (x, y, z each); none
    for a ghost."""

    def __init__(self, inner, hostile: Callable[[int, float, float, float], list]):
        self.inner, self.hostile = inner, hostile

    def path(self, map_id: int, start, end) -> Path:
        direct = self.inner.path(map_id, start, end)
        try:
            return self._fewer(map_id, start, end, direct)
        except Exception:
            return direct

    def close(self) -> None:
        self.inner.close()

    def exposed(self, map_id: int, points, start, end) -> set[tuple[float, float]]:
        """The spawns a route passes within `EXPOSED_YARDS`, those round its ends aside."""
        seen: set[tuple[float, float]] = set()
        for x, y in _samples(points, SAMPLE_YARDS):
            for spawn in self.hostile(map_id, x, y, SAMPLE_YARDS + EXPOSED_YARDS):
                seen.add((spawn[0], spawn[1]))
        return {s for s in seen
                if math.dist(s, start[:2]) > END_YARDS and math.dist(s, end[:2]) > END_YARDS
                and _distance_to(points, s) <= EXPOSED_YARDS}

    @staticmethod
    def cost(length: float, exposed: int) -> float:
        return length / RUN_YARDS_PER_S + EXPOSURE_COST_S * exposed

    def _fewer(self, map_id: int, start, end, direct: Path) -> Path:
        if (not direct.usable or direct.status is not PathStatus.COMPLETE
                or start[:2] == end[:2]):
            return direct
        passed = self.exposed(map_id, direct.points, start, end)
        if not passed:
            return direct
        length = direct.length_yards()
        best_cost, best = self.cost(length, len(passed)), direct
        limit = length * EXPOSURE_DETOUR
        cx = sum(x for x, _ in passed) / len(passed)
        cy = sum(y for _, y in passed) / len(passed)
        spread = max(math.dist((cx, cy), s) for s in passed)
        z = (start[2] + end[2]) / 2
        for margin in EXPOSURE_MARGINS:
            radius = spread + EXPOSED_YARDS + margin
            for k in range(EXPOSURE_BEARINGS):
                angle = 2 * math.pi * k / EXPOSURE_BEARINGS
                via = (cx + radius * math.cos(angle), cy + radius * math.sin(angle), z)
                first = self.inner.path(map_id, start, via)
                if first.status is not PathStatus.COMPLETE or len(first.points) < 2:
                    continue
                second = self.inner.path(map_id, first.points[-1], end)
                if second.status is not PathStatus.COMPLETE or len(second.points) < 2:
                    continue
                total = first.length_yards() + second.length_yards()
                if total > limit:
                    continue
                points = first.points + second.points[1:]
                cost = self.cost(total, len(self.exposed(map_id, points, start, end)))
                if cost < best_cost:
                    best_cost = cost
                    best = Path(direct.status, points, direct.source,
                                f"round {len(passed)} units that attack on sight")
        return best
