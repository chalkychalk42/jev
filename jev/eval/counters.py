"""The numbers on the eval board (PLAN §16). Arithmetic only — `board.py` prints them.

The headline is **`unresolved/h`** (`ARCHITECTURE.md` §1): decisions per hour that no rule
could settle. It leads because it separates two failures that look identical from the
outside. At 1% of ticks the project is cheap; at 30% the problem is *perception* and a
bigger model will not fix it. Tokens/hour cannot tell those apart — it falls when you get
smarter and it also falls when you get cheaper, which is why it is the second line here
rather than the first.

Everything is computed over a rolling window per client, defaulting to the 15 minutes
PLAN §16 asks for. Rates divide by the **observed** span, not the nominal window: a board
that has three minutes of ticks and divides two deaths by fifteen minutes is reporting a
rate nobody measured.

Rows are read as plain dicts, never validated back into `State`. The board must survive a
corpus written by a different `state_v1` — `State` is `extra="forbid"`, so validating here
would make the one component whose job is to notice trouble the first component to die of
it. Missing fields therefore read as unknown and are counted as unknown, never as zero.
"""

from __future__ import annotations

import itertools
import pathlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from jev.coach.schema import Intent
from jev.learn.episode import Stream, read
from jev.world.state_v1 import ArmedBy

WINDOW_S = 900.0
"""15 minutes (PLAN §16). Long enough that one unlucky pull is not a trend, short enough
that a run going wrong shows up while you can still stop it."""

FREEZE_SPAN_S = 7200.0
"""2 hours — the bracket freeze rule's own span (PLAN §16)."""

STUCK_S = 15.0
"""A stuck episode only counts as a stuck *event* past this. PLAN §12.3 penalises
`stuck>15s`, and the freeze rule demands zero of them; shorter hitches are the terrain."""

COPPER_PER_GOLD = 10_000

DEATHS_FREEZE_MAX = 2.0
TEACHER_FREEZE_MAX = 3.0

# Every armed_by value appears in the tick-share table even at zero. A share table whose
# rows appear and vanish cannot be read at a glance, and "teacher: absent" and
# "teacher: 0%" are the same fact here only because the denominator is ticks.
ARMED_BY_ORDER: tuple[str, ...] = tuple(a.value for a in ArmedBy)


# --------------------------------------------------------------------------- streams


@dataclass(frozen=True)
class Streams:
    """The three streams, already read. Kept as dicts — see the module docstring."""

    ticks: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    grades: list[dict[str, Any]]

    def __add__(self, other: Streams) -> Streams:
        return Streams(
            ticks=[*self.ticks, *other.ticks],
            decisions=[*self.decisions, *other.decisions],
            grades=[*self.grades, *other.grades],
        )


def empty_streams() -> Streams:
    return Streams(ticks=[], decisions=[], grades=[])


def load_run(run_dir: str | pathlib.Path) -> Streams:
    """Read one run directory. A stream that does not exist yet is empty, not an error —
    grades are written a minute behind the ticks they grade, and a live run has none."""
    d = pathlib.Path(run_dir)

    def _read(stream: Stream) -> list[dict[str, Any]]:
        p = d / f"{stream.value}.jsonl"
        return read(p) if p.exists() else []

    return Streams(_read(Stream.TICKS), _read(Stream.DECISIONS), _read(Stream.GRADES))


def load_runs(run_dirs: Iterable[str | pathlib.Path]) -> Streams:
    out = empty_streams()
    for d in run_dirs:
        out = out + load_run(d)
    return out


def clients(streams: Streams) -> list[str]:
    """Every client that produced a tick, in a stable order so the board does not shuffle."""
    return sorted({t.get("client_id", "?") for t in streams.ticks})


# --------------------------------------------------------------------------- brackets

BRACKETS: tuple[tuple[int, int], ...] = (
    (1, 6), (6, 12), (12, 18), (18, 24), (24, 30), (30, 40),
    (40, 50), (50, 60), (60, 62), (62, 64), (64, 66), (66, 68), (68, 70),
)
"""The level bands from PLAN §7.7, verbatim. They are the promotion unit
(`ARCHITECTURE.md` §4: per bracket, never global), so they live next to the counters that
decide promotion rather than being redefined by each consumer."""


def bracket_of(level: int | None) -> str | None:
    """The band a level belongs to. `None` in, `None` out — an unobserved level does not
    belong to a band, and guessing one would promote a policy on somebody else's evidence."""
    if level is None:
        return None
    for lo, hi in BRACKETS:
        if lo <= level < hi:
            return f"{lo}-{hi}"
    return f"{BRACKETS[-1][0]}-{BRACKETS[-1][1]}" if level >= BRACKETS[-1][1] else None


# --------------------------------------------------------------------------- helpers


def _path(row: Any, *keys: str) -> Any:
    """Dig through nested dicts, returning `None` for anything absent or the wrong shape."""
    cur = row
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _state(tick: dict[str, Any]) -> dict[str, Any]:
    s = tick.get("state")
    return s if isinstance(s, dict) else {}


def _per_h(n: float, span_s: float) -> float | None:
    """`None` when there is no span to divide by. Zero would be a claim about a rate that
    was never observed, and the freeze rule is allowed to read that claim."""
    if span_s <= 0.0:
        return None
    return n * 3600.0 / span_s


def _dead(tick: dict[str, Any]) -> bool | None:
    """Tri-state "is the character down". Unknown stays unknown so a blink in the reader
    cannot manufacture a death and a resurrection."""
    v = _path(tick, "state", "vitals") or {}
    if v.get("dead") is True or v.get("ghost") is True:
        return True
    if v.get("dead") is False and v.get("ghost") is False:
        return False
    return None


def _spans(ticks: Sequence[dict[str, Any]], pred: Callable[[dict[str, Any]], bool]) -> list[tuple[float, float]]:
    """Contiguous (start_t, end_t) runs where `pred` holds.

    A run is closed by the timestamp of the first tick that fails the predicate, so the
    duration is measured rather than counted in ticks — tick rate varies with load and a
    count of ticks would quietly rescale every duration on a busy machine.
    """
    out: list[tuple[float, float]] = []
    start: float | None = None
    for row in ticks:
        t = float(row.get("t", 0.0))
        if pred(row):
            if start is None:
                start = t
        elif start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, float(ticks[-1].get("t", start))))
    return out


def _duration(spans: Sequence[tuple[float, float]]) -> float:
    return sum(b - a for a, b in spans)


# --------------------------------------------------------------- unresolved decisions


def is_unresolved(decision: dict[str, Any]) -> bool:
    """Did this decision need something a rule could not supply?

    Two shapes count. The coach saying `escalate` is the literal statement "no rule of mine
    settles this". A teacher row is the same admission after the fact: the teacher is never
    consulted about anything a rule already answered. Everything else — tracker predicates,
    scripted GOAP priority, a confident policy — settled itself and costs nothing.
    """
    if decision.get("intent") == Intent.ESCALATE.value:
        return True
    return decision.get("author") == ArmedBy.TEACHER.value


def escalation_id(decision: dict[str, Any]) -> tuple[Any, ...]:
    """Collapse one question into one row.

    A coach escalation and the teacher answer to it are two rows describing one unresolved
    decision, and the schema has no edge between them: `dedup_of` coalesces duplicate
    *questions*, not a question and its answer. They do share the tick they apply to and
    the bucket they were asked about, so that triple is the identity used here. If
    `DecisionRow` ever gains an explicit `escalated_from`, prefer it — this join is a
    reconstruction and it will over-count if a client ever escalates the same bucket twice
    on one tick.
    """
    parent = decision.get("dedup_of")
    if parent:
        return ("dedup", parent)
    return (decision.get("run_id"), decision.get("situation_key"), decision.get("tick_id"))


def is_teacher_call(decision: dict[str, Any]) -> bool:
    """Did this row actually cost a rung on the rate limit?

    A cache hit and a deduplicated question are the entire point of `situation_key`
    (`ARCHITECTURE.md` §2), so they must not be billed — otherwise the saving is invisible
    in exactly the number the freeze rule reads.
    """
    if decision.get("author") != ArmedBy.TEACHER.value:
        return False
    return not decision.get("cache_hit") and not decision.get("dedup_of")


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class SkillStat:
    """One row of the skill success table.

    `rate` is `None` rather than 0.0 when nothing was graded: a skill nobody has scored has
    an unknown success rate, and a table that prints 0% for it retires working skills.
    """

    skill: str
    attempts: int
    successes: int
    ungraded_episodes: int

    @property
    def rate(self) -> float | None:
        return None if self.attempts == 0 else self.successes / self.attempts


@dataclass(frozen=True)
class Agreement:
    """Shadow prediction against what was actually armed (`ARCHITECTURE.md` §4 rule 2).

    `unmeasurable` is on the face of the metric on purpose. Agreement measured only where
    it happened to be measurable is a number that improves when the instrumentation gets
    worse, and Gate C's 90% is worth nothing if the denominator is chosen by accident.
    """

    samples: int
    agreed: int
    unmeasurable: int = 0

    @property
    def rate(self) -> float | None:
        return None if self.samples == 0 else self.agreed / self.samples


@dataclass(frozen=True)
class Counters:
    """One client, one window. Every rate is `None` where it was not measured."""

    client_id: str
    window_s: float
    span_s: float
    first_t: float | None
    last_t: float | None
    ticks: int

    # The headline, and the line under it that says what the headline cost.
    unresolved: int
    unresolved_per_h: float | None
    teacher_calls: int
    teacher_calls_per_h: float | None
    cache_hits: int
    deduped: int

    level: int | None
    xp_pct: float | None
    levels_per_h: float | None
    gold: float | None
    gold_per_h: float | None

    deaths: int
    deaths_per_h: float | None
    time_to_rez_s: float | None
    rez_samples: int
    vitals_unobserved: int

    stuck_events: int
    stuck_events_per_h: float | None
    stuck_long_events: int
    stuck_s: float
    off_route_s: float

    steps: int
    steps_per_h: float | None

    tick_share: dict[str, float]
    skills: tuple[SkillStat, ...]
    addon_ok_pct: float | None

    intent_agreement: Agreement
    skill_agreement: Agreement

    bracket: str | None


def in_window(rows: Iterable[dict[str, Any]], *, start: float, end: float) -> list[dict[str, Any]]:
    """Rows whose timestamp falls in `[start, end]`, in time order."""
    kept = [r for r in rows if r.get("t") is not None and start <= float(r["t"]) <= end]
    kept.sort(key=lambda r: float(r["t"]))
    return kept


def compute(
    streams: Streams,
    *,
    client_id: str,
    now: float | None = None,
    window_s: float = WINDOW_S,
) -> Counters:
    """Every PLAN §16 counter for one client over one window.

    `now` defaults to the last tick rather than the wall clock, so a board rendered from a
    replayed corpus is not silently empty — which is the failure you get exactly once and
    then spend an hour on.
    """
    ticks_all = [t for t in streams.ticks if t.get("client_id") == client_id]
    decs_all = [d for d in streams.decisions if d.get("client_id") == client_id]

    stamps = [float(r["t"]) for r in (*ticks_all, *decs_all) if r.get("t") is not None]
    if now is None:
        now = max(stamps) if stamps else 0.0
    start = now - window_s

    ticks = in_window(ticks_all, start=start, end=now)
    decisions = in_window(decs_all, start=start, end=now)

    first_t = float(ticks[0]["t"]) if ticks else None
    last_t = float(ticks[-1]["t"]) if ticks else None
    span = (last_t - first_t) if (first_t is not None and last_t is not None) else 0.0

    unresolved = {escalation_id(d) for d in decisions if is_unresolved(d)}
    teacher_calls = [d for d in decisions if is_teacher_call(d)]

    progress = _progress_counters(ticks, span)
    safety = _safety_counters(ticks, span)

    return Counters(
        client_id=client_id,
        window_s=window_s,
        span_s=span,
        first_t=first_t,
        last_t=last_t,
        ticks=len(ticks),
        unresolved=len(unresolved),
        unresolved_per_h=_per_h(len(unresolved), span),
        teacher_calls=len(teacher_calls),
        teacher_calls_per_h=_per_h(len(teacher_calls), span),
        cache_hits=sum(1 for d in decisions if d.get("cache_hit")),
        deduped=sum(1 for d in decisions if d.get("dedup_of")),
        tick_share=_tick_share(ticks),
        skills=_skill_table(ticks, decisions, streams.grades),
        addon_ok_pct=_addon_ok_pct(ticks),
        intent_agreement=_intent_agreement(ticks, streams.decisions),
        skill_agreement=_skill_agreement(ticks),
        bracket=bracket_of(progress["level"]),
        **progress,
        **safety,
    )


def _progress_counters(ticks: list[dict[str, Any]], span: float) -> dict[str, Any]:
    levels = [(_path(t, "state", "char", "level"), _path(t, "state", "char", "xp_pct")) for t in ticks]
    seen = [(lv, pc or 0.0) for lv, pc in levels if lv is not None]
    level = seen[-1][0] if seen else None
    xp_pct = seen[-1][1] if seen else None
    lp = [lv + pc for lv, pc in seen]
    levels_gained = (lp[-1] - lp[0]) if len(lp) >= 2 else 0.0

    money = [_path(t, "state", "bags", "money_copper") for t in ticks]
    money = [m for m in money if m is not None]
    gold = money[-1] / COPPER_PER_GOLD if money else None
    gold_delta = ((money[-1] - money[0]) / COPPER_PER_GOLD) if len(money) >= 2 else 0.0

    step_ids = [_path(t, "state", "guide", "step_id") for t in ticks]
    steps = sum(
        1
        for a, b in itertools.pairwise(step_ids)
        if a is not None and b is not None and a != b
    )

    return {
        "level": level,
        "xp_pct": xp_pct,
        "levels_per_h": _per_h(levels_gained, span),
        "gold": gold,
        "gold_per_h": _per_h(gold_delta, span),
        "steps": steps,
        "steps_per_h": _per_h(steps, span),
    }


def _safety_counters(ticks: list[dict[str, Any]], span: float) -> dict[str, Any]:
    deaths = 0
    blind = 0
    rez_times: list[float] = []
    down_since: float | None = None
    for row in ticks:
        d = _dead(row)
        t = float(row.get("t", 0.0))
        if d is None:
            # An unobserved tick carries no information either way, so it neither opens nor
            # closes a death. That makes deaths/h a **floor**: a rez and the next death can
            # both hide inside one blind gap and arrive as a single death. Under-counting
            # is the dangerous direction here — the freeze rule and promotion both read
            # this number — so the blind ticks are counted and shown beside it rather than
            # guessed at.
            blind += 1
            continue
        if d is True and down_since is None:
            # A death is an edge, not a state: counting ticks-while-dead would score one
            # corpse run as forty deaths and make deaths/h a function of the tick rate.
            deaths += 1
            down_since = t
        elif d is False and down_since is not None:
            rez_times.append(t - down_since)
            down_since = None

    stuck = _spans(ticks, lambda r: _path(r, "state", "control", "s1_mode") == "stuck")
    long_stuck = [s for s in stuck if (s[1] - s[0]) > STUCK_S]
    off_route = _spans(ticks, lambda r: _path(r, "state", "guide", "on_route") is False)

    return {
        "deaths": deaths,
        "deaths_per_h": _per_h(deaths, span),
        "time_to_rez_s": (sum(rez_times) / len(rez_times)) if rez_times else None,
        "rez_samples": len(rez_times),
        "vitals_unobserved": blind,
        "stuck_events": len(stuck),
        "stuck_events_per_h": _per_h(len(stuck), span),
        "stuck_long_events": len(long_stuck),
        "stuck_s": _duration(stuck),
        "off_route_s": _duration(off_route),
    }


def _tick_share(ticks: list[dict[str, Any]]) -> dict[str, float]:
    share = dict.fromkeys(ARMED_BY_ORDER, 0.0)
    if not ticks:
        return share
    for row in ticks:
        key = str(row.get("armed_by") or "")
        if key in share:
            share[key] += 1.0
    return {k: v / len(ticks) for k, v in share.items()}


def _addon_ok_pct(ticks: list[dict[str, Any]]) -> float | None:
    seen = [_path(t, "state", "sense", "addon_ok") for t in ticks]
    seen = [v for v in seen if v is not None]
    if not seen:
        return None
    return 100.0 * sum(1 for v in seen if v is True) / len(seen)


def _skill_table(
    ticks: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    grades: list[dict[str, Any]],
) -> tuple[SkillStat, ...]:
    """Success per skill, joined decision -> grade.

    Only skills armed through a decision can be scored: a skill the tracker armed
    mechanically writes no decision row, so it has no outcome to join to. Those show up as
    `ungraded_episodes` instead of being silently dropped, because a skill table that omits
    the skills it cannot score reads as though they never ran.
    """
    by_id = {g.get("decision_id"): g for g in grades}
    attempts: dict[str, int] = {}
    wins: dict[str, int] = {}
    for d in decisions:
        skill = d.get("skill")
        g = by_id.get(d.get("decision_id"))
        if not skill or g is None:
            continue
        attempts[skill] = attempts.get(skill, 0) + 1
        wins[skill] = wins.get(skill, 0) + (1 if g.get("good") else 0)

    episodes: dict[str, int] = {}
    prev: str | None = None
    for row in ticks:
        cur = row.get("armed_skill")
        if cur and cur != prev:
            episodes[cur] = episodes.get(cur, 0) + 1
        prev = cur

    names = sorted(set(attempts) | set(episodes))
    return tuple(
        SkillStat(
            skill=n,
            attempts=attempts.get(n, 0),
            successes=wins.get(n, 0),
            ungraded_episodes=max(0, episodes.get(n, 0) - attempts.get(n, 0)),
        )
        for n in names
    )


def _intent_agreement(ticks: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> Agreement:
    """Gate C's number: shadow intent against the intent the teacher actually armed.

    Only ticks the teacher decided are in the denominator. Ticks the policy drove would be
    the policy agreeing with itself, and ticks the tracker drove are mechanical — neither
    is evidence that the student has learned the teacher.

    The armed intent is not on the tick. `TickRow` carries `armed_skill` but no
    `armed_intent`, so it is recovered by joining `decision_id` to the decision stream, and
    a teacher tick without a `decision_id` is counted as `unmeasurable` rather than
    assumed to agree.
    """
    intents = {d.get("decision_id"): d.get("intent") for d in decisions}
    samples = agreed = unmeasurable = 0
    for row in ticks:
        if row.get("armed_by") != ArmedBy.TEACHER.value:
            continue
        armed = intents.get(row.get("decision_id"))
        shadow = row.get("shadow_intent")
        if armed is None or shadow is None:
            unmeasurable += 1
            continue
        samples += 1
        agreed += 1 if armed == shadow else 0
    return Agreement(samples=samples, agreed=agreed, unmeasurable=unmeasurable)


def _skill_agreement(ticks: list[dict[str, Any]]) -> Agreement:
    """The same comparison one rung lower. Naming the right intent and then arming the
    wrong skill for it is still a wrong answer, and `armed_skill` is on the tick so this
    one needs no join."""
    samples = agreed = unmeasurable = 0
    for row in ticks:
        if row.get("armed_by") != ArmedBy.TEACHER.value:
            continue
        armed, shadow = row.get("armed_skill"), row.get("shadow_skill")
        if armed is None or shadow is None:
            unmeasurable += 1
            continue
        samples += 1
        agreed += 1 if armed == shadow else 0
    return Agreement(samples=samples, agreed=agreed, unmeasurable=unmeasurable)


# --------------------------------------------------------------------- freeze rule


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class FreezeVerdict:
    """Is this band scriptable? PLAN §16's bracket freeze rule, with its workings shown.

    Every criterion is kept even when an earlier one has already failed, because "not yet"
    and "not ever" are different answers and the board is where you tell them apart.
    """

    bracket: str | None
    scriptable: bool
    span_s: float
    checks: tuple[Check, ...]

    def failures(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]


def freeze_verdict(c: Counters, *, min_span_s: float = FREEZE_SPAN_S) -> FreezeVerdict:
    """A band is scriptable after two hours of: deaths/h < 2, no stuck event over 15 s,
    teacher under 3/h, and steps still advancing.

    The last clause is the one that stops this rule being gameable. Every other criterion
    is satisfied perfectly by a character standing still in a safe spot forever, and a
    policy that never moves never dies (`ARCHITECTURE.md` §10, reward hacking).
    """
    checks = (
        Check(
            "span",
            c.span_s >= min_span_s,
            f"{c.span_s / 3600:.2f} h observed, needs {min_span_s / 3600:.0f} h",
        ),
        Check(
            "deaths",
            c.deaths_per_h is not None and c.deaths_per_h < DEATHS_FREEZE_MAX,
            f"deaths/h {_fmt(c.deaths_per_h)}, needs < {DEATHS_FREEZE_MAX:g}",
        ),
        Check(
            "stuck",
            c.stuck_long_events == 0,
            f"{c.stuck_long_events} stuck events over {STUCK_S:g}s, needs 0",
        ),
        Check(
            "teacher",
            c.teacher_calls_per_h is not None and c.teacher_calls_per_h < TEACHER_FREEZE_MAX,
            f"teacher/h {_fmt(c.teacher_calls_per_h)}, needs < {TEACHER_FREEZE_MAX:g}",
        ),
        Check(
            "advancing",
            bool(c.steps_per_h) and c.steps_per_h > 0.0,
            f"steps/h {_fmt(c.steps_per_h)}, needs > 0",
        ),
    )
    return FreezeVerdict(
        bracket=c.bracket,
        scriptable=all(ch.passed for ch in checks),
        span_s=c.span_s,
        checks=checks,
    )


def _fmt(v: float | None) -> str:
    return "unmeasured" if v is None else f"{v:.2f}"
