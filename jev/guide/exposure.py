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

The route this layer is given has already gone round what the layers below keep clear of -
where the character died, where it keeps being attacked, a death camp - and its ways round are
planned below those layers (V257). So a way round passing any such place the route it would
replace did not pass is not taken (`keep`, V307): after each of the level 8 mage Merany's
first three deaths at one spot by Raven Hill's graveyard, its walk to a repairer was planned
"round 21 units that attack on sight" (23 the third time) and passed within 6 yards of the
spot, kept in the route memory since the first death (the hive, 28 Sep 12:15-12:23).
"""

from __future__ import annotations

import math
import time
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
# The search for a way round stops after this long, with the best found (V257): a level 9
# mage's repair, ranking twelve repairers through it from Windows, ran past its time with
# not a step walked (sessions 226-227).
EXPOSURE_BUDGET_S = 1.0
# The route is looked along at points this far apart, for spawns this far round each, and
# as far again as a far-wandering unit carries its reach (V255).
SAMPLE_YARDS = 30.0
WANDER_LOOK = 20.0
# Then each place the chosen route still passes spawns is gone round on its own (V270): spawns
# further apart than this along the route are separate places, each rounded between points this
# far before and after it, by a way at most `EXPOSURE_DETOUR` times the stretch it replaces and
# this much more. One way round the middle of every spawn a long route passed was no way round:
# the level 12 walk from Elwynn's Prowlers to Sentinel Hill, 3,261 yards, passed 27 spawns with
# or without it; round each place, 8, in 409 yards more.
PLACE_GAP_YARDS = 80.0
PLACE_SIDE_YARDS = 40.0
PLACE_SLACK_YARDS = 60.0
PLACES_BUDGET_S = 1.0


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
    for a ghost. `keep(map_id, start, end)` gives, for a walk, what a route passes of the
    places the layers below keep clear of (`DangerAvoidingQuery.keeper`)."""

    def __init__(self, inner, hostile: Callable[[int, float, float, float], list], *,
                 legs=None, clock: Callable[[], float] = time.monotonic, keep=None):
        # `legs` plans the ways round: below the layers that search rings of their own, whose
        # searches inside each of this one's legs multiplied the queries (V257).
        self.inner, self.hostile = inner, hostile
        self.legs = legs if legs is not None else inner
        self.clock = clock
        self.keep = keep

    def estimate(self):
        """The planner for a walk's cost, without this layer's search (V257)."""
        return self.inner

    def path(self, map_id: int, start, end) -> Path:
        direct = self.inner.path(map_id, start, end)
        try:
            kept = self.keep(map_id, start, end) if self.keep is not None else _nothing
            chosen = self._fewer(map_id, start, end, direct, kept)
        except Exception:
            return direct
        try:
            return self._round_places(map_id, start, end, chosen, kept)
        except Exception:
            return chosen

    def close(self) -> None:
        self.inner.close()

    def exposed(self, map_id: int, points, start, end) -> set[tuple[float, float]]:
        """The spawns a route passes within `EXPOSED_YARDS`, those round its ends aside."""
        seen: dict[tuple[float, float], float] = {}
        for x, y in _samples(points, SAMPLE_YARDS):
            for spawn in self.hostile(map_id, x, y, SAMPLE_YARDS + EXPOSED_YARDS + WANDER_LOOK):
                seen[(spawn[0], spawn[1])] = float(spawn[3]) if len(spawn) > 3 else 0.0
        return {s for s, extra in seen.items()
                if math.dist(s, start[:2]) > END_YARDS and math.dist(s, end[:2]) > END_YARDS
                and _distance_to(points, s) <= EXPOSED_YARDS + extra}

    @staticmethod
    def cost(length: float, exposed: int) -> float:
        return length / RUN_YARDS_PER_S + EXPOSURE_COST_S * exposed

    def _fewer(self, map_id: int, start, end, direct: Path, kept=None) -> Path:
        if (not direct.usable or direct.status is not PathStatus.COMPLETE
                or start[:2] == end[:2]):
            return direct
        passed = self.exposed(map_id, direct.points, start, end)
        if not passed:
            return direct
        kept = kept or _nothing
        allowed = kept(direct.points)
        length = direct.length_yards()
        best_cost, best = self.cost(length, len(passed)), direct
        limit = length * EXPOSURE_DETOUR
        cx = sum(x for x, _ in passed) / len(passed)
        cy = sum(y for _, y in passed) / len(passed)
        spread = max(math.dist((cx, cy), s) for s in passed)
        z = (start[2] + end[2]) / 2
        deadline = self.clock() + EXPOSURE_BUDGET_S
        for margin in EXPOSURE_MARGINS:
            radius = spread + EXPOSED_YARDS + margin
            for k in range(EXPOSURE_BEARINGS):
                if self.clock() >= deadline:
                    return best
                angle = 2 * math.pi * k / EXPOSURE_BEARINGS
                via = (cx + radius * math.cos(angle), cy + radius * math.sin(angle), z)
                first = self.legs.path(map_id, start, via)
                if first.status is not PathStatus.COMPLETE or len(first.points) < 2:
                    continue
                second = self.legs.path(map_id, first.points[-1], end)
                if second.status is not PathStatus.COMPLETE or len(second.points) < 2:
                    continue
                total = first.length_yards() + second.length_yards()
                if total > limit:
                    continue
                points = first.points + second.points[1:]
                if kept(points) - allowed:
                    continue                 # back through what the layers below went round
                cost = self.cost(total, len(self.exposed(map_id, points, start, end)))
                if cost < best_cost:
                    best_cost = cost
                    best = Path(direct.status, points, direct.source,
                                f"round {len(passed)} units that attack on sight")
        return best

    def _round_places(self, map_id: int, start, end, route: Path, kept=None) -> Path:
        """Go round each place `route` still passes spawns, on its own (V270)."""
        if (not route.usable or route.status is not PathStatus.COMPLETE
                or len(route.points) < 2 or start[:2] == end[:2]):
            return route
        points = [tuple(p) for p in route.points]
        cum = _arcs(points)
        passed = self.exposed(map_id, points, start, end)
        if not passed:
            return route
        deadline = self.clock() + PLACES_BUDGET_S
        out: list = []
        done, bent = 0.0, 0
        for place in _places(points, cum, passed):
            if self.clock() >= deadline:
                break
            lo = max(done, place[0][0] - PLACE_SIDE_YARDS)
            hi = min(cum[-1], place[-1][0] + PLACE_SIDE_YARDS)
            if hi - lo < 1.0:
                continue
            stretch = _between(points, cum, lo, hi)
            way = self._round_place(map_id, start, end, stretch, [s for _, s in place], deadline,
                                    kept)
            if way is stretch:
                continue
            _extend(out, _between(points, cum, done, lo))
            _extend(out, way)
            done, bent = hi, bent + 1
        if not bent:
            return route
        _extend(out, _between(points, cum, done, cum[-1]))
        return Path(route.status, tuple(out), route.source,
                    f"round {len(passed)} units that attack on sight")

    def _round_place(self, map_id: int, start, end, stretch: list, spawns: list,
                     deadline: float, kept=None) -> list:
        """The cheapest way from the stretch's first point to its last round these spawns, or
        the stretch itself; none passing a place kept clear of that the stretch does not."""
        a, b = stretch[0], stretch[-1]
        length = _length(stretch)
        best_cost = self.cost(length, len(self.exposed(map_id, stretch, start, end)))
        best = stretch
        kept = kept or _nothing
        allowed = kept(stretch)
        cx = sum(x for x, _ in spawns) / len(spawns)
        cy = sum(y for _, y in spawns) / len(spawns)
        spread = max(math.dist((cx, cy), s) for s in spawns)
        z = (a[2] + b[2]) / 2
        for margin in EXPOSURE_MARGINS:
            radius = spread + EXPOSED_YARDS + margin
            for k in range(EXPOSURE_BEARINGS):
                if self.clock() >= deadline:
                    return best
                angle = 2 * math.pi * k / EXPOSURE_BEARINGS
                via = (cx + radius * math.cos(angle), cy + radius * math.sin(angle), z)
                first = self.legs.path(map_id, a, via)
                if first.status is not PathStatus.COMPLETE or len(first.points) < 2:
                    continue
                second = self.legs.path(map_id, first.points[-1], b)
                if second.status is not PathStatus.COMPLETE or len(second.points) < 2:
                    continue
                way = [tuple(p) for p in first.points] + [tuple(p) for p in second.points[1:]]
                total = _length(way)
                if total > length * EXPOSURE_DETOUR + PLACE_SLACK_YARDS:
                    continue
                if kept(way) - allowed:
                    continue
                cost = self.cost(total, len(self.exposed(map_id, way, start, end)))
                if cost < best_cost:
                    best_cost, best = cost, way
        return best


def _nothing(points) -> frozenset:
    """Nothing kept clear of: a planner with no layer below that keeps anything."""
    return frozenset()


def _arcs(points) -> list[float]:
    """How far along the polyline each of its points lies."""
    out = [0.0]
    for a, b in zip(points, points[1:], strict=False):
        out.append(out[-1] + math.dist(a[:2], b[:2]))
    return out


def _length(points) -> float:
    return sum(math.dist(a[:2], b[:2]) for a, b in zip(points, points[1:], strict=False))


def _along(points, cum: list[float], p: tuple[float, float]) -> float:
    """How far along the polyline its point nearest `p` lies."""
    best, at = math.inf, 0.0
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy)
                                                  / length2))
        d = math.dist((a[0] + t * dx, a[1] + t * dy), p)
        if d < best:
            best, at = d, cum[i - 1] + t * math.sqrt(length2)
    return at


def _places(points, cum: list[float], passed) -> list:
    """The passed spawns by where along the route they are passed, in places: runs of spawns
    no further apart along it than `PLACE_GAP_YARDS`."""
    along = sorted((_along(points, cum, s), s) for s in passed)
    places = [[along[0]]]
    for item in along[1:]:
        if item[0] - places[-1][-1][0] > PLACE_GAP_YARDS:
            places.append([item])
        else:
            places[-1].append(item)
    return places


def _point_at(points, cum: list[float], s: float):
    """The point `s` along the polyline, height and all, and the index of the next vertex."""
    for i in range(1, len(points)):
        if cum[i] >= s:
            a, b = points[i - 1], points[i]
            f = 0.0 if cum[i] == cum[i - 1] else (s - cum[i - 1]) / (cum[i] - cum[i - 1])
            return tuple(a[k] + (b[k] - a[k]) * f for k in range(len(a))), i
    return tuple(points[-1]), len(points)


def _between(points, cum: list[float], lo: float, hi: float) -> list:
    """The polyline from `lo` along it to `hi`."""
    first, i = _point_at(points, cum, lo)
    last, j = _point_at(points, cum, hi)
    return [first, *points[i:j], last]


def _extend(out: list, more) -> None:
    """Add a piece of polyline, without repeating the point it starts at."""
    for p in more:
        if not out or math.dist(out[-1], p) > 0.01:
            out.append(p)
