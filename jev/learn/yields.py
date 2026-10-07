"""What a kill pays an hour, net of the time its deaths cost, as the hive measured it (V392).

The hive (JevHive, `hive.ribyield`) plays this code with hundreds of characters and measures,
from its last 24 hours of grinding, for each class and each band of five levels, by the
creature's level less the character's: the kills an hour of a kill's whole cycle (the fight,
the loot, the walk to it, the search and the rest after), the experience a kill paid, the
deaths a kill cost, and, for each class, the seconds a death costs (dead, released, the corpse
run). A rib is then worth the experience a kill of each of its creatures' levels pays the
character (`kill_xp`), over the seconds a kill and its share of a death take, an hour:

    xp an hour = kill_xp(level, creature) x 3600 / (3600 / kills_h + deaths_kill x death_s)

The kill's own experience is the server's for the character's level, not the band's measured
mean, which pools five levels; the table's `xp_kill` is kept for whoever reads it. Ranked by a
kill's experience, a rib of creatures a level above was the best, and there Jev dies most: at
11-20 in the hive's 7 Oct 01:25-03:50, 39 deaths a hundred kills at the character's level and
60 a level above, against 16 two below; net of 149 s a death, a kill's cycle paid most at two
levels below (11-15) and one below (16-20).

The file is another's, and read as tolerantly as the other priors: missing, unreadable or of
another format, it is no table, and the rib choice is as it was without it (`rib_pays`).

    {"format": 1, "written": "...", "since": "...", "until": "...",
     "classes": {"<class>" | "all": {"death_s": 149.0, "deaths": 312,
                                      "bands": {"11-15": {"-2": {"kills": 812, "kills_h": 41.0,
                                                                 "xp_kill": 76.1,
                                                                 "deaths_kill": 0.16}}}}}}
"""

from __future__ import annotations

import json
from pathlib import Path

from jev.world.combat import grey_level, kill_xp

FORMAT = 1
# The pooled classes, read where a class's own cell or death time is missing.
POOLED = "all"
# A cell is read on this many kills or more, a class's seconds a death on this many deaths;
# the hive writes none under them, and a hand-made file is held to the same.
MIN_KILLS = 40
MIN_DEATHS = 10


def _number(entry, key: str) -> float | None:
    value = entry.get(key) if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _band(key: str) -> tuple[int, int] | None:
    low, _, high = str(key).partition("-")
    try:
        return int(low), int(high or low)
    except ValueError:
        return None


class Yields:
    """The hive's kill yields by class, level band and level difference (see the module).

    A character's rates are all read from one source: its class's band where that has a cell for
    every difference that pays it (from a level over its grey level to one above it, the ribs
    `rib_fits` lets it take), else the pooled band where that has, else none. Mixed, a class's
    own cells beside the pooled ones ranked the differences by where each came from: the hive's
    hunters at 11-15 had their own cells only to two levels below, dying 310 times a hundred
    kills there (6-7 Oct), and the pooled cells above them looked four times as good."""

    def __init__(self, classes: dict):
        self.classes = classes
        self._sources: dict = {}

    @classmethod
    def load(cls, path: str | Path) -> Yields | None:
        """The file's table; `None` when there is none to read."""
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            return None
        classes = document.get("classes")
        if not isinstance(classes, dict) or not classes:
            return None
        return cls({str(k).lower(): v for k, v in classes.items() if isinstance(v, dict)})

    def _cells(self, klass: str, level: int) -> dict | None:
        """A class's cells read at `level`, by level difference: those of its band with
        `MIN_KILLS` or more."""
        bands = self.classes.get(klass, {}).get("bands")
        if not isinstance(bands, dict):
            return None
        for key, cells in bands.items():
            band = _band(key)
            if band is not None and band[0] <= level <= band[1] and isinstance(cells, dict):
                return {int(d): cell for d, cell in cells.items()
                        if str(d).lstrip("-").isdigit()
                        and (_number(cell, "kills") or 0) >= MIN_KILLS
                        and (_number(cell, "kills_h") or 0) > 0}
        return None

    def _source(self, klass: str, level: int) -> tuple[dict, float] | None:
        """The cells and the seconds a death a character's rates are read from (see the
        class): its class's, else the pooled; `None` when neither covers what pays it."""
        key = (klass, level)
        if key not in self._sources:
            paying = range(grey_level(level) + 1 - level, 2)
            found = None
            for who in (klass, POOLED):
                cells, death = self._cells(who, level), self._death_s(who)
                if cells is not None and death is not None and all(d in cells for d in paying):
                    found = (cells, death)
                    break
            self._sources[key] = found
        return self._sources[key]

    def _death_s(self, klass: str) -> float | None:
        entry = self.classes.get(klass, {})
        seconds, deaths = _number(entry, "death_s"), _number(entry, "deaths")
        return seconds if seconds is not None and seconds >= 0 and (deaths or 0) >= MIN_DEATHS \
            else None

    def rate(self, klass: str | None, level: int, creature: int) -> float | None:
        """Experience an hour net of death time from kills of level `creature` for a level
        `level` character of class `klass` (its class's cells, else the pooled, `_source`); 0
        for a creature grey to it; `None` where the table says nothing."""
        xp = kill_xp(level, creature)
        if xp <= 0.0:
            return 0.0
        source = self._source((klass or "").lower(), level)
        cell = source[0].get(creature - level) if source is not None else None
        if cell is None:
            return None
        seconds = (3600.0 / _number(cell, "kills_h")
                   + (_number(cell, "deaths_kill") or 0.0) * source[1])
        return xp * 3600.0 / seconds

    def rib_rate(self, low: int, high: int, level: int, klass: str | None) -> float | None:
        """A rib's worth, its creatures' levels `low` to `high` alike (as `rib_xp` takes
        them): the mean of their rates; `None` when any is not known."""
        rates = [self.rate(klass, level, m) for m in range(low, high + 1)]
        return None if any(r is None for r in rates) else sum(rates) / len(rates)
