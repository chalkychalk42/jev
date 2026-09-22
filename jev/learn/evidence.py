"""Outcome comparisons, with run isolation and non-overlapping observation windows.

Shadow agreement is descriptive only. A canary needs held-out observed support for its
actual intent/skill pairs; promotion needs subsequent outcomes actually driven by that
model. Neither evaluator calls matched historical actions a counterfactual experiment.
"""
from __future__ import annotations

import hashlib
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from jev.eval.counters import bracket_of
from jev.learn.dataset import Dataset
from jev.learn.distill import Policy
from jev.world.state_v1 import Source, State


def split_runs(dataset: Dataset, *, fraction: float = 0.25) -> tuple[Dataset, Dataset]:
    if not 0 < fraction < 1:
        raise ValueError("held-out fraction must lie between zero and one")
    runs = sorted({e.run_id for e in dataset.examples},
                  key=lambda run: hashlib.sha256(run.encode()).hexdigest())
    n = max(1, math.ceil(len(runs) * fraction)) if len(runs) > 1 else 0
    held = set(runs[-n:]) if n else set()
    training = Dataset(tuple(e for e in dataset.examples if e.run_id not in held),
                       dataset.authors, dataset.dropped)
    testing = Dataset(tuple(e for e in dataset.examples if e.run_id in held),
                      dataset.authors, dataset.dropped)
    return training, testing


@dataclass(frozen=True)
class Observation:
    run_id: str
    client_id: str
    decision_id: str
    model: str
    t: float
    window_s: float
    graph: str
    bracket: str
    intent: str
    skill: str | None
    state: State
    advanced: bool
    xp: float
    died: bool
    stuck_s: float
    reward: float
    good: bool
    graph_digest: str | None = None
    exclusive_model: bool = True
    deaths: int | None = None
    advances: int | None = None


def observations(ticks: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]],
                 grades: Sequence[Mapping[str, Any]], *,
                 include_synthetic: bool = False,
                 graph_digests: Mapping[str, str] | None = None) -> list[Observation]:
    by_tick = {(t.get("run_id"), t.get("client_id"), t.get("tick_id")): t for t in ticks}
    by_grade = {(g.get("run_id"), g.get("decision_id")): g for g in grades}
    by_decision = {(d.get("run_id"), d.get("decision_id")): d for d in decisions}
    by_client = defaultdict(list)
    for tick in ticks:
        by_client[(tick.get("run_id"), tick.get("client_id"))].append(tick)
    coverage = {}
    events = {}
    for key, rows in by_client.items():
        rows.sort(key=lambda t: t["t"])
        missing = [0]
        death_edges, completions, was_down = [0], [0], None
        for tick in rows:
            vitals = tick.get("state", {}).get("vitals", {})
            char = tick.get("state", {}).get("char", {})
            source = tick.get("state", {}).get("sense", {}).get("source")
            known = (type(vitals.get("dead")) is bool and type(vitals.get("ghost")) is bool
                     and char.get("level") is not None and char.get("xp_pct") is not None
                     and (include_synthetic or source not in ("synthetic", "replay")))
            missing.append(missing[-1] + int(not known))
            down = vitals.get("dead") is True or vitals.get("ghost") is True
            death_edges.append(death_edges[-1] + int(down and was_down is not True))
            completions.append(completions[-1] + int(tick.get("tracker_event") == "advance"))
            if type(vitals.get("dead")) is bool and type(vitals.get("ghost")) is bool:
                was_down = down
        coverage[key] = [r["t"] for r in rows], missing
        events[key] = death_edges, completions
    result = []
    seen = set()
    for d in decisions:
        key = (d.get("run_id"), d.get("decision_id"))
        if key in seen:
            raise ValueError(f"duplicate outcome decision: {key}")
        seen.add(key)
        tick = by_tick.get((key[0], d.get("client_id"), d.get("tick_id")))
        g = by_grade.get(key)
        if (not tick or not g or d.get("status", "ok") != "ok"
                or tick.get("decision_id") != key[1]
                or d.get("intent") in (None, "wait", "escalate")
                or g.get("outcome") == "abandoned"):
            continue
        try:
            state = State.model_validate(tick["state"])
        except Exception:
            continue
        if (state.t != d.get("t") or state.client_id != d.get("client_id")
                or (not include_synthetic and state.sense.source in (Source.SYNTHETIC, Source.REPLAY))):
            continue
        bracket, graph = bracket_of(state.char.level), state.guide.graph_id
        if not bracket or not graph or g.get("window_s", 0) <= 0:
            continue
        stamps, missing = coverage[(key[0], state.client_id)]
        lo, hi = bisect_left(stamps, state.t), bisect_right(stamps, state.t + g["window_s"])
        if missing[hi] - missing[lo]:
            # A completion label can be useful for training while the death/XP baseline
            # is unknown; it still cannot prove that a candidate is safe to promote.
            continue
        model = d.get("model") or "unknown"
        owner = _driver(model)
        exclusive = True
        for observed_tick in by_client[(key[0], state.client_id)][lo:hi]:
            controlling = by_decision.get((key[0], observed_tick.get("decision_id")))
            if (not controlling or controlling.get("client_id") != state.client_id
                    or controlling.get("status", "ok") != "ok"
                    or _driver(controlling.get("model") or "unknown") != owner):
                exclusive = False
                break
        death_edges, completions = events[(key[0], state.client_id)]
        # Events on the decision's initial observation preceded its action. Counting
        # (start, end] also prevents adjacent windows sharing an event at their boundary.
        event_start = bisect_right(stamps, state.t)
        result.append(Observation(str(key[0]), state.client_id, str(key[1]),
                                  model, state.t,
                                  float(g["window_s"]), graph, bracket, d["intent"],
                                  d.get("skill"), state, bool(g["step_advanced"]),
                                  float(g["level_progress_delta"]), bool(g["died"]),
                                  float(g["stuck_s"]), float(g["reward"]), bool(g["good"]),
                                  (graph_digests or {}).get(graph), exclusive,
                                  death_edges[hi] - death_edges[event_start],
                                  completions[hi] - completions[event_start]))
    return result


def _driver(model: str) -> str:
    # Different scripted priority rules are one baseline controller. Two learned
    # versions (even when emitting identical actions) are separate interventions.
    return "scripted" if model.startswith("scripted:") else model


def independent(rows: Iterable[Observation]) -> list[Observation]:
    """One death/progress window cannot become fifty samples by choosing at 2 Hz."""
    ends: dict[tuple[str, str], float] = {}
    output = []
    for row in sorted(rows, key=lambda r: (r.run_id, r.client_id, r.t, r.decision_id)):
        key = (row.run_id, row.client_id)
        if row.t < ends.get(key, -math.inf):
            continue
        output.append(row)
        ends[key] = row.t + row.window_s
    return output


@dataclass(frozen=True)
class OutcomeMetrics:
    windows: int
    runs: int
    observed_s: float
    good_rate: float | None
    steps_per_h: float | None
    xp_per_h: float | None
    deaths_per_h: float | None
    stall_fraction: float | None
    mean_reward: float | None


def metrics(rows: Iterable[Observation]) -> OutcomeMetrics:
    rows = independent(rows)
    seconds = sum(r.window_s for r in rows)
    if not rows or seconds <= 0:
        return OutcomeMetrics(0, 0, 0, None, None, None, None, None, None)
    scale = 3600 / seconds
    return OutcomeMetrics(len(rows), len({r.run_id for r in rows}), seconds,
                          sum(r.good and r.exclusive_model for r in rows) / len(rows),
                          sum((r.advances if r.advances is not None else int(r.advanced))
                              if r.exclusive_model else 0 for r in rows) * scale,
                          sum(r.xp if r.exclusive_model else min(0, r.xp) for r in rows) * scale,
                          sum(r.deaths if r.deaths is not None else int(r.died)
                              for r in rows) * scale,
                          sum(max(r.stuck_s, r.window_s if not r.advanced and r.xp <= 0 else 0)
                              for r in rows) / seconds,
                          sum(r.reward if r.exclusive_model else min(0, r.reward)
                              for r in rows) / len(rows))


@dataclass(frozen=True)
class EvidenceGates:
    min_windows: int = 30
    min_runs: int = 3
    min_observed_s: float = 1800
    min_good_rate: float = 0.8
    min_match_rate: float = 0.9
    min_coverage: float = 0.25
    confidence: float = 0.7
    deaths_slack_per_h: float = 0.0
    stall_slack: float = 0.01
    progress_fraction: float = 0.95

    def __post_init__(self) -> None:
        if min(self.min_windows, self.min_runs, self.min_observed_s) <= 0:
            raise ValueError("promotion needs positive observed windows, runs and time")
        probabilities = (self.min_good_rate, self.min_match_rate, self.min_coverage,
                         self.confidence, self.progress_fraction)
        if any(not 0 < p <= 1 for p in probabilities):
            raise ValueError("evidence gates must be probabilities in (0,1]")
        if self.deaths_slack_per_h < 0 or self.stall_slack < 0:
            raise ValueError("regression tolerances cannot be negative")


def compare(candidate: Iterable[Observation], baseline: Iterable[Observation],
            gates: EvidenceGates, *, improvement: bool) -> dict[str, Any]:
    candidate, baseline = list(candidate), list(baseline)
    checks: dict[str, bool] = {}
    c, b = metrics(candidate), metrics(baseline)
    for label, value in (("candidate", c), ("baseline", b)):
        checks[f"{label}_windows"] = value.windows >= gates.min_windows
        checks[f"{label}_runs"] = value.runs >= gates.min_runs
        checks[f"{label}_span"] = value.observed_s >= gates.min_observed_s
    checks["observed_progress"] = bool((c.steps_per_h and c.steps_per_h > 0)
                                       or (c.xp_per_h and c.xp_per_h > 0))
    checks["good_outcomes"] = c.good_rate is not None and c.good_rate >= gates.min_good_rate
    checks["unmixed_baseline"] = all(r.exclusive_model for r in independent(baseline))
    # Compare each route separately; a safer/easier guide cannot carry another guide.
    routes = sorted({r.graph for r in candidate})
    details = {}
    improved = False
    for graph in routes:
        cm = metrics(r for r in candidate if r.graph == graph)
        bm = metrics(r for r in baseline if r.graph == graph)
        observed = cm.windows > 0 and bm.windows > 0
        checks[f"{graph}:baseline"] = observed
        if not observed:
            continue
        checks[f"{graph}:deaths"] = cm.deaths_per_h <= bm.deaths_per_h + gates.deaths_slack_per_h
        checks[f"{graph}:stalls"] = cm.stall_fraction <= bm.stall_fraction + gates.stall_slack
        checks[f"{graph}:steps"] = cm.steps_per_h >= bm.steps_per_h * gates.progress_fraction
        checks[f"{graph}:xp"] = cm.xp_per_h >= bm.xp_per_h * gates.progress_fraction
        improved |= (cm.steps_per_h > bm.steps_per_h * 1.05
                     or cm.xp_per_h > bm.xp_per_h * 1.05
                     or cm.deaths_per_h < bm.deaths_per_h
                     or cm.stall_fraction + 0.01 < bm.stall_fraction)
        details[graph] = {"candidate": asdict(cm), "baseline": asdict(bm)}
    if improvement:
        checks["measured_improvement"] = improved
    return {"eligible": bool(checks) and all(checks.values()), "checks": checks,
            "candidate": asdict(c), "baseline": asdict(b), "routes": details}


def shadow_evidence(policy: Policy, rows: Sequence[Observation], bracket: str,
                    heldout_runs: set[str], gates: EvidenceGates) -> dict[str, Any]:
    rows = independent(r for r in rows if r.bracket == bracket and r.run_id in heldout_runs
                       and r.exclusive_model)
    matches, confident = [], 0
    for row in rows:
        p = policy.predict_full(row.state)
        if (p.confidence < gates.confidence or p.skill_confidence is None
                or p.skill_confidence < gates.confidence):
            continue
        confident += 1
        if p.intent == row.intent and p.skill == row.skill:
            matches.append(row)
    report = compare(matches, rows, gates, improvement=False)
    coverage = confident / len(rows) if rows else 0.0
    match_rate = len(matches) / confident if confident else 0.0
    report["checks"].update(coverage=coverage >= gates.min_coverage,
                            supported_actions=match_rate >= gates.min_match_rate)
    report.update(coverage=coverage, match_rate=match_rate,
                  eligible=all(report["checks"].values()),
                  interpretation="held-out support for observed actions; not a causal improvement claim")
    return report
