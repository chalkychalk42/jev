"""What the hive measured each quest and grind to be worth, lent to Jev (DECISIONS V312).

The hive (JevHive, `hive.values`) plays this code with dozens of characters and measures, for
each quest (its accept, objective and hand-in together) and each grind (a rib, or the gate a
route waits at for a level), per level band of two, the experience an hour it paid, the deaths
an hour it cost, the played hours and the characters that measured it, pooled over classes,
with a class's own numbers beside when they rest on two hours or more. The operator lends it to the live bot beside the other priors
(`var/prior/values.json`, V290). Jev reads a candidate plan's number beside its description
(`jev.coach.judge.describe`), so a pick between a quest step and a grind is between measured
rates, not between two sentences.

The file is another's, and read as tolerantly as the other priors: missing, unreadable or of
another format, it is no values, and the prompt is as it was without it.

    {"format": 1, ...,
     "quests": {"<quest id>": {"<lo>-<hi>": {"xp_h": 1900, "deaths_h": 0.4, "hours": 3.2,
                                              "characters": 7, "crowded": false, ...,
                                              "classes": {"<class>": {...the same...}}}}},
     "grinds": {"grind_<zone>_<lo>_<hi>": {"<lo>-<hi>": {...the same...}}}}

A grind is named by its step's id past its guide's (`grind_elwynn_3_5` of
`alli_human_1_12_grind_elwynn_3_5`, a gate's `gate_<level>_<zone>`): the zone and window are
the same in every guide made by `jev.guide.generate` and the hive's `hive.convert`, where the
guide's own name is not. A band the hive marks crowded is not read: it measured the hive's
crowding, three or more characters on one quest's targets at once (`hive.quests.CROWDED`),
not what the quest pays a character playing it alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FORMAT = 1
# A class's own numbers stand in for the pooled ones only on this many played hours or more:
# the hive writes them beside the pooled from two hours (JevHive `docs/plans/reward.md`).
CLASS_HOURS = 2.0
# How a grind's step id begins, past its guide's (`jev.guide.generate`, `hive.convert`).
GRIND_MARKS = ("grind_", "gate_")


@dataclass(frozen=True)
class Value:
    """One quest's or grind's measured worth at a level band."""

    xp_h: float
    deaths_h: float | None = None
    hours: float | None = None
    chars: int | None = None
    of: int = 1                  # how many grinds it pools (`Values.grinds`)

    def text(self) -> str:
        """As Jev reads it: short, since it bills by the token."""
        parts = [f"{self.xp_h:,.0f} XP/h"]
        if self.deaths_h is not None:
            parts.append(f"{self.deaths_h:.1f} deaths/h")
        if self.of > 1:
            parts.append(f"{self.of} grinds")
        elif self.chars is not None:
            parts.append(f"{self.chars} chars")
        return "measured " + ", ".join(parts)


def _number(entry: dict, *keys: str) -> float | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _value(entry) -> Value | None:
    if not isinstance(entry, dict) or entry.get("crowded") is True:
        return None
    xp_h = _number(entry, "xp_h")
    if xp_h is None:
        return None
    chars = _number(entry, "characters")
    return Value(xp_h=xp_h, deaths_h=_number(entry, "deaths_h"), hours=_number(entry, "hours"),
                 chars=None if chars is None else int(chars))


def _in_band(key: str, level: int) -> bool:
    low, _, high = str(key).partition("-")
    try:
        return int(low) <= level <= int(high or low)
    except ValueError:
        return False


def _band(bands, level: int) -> dict | None:
    if not isinstance(bands, dict):
        return None
    entry = next((e for k, e in bands.items() if _in_band(k, level)), None)
    return entry if isinstance(entry, dict) else None


def _played(bands, level: int | None, cls: str | None,
            min_hours: float) -> tuple[float, float] | None:
    """A band's experience an hour as played and its hours (`Values.played`), crowded or not."""
    if level is None:
        return None
    entry = _band(bands, level)
    if entry is None:
        return None
    own = entry.get("classes") if isinstance(entry.get("classes"), dict) else {}
    mine = next((e for k, e in own.items()
                 if cls is not None and str(k).lower() == cls.lower() and isinstance(e, dict)),
                None)
    for found, least in ((mine, max(CLASS_HOURS, min_hours)), (entry, min_hours)):
        if found is None:
            continue
        xp_h, hours = _number(found, "xp_h"), _number(found, "hours")
        if xp_h is not None and hours is not None and hours >= least:
            return xp_h, hours
    return None


def rib_name(step_id: str) -> str:
    """A grind as the values name it, whichever guide its step is in: `grind_<zone>_<lo>_<hi>`
    or `gate_<level>_<zone>`."""
    if step_id.startswith(GRIND_MARKS):
        return step_id
    at = max(step_id.rfind(f"_{mark}") for mark in GRIND_MARKS)
    return step_id[at + 1:] if at >= 0 else step_id


class Values:
    """The measured worth of quests and grinds, by level band (see the module)."""

    def __init__(self, quests: dict | None = None, grinds: dict | None = None):
        self.quests = quests if isinstance(quests, dict) else {}
        self.grinds_by = grinds if isinstance(grinds, dict) else {}

    def __bool__(self) -> bool:
        return bool(self.quests or self.grinds_by)

    @classmethod
    def load(cls, path: str | Path) -> Values | None:
        """The file's values; `None` when there is none to read."""
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict) or document.get("format", FORMAT) != FORMAT:
            return None
        values = cls({str(k): v for k, v in (document.get("quests") or {}).items()}
                     if isinstance(document.get("quests"), dict) else None,
                     {rib_name(str(k)): v for k, v in (document.get("grinds") or {}).items()}
                     if isinstance(document.get("grinds"), dict) else None)
        return values or None

    @staticmethod
    def _at(bands, level: int | None, cls: str | None) -> Value | None:
        if not isinstance(bands, dict) or level is None:
            return None
        entry = next((e for k, e in bands.items() if _in_band(k, level)), None)
        if not isinstance(entry, dict):
            return None
        own = (entry.get("classes") or {}) if isinstance(entry.get("classes"), dict) else {}
        mine = next((e for k, e in own.items()
                     if cls is not None and str(k).lower() == cls.lower()), None)
        mine = _value(mine)
        if mine is not None and (mine.hours or 0.0) >= CLASS_HOURS:
            return mine
        return _value(entry)

    def quest(self, quest_id: int | None, level: int | None,
              cls: str | None = None) -> Value | None:
        if quest_id is None:
            return None
        return self._at(self.quests.get(str(quest_id)), level, cls)

    def grind(self, step_id: str, level: int | None, cls: str | None = None) -> Value | None:
        return self._at(self.grinds_by.get(rib_name(step_id)), level, cls)

    def played(self, step_id: str, level: int | None, cls: str | None = None,
               min_hours: float = 0.0, toward: float | None = None,
               prior_hours: float = 0.0) -> float | None:
        """What a grind paid an hour, as played, at `level` (V560): its band's experience an hour
        over at least `min_hours`, the class's own where it has `CLASS_HOURS`; a band the hive
        marked crowded counts, as a choice of grind is made where the others play too. With
        `toward`, drawn to it as if `prior_hours` more had paid that: a grind of few hours is
        not taken at its luck. `None` where it was not measured so long."""
        found = _played(self.grinds_by.get(rib_name(step_id)), level, cls, min_hours)
        if found is None:
            return None
        xp_h, hours = found
        if toward is None or prior_hours <= 0.0:
            return xp_h
        return (xp_h * hours + toward * prior_hours) / (hours + prior_hours)

    def played_mean(self, level: int | None, cls: str | None = None,
                    min_hours: float = 0.0) -> float | None:
        """What grinding paid an hour at `level` over every grind measured `min_hours` there, by
        its hours (V560): a grind not measured is taken at it."""
        if level is None:
            return None
        total = weight = 0.0
        for bands in self.grinds_by.values():
            entry = _band(bands, level)
            hours = _number(entry, "hours") if entry is not None else None
            found = _played(bands, level, cls, min_hours)
            if found is not None and hours:
                total += found[0] * hours
                weight += hours
        return total / weight if weight else None

    def grinds(self, level: int | None, cls: str | None = None) -> Value | None:
        """What grinding pays at `level`, over every rib measured there, by their hours: the
        grind where the character stands, whose rib is not known until it is walked to."""
        found = [v for v in (self._at(bands, level, cls) for bands in self.grinds_by.values())
                 if v is not None]
        if not found:
            return None
        weights = [v.hours if v.hours else 1.0 for v in found]
        total = sum(weights)
        dying = [(v.deaths_h, w) for v, w in zip(found, weights, strict=True)
                 if v.deaths_h is not None]
        return Value(xp_h=sum(v.xp_h * w for v, w in zip(found, weights, strict=True)) / total,
                     deaths_h=(sum(d * w for d, w in dying) / sum(w for _, w in dying)
                               if dying else None),
                     hours=total, of=len(found))
