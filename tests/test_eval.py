"""The eval board. `unresolved/h` is the number the project is judged by, so it is the
number these tests are hardest on.

Rows are built through the real `TickRow` / `DecisionRow` / `GradeRow` dataclasses and
serialised the way `Recorder` serialises them, rather than hand-written as dicts. A test
that hand-rolls its own idea of a row keeps passing after the schema moves underneath it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest

from jev.coach.schema import Intent
from jev.coach.situation import situation_key
from jev.eval import board
from jev.eval.counters import (
    Streams,
    bracket_of,
    compute,
    freeze_verdict,
    is_teacher_call,
    is_unresolved,
)
from jev.learn.episode import DecisionRow, GradeRow, Outcome, TickRow
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    Control,
    GuidePos,
    Pos,
    Sense,
    State,
    StepKind,
    Vitals,
)

RUN = "r1"
CLIENT = "c01"
HOUR = 3600.0


def _row(obj: Any) -> dict[str, Any]:
    """Exactly what `Recorder` writes and `episode.read` reads back."""
    return json.loads(json.dumps(asdict(obj), default=str))


def _state(t: float, **over: Any) -> State:
    base: dict[str, Any] = {
        "t": t,
        "client_id": CLIENT,
        "char": Char(level=14, xp_pct=0.4),
        "pos": Pos(zone="Westfall", zone_id=40, mx=0.4, my=0.5),
        "vitals": Vitals(hp=0.9, combat=False, dead=False, ghost=False),
        "bags": Bags(free=8, durability_min=0.9, money_copper=18_400),
        "guide": GuidePos(
            graph_id="g1", step_id="s1", kind=StepKind.QUEST_OBJECTIVE,
            age_s=30.0, on_route=True, progress=0.2,
        ),
        "sense": Sense(addon_ok=True),
        "control": Control(s1_mode="travel"),
    }
    base.update(over)
    return State(**base)


def _tick(i: int, s: State, **kw: Any) -> dict[str, Any]:
    return _row(
        TickRow(
            run_id=RUN,
            tick_id=i,
            t=s.t,
            client_id=s.client_id,
            state=s.model_dump(mode="json"),
            situation_key=situation_key(s),
            armed_skill=kw.pop("armed_skill", "GRIND_UNTIL"),
            armed_by=kw.pop("armed_by", ArmedBy.TRACKER),
            **kw,
        )
    )


def _decision(did: str, t: float, **kw: Any) -> dict[str, Any]:
    return _row(
        DecisionRow(
            run_id=RUN,
            decision_id=did,
            tick_id=kw.pop("tick_id", 0),
            t=t,
            client_id=CLIENT,
            situation_key=kw.pop("situation_key", "k"),
            author=kw.pop("author", ArmedBy.TEACHER),
            model=kw.pop("model", "claude-sub"),
            intent=kw.pop("intent", Intent.GRIND_RIB.value),
            skill=kw.pop("skill", "GRIND_UNTIL"),
            **kw,
        )
    )


def _grade(did: str, *, good: bool = True) -> dict[str, Any]:
    return _row(
        GradeRow(
            run_id=RUN, decision_id=did, tick_id=0, window_s=60.0,
            outcome=Outcome.ADVANCED if good else Outcome.DIED,
            step_advanced=good, level_progress_delta=0.1, died=not good,
            stuck_s=0.0, off_route_s=0.0, reward=1.1 if good else -1.0, good=good,
        )
    )


def _walk(n: int, dt: float = 60.0, **kw: Any) -> list[dict[str, Any]]:
    """`n` ordinary ticks, `dt` apart, each on its own step so steps keep advancing."""
    return [
        _tick(i, _state(i * dt, guide=GuidePos(
            graph_id="g1", step_id=f"s{i}", kind=StepKind.QUEST_OBJECTIVE,
            age_s=30.0, on_route=True, progress=0.2)), **kw)
        for i in range(n)
    ]


# --------------------------------------------------------------- unresolved/h


def test_unresolved_counts_only_decisions_no_rule_could_settle():
    """Guards the headline against inflation. If tracker predicates and confident policy
    calls were counted, the number would never fall and the metric would say nothing."""
    decisions = [
        _decision("d1", 10.0, author=ArmedBy.POLICY, intent=Intent.ESCALATE.value, skill=None),
        _decision("d2", 20.0, author=ArmedBy.POLICY, intent=Intent.ADVANCE.value),
        _decision("d3", 30.0, author=ArmedBy.TEACHER, situation_key="k3"),
    ]
    assert [is_unresolved(d) for d in decisions] == [True, False, True]


def test_an_escalation_and_the_answer_to_it_are_one_unresolved_decision():
    """The coach escalating and the teacher answering are two rows about one question.
    Counting rows would double the headline the moment escalation starts working."""
    streams = Streams(
        ticks=_walk(4),
        decisions=[
            _decision("d1", 60.0, tick_id=1, situation_key="kx",
                      author=ArmedBy.POLICY, intent=Intent.ESCALATE.value, skill=None),
            _decision("d2", 62.0, tick_id=1, situation_key="kx", author=ArmedBy.TEACHER),
        ],
        grades=[],
    )
    assert compute(streams, client_id=CLIENT).unresolved == 1


def test_unresolved_per_hour_falls_when_escalations_fall():
    """The whole point of the metric: it has to move in the direction the project does.
    A counter that cannot show improvement cannot show a regression either."""
    ticks = _walk(121)  # two hours of ticks, one a minute
    early = [
        _decision(f"e{i}", i * 60.0, tick_id=i, situation_key=f"k{i}",
                  author=ArmedBy.TEACHER)
        for i in range(1, 21)
    ]
    late = [
        _decision(f"l{i}", HOUR + i * 60.0, tick_id=60 + i, situation_key=f"m{i}",
                  author=ArmedBy.TEACHER)
        for i in range(1, 4)
    ]
    streams = Streams(ticks=ticks, decisions=[*early, *late], grades=[])

    first = compute(streams, client_id=CLIENT, now=HOUR, window_s=HOUR)
    second = compute(streams, client_id=CLIENT, now=2 * HOUR, window_s=HOUR)

    assert first.unresolved == 20
    assert second.unresolved == 3
    assert second.unresolved_per_h < first.unresolved_per_h


def test_a_cached_answer_is_still_unresolved_but_costs_nothing():
    """`situation_key`'s saving has to be visible. Billing a cache hit as a teacher call
    hides the only number that shows dedup working; calling it resolved hides the fact
    that no rule settled it."""
    d = _decision("d1", 10.0, author=ArmedBy.TEACHER, cache_hit=True)
    assert is_unresolved(d)
    assert not is_teacher_call(d)


def test_a_rate_nobody_measured_is_not_reported_as_zero():
    """Zero deaths per hour and no evidence about deaths are opposite findings, and the
    freeze rule reads this field."""
    empty = compute(Streams([], [], []), client_id=CLIENT)
    assert empty.deaths_per_h is None
    assert empty.unresolved_per_h is None
    assert empty.addon_ok_pct is None


# ------------------------------------------------------------------- safety


def test_deaths_are_counted_as_edges_not_as_time_spent_dead():
    """Counting ticks-while-dead makes deaths/h a function of the tick rate, so the same
    run scores differently on a loaded machine."""
    dead = Vitals(hp=0.0, combat=False, dead=True, ghost=False)
    ticks = [
        _tick(0, _state(0.0)),
        *[_tick(i, _state(i * 10.0, vitals=dead)) for i in range(1, 6)],
        _tick(6, _state(60.0)),
        _tick(7, _state(70.0, vitals=dead)),
        _tick(8, _state(120.0)),
    ]
    c = compute(Streams(ticks, [], []), client_id=CLIENT)
    assert c.deaths == 2
    assert c.rez_samples == 2


def test_an_unobserved_corpse_does_not_invent_a_death():
    """`None` means nobody looked. Reading it as alive manufactures a death and a
    resurrection every time the vitals reader blinks."""
    blind = Vitals(hp=None, dead=None, ghost=None)
    ticks = [_tick(i, _state(i * 10.0, vitals=blind)) for i in range(5)]
    assert compute(Streams(ticks, [], []), client_id=CLIENT).deaths == 0


def test_deaths_per_hour_is_a_floor_when_the_reader_was_blind():
    """A rez and the next death can both hide inside one blind gap and arrive as a single
    death. The freeze rule and promotion both read deaths/h, and under-counting is the
    direction that hands a band to a policy that is still dying, so the blind ticks are
    reported next to the number rather than silently folded into it."""
    dead = Vitals(hp=0.0, combat=False, dead=True, ghost=False)
    unreadable = Vitals(hp=None, dead=None, ghost=None)
    ticks = [
        _tick(0, _state(0.0)),
        _tick(1, _state(10.0, vitals=dead)),
        _tick(2, _state(20.0, vitals=unreadable)),   # the rez and the next death hide here
        _tick(3, _state(30.0, vitals=dead)),
        _tick(4, _state(40.0)),
    ]
    c = compute(Streams(ticks, [], []), client_id=CLIENT)
    assert c.deaths == 1, "one observed down-episode, not two claimed ones"
    assert c.vitals_unobserved == 1, "and the gap that could hide another is on the face of it"


def test_short_hitches_are_not_stuck_events():
    """PLAN §12.3 penalises stuck over 15 s. Counting every hitch would make the freeze
    rule unsatisfiable on any terrain with a rock in it."""
    stuck = Control(s1_mode="stuck")
    ticks = [
        _tick(0, _state(0.0)),
        _tick(1, _state(10.0, control=stuck)),
        _tick(2, _state(15.0)),                    # 5 s hitch
        _tick(3, _state(20.0, control=stuck)),
        _tick(4, _state(60.0)),                    # 40 s, a real stuck event
    ]
    c = compute(Streams(ticks, [], []), client_id=CLIENT)
    assert c.stuck_events == 2
    assert c.stuck_long_events == 1


# ------------------------------------------------------------------ agreement


def test_agreement_is_computed_only_over_ticks_a_teacher_decided():
    """Ticks the policy drove are the policy agreeing with itself, and tracker ticks are
    mechanical. Including either inflates Gate C's 90% with rows that prove nothing."""
    decisions = [_decision("d1", 0.0, intent=Intent.GRIND_RIB.value)]
    ticks = [
        # Policy-driven, and deliberately disagreeing. Must not enter the denominator.
        *[
            _tick(i, _state(i * 10.0), armed_by=ArmedBy.POLICY,
                  shadow_intent=Intent.SKIP.value, shadow_skill="HEARTH")
            for i in range(5)
        ],
        _tick(5, _state(50.0), armed_by=ArmedBy.TEACHER, decision_id="d1",
              shadow_intent=Intent.GRIND_RIB.value, shadow_skill="GRIND_UNTIL"),
    ]
    a = compute(Streams(ticks, decisions, []), client_id=CLIENT).intent_agreement
    assert a.samples == 1
    assert a.rate == 1.0


def test_a_teacher_tick_with_no_recoverable_intent_is_unmeasurable_not_agreed():
    """The armed intent lives on the decision, not the tick. A teacher tick that cannot be
    joined must not be quietly scored as agreement — a metric that improves when the
    instrumentation degrades is worse than no metric."""
    ticks = [
        _tick(0, _state(0.0), armed_by=ArmedBy.TEACHER,
              shadow_intent=Intent.ADVANCE.value),  # no decision_id to join to
    ]
    a = compute(Streams(ticks, [], []), client_id=CLIENT).intent_agreement
    assert a.samples == 0
    assert a.unmeasurable == 1
    assert a.rate is None


def test_disagreement_is_counted_against_the_policy():
    """A metric that only ever counts hits is an accuracy of 100% with extra steps."""
    decisions = [
        _decision("d1", 0.0, intent=Intent.GRIND_RIB.value),
        _decision("d2", 10.0, intent=Intent.SERVICE.value),
    ]
    ticks = [
        _tick(0, _state(0.0), armed_by=ArmedBy.TEACHER, decision_id="d1",
              shadow_intent=Intent.GRIND_RIB.value),
        _tick(1, _state(10.0), armed_by=ArmedBy.TEACHER, decision_id="d2",
              shadow_intent=Intent.ADVANCE.value),
    ]
    a = compute(Streams(ticks, decisions, []), client_id=CLIENT).intent_agreement
    assert (a.samples, a.agreed) == (2, 1)
    assert a.rate == 0.5


# ------------------------------------------------------------- bracket freeze


def _two_hours(**tick_kw: Any) -> list[dict[str, Any]]:
    return _walk(121, dt=60.0, **tick_kw)


def test_a_bracket_does_not_freeze_while_deaths_are_high():
    """The freeze rule declares a band scriptable and drops the teacher 10x. Freezing a
    band that is still dying hands it to nobody."""
    dead = Vitals(hp=0.0, combat=False, dead=True, ghost=False)
    ticks = _two_hours()
    for i in (10, 11, 30, 31, 50, 51, 70, 71, 90, 91):
        s = _state(i * 60.0, vitals=dead if i % 2 == 0 else Vitals(
            hp=0.9, combat=False, dead=False, ghost=False))
        ticks[i] = _tick(i, s)

    v = freeze_verdict(compute(Streams(ticks, [], []), client_id=CLIENT, window_s=3 * HOUR))
    assert not v.scriptable
    assert any("deaths/h" in why for why in v.failures())


def test_a_bracket_does_not_freeze_on_twenty_minutes_of_evidence():
    """PLAN §16 says two hours. A band that behaved for one quarter of an hour has not
    shown you anything a lucky stretch would not."""
    v = freeze_verdict(compute(Streams(_walk(20), [], []), client_id=CLIENT))
    assert not v.scriptable
    assert any("needs 2 h" in why for why in v.failures())


def test_a_character_standing_still_does_not_freeze_its_bracket():
    """Every other freeze criterion is satisfied perfectly by never moving. Without the
    steps clause the rule rewards a policy that parks in a safe spot for two hours."""
    parked = [
        _tick(i, _state(i * 60.0))  # same step_id throughout
        for i in range(121)
    ]
    v = freeze_verdict(compute(Streams(parked, [], []), client_id=CLIENT, window_s=3 * HOUR))
    assert not v.scriptable
    assert any("steps/h" in why for why in v.failures())


def test_a_clean_quiet_bracket_freezes():
    """The rule has to be satisfiable, or it is a decoration."""
    c = compute(Streams(_two_hours(), [], []), client_id=CLIENT, window_s=3 * HOUR)
    v = freeze_verdict(c)
    assert v.scriptable, v.failures()
    assert v.bracket == "12-18"


def test_the_freeze_rule_reads_teacher_calls_not_unresolved_decisions():
    """PLAN §16 gates on what the teacher cost, and dedup plus cache are what make that
    number fall. Gating on unresolved decisions would punish the saving."""
    ticks = _two_hours()
    cached = [
        _decision(f"c{i}", i * 60.0, tick_id=i, situation_key=f"k{i}",
                  author=ArmedBy.TEACHER, cache_hit=True)
        for i in range(1, 100)
    ]
    c = compute(Streams(ticks, cached, []), client_id=CLIENT, window_s=3 * HOUR)
    assert c.unresolved == 99
    assert c.teacher_calls == 0
    assert freeze_verdict(c).scriptable


# ---------------------------------------------------------------- skill table


def test_an_ungraded_skill_has_an_unknown_rate_not_a_zero_one():
    """A skill table that prints 0% for a skill nobody scored retires working skills, and
    PLAN §10 retires on exactly that number."""
    ticks = [_tick(i, _state(i * 10.0), armed_skill="STUCK_RECOVER") for i in range(4)]
    stats = {s.skill: s for s in compute(Streams(ticks, [], []), client_id=CLIENT).skills}
    assert stats["STUCK_RECOVER"].rate is None
    assert stats["STUCK_RECOVER"].ungraded_episodes == 1


def test_skill_success_comes_from_the_grade_not_from_the_teacher_saying_so():
    """`DECISIONS.md` V9 at the level of the skill table: the teacher choosing a skill is
    not evidence that it worked."""
    decisions = [
        _decision("d1", 0.0, skill="VENDOR_REPAIR"),
        _decision("d2", 10.0, skill="VENDOR_REPAIR"),
    ]
    grades = [_grade("d1", good=True), _grade("d2", good=False)]
    stats = {
        s.skill: s
        for s in compute(Streams(_walk(3), decisions, grades), client_id=CLIENT).skills
    }
    assert stats["VENDOR_REPAIR"].attempts == 2
    assert stats["VENDOR_REPAIR"].rate == 0.5


# --------------------------------------------------------------------- board


def test_the_board_leads_with_unresolved_per_hour():
    """`ARCHITECTURE.md` §1 makes this the headline. A layout that files it among twenty
    other rates is a layout that lets it climb unnoticed."""
    text = board.render(compute(Streams(_walk(5), [], []), client_id=CLIENT))
    head, rest = text.split("PROGRESS", 1)
    assert "UNRESOLVED/h" in head
    assert "UNRESOLVED/h" not in rest


def test_the_board_prints_a_dash_for_what_it_did_not_measure():
    """A board that prints 0.0 for an unmeasured rate is a board that will be believed."""
    text = board.render(compute(Streams([], [], []), client_id=CLIENT))
    assert "no ticks" not in text
    assert "-" in text


def test_the_board_names_every_author_even_at_zero():
    """A share table whose rows come and go cannot be read at a glance, and `armed_by` is
    the field the whole corpus hangs on (`ARCHITECTURE.md` §4 rule 1)."""
    text = board.render(compute(Streams(_walk(3), [], []), client_id=CLIENT))
    for name in ("s1_preempt", "tracker", "policy", "teacher", "human"):
        assert name in text


@pytest.mark.parametrize(
    ("level", "expected"),
    [(1, "1-6"), (5, "1-6"), (6, "6-12"), (14, "12-18"), (69, "68-70"), (70, "68-70")],
)
def test_levels_land_in_the_plan_s_bands(level: int, expected: str):
    """Promotion is per bracket, so a level in the wrong band promotes the wrong band."""
    assert bracket_of(level) == expected


def test_an_unobserved_level_belongs_to_no_band():
    """Guessing a band would promote a policy on evidence gathered somewhere else."""
    assert bracket_of(None) is None
