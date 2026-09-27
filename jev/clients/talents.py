"""Spend talent points on the class's build: open the talent frame, click, close (V261).

A character gains a talent point a level from 10, and the bot spent none: the paladin reached
15.87 with six unspent. The strip paints the points unspent, and the talents one per paint:
each one's tab, tier, column and rank, and while the frame is open its button when its tab
shows, else the tab's button (schema 19). The build (`content/tbc/talent-builds.json`) is
the order the points go in, point by point: the next point goes to the first entry whose
talent the census reads short of that entry's rank.

A point is a click on the talent's button, after a click on its tab's when another tab
shows; it is spent when the census reads the talent one rank higher. Nothing else is
clicked, and the frame is closed again however the visit ended. In a fight, dead, or with
the strip unreadable the desk stops at once.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path

from jev.run.evidence import event, traced

BUILDS_PATH = Path(__file__).resolve().parents[2] / "content/tbc/talent-builds.json"
# The stock binding that opens and closes the talent frame (TOGGLETALENTS).
TALENTS_KEY = "n"
# The first schema that paints the talents.
TALENTS_SCHEMA = 19
OPEN_S = 4.0
# A class has some sixty talents, one a paint: every one comes round within a pass of the
# census, about six seconds at ten paints a second, twice that for a reader one paint in two.
CENSUS_S = 15.0
TAB_S = 3.0
SPENT_S = 5.0
MAX_POINTS = 10


class Spent(StrEnum):
    DONE = "done"
    NOTHING = "nothing"            # no points, no build, or the build complete
    OLD_STRIP = "old_strip"        # an addon older than schema 19: nothing to read
    BLIND = "blind"
    INTERRUPTED = "interrupted"
    REFUSED = "refused"
    NO_FRAME = "no_frame"
    NOT_SEEN = "not_seen"          # the talent wanted never came round, or had no button
    NOT_SPENT = "not_spent"        # clicked, and the census did not read it a rank higher
    TIMEOUT = "timeout"

    @property
    def ok(self) -> bool:
        return self in (Spent.DONE, Spent.NOTHING)


class _Stop(Exception):
    def __init__(self, outcome: Spent, detail: str):
        super().__init__(detail)
        self.outcome, self.detail = outcome, detail


@cache
def _builds() -> dict[int, tuple[dict, ...]]:
    try:
        raw = json.loads(BUILDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(c): tuple(points) for c, points in (raw.get("classes") or {}).items()}


def build_for(class_id: int | None) -> tuple[dict, ...]:
    """The class's build, point by point: {"name", "tab", "tier", "column", "rank"} each."""
    return _builds().get(class_id, ()) if isinstance(class_id, int) else ()


def next_point(build: Sequence[dict], ranks: dict[tuple[int, int, int], int]) -> dict | None:
    """The build's first point whose talent the census reads short of its rank; `None` when
    every point read so far is spent. A talent the census has not read is not assumed."""
    for point in build:
        key = (point["tab"], point["tier"], point["column"])
        if key not in ranks:
            return None
        if ranks[key] < point["rank"]:
            return point
    return None


def _key(values: dict) -> tuple[int, int, int] | None:
    tab, tier, column = (values.get("talents.tab"), values.get("talents.tier"),
                         values.get("talents.column"))
    if not all(isinstance(v, int) for v in (tab, tier, column)):
        return None
    return tab, tier, column


@dataclass
class TalentDesk:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    spent: int = field(default=0, init=False)
    detail: str = field(default="", init=False)
    learned: list[str] = field(default_factory=list, init=False)
    _deadline: float = field(default=0.0, init=False)
    _ranks: dict = field(default_factory=dict, init=False)

    @traced("talents")
    def run(self, build: Sequence[dict], *, timeout_s: float = 60.0) -> Spent:
        self.spent, self.detail, self.learned = 0, "", []
        self._ranks = {}
        self._deadline = self.clock() + timeout_s
        opened = False
        try:
            values = self._look()
            if (values.get("schema") or 0) < TALENTS_SCHEMA:
                raise _Stop(Spent.OLD_STRIP, "the strip paints no talents (schema before 19)")
            if not values.get("char.talent_points") or not build:
                raise _Stop(Spent.NOTHING, "no talent points to spend" if build
                            else "no build for this class")
            self._read_ranks(build)
            if next_point(build, self._ranks) is None:
                raise _Stop(Spent.NOTHING, "the build's points are all spent")
            opened = self._open()
            for _ in range(MAX_POINTS):
                values = self._look()
                if not values.get("char.talent_points"):
                    break
                point = next_point(build, self._ranks)
                if point is None:
                    break
                self._spend(point)
            return Spent.DONE if self.spent else Spent.NOTHING
        except _Stop as stop:
            self.detail = stop.detail
            return stop.outcome
        finally:
            if opened:
                self._close()

    # -- steps ------------------------------------------------------------------------

    def _read_ranks(self, build: Sequence[dict]) -> None:
        """Read the census round until every talent of the build has been seen once."""
        wanted = {(p["tab"], p["tier"], p["column"]) for p in build}
        until = min(self._deadline, self.clock() + CENSUS_S)
        while not wanted <= set(self._ranks):
            if next_point(build, self._ranks) is not None:
                return                      # the next point is known; the rest can wait
            if self.clock() >= until:
                raise _Stop(Spent.NOT_SEEN, "the build's talents did not all come round")
            self._look()
            self.sleep(0.05)

    def _open(self) -> bool:
        values = self._look()
        if values.get("ui.talents") is True:
            return True
        event("talents.open", data={"key": TALENTS_KEY})
        if self.hid.tap(TALENTS_KEY) is False:
            raise _Stop(Spent.REFUSED, "talent frame key refused")
        if self._await(lambda v: v.get("ui.talents") is True, OPEN_S) is None:
            raise _Stop(Spent.NO_FRAME, "the talent frame did not open")
        return True

    def _close(self) -> None:
        values = self.read() or {}
        if values.get("ui.talents") is True:
            event("talents.close", data={"key": TALENTS_KEY})
            self.hid.tap(TALENTS_KEY)

    def _spend(self, point: dict) -> None:
        key = (point["tab"], point["tier"], point["column"])
        before = self._ranks.get(key, 0)
        shown = self._await(lambda v: _key(v) == key and v.get("talents.x") is not None,
                            CENSUS_S)
        if shown is None:
            raise _Stop(Spent.NOT_SEEN, f"{point['name']}: no button came round")
        if shown.get("talents.shown") is not True:
            # Its tab's button: show that tab, then wait for the talent's own button.
            self._click(shown, f"{point['name']}: tab {point['tab']}")
            shown = self._await(lambda v: _key(v) == key and v.get("talents.shown") is True
                                and v.get("talents.x") is not None, CENSUS_S)
            if shown is None:
                raise _Stop(Spent.NOT_SEEN, f"{point['name']}: its tab did not show it")
        if shown.get("talents.rank") != before:
            self._ranks[key] = shown.get("talents.rank")      # read afresh: choose again
            return
        self._click(shown, point["name"])
        after = self._await(lambda v: _key(v) == key and isinstance(v.get("talents.rank"), int)
                            and v["talents.rank"] > before, SPENT_S + CENSUS_S)
        if after is None:
            raise _Stop(Spent.NOT_SPENT, f"{point['name']}: clicked, and no rank came of it")
        self.spent += 1
        self.learned.append(f"{point['name']} {after['talents.rank']}")
        event("talents.spent", data={"name": point["name"], "rank": after["talents.rank"]})

    # -- plumbing ---------------------------------------------------------------------

    def _click(self, values: dict, what: str) -> None:
        x, y = values.get("talents.x"), values.get("talents.y")
        if x is None or y is None or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise _Stop(Spent.NOT_SEEN, f"{what}: no button")
        ox, oy = self.window_origin
        w, h = self.window_size
        point = (ox + round(x * w), oy + round(y * h))
        event("talents.click", data={"what": what, "point": list(point)})
        if self.hid.click(*point) is False:
            raise _Stop(Spent.REFUSED, f"{what}: click refused")

    def _await(self, predicate, seconds: float) -> dict | None:
        until = min(self._deadline, self.clock() + seconds)
        while self.clock() < until:
            values = self._look()
            if predicate(values):
                return values
            self.sleep(0.05)
        return None

    def _look(self) -> dict:
        values = self.read()
        if values is None:
            raise _Stop(Spent.BLIND, "strip unreadable")
        if (values.get("vitals.combat") is True or values.get("vitals.dead") is True
                or values.get("vitals.ghost") is True):
            raise _Stop(Spent.INTERRUPTED, "combat or death while spending talents")
        if self.clock() >= self._deadline:
            raise _Stop(Spent.TIMEOUT, "the talent visit's time ran out")
        key = _key(values)
        if key is not None and isinstance(values.get("talents.rank"), int):
            self._ranks[key] = values["talents.rank"]
        return values
