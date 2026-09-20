"""Distillation: what becomes a training example, what the policy does with it, and what
gets promoted on the strength of it.

The load-bearing claim under all of it is `DECISIONS.md` V9 — **label by outcome, not by
authorship** — so most of these tests are about rows that do *not* become examples.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict
from typing import Any

import pytest

from jev.coach.schema import Intent
from jev.coach.situation import situation_key
from jev.learn import distill, parquet
from jev.learn.dataset import (
    CATEGORICAL,
    CONTINUOUS,
    DEFAULT_AUTHORS,
    EXCLUDED,
    FEATURE_NAMES,
    TEACHER_AND_HUMAN,
    build_from_rows,
    featurize,
)
from jev.learn.episode import DecisionRow, GradeRow, Outcome, Stream, TickRow
from jev.learn.promote import (
    AGREEMENT_GATE,
    DEFAULT_HEARTBEAT_S,
    HEARTBEAT_DROP,
    MIN_AGREEMENT_SAMPLES,
    RETIRE_WINDOW,
    PromotionLedger,
    SkillLedger,
    SkillStatus,
)
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    GuidePos,
    Sense,
    State,
    StepKind,
    Vitals,
)

RUN = "r1"


def _row(obj: Any) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(obj), default=str))


def _state(i: int, *, hp: float = 0.9, free: int = 8, level: int = 14) -> State:
    s = State(
        t=float(i),
        client_id="c01",
        char=Char(level=level, xp_pct=0.4),
        vitals=Vitals(hp=hp, combat=False, dead=False, ghost=False),
        bags=Bags(free=free, durability_min=0.9, money_copper=10_000),
        guide=GuidePos(
            graph_id="g1", step_id=f"s{i % 5}", kind=StepKind.QUEST_OBJECTIVE,
            age_s=30.0, on_route=True, progress=0.3,
        ),
        sense=Sense(addon_ok=True),
    )
    return s.model_copy(update={"situation_key": situation_key(s)})


def _example(
    i: int,
    *,
    intent: str,
    skill: str | None,
    good: bool = True,
    author: ArmedBy = ArmedBy.TEACHER,
    client_id: str = "c01",
    hp: float = 0.9,
    free: int = 8,
    level: int = 14,
    run: str = RUN,
) -> tuple[dict, dict, dict]:
    """One (tick, decision, grade) triple, joined the way the builder joins them."""
    s = _state(i, hp=hp, free=free, level=level).model_copy(update={"client_id": client_id})
    did = f"{run}-d{i}"
    tick = _row(TickRow(
        run_id=run, tick_id=i, t=s.t, client_id=client_id,
        state=s.model_dump(mode="json"), situation_key=s.situation_key or "",
        armed_skill=skill, armed_by=author, decision_id=did,
    ))
    decision = _row(DecisionRow(
        run_id=run, decision_id=did, tick_id=i, t=s.t, client_id=client_id,
        situation_key=s.situation_key or "", author=author, model="claude-sub",
        intent=intent, skill=skill,
    ))
    grade = _row(GradeRow(
        run_id=run, decision_id=did, tick_id=i, window_s=60.0,
        outcome=Outcome.ADVANCED if good else Outcome.DIED,
        step_advanced=good, level_progress_delta=0.1, died=not good,
        stuck_s=0.0, off_route_s=0.0, reward=1.1 if good else -1.0, good=good,
    ))
    return tick, decision, grade


def _corpus(n: int = 300, **kw: Any) -> tuple[list, list, list]:
    """A separable corpus: empty bags mean service, low health means service, else advance.

    Separable on purpose. These tests are about the pipeline, not about whether a boosted
    tree can learn; a corpus with no signal in it would make every assertion about the
    policy a coin flip.
    """
    ticks, decisions, grades = [], [], []
    for i in range(n):
        if i % 3 == 0:
            t, d, g = _example(i, intent="service", skill="BAG_MAKE_SPACE", free=0, **kw)
        elif i % 3 == 1:
            t, d, g = _example(i, intent="service", skill="EAT_DRINK", hp=0.1, **kw)
        else:
            t, d, g = _example(i, intent="advance", skill="GRIND_UNTIL", **kw)
        ticks.append(t)
        decisions.append(d)
        grades.append(g)
    return ticks, decisions, grades


# -------------------------------------------------------------------- features


def test_the_label_is_not_among_the_features():
    """Feeding the armed skill back in gives a model that predicts itself perfectly and
    collapses the moment it drives, when nothing is arming anything yet."""
    assert "armed_skill" not in FEATURE_NAMES
    assert "armed_by" not in FEATURE_NAMES
    assert "control.armed_skill" in EXCLUDED
    assert not set(FEATURE_NAMES) & {"shadow_intent", "shadow_skill", "situation_key"}


def test_coarse_features_bin_exactly_as_the_situation_key_does():
    """The label was given on a bucket. If the classifier's bands drift from the cache's
    bands, the agreement metric compares answers to two different questions."""
    hurt = featurize(_state(1, hp=0.30))
    critical = featurize(_state(1, hp=0.10))
    fine_a, fine_b = featurize(_state(1, hp=0.95)), featurize(_state(1, hp=0.70))

    assert hurt["health_band"] == "hurt"
    assert critical["health_band"] == "critical"
    assert fine_a["health_band"] == fine_b["health_band"] == "ok"
    assert situation_key(_state(1, hp=0.95)) == situation_key(_state(1, hp=0.70))


def test_continuous_features_are_finer_than_the_key_where_the_answer_is():
    """`situation_key` does not carry money at all, and "gold is 40, the mount costs 90" is
    one of the ambiguities the coach exists for. A binned wallet cannot pose it."""
    poor = _state(1).model_copy(update={"bags": Bags(free=8, durability_min=0.9, money_copper=1)})
    rich = _state(1).model_copy(
        update={"bags": Bags(free=8, durability_min=0.9, money_copper=900_000)}
    )
    assert situation_key(poor) == situation_key(rich), "the key deliberately ignores money"
    assert featurize(poor)["gold"] != featurize(rich)["gold"]
    assert "gold" in CONTINUOUS


def test_an_unobserved_field_stays_unobserved():
    """Imputing a median for something nobody looked at invents the observation, inside
    the model, where no other test can see it (`ARCHITECTURE.md` §6)."""
    blind = _state(1).model_copy(update={"vitals": Vitals(hp=None, combat=None)})
    f = featurize(blind)
    assert f["combat"] == "?", "tri-state renders as three values, never two"
    assert f["hp"] != f["hp"], "unknown is NaN, not 0.0"
    assert "?" in CATEGORICAL["combat"]


# --------------------------------------------------------------------- dataset


def test_only_graded_good_rows_become_examples():
    """`DECISIONS.md` V9. Without this filter the student is capped at the teacher's error
    rate and every bad night is training data."""
    ticks, decisions, grades = _corpus(30)
    grades = [{**g, "good": i % 2 == 0} for i, g in enumerate(grades)]
    d = build_from_rows(ticks, decisions, grades)
    assert len(d) == 15
    assert d.dropped.not_good == 15


def test_a_wrong_teacher_call_never_becomes_an_example():
    """The property that makes the teacher affordable: being wrong costs nothing, so the
    teacher can be asked hard questions instead of safe ones."""
    t, dec, g = _example(1, intent="skip", skill="HEARTH", good=False)
    d = build_from_rows([t], [dec], [g])
    assert len(d) == 0
    assert d.dropped.not_good == 1


def test_an_ungraded_decision_is_not_an_example():
    """A live run always has a tail of decisions whose sixty seconds have not elapsed.
    Absence of an outcome is not a good outcome."""
    t, dec, _ = _example(1, intent="advance", skill="GRIND_UNTIL")
    d = build_from_rows([t], [dec], [])
    assert len(d) == 0
    assert d.dropped.no_grade == 1


def test_good_policy_trajectories_can_be_included():
    """`ARCHITECTURE.md` §4 rule 4, the DAgger path. A policy trained only on states the
    teacher was asked about meets a different distribution once it drives, and the states
    it then fails on are exactly the ones missing from its training set."""
    ticks, decisions, grades = _corpus(30, author=ArmedBy.POLICY)
    d = build_from_rows(ticks, decisions, grades, authors=DEFAULT_AUTHORS)
    assert len(d) == 30
    assert d.author_counts() == {ArmedBy.POLICY.value: 30}


def test_policy_trajectories_are_excluded_when_the_filter_says_so():
    """The filter is a parameter, so the conservative corpus has to actually be reachable —
    otherwise the default is the only behaviour and the argument is decoration."""
    ticks, decisions, grades = _corpus(30, author=ArmedBy.POLICY)
    d = build_from_rows(ticks, decisions, grades, authors=TEACHER_AND_HUMAN)
    assert len(d) == 0
    assert d.dropped.wrong_author == 30


def test_a_human_takeover_is_training_data_by_default():
    """The strongest label available. Dropping it would throw away the only rows nobody
    doubts."""
    ticks, decisions, grades = _corpus(6, author=ArmedBy.HUMAN)
    assert len(build_from_rows(ticks, decisions, grades)) == 6


def test_examples_carry_the_bracket_they_happened_in():
    """Promotion is per bracket, so training has to be able to select per bracket too."""
    a = _corpus(6, level=14, run="lowband")
    b = _corpus(6, level=45, run="highband")
    merged = build_from_rows(
        [*a[0], *b[0]], [*a[1], *b[1]], [*a[2], *b[2]]
    )
    assert merged.brackets() == ["12-18", "40-50"]
    assert len(merged.for_bracket("40-50")) == 6


# ---------------------------------------------------------------------- policy


def test_a_cold_start_policy_is_not_confident():
    """With no corpus the scripted GOAP default must keep driving (`ARCHITECTURE.md` §0).
    A policy that guesses confidently on day 0 takes the wheel with nothing behind it."""
    p = distill.cold_start()
    intent, skill, confidence = p.predict(_state(1))
    assert intent is None and skill is None
    assert confidence == 0.0


def test_a_cold_start_policy_abstains_rather_than_saying_wait():
    """`wait` is an instruction — something is in flight, do not act. An empty model
    emitting it would be an answer nobody gave (`ARCHITECTURE.md` §6)."""
    assert distill.cold_start().shadow(_state(1)).is_abstention


def test_training_on_an_empty_corpus_does_not_raise():
    """The nightly job runs on nights where nothing was graded. A learner that crashes on
    its quietest night stops the pipeline."""
    p = distill.train(build_from_rows([], [], []))
    assert p.is_cold


def test_a_trained_policy_predicts_the_outcome_it_was_taught():
    """If the pipeline cannot recover a separable rule, nothing downstream of it means
    anything."""
    p = distill.train(build_from_rows(*_corpus(300)), version=3)
    intent, skill, confidence = p.predict(_state(1, hp=0.1))
    assert intent == Intent.SERVICE.value
    assert skill == "EAT_DRINK"
    assert confidence > 0.0
    assert p.model_name == "policy:v3"


def test_confidence_is_lower_in_a_bucket_the_policy_has_never_seen():
    """A situation nobody trained on is the definition of the case the coach must escalate.
    Reporting the same confidence there is how a wean becomes an outage on new content."""
    p = distill.train(build_from_rows(*_corpus(300)))
    seen = _state(1, hp=0.1)
    unseen = seen.model_copy(update={"situation_key": "v1|never|seen"})
    assert p.predict(unseen)[2] < p.predict(seen)[2]


def test_confidence_is_discounted_by_how_little_evidence_there_is():
    """`predict_proba` on forty rows is a statement about forty rows. Handing it over raw
    is how a policy takes a bracket it has not learned."""
    small = distill.train(build_from_rows(*_corpus(30)))
    large = distill.train(build_from_rows(*_corpus(600)))
    probe = _state(1, hp=0.1)
    assert small.predict(probe)[2] < large.predict(probe)[2]


def test_a_policy_that_only_ever_saw_one_answer_is_not_trusted():
    """A head with no counterexample is right about everything it has seen and knows
    nothing about when the answer changes. It is a default, not a decision."""
    ticks, decisions, grades = [], [], []
    for i in range(50):
        t, d, g = _example(i, intent="advance", skill="GRIND_UNTIL")
        ticks.append(t)
        decisions.append(d)
        grades.append(g)
    p = distill.train(build_from_rows(ticks, decisions, grades))
    assert p.predict(_state(1))[0] == Intent.ADVANCE.value
    assert p.predict(_state(1))[2] <= distill.CONSTANT_HEAD_MAX


def test_the_shadow_prediction_never_raises():
    """It runs on every tick including the ones it is not driving (`ARCHITECTURE.md` §4
    rule 2). An exception in a metric must not take down a live client."""
    p = distill.train(build_from_rows(*_corpus(30)))
    p.intent_head.model = object()  # something that cannot predict
    assert p.shadow(_state(1)).is_abstention


def test_a_saved_policy_reloads_and_predicts_the_same_thing(tmp_path: pathlib.Path):
    """`DecisionRow.model` names `policy:v3`, so v3 has to still be v3 tomorrow."""
    p = distill.train(build_from_rows(*_corpus(300)), version=3)
    reloaded = distill.Policy.load(p.save(tmp_path))
    probe = _state(1, hp=0.1)
    assert reloaded.model_name == "policy:v3"
    assert reloaded.predict(probe) == p.predict(probe)


def test_a_policy_trained_on_other_columns_refuses_to_load(tmp_path: pathlib.Path):
    """A model whose columns moved does not raise on its own — it reads the wrong column
    and answers confidently, all night."""
    import joblib

    p = distill.train(build_from_rows(*_corpus(30)))
    path = p.save(tmp_path)
    payload = joblib.load(path)
    payload["feature_names"] = ("something", "else")
    joblib.dump(payload, path)
    with pytest.raises(ValueError, match="different features"):
        distill.Policy.load(path)


# ------------------------------------------------------------------- promotion


def test_promotion_is_per_bracket_and_does_not_leak():
    """`ARCHITECTURE.md` §4: per bracket, never global. Elwynn's answers are not
    Westfall's, and a global promotion hands the farm to a model tested in one zone."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    led.observe("12-18", agreement=0.95, samples=MIN_AGREEMENT_SAMPLES, deaths_per_h=1.0)

    assert led.is_promoted("12-18")
    assert not led.is_promoted("18-24")
    assert led.promoted_brackets() == ["12-18"]
    assert led.heartbeat_s("18-24") == DEFAULT_HEARTBEAT_S


def test_a_promoted_bracket_drops_the_teacher_heartbeat_tenfold():
    """Gate C's actual deliverable. The units invert the direction, and getting it
    backwards multiplies the teacher bill by a hundred."""
    led = PromotionLedger()
    led.set_baseline("12-18", 0.0)
    led.observe("12-18", agreement=1.0, samples=MIN_AGREEMENT_SAMPLES, deaths_per_h=0.0)
    assert led.heartbeat_s("12-18") == DEFAULT_HEARTBEAT_S * HEARTBEAT_DROP


def test_a_bracket_does_not_promote_on_agreement_alone():
    """A policy can agree with the teacher on every question it is asked and still be
    killing the character between questions."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    state = led.observe("12-18", agreement=1.0, samples=200, deaths_per_h=6.0)
    assert not state.promoted
    assert "deaths/h" in state.reason


def test_a_bracket_does_not_promote_on_a_handful_of_samples():
    """At ten samples one disagreement moves the number ten points, so the gate would be
    decided by which tick the teacher happened to be asked about."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    state = led.observe("12-18", agreement=1.0, samples=5, deaths_per_h=0.0)
    assert not state.promoted
    assert "teacher-decided ticks" in state.reason


def test_the_first_measurement_cannot_promote_itself():
    """"deaths/h has not risen" needs something to have risen from. Comparing a number to
    itself promotes every band on its first quarter of an hour."""
    led = PromotionLedger()
    state = led.observe("12-18", agreement=1.0, samples=200, deaths_per_h=0.0)
    assert not state.promoted
    assert state.baseline_deaths_per_h == 0.0


def test_a_promoted_bracket_is_demoted_when_deaths_rise():
    """Promotion is a measurement, not a verdict. It has to be reversible on the same
    evidence that granted it."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    led.observe("12-18", agreement=1.0, samples=200, deaths_per_h=1.0)
    assert led.is_promoted("12-18")
    led.observe("12-18", agreement=1.0, samples=200, deaths_per_h=8.0)
    assert not led.is_promoted("12-18")


def test_a_promoted_bracket_is_not_demoted_for_having_few_agreement_samples():
    """Promotion itself cuts the agreement denominator tenfold. Re-applying the sample gate
    afterwards would demote every band that was ever promoted, by construction."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    led.observe("12-18", agreement=1.0, samples=200, deaths_per_h=1.0)
    led.observe("12-18", agreement=None, samples=2, deaths_per_h=1.0)
    assert led.is_promoted("12-18")


def test_the_promotion_ledger_survives_a_restart(tmp_path: pathlib.Path):
    """"2 clients x 3 runs" and "deaths/h has not risen" are both claims across runs. A
    ledger that dies with the process cannot make either."""
    led = PromotionLedger()
    led.set_baseline("12-18", 1.0)
    led.observe("12-18", agreement=AGREEMENT_GATE, samples=200, deaths_per_h=1.0)
    back = PromotionLedger.load(led.save(tmp_path / "promotion.json"))
    assert back.is_promoted("12-18")
    assert not back.is_promoted("18-24")


# ---------------------------------------------------------------------- skills


def test_a_skill_becomes_stable_on_two_clients_times_three_runs():
    """PLAN §10. Reproduction across clients is the point: a skill that only works on one
    client's timing is exactly what this gate exists to catch."""
    led = SkillLedger()
    for client in ("c01", "c02"):
        for run in ("r1", "r2", "r3"):
            led.record("VENDOR_REPAIR", client_id=client, run_id=run, ok=True)
    assert led.retrieve("VENDOR_REPAIR").status is SkillStatus.STABLE


def test_one_lucky_client_cannot_make_a_skill_stable():
    """Six successes on one body is not two bodies agreeing, and the looser reading of
    "2 clients x 3 runs" lets exactly that through."""
    led = SkillLedger()
    for run in ("r1", "r2", "r3", "r4", "r5", "r6"):
        led.record("BOAT_OR_ZEP", client_id="c01", run_id=run, ok=True)
    assert led.retrieve("BOAT_OR_ZEP").status is SkillStatus.PROPOSED


def test_a_retired_skill_is_not_returned_by_normal_retrieval():
    """PLAN §10: never let the coach retrieve a retired skill. Returning it and hoping the
    caller checks the status is the same as not retiring it."""
    led = SkillLedger()
    for i in range(RETIRE_WINDOW):
        led.record("BOAT_OR_ZEP", client_id="c01", run_id=f"r{i}", ok=i % 5 == 0)

    assert led.retrieve("BOAT_OR_ZEP") is None
    assert "BOAT_OR_ZEP" not in [r.name for r in led.catalog()]
    assert led.retrieve("BOAT_OR_ZEP", teacher_in_loop=True).status is SkillStatus.RETIRED


def test_a_retired_skill_does_not_un_retire_itself_on_a_lucky_run():
    """Whatever retired it is a fact about the world, and one success does not overturn a
    fact about the world."""
    led = SkillLedger()
    for i in range(RETIRE_WINDOW):
        led.record("BOAT_OR_ZEP", client_id="c01", run_id=f"r{i}", ok=False)
    for i in range(RETIRE_WINDOW):
        led.record("BOAT_OR_ZEP", client_id="c02", run_id=f"g{i}", ok=True)
    assert led.retrieve("BOAT_OR_ZEP") is None


def test_reinstating_a_retired_skill_needs_the_teacher_in_the_loop():
    """The rule is not that a retired skill is unusable — it is that nothing automatic may
    pick one up again."""
    led = SkillLedger()
    for i in range(RETIRE_WINDOW):
        led.record("BOAT_OR_ZEP", client_id="c01", run_id=f"r{i}", ok=False)
    with pytest.raises(PermissionError):
        led.reinstate("BOAT_OR_ZEP", teacher_in_loop=False)
    assert led.reinstate("BOAT_OR_ZEP", teacher_in_loop=True).status is SkillStatus.PROPOSED


def test_a_skill_is_not_retired_before_it_has_twenty_attempts():
    """PLAN §10 says "over 20". Retiring on the first three failures kills every skill
    that needs its `on_fail` edge written."""
    led = SkillLedger()
    for i in range(RETIRE_WINDOW - 1):
        led.record("STUCK_RECOVER", client_id="c01", run_id=f"r{i}", ok=False)
    assert led.retrieve("STUCK_RECOVER") is not None


def test_a_new_skill_has_an_unknown_success_rate_not_a_zero_one():
    """A rate of 0% for a skill nobody has run is the number PLAN §10 retires on."""
    led = SkillLedger()
    assert led.propose("MOUNT_UP").success_rate is None


# --------------------------------------------------------------------- parquet


def test_parquet_conversion_round_trips(tmp_path: pathlib.Path):
    """The converted corpus is what the learner trains on. A lossy conversion is a corpus
    that cannot be regenerated, silently altered."""
    ticks, decisions, grades = _corpus(5)
    for stream, rows in (
        (Stream.TICKS, ticks), (Stream.DECISIONS, decisions), (Stream.GRADES, grades)
    ):
        (tmp_path / f"{stream.value}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )

    out = parquet.convert_run(tmp_path)
    assert parquet.read_parquet(out[Stream.TICKS]) == ticks
    assert parquet.read_parquet(out[Stream.DECISIONS]) == decisions
    assert parquet.read_parquet(out[Stream.GRADES]) == grades


def test_parquet_conversion_tolerates_a_truncated_tail(tmp_path: pathlib.Path):
    """The normal way a run ends is a crash mid-write. A converter that refuses the file
    loses the eight hours in front of the torn line."""
    ticks, _, _ = _corpus(4)
    path = tmp_path / "ticks.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in ticks:
            fh.write(json.dumps(row) + "\n")
        fh.write('{"run_id": "partial", "tick_i')

    out = parquet.convert_run(tmp_path)
    assert parquet.read_parquet(out[Stream.TICKS]) == ticks


def test_parquet_keeps_fields_this_converter_has_never_heard_of(tmp_path: pathlib.Path):
    """A client on a newer `state_v1` writes columns this code does not know. Dropping
    them makes the converter a quiet way to lose the only unreproducible artifact."""
    ticks, _, _ = _corpus(2)
    ticks = [{**t, "field_from_the_future": 7} for t in ticks]
    (tmp_path / "ticks.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ticks), encoding="utf-8"
    )
    out = parquet.convert_run(tmp_path)
    assert parquet.read_parquet(out[Stream.TICKS]) == ticks


def test_an_empty_stream_still_produces_a_readable_table(tmp_path: pathlib.Path):
    """The schema is declared rather than inferred so that files from different nights
    concatenate. An empty night must not be the one that breaks that."""
    (tmp_path / "grades.jsonl").write_text("", encoding="utf-8")
    out = parquet.convert_run(tmp_path)
    assert parquet.read_parquet(out[Stream.GRADES]) == []


def test_the_dataset_reads_parquet_and_jsonl_identically(tmp_path: pathlib.Path):
    """The nightly conversion exists for the learner. If the two paths disagree, training
    depends on whether the converter has run yet."""
    ticks, decisions, grades = _corpus(30)
    for stream, rows in (
        (Stream.TICKS, ticks), (Stream.DECISIONS, decisions), (Stream.GRADES, grades)
    ):
        (tmp_path / f"{stream.value}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    from jev.learn.dataset import build

    before = build([tmp_path])
    parquet.convert_run(tmp_path)
    after = build([tmp_path])
    assert [e.intent for e in before.examples] == [e.intent for e in after.examples]
    assert len(after) == 30
