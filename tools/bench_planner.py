"""Time the planner's memory lookups against an earlier commit's, on real files, and check
that both answer the same (V325).

    python tools/bench_planner.py --routes /home/ash/JevHive/var/route-memory.json \
        --danger /home/ash/JevHive/var/danger.json --base a578532

Both files are copied to a temporary directory and only the copies are opened: nothing is
written back. `--base` names the commit whose `jev/guide/route_memory.py` and
`jev/learn/danger.py` are "before"; the working tree's are "after". Walks of 50-800 yards
are drawn at random in Elwynn, Dun Morogh and Durotar, each planned by a stand-in planner
(a jittered line, the same for both), at random levels, at the time the files were read.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev.guide import route_memory as new_rm  # noqa: E402
from jev.guide.path import Path as Route  # noqa: E402
from jev.guide.path import PathStatus  # noqa: E402
from jev.learn import danger as new_dm  # noqa: E402

# (name, map, x from, x to, y from, y to), from data/zones-tbc-243.json.
ZONES = (("Elwynn", 0, -10254.0, -7939.0, -1935.0, 1535.0),
         ("Dun Morogh", 0, -7160.0, -3877.0, -3122.0, 1802.0),
         ("Durotar", 1, -1716.0, 1808.0, -7250.0, -1962.0))


def _old(base: str, relative: str, name: str, into: Path):
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{relative}"],
                            check=True, capture_output=True, text=True).stdout
    file = into / f"{name}.py"
    file.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Jittered:
    """A stand-in planner: a line from start to end, a point every 25 yards moved up to 12
    yards aside, the same for the same ask."""

    def path(self, map_id, start, end):
        rng = random.Random(hash((map_id, round(start[0], 3), round(start[1], 3),
                                  round(end[0], 3), round(end[1], 3))))
        length = math.dist(start[:2], end[:2])
        n = max(1, int(length // 25))
        points = [tuple(start)]
        for i in range(1, n):
            f = i / n
            points.append((start[0] + (end[0] - start[0]) * f + rng.uniform(-12, 12),
                           start[1] + (end[1] - start[1]) * f + rng.uniform(-12, 12),
                           start[2] + (end[2] - start[2]) * f))
        points.append(tuple(end))
        return Route(PathStatus.COMPLETE, tuple(points), "stand-in")

    def close(self):
        pass


def walks(count: int, seed: int, through=()):
    """Walks of 50-800 yards: every other one anywhere in a zone, the rest through or past one
    of `through` (map, x, y, level), a place kept clear of, as the walks that matter are."""
    rng = random.Random(seed)
    through = [t for t in through if any(_in(z, t) for z in ZONES)]
    for k in range(count):
        level = rng.choice([None, *range(3, 21)])
        yards, bearing = rng.uniform(50, 800), rng.uniform(0, 2 * math.pi)
        if k % 2 and through:
            map_id, x, y, near = rng.choice(through)
            level = near if near is not None and rng.random() < 0.7 else level
            f, aside = rng.uniform(0.1, 0.9), rng.uniform(-60, 60)
            start = (x - f * yards * math.cos(bearing) - aside * math.sin(bearing),
                     y - f * yards * math.sin(bearing) + aside * math.cos(bearing), 0.0)
        else:
            _, map_id, x0, x1, y0, y1 = ZONES[k % len(ZONES)]
            start = (rng.uniform(x0, x1), rng.uniform(y0, y1), 0.0)
        end = (start[0] + yards * math.cos(bearing), start[1] + yards * math.sin(bearing), 0.0)
        yield map_id, start, end, level


def _in(zone, place) -> bool:
    _, map_id, x0, x1, y0, y1 = zone
    return place[0] == map_id and x0 <= place[1] <= x1 and y0 <= place[2] <= y1


def _camps(module, memory, places, now: float) -> None:
    """Death camps at some of `places`, as the hive's: one character dying twice, made as
    `died` makes them. Added, not died: `died` drops what the hive's merge brings back."""
    until = now - 200.0 + module.CAMP_S
    memory.dangers = memory.dangers + [
        module.Danger(map_id, x + dx, y + dy, now - ago, level, until, -1 - k)
        for k, (map_id, x, y, level) in enumerate(places)
        for dx, dy, ago in ((3.0, 0.0, 300.0), (0.0, 3.0, 200.0))]


def _query(module, memory, danger, now, level):
    return module.DangerAvoidingQuery(Jittered(), memory, clock=lambda: now,
                                      hot=lambda m: danger.hot(m, level),
                                      level=lambda: level)


def _rounds(start, end, count=8):
    """Ways round a walk's middle, as the exposure layer asks the keeper about."""
    cx, cy = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
    radius = max(40.0, math.dist(start[:2], end[:2]) / 3)
    planner = Jittered()
    for k in range(count):
        a = 2 * math.pi * k / count
        via = (cx + radius * math.cos(a), cy + radius * math.sin(a), 0.0)
        yield planner.path(0, start, via).points + planner.path(0, via, end).points[1:]


def _timed(label, fn, items, results):
    started = time.perf_counter()
    out = [fn(*item) for item in items]
    results[label] = (time.perf_counter() - started) / max(1, len(items))
    return out


def _measure(rm, memory, danger, now: float, asks, into: dict) -> dict:
    """Each kind of ask, timed into `into`; the answers, by kind."""
    got = {}
    got["dangers_on"] = _timed("dangers_on", lambda m, s, e, lv: [
        (d.x, d.y, d.at, d.level, d.camp_until, d.who)
        for d in memory.dangers_on(m, now, lv)], asks, into)
    got["hot"] = _timed("hot", lambda m, s, e, lv: danger.hot(m, lv), asks, into)
    got["pooled"] = _timed("pooled", lambda m, s, e, lv: danger.pooled(m, lv or 10), asks, into)
    got["_kept"] = _timed("_kept", lambda m, s, e, lv: _query(
        rm, memory, danger, now, lv)._kept(m, s, e), asks, into)

    def keeper(m, s, e, lv):
        passed = _query(rm, memory, danger, now, lv).keeper(m, s, e)
        return [passed(Jittered().path(m, s, e).points),
                *(passed(points) for points in _rounds(s, e))]
    got["keeper"] = _timed("keeper (+9 routes)", keeper, asks, into)

    def plan(m, s, e, lv):
        route = _query(rm, memory, danger, now, lv).path(m, s, e)
        return route.status, route.points, route.detail
    got["path"] = _timed("path (round deaths)", plan, asks, into)
    got["camp_at"] = [memory.camp_at(m, s, now, lv) is not None for m, s, _, lv in asks]
    return got


def run(routes: Path, danger_file: Path, base: str, count: int, seed: int) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        old_dm = _old(base, "jev/learn/danger.py", "old_danger", tmp)
        old_rm = _old(base, "jev/guide/route_memory.py", "old_route_memory", tmp)
        shutil.copy(routes, tmp / "old-routes.json")
        shutil.copy(routes, tmp / "new-routes.json")
        shutil.copy(danger_file, tmp / "danger.json")
        before, after = {}, {}
        sides = {}
        for side, rm, dm, into in (("old", old_rm, old_dm, before), ("new", new_rm, new_dm, after)):
            started = time.perf_counter()
            memory = rm.RouteMemory(tmp / f"{side}-routes.json")
            danger = dm.DangerMap(tmp / "danger.json")
            into["load (s)"] = time.perf_counter() - started
            memory.file = None                      # never written
            danger.file = None
            sides[side] = (rm, memory, danger)
        now = time.time()
        rm, memory, danger = sides["new"]
        places = [(d.map_id, d.x, d.y, d.level) for d in memory.dangers_on(0, now)
                  + memory.dangers_on(1, now)]
        places += [(m, x, y, level) for m in (0, 1) for level in range(3, 21, 3)
                   for x, y, _ in danger.hot(m, level)]
        rng = random.Random(seed)
        made = rng.sample(places, min(40, len(places)))
        for rm, memory, _ in sides.values():
            _camps(rm, memory, made, now)
        asks = list(walks(count, seed, places))
        answers = {side: _measure(*sides[side], now, asks, into)
                   for side, into in (("old", before), ("new", after))}
        kept_old, kept_new = len(sides["old"][1].dangers), len(sides["new"][1].dangers)
    print(f"{count} walks in {', '.join(z[0] for z in ZONES)}; deaths held: before {kept_old},"
          f" after {kept_new} (the rest past every rule that keeps one)")
    print(f"{'':24}{'before':>12}{'after':>12}{'faster':>9}")
    for label in before:
        b, a = before[label], after[label]
        unit, scale = ("s", 1) if label.startswith("load") else ("ms", 1000)
        print(f"{label:24}{b * scale:>10.3f}{unit:>2}{a * scale:>10.3f}{unit:>2}"
              f"{(b / a if a else math.inf):>8.1f}x")
    differ = 0
    for key in answers["old"]:
        mismatched = sum(1 for o, n in zip(answers["old"][key], answers["new"][key], strict=True)
                         if o != n)
        differ += mismatched
        telling = sum(1 for a in answers["new"][key] if _telling(key, a))
        print(f"same answers, {key}: {len(asks) - mismatched}/{len(asks)}"
              f" ({telling} {TELLING[key]})")
    return 1 if differ else 0


TELLING = {"dangers_on": "with a death kept", "hot": "with a hot cell",
           "pooled": "above nothing", "_kept": "keeping clear of something",
           "keeper": "passing something", "path": "bent round or refused",
           "camp_at": "in a camp"}


def _telling(key, answer) -> bool:
    """Is this an answer with something in it, for which the two could have differed?"""
    if key == "keeper":
        return any(answer)
    if key == "path":
        return bool(answer[2]) and answer[2] != ""
    return bool(answer)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--routes", type=Path, default=Path("/home/ash/JevHive/var/route-memory.json"))
    parser.add_argument("--danger", type=Path, default=Path("/home/ash/JevHive/var/danger.json"))
    parser.add_argument("--base", default="a578532", help="the commit that is 'before'")
    parser.add_argument("--walks", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=29)
    args = parser.parse_args(argv)
    return run(args.routes, args.danger, args.base, args.walks, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
