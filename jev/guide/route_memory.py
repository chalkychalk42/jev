"""Where walking got stuck and no detour got past, where the character died: kept, applied.

The planner's navmesh is the server's, built for creatures, and it treats some tree gaps,
fences and ledges as open ground. The follower's own detour gets past those each time.

Some spots have no way round for a detour to find. At Echo Ridge Mine the mesh steps from
a grass slope onto the mine's platform across a log the client does not let a character
over, and the slope beside it ends in a pocket under the platform's pit prop: every detour
from there met rock, log or prop (run 20260923T184413-a386ff). Such a spot is remembered
as **blocked**, and `AvoidingQuery` asks the planner for routes that stay clear of it: by
a point on a ring round the spot, both halves of the route passing wide of it. The mesh
knows the platform's open front; it only had to be asked for a route that uses it.

Where the character died is kept too, and walks keep clear of it for a while
(`DangerAvoidingQuery`); two deaths at one place within minutes make it a death camp, which
walks keep well clear of or do not go at all (V307).

The points escapes reached (`Passage`, V52) are no longer learned or taken (V178). An
escape lands wherever an unstick move took it, and every later route through its spot was
bent there in a straight line: through Northshire Abbey's front wall beside the door, and
up the Lion's Pride Inn's stairs (V137, V143). Those already learned stay in the file as
data.
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
from jev.learn.danger import LEVELS_ABOVE, LEVELS_BELOW
from jev.persist import atomic_json

# Bounded: the oldest blocked spots go first once a map has this many.
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
# Where the character died is kept clear of for a while (`DangerAvoidingQuery`): walks keep
# `DANGER_YARDS` from it, a mob's reach and a camp's, by a point on these rings, unless the
# way round is more than `DANGER_DETOUR` times the way through. Three deaths in twenty
# minutes at Jerod's Landing, the Defias camp on the way between Ma Stonefield and
# Princess's pumpkin patch, each walked straight through it (sessions 122 and 123).
DANGER_YARDS = 35.0
DANGER_S = 2 * 3600.0
DANGER_MERGE_YARDS = 15.0
DANGER_RINGS = (50.0, 70.0, 95.0)
DANGER_DETOUR = 2.0
# A cell the learned danger map calls hot is kept this far from (`jev.learn.danger`): its
# half-width and some of a mob's reach.
HOT_YARDS = 25.0
# A walk that starts or ends inside a spot's reach keeps the distance it has, less this
# much room to turn away; one that starts or ends closer than `SPOT_TURN_YARDS` plus
# `SPOT_MIN_KEEP` is at the spot, and goes by it. A character gets up 32 yards short of
# where it died (`TRAP_RECLAIM_YARDS`), inside the 35 kept from a death, and the walk on
# used to be let straight back through what had killed it: it died there again 42 s after
# getting up (session 142).
SPOT_TURN_YARDS = 10.0
SPOT_MIN_KEEP = 10.0
# Deaths counted per place (V307). A death within `CAMP_YARDS` of another at about its level,
# no more than `CAMP_WINDOW_S` after it, makes the place a death camp for that level for
# `CAMP_S`: walks keep `CAMP_YARDS` from each of its deaths however long the way round, and
# are refused where there is none. In the hive's runs begun 11:50-13:08 on 28 Sep, 172 of 406
# deaths came within 100 yards of the same character's death in the ten minutes before;
# Merany, a level 8 mage, died four times in 8 minutes at one spot by Raven Hill's graveyard,
# and after each of the first three got up 26 to 31 yards from it, and its walk to a repairer
# passed within 6 yards of it. A death counts for the levels a danger cell's attacks do (`jev.learn.danger`, V161): the hive
# keeps every character's deaths in one memory, and 348 on the Eastern Kingdoms at 12:23 were
# kept from every walk alike.
CAMP_YARDS = 100.0
CAMP_WINDOW_S = 600.0
CAMP_S = 3600.0
# A walk refused through a death camp (`CAMP_REFUSED`) is refused again without a search for
# `REFUSAL_S`, asked from and to within `REFUSAL_YARDS` of where it was with the same camp on
# its way through (review of 28 Sep). A refusal is two ring searches, up to 144 planner
# queries, and was asked again at each of `jev.run.client`'s 33 start heights, by each
# teleport's walk in and by every ranking of merchants (V309): about 4,700 queries a walk,
# where one merchant behind a camp could spend `RANKING_BUDGET_S` alone. A minute holds a
# walk's re-plans and a ranking's; the camp's own hour does not, as a camp beside it may end.
CAMP_REFUSED = "no way that keeps out of a death camp"
REFUSAL_S = 60.0
REFUSAL_YARDS = 5.0


@dataclass
class Passage:
    """Kept as data only (V178): where walking stopped and where its escape went."""

    map_id: int
    x: float            # where the character stopped
    y: float
    via_x: float        # where the detour that got past it took the character
    via_y: float
    hits: int = 1
    updated: float = 0.0
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


@dataclass
class Danger:
    map_id: int
    x: float            # where the character died
    y: float
    at: float = 0.0     # when, as wall time; the latest death within `DANGER_MERGE_YARDS`
    level: int | None = None        # the character's then, when read (V307): None counts for all
    camp_until: float | None = None  # wall time a death camp holds to (V307); None: none

    def camp(self, now: float) -> bool:
        """Is this death part of a death camp at `now` (V307)?"""
        return self.camp_until is not None and now < self.camp_until


def counts_for(died_at: int | None, level: int | None) -> bool:
    """Does a death at level `died_at` count for a character of `level`: from one level below
    the character's to two above, as a danger cell's attacks do (V161); either unread, yes."""
    return (died_at is None or level is None
            or level - LEVELS_BELOW <= died_at <= level + LEVELS_ABOVE)


def nearest_height(points, xy: tuple[float, float]) -> tuple[float, float] | None:
    """How far a route passes from `xy`, and its height there - interpolated along the
    segment, not the nearest waypoint's."""
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
    """Blocked spots and deaths for one installation, persisted as JSON."""

    def __init__(self, file: str | FilePath | None = None):
        self.file = FilePath(file) if file is not None else None
        self.passages: list[Passage] = []
        self.blocked: list[Block] = []
        self.dangers: list[Danger] = []
        if self.file is not None and self.file.exists():
            document = json.loads(self.file.read_text(encoding="utf-8"))
            self.passages = [Passage(**row) for row in document.get("passages", ())]
            self.blocked = [Block(**row) for row in document.get("blocked", ())]
            self.dangers = [Danger(**row) for row in document.get("dangers", ())]

    def died(self, map_id: int, spot: tuple[float, float], now: float | None = None,
             level: int | None = None) -> Danger:
        """Remember where the character died, at what level, for walks to keep clear of
        (`DANGER_S`); a death near another at about its level within `CAMP_WINDOW_S` makes
        both a death camp (V307), and one near a camp still held is a death of that camp,
        which it holds for `CAMP_S` more. The death kept, a camp's when it made or fell in one."""
        now = time.time() if now is None else now
        self.dangers = [d for d in self.dangers if now - d.at < DANGER_S or d.camp(now)]
        near = [d for d in self.dangers if d.map_id == map_id and counts_for(d.level, level)
                and math.dist((d.x, d.y), spot[:2]) <= CAMP_YARDS]
        recent = [d for d in near if now - d.at <= CAMP_WINDOW_S]
        # A camp's deaths may all be older than `CAMP_WINDOW_S` while it holds: a death there
        # is one of the camp's, or the character was never led out and walked back in
        # (review of 28 Sep).
        held = [d for d in near if d.camp(now)]
        found = next((d for d in near if math.dist((d.x, d.y), spot[:2]) <= DANGER_MERGE_YARDS),
                     None)
        if found is None:
            found = Danger(map_id, spot[0], spot[1], now, level)
            self.dangers.append(found)
        else:
            found.at, found.level = now, level if level is not None else found.level
        if recent or held:
            for death in (*recent, *held, found):
                death.camp_until = now + CAMP_S
        self._save()
        return found

    def dangers_on(self, map_id: int, now: float | None = None,
                   level: int | None = None) -> list[Danger]:
        """The places on a map the character died at within `DANGER_S`, and the death camps
        still held, that count at `level` (`counts_for`)."""
        now = time.time() if now is None else now
        return [d for d in self.dangers if d.map_id == map_id
                and (now - d.at < DANGER_S or d.camp(now)) and counts_for(d.level, level)]

    def camp_at(self, map_id: int, point: tuple[float, float], now: float | None = None,
                level: int | None = None) -> Danger | None:
        """A death of a death camp within `CAMP_YARDS` of `point` that counts at `level`, the
        nearest; `None` when the point lies in none (V307)."""
        now = time.time() if now is None else now
        camps = [d for d in self.dangers_on(map_id, now, level) if d.camp(now)
                 and math.dist((d.x, d.y), point[:2]) <= CAMP_YARDS]
        return min(camps, key=lambda d: math.dist((d.x, d.y), point[:2]), default=None)

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

    def _save(self) -> None:
        if self.file is None:
            return
        atomic_json(self.file, {"format": 1, "passages": [asdict(p) for p in self.passages],
                                "blocked": [asdict(b) for b in self.blocked],
                                "dangers": [asdict(d) for d in self.dangers]})


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


def near_route(path: Path, x: float, y: float, reach: float) -> bool:
    """Does the route come within `reach` of (x, y), in plan?"""
    return _passes(path.points, x, y, reach)


def _passes(points, x: float, y: float, reach: float) -> bool:
    """Does the polyline `points` come within `reach` of (x, y), in plan?"""
    if len(points) == 1:
        return math.dist(points[0][:2], (x, y)) < reach
    return any(_segment_distance((x, y), a, b) < reach for a, b in pairwise(points))


class DangerAvoidingQuery:
    """The planner, asked for routes that keep clear of where the character recently died
    (`RouteMemory.died`, `DANGER_YARDS`) and of where it keeps being attacked (`hot`, a
    learned `jev.learn.danger.DangerMap`'s cells, `HOT_YARDS`). A walk that starts or ends
    at such a place goes by it: a corpse run is a walk to one, and a hunt's camp is where it
    hunts. One that starts or ends inside its reach keeps the distance it has
    (`SPOT_TURN_YARDS`). A death camp (V307) is kept `CAMP_YARDS` off however long the way
    round, and a walk with none is refused. The deaths kept are those that count at the
    character's level (`level`, read once a plan); a ghost (`ghost`) keeps clear of none, as
    it passes hostile spawns unharmed (V248), and a corpse run is not refused its way. Anything
    that goes wrong here plans as before."""

    def __init__(self, inner, memory: RouteMemory, clock: Callable[[], float] = time.time,
                 hot: Callable[[int], list] | None = None,
                 level: Callable[[], int | None] | None = None,
                 ghost: Callable[[], bool] | None = None):
        self.inner, self.memory, self.clock = inner, memory, clock
        self.hot = hot
        self.level = level
        self.ghost = ghost
        # (when, map, start, end, camp) of each walk refused through a camp (`REFUSAL_S`).
        self._refused: list[tuple[float, int, tuple, tuple, tuple]] = []

    def path(self, map_id: int, start: Point, end: Point) -> Path:
        direct = self.inner.path(map_id, start, end)
        try:
            return self._round(map_id, start, end, direct)
        except Exception:
            return direct

    def _spots(self, map_id: int) -> list[tuple[float, float, float, str, bool]]:
        """(x, y, reach, why, camp) of every place a route keeps clear of on `map_id`."""
        if self.ghost is not None and self.ghost() is True:
            return []
        now = self.clock()
        level = self.level() if self.level is not None else None
        level = level if isinstance(level, int) else None
        spots = [(d.x, d.y, CAMP_YARDS, "a death camp", True) if d.camp(now)
                 else (d.x, d.y, DANGER_YARDS, "where the character died", False)
                 for d in self.memory.dangers_on(map_id, now, level)]
        if self.hot is not None:
            spots += [(x, y, HOT_YARDS, "where the character keeps being attacked", False)
                      for x, y, *_ in self.hot(map_id)]
        return spots

    def _kept(self, map_id: int, start: Point, end: Point) -> list:
        """The spots a walk from `start` to `end` keeps clear of, each with the reach it keeps:
        less where the walk starts or ends inside a spot's reach, and none at the spot."""
        def kept(x, y, reach) -> float:
            return min(reach, math.dist((x, y), start[:2]) - SPOT_TURN_YARDS,
                       math.dist((x, y), end[:2]) - SPOT_TURN_YARDS)

        return [(x, y, r, why, camp) for x, y, reach, why, camp in self._spots(map_id)
                if (r := kept(x, y, reach)) >= SPOT_MIN_KEEP]

    def keeper(self, map_id: int, start: Point, end: Point):
        """What a route of a walk from `start` to `end` passes of the spots this layer keeps
        clear of, as a set of their places: for the layers over it, whose ways round must not
        walk back through what this one went round (V307). Nothing, when anything goes wrong."""
        try:
            spots = self._kept(map_id, start, end)
        except Exception:
            spots = []

        def passed(points) -> frozenset:
            return frozenset((x, y) for x, y, reach, *_ in spots if _passes(points, x, y, reach))
        return passed

    def _round(self, map_id: int, start: Point, end: Point, direct: Path) -> Path:
        if not direct.usable or start[:2] == end[:2]:
            return direct
        spots = self._kept(map_id, start, end)
        hit = next((spot for spot in spots if near_route(direct, spot[0], spot[1], spot[2])), None)
        if hit is None:
            return direct
        camps = [spot for spot in spots if spot[4]]
        camp = next((spot for spot in camps if near_route(direct, *spot[:3])), None)
        now = self.clock()
        if camp is not None and self._refused_before(map_id, start, end, camp, now):
            return Path(PathStatus.NOPATH, (), direct.source, CAMP_REFUSED)
        # Round every spot within `DANGER_DETOUR` of the way through; through a death camp,
        # round every spot, then round the camps alone, however far (V307).
        tries = [(hit, spots, direct.length_yards() * DANGER_DETOUR)] if camp is None else [
            (hit, spots, math.inf), (camp, camps, math.inf)]
        for centre, keep, limit in tries:
            way = self._ring(map_id, start, end, direct, centre, keep, limit)
            if way is not None:
                return way
        if camp is not None:
            self._refused.append((now, map_id, tuple(start[:2]), tuple(end[:2]), camp[:2]))
            return Path(PathStatus.NOPATH, (), direct.source, CAMP_REFUSED)
        return direct

    def _refused_before(self, map_id: int, start: Point, end: Point, camp, now: float) -> bool:
        """Was this walk refused through the same camp within `REFUSAL_S`?"""
        self._refused = [r for r in self._refused if 0 <= now - r[0] < REFUSAL_S]
        return any(m == map_id and was == camp[:2]
                   and math.dist(a, start[:2]) <= REFUSAL_YARDS
                   and math.dist(b, end[:2]) <= REFUSAL_YARDS
                   for _, m, a, b, was in self._refused)

    def _ring(self, map_id: int, start: Point, end: Point, direct: Path, hit, spots,
              limit: float) -> Path | None:
        """The shortest way through a point on rings round `hit` that keeps clear of `spots`
        and is no longer than `limit`, or `None`."""
        z = (start[2] + end[2]) / 2

        def clear(route) -> bool:
            return not any(near_route(route, x, y, reach) for x, y, reach, *_ in spots)

        for margin in DANGER_RINGS:
            radius = hit[2] + margin - DANGER_YARDS    # rings kept the death spots' spacing
            best: tuple[float, Path] | None = None
            for k in range(VIA_BEARINGS):
                angle = 2 * math.pi * k / VIA_BEARINGS
                via = (hit[0] + radius * math.cos(angle), hit[1] + radius * math.sin(angle), z)
                first = self.inner.path(map_id, start, via)
                if first.status is not PathStatus.COMPLETE or len(first.points) < 2 \
                        or not clear(first):
                    continue
                second = self.inner.path(map_id, first.points[-1], end)
                if not second.usable or second.status is not direct.status or not clear(second):
                    continue
                length = first.length_yards() + second.length_yards()
                if length <= limit and (best is None or length < best[0]):
                    best = (length, Path(direct.status, first.points + second.points[1:],
                                         direct.source, f"round {hit[3]}"))
            if best is not None:
                return best[1]
        return None

    def close(self) -> None:
        self.inner.close()
