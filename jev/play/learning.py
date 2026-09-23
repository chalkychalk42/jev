"""Outcome-qualified motor learning and measured, per-capability teacher handover.

The student is an inspectable nearest-neighbour classifier. Its labels are complete
bounded actions, including duration, direction and slot. It learns from observed
successful effects, not from teacher authorship. Failed attempts remain in the corpus
and veto repeating the same action in a covered context. Screenshots contribute a
separate distance term: similar radio state cannot make two different scenes equivalent.

Models and records are JSON, atomically published, with no executable pickle payload.
Whole runs (and linked encounters) are held out. Later live shadow and canary evidence
must be outside both fitting and evaluation runs. Synthetic experience can exercise this
module in tests, but cannot train or promote a production student.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jev.persist import atomic_json, file_lock

FORMAT = 1
_MOTION = {"move_forward", "move_backward", "turn_left", "turn_right", "strafe_left",
           "strafe_right", "jump"}
_CATEGORICAL = (
    "char.class_id", "vitals.power_type", "vitals.combat", "vitals.dead", "vitals.ghost",
    "flags.mounted", "flags.swimming", "flags.falling", "flags.on_taxi",
    "target.has", "target.reaction", "target.classification", "target.in_melee",
    "target.attacking_me", "target.tapped_by_other", "ui.modal", "ui.loot", "ui.gossip",
    "ui.vendor", "ui.quest_frame", "ui.trainer", "ui.mail", "ui.error_id",
)
_NUMERIC = ("vitals.hp", "vitals.power", "target.hp", "target.dist", "bags.free",
            "bags.durability_min", "bars.gcd")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stamp(row: Mapping[str, Any]) -> float:
    value = row.get("timestamp", row.get("t", 0))
    return float(value) if _number(value) else 0.0


def _flat(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            result.update(_flat(item, name))
        elif isinstance(item, (list, tuple)):
            result.update({f"{name}.{i}": part for i, part in enumerate(item) if _number(part)})
        else:
            result[name] = item
    return result


@dataclass(frozen=True)
class LearningConfig:
    """Evaluation gates, not motion/calibration constants; saved with every model."""

    min_train_runs: int = 3
    min_train_examples: int = 40
    min_holdout_runs: int = 2
    min_holdout_examples: int = 20
    min_support: int = 3
    min_support_runs: int = 2
    min_agreement: float = 0.90
    min_coverage: float = 0.50
    max_distance: float = 0.15
    min_shadow_examples: int = 50
    min_shadow_runs: int = 3
    min_canary_examples: int = 30
    min_canary_runs: int = 3
    min_teacher_baseline: int = 30
    canary_fraction: float = 0.10
    canary_duration_s: float = 7200.0
    audit_fraction: float = 0.10
    retrain_new_runs: int = 3
    max_train_examples: int = 1200

    def __post_init__(self) -> None:
        for name in ("min_train_runs", "min_train_examples", "min_holdout_runs",
                     "min_holdout_examples", "min_support", "min_support_runs",
                     "min_shadow_examples", "min_shadow_runs", "min_canary_examples",
                     "min_canary_runs", "min_teacher_baseline", "retrain_new_runs",
                     "max_train_examples"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in ("min_agreement", "min_coverage", "max_distance", "canary_fraction",
                     "audit_fraction"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.canary_duration_s <= 0:
            raise ValueError("canary_duration_s must be positive")


@dataclass(frozen=True)
class MotorPrediction:
    """Only `canary`/`active` permits execution; `shadow` is a comparison proposal."""

    action: dict[str, Any] | None = None
    model: str | None = None
    mode: str = "teacher"
    confidence: float = 0.0
    reason: str = "no trained student"
    support: int = 0
    expected_effect: str | None = None

    @property
    def can_execute(self) -> bool:
        return self.action is not None and self.mode in {"canary", "active"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kind(action: Mapping[str, Any]) -> str:
    return str(action.get("type", action.get("kind", "")))


def _spatial(action: Mapping[str, Any]) -> bool:
    return (_kind(action) in {"camera", "pointer"}
            or (_kind(action) == "key" and action.get("control") in _MOTION)
            or (_kind(action) == "click" and action.get("intent") != "ui"))


def _parameters(action: Mapping[str, Any]) -> dict[str, float]:
    fields = {"key": ("duration_s",), "observe": ("wait_s",), "pointer": ("x", "y"),
              "click": ("x", "y"), "camera": ("pixels",)}.get(_kind(action), ())
    return {key: float(action[key]) for key in fields if _number(action.get(key))}


def _template(action: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in action.items() if key not in _parameters(action)}
    if _kind(action) == "camera":
        result["direction"] = 1 if action["pixels"] > 0 else -1
    return result


def _class_key(action: Mapping[str, Any], expected: str) -> str:
    return _json([_template(action), expected])


def _parameter_error(a: Mapping[str, Any], b: Mapping[str, Any], scales: Mapping[str, float]) -> float:
    if _template(a) != _template(b):
        return math.inf
    left, right = _parameters(a), _parameters(b)
    if left.keys() != right.keys():
        return math.inf
    prefix = _digest(_template(a))
    return max((abs(left[key] - right[key]) / scales[f"{prefix}.{key}"] for key in left), default=0.0)


def _equivalent(model: Mapping[str, Any], a: Mapping[str, Any], b: Mapping[str, Any],
                expected: str) -> bool:
    return (_template(a) == _template(b) and _parameter_error(a, b, model["parameter_scales"])
            <= model["parameter_radii"].get(_class_key(a, expected), 0.0))


def _label(action: Mapping[str, Any]) -> dict[str, Any] | None:
    """Only executable bounded schemas become labels; IDs are bound from current context."""
    from jev.play.actions import action_dict, parse_action

    try:
        clean = action_dict(parse_action(dict(action)))
    except (TypeError, ValueError):
        return None
    if _kind(clean) == "skill":
        # A routine Jev chose inside an objective is a whole action: which routine, in
        # which situation, and whether the episode it served succeeded. Parameters would
        # be graph facts the coach owns, so only parameterless choices become labels -
        # the tutor's contract never sends any; the body binds the rest itself.
        return clean if not clean.get("params") else None
    if _kind(clean) == "click":
        if clean.get("intent") == "ui":
            if not clean.get("ui_control"):
                return None
            # UI text identity is fresh context, not a remembered gossip entry.
            if clean.get("ui_name_id") is not None:
                clean["ui_name_id"] = "$current_ui"
        else:
            if (not _number(clean.get("x")) or not _number(clean.get("y"))
                    or clean.get("expected_target_id") is None):
                return None
            clean["expected_target_id"] = "$current_target"
    return clean


def _bind(action: dict[str, Any], observation: Mapping[str, Any]) -> dict[str, Any] | None:
    result = dict(action)
    context = observation.get("context") or {}
    if result.get("expected_target_id") == "$current_target":
        target = context.get("target_name_id")
        if target is None:
            target = (observation.get("values") or {}).get("target.name_id")
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            return None
        result["expected_target_id"] = target
    if result.get("ui_name_id") == "$current_ui":
        target = context.get("ui_name_id")
        if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
            return None
        result["ui_name_id"] = target
    return result


def _features(observation: Mapping[str, Any], *, visual: bool = True) -> dict[str, Any]:
    values = _flat(observation.get("state") or {})
    values.update(observation.get("values") or {})
    context = observation.get("context") or {}
    categories = {key: values.get(key) for key in _CATEGORICAL}
    categories.update({"context.skill": context.get("skill"),
                       "context.kind": context.get("kind"),
                       "size": observation.get("size")})
    for field in ("bars.ready", "bars.usable"):
        bits = values.get(field)
        for slot in range(1, 13):
            categories[f"{field}.{slot}"] = bool(bits & (1 << (slot - 1))) if isinstance(bits, int) else None
    numeric = {key: float(values[key]) for key in _NUMERIC if _number(values.get(key))}
    features = _flat(observation.get("features") or {})
    pixels = {key: float(value) for key, value in features.items() if _number(value)}
    screen = observation.get("screen") or {}
    if not screen.get("sha256") or not visual:
        # A numerical claim without an owned screen is not visual evidence. A choice that
        # moves nothing in the world - a routine, a key tap, an action slot - is a decision
        # about the game state, and needs no familiar scenery to transfer.
        pixels = {}
    return {"categorical": categories, "numeric": numeric, "visual": pixels}


def _distance(a: dict[str, Any], b: dict[str, Any], scales: dict[str, float]) -> float:
    """Distance from an observation `a` to a stored example or failure `b`."""
    if a["categorical"] != b["categorical"]:
        return math.inf
    distances = []
    for block in ("numeric", "visual"):
        left, right = a[block], b[block]
        if block == "visual" and not right:
            continue         # the example is a state decision; scenery does not decide it
        if left.keys() != right.keys():
            return math.inf  # Missing information is a new context, not an imputed zero.
        if left:
            distances.append(math.sqrt(sum(
                ((left[key] - right[key]) / scales[f"{block}.{key}"]) ** 2 for key in left
            ) / len(left)))
    return max(distances, default=math.inf)


def _effect_qualified(row: Mapping[str, Any]) -> bool:
    outcome = row.get("outcome") or {}
    before, after = row.get("before") or {}, row.get("after") or {}
    executed = row.get("execution_before") or before
    return (row.get("synthetic") is False and before.get("synthetic") is not True
            and executed.get("synthetic") is not True
            and after.get("synthetic") is not True and outcome.get("verified") is True
            and outcome.get("success") is True and bool(outcome.get("effects"))
            and row.get("expected_effect") in outcome["effects"]
            and _performed(row)
            and outcome.get("fatal") is not True and before.get("id") is not None
            and after.get("id") is not None and executed.get("id") is not None
            and executed["id"] != after["id"]
            and _number(executed.get("captured_at")) and _number(after.get("captured_at"))
            and after["captured_at"] > executed["captured_at"])


def _episode_qualified(outcome: Mapping[str, Any]) -> bool:
    return (outcome.get("verified") is True and outcome.get("success") is True
            and _number(outcome.get("progress")) and outcome["progress"] > 0
            and outcome.get("synthetic") is not True
            and outcome.get("fatal") is not True)


def _qualified(row: Mapping[str, Any]) -> bool:
    return _effect_qualified(row) and _episode_qualified(row.get("episode_outcome") or {})


def _performed(row: Mapping[str, Any]) -> bool:
    delivery = row.get("delivery")
    return (delivery is None or delivery.get("delivered") is True
            or (_kind(row.get("action") or {}) == "observe" and delivery.get("code") == "observed")
            or (_kind(row.get("action") or {}) == "skill" and delivery.get("code") == "delegated"))


def _failed(row: Mapping[str, Any]) -> bool:
    outcome = row.get("outcome") or {}
    return (row.get("synthetic") is False and outcome.get("verified") is True
            and _performed(row)
            and (outcome.get("success") is False or outcome.get("fatal") is True))


def _grouped_split(rows: list[dict[str, Any]], holdout_runs: int) -> tuple[list, list]:
    """Keep entire runs together and union runs linked by explicit encounter IDs."""
    runs = {str(row["run_id"]) for row in rows}
    parent = {run: run for run in runs}

    def find(run: str) -> str:
        while parent[run] != run:
            parent[run] = parent[parent[run]]
            run = parent[run]
        return run

    encounters: dict[str, str] = {}
    for row in rows:
        encounter = row.get("encounter_id")
        run = str(row["run_id"])
        if encounter is not None:
            if str(encounter) in encounters:
                parent[find(run)] = find(encounters[str(encounter)])
            else:
                encounters[str(encounter)] = run
    groups: dict[str, set[str]] = defaultdict(set)
    for run in runs:
        groups[find(run)].add(run)
    # Stable independent of directory order and timestamps. No example-level random split.
    ordered = sorted(groups.values(), key=lambda group: _digest(sorted(group)))
    selected: set[str] = set()
    for group in ordered:
        if len(selected) >= holdout_runs:
            break
        selected.update(group)
    return ([r for r in rows if str(r["run_id"]) not in selected],
            [r for r in rows if str(r["run_id"]) in selected])


def _proposal(model: dict[str, Any], observation: Mapping[str, Any], config: LearningConfig,
              *, failures: list[dict[str, Any]] | None = None) -> tuple[dict | None, float, int, str, str | None]:
    features = _features(observation)
    neighbours = []
    for example in model["examples"]:
        if _spatial(example["action"]) and not features["visual"]:
            continue
        distance = _distance(features, example["features"], model["scales"])
        if distance <= model["radius"]:
            neighbours.append((distance, example))
    if not neighbours:
        return None, 0.0, 0, "unseen state or visual context", None
    counts = Counter(_class_key(example["action"], example["expected_effect"])
                     for _, example in neighbours)
    label, count = counts.most_common(1)[0]
    selected = [example for _, example in neighbours
                if _class_key(example["action"], example["expected_effect"]) == label]
    agreement = count / len(neighbours)
    if (count < config.min_support or len({r["run_id"] for r in selected}) < config.min_support_runs
            or agreement < config.min_agreement):
        return None, agreement, count, "insufficient independent or unambiguous support", None
    # Labels choose a structural action and effect; continuous parameters come from a
    # locally supported observed example. The median supplies only a robust ranking:
    # an interpolated coordinate or duration is never emitted as an untested action.
    parameters = [_parameters(example["action"]) for example in selected]
    centre = {key: statistics.median(row[key] for row in parameters) for key in parameters[0]}
    prefix = _digest(_template(selected[0]["action"]))
    def parameter_rank(example):
        actual = _parameters(example["action"])
        score = sum(abs(actual[key] - value) / model["parameter_scales"][f"{prefix}.{key}"]
                    for key, value in centre.items())
        return score, _distance(features, example["features"], model["scales"]), _json(example["action"])

    representative = min(selected, key=parameter_rank)
    action, expected = representative["action"], representative["expected_effect"]
    for failed in [*model.get("failures", []), *(failures or [])]:
        if (_equivalent(model, failed["action"], action, expected)
                and _distance(features, failed["features"], model["scales"]) <= model["radius"]):
            return None, agreement, count, "observed failure vetoes this action in this context", None
    bound = _bind(action, observation)
    if bound is None:
        return None, agreement, count, "current target or UI identity missing", None
    return (bound, agreement, count, "covered by outcome-qualified independent examples",
            expected)


def _cross_run_radii(examples: list[dict], scales: dict[str, float],
                     parameter_scales: dict[str, float],
                     cancelled: Callable[[], bool]) -> tuple[list[float], dict[str, float]]:
    """Calibrate coverage using independent runs, with bounded matrix allocation."""
    import numpy as np

    groups: dict[str, list] = defaultdict(list)
    for example in examples:
        features = example["features"]
        signature = _json([_template(example["action"]), example["expected_effect"],
                           features["categorical"], sorted(features["numeric"]),
                           sorted(features["visual"])])
        groups[signature].append(example)
    radii = []
    parameter_errors: dict[str, list] = defaultdict(list)
    for group in groups.values():
        if cancelled():
            return [], {}
        if len({row["run_id"] for row in group}) < 2:
            continue
        distances = np.zeros((len(group), len(group)))
        for block in ("numeric", "visual"):
            keys = sorted(group[0]["features"][block])
            if not keys:
                continue
            matrix = np.asarray([[row["features"][block][key] / scales[f"{block}.{key}"]
                                  for key in keys] for row in group], dtype=float)
            # Centre first: constants should yield zero, without subtracting huge nearly
            # equal floating-point dot products from very narrow calibrated ranges.
            matrix -= matrix[0]
            norms = (matrix * matrix).sum(axis=1)
            squared = (norms[:, None] + norms[None, :] - 2 * (matrix @ matrix.T)) / len(keys)
            np.maximum(distances, np.maximum(squared, 0), out=distances)
        runs = np.asarray([row["run_id"] for row in group])
        distances[runs[:, None] == runs[None, :]] = np.inf
        nearest = np.sqrt(distances.min(axis=1))
        radii.extend(float(value) for value in nearest if math.isfinite(value))
        for row, index, distance in zip(group, distances.argmin(axis=1), nearest, strict=True):
            if math.isfinite(distance):
                key = _class_key(row["action"], row["expected_effect"])
                parameter_errors[key].append(_parameter_error(
                    row["action"], group[int(index)]["action"], parameter_scales))
    calibrated = {key: sorted(errors)[int((len(errors) - 1) * 0.90)]
                  for key, errors in parameter_errors.items()}
    return radii, calibrated


def _fit(rows: list[dict[str, Any]], config: LearningConfig, *,
         cancelled: Callable[[], bool] = lambda: False) -> tuple[dict | None, str]:
    qualified = [row for row in rows if _qualified(row) and _label(row.get("action") or {})]
    total_runs = {row["run_id"] for row in qualified}
    if len(total_runs) < config.min_train_runs + config.min_holdout_runs:
        return None, "more independent successful runs needed"
    train, holdout = _grouped_split(qualified, config.min_holdout_runs)
    if (len(train) < config.min_train_examples or len(holdout) < config.min_holdout_examples
            or len({r["run_id"] for r in train}) < config.min_train_runs):
        return None, "insufficient disjoint training and held-out examples"
    examples = [{"features": _features(row["before"], visual=_spatial(_label(row["action"]))),
                 "action": _label(row["action"]), "expected_effect": row["expected_effect"],
                 "run_id": row["run_id"], "decision_id": row["decision_id"]} for row in train]
    examples = [row for row in examples if not _spatial(row["action"]) or row["features"]["visual"]]
    if len(examples) > config.max_train_examples:
        # Deterministic, run-interleaved reservoir bounds retraining and inference work.
        # Every retained row remains inspectable; omitted rows stay in the durable corpus.
        per_run: dict[str, list] = defaultdict(list)
        for row in sorted(examples, key=lambda r: _digest(r["decision_id"])):
            per_run[row["run_id"]].append(row)
        examples = []
        while len(examples) < config.max_train_examples and any(per_run.values()):
            for run in sorted(per_run):
                if per_run[run] and len(examples) < config.max_train_examples:
                    examples.append(per_run[run].pop())
    if len(examples) < config.min_train_examples:
        return None, "spatial controls need recorded visual features"
    values: dict[str, list[float]] = defaultdict(list)
    for example in examples:
        for block in ("numeric", "visual"):
            for key, value in example["features"][block].items():
                values[f"{block}.{key}"].append(value)
    # Feature ranges learned only from fitting runs. A constant feature stays strict:
    # changed values remain detectable rather than divided by an invented giant scale.
    scales = {key: max(max(items) - min(items), 1.0e-6) for key, items in values.items()}
    parameter_values: dict[str, list[float]] = defaultdict(list)
    for example in examples:
        prefix = _digest(_template(example["action"]))
        for key, value in _parameters(example["action"]).items():
            parameter_values[f"{prefix}.{key}"].append(value)
    parameter_scales = {key: max(max(items) - min(items), 1.0e-6)
                        for key, items in parameter_values.items()}
    radii, parameter_radii = _cross_run_radii(examples, scales, parameter_scales, cancelled)
    if not radii:
        return None, "no cross-run context support"
    radius = min(config.max_distance, sorted(radii)[int((len(radii) - 1) * 0.90)])
    train_runs = sorted({row["run_id"] for row in train})
    failures = [{"features": _features(row["before"], visual=_spatial(_label(row["action"]))),
                 "action": _label(row["action"])}
                for row in rows if row["run_id"] in train_runs and _failed(row)
                and _label(row.get("action") or {})]
    model = {"format": FORMAT, "examples": examples, "scales": scales, "radius": radius,
             "parameter_scales": parameter_scales, "parameter_radii": parameter_radii,
             "failures": failures, "train_runs": train_runs,
             "holdout_runs": sorted({row["run_id"] for row in holdout}),
             "fitting_decisions": sorted(row["decision_id"] for row in train),
             "holdout_decisions": sorted(row["decision_id"] for row in holdout),
             "encounters": sorted({str(row["encounter_id"]) for row in [*train, *holdout]
                                   if row.get("encounter_id") is not None}),
             "config": asdict(config)}
    correct = exact = structural = covered = 0
    for row in holdout:
        if cancelled():
            return None, "training cancelled"
        action, _, _, _, expected = _proposal(model, row["before"], config)
        if action is not None:
            covered += 1
            predicted, actual = _label(action), _label(row["action"])
            same_effect = expected == row["expected_effect"]
            exact += predicted == actual and same_effect
            structural += _template(predicted) == _template(actual) and same_effect
            correct += same_effect and _equivalent(model, predicted, actual, expected)
    precision = correct / covered if covered else 0.0
    coverage = covered / len(holdout)
    model["evaluation"] = {"examples": len(holdout), "covered": covered, "correct": correct,
                           "precision": precision, "coverage": coverage,
                           "structural_agreement": structural / covered if covered else 0.0,
                           "parameter_coverage": correct / structural if structural else 0.0,
                           "exact_agreement": exact / covered if covered else 0.0,
                           "eligible": precision >= config.min_agreement and coverage >= config.min_coverage}
    return model, "held-out gate passed" if model["evaluation"]["eligible"] else "held-out gate failed"


def _cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    calls = input_tokens = output_tokens = 0.0
    elapsed = 0.0
    complete = bool(rows)
    for row in rows:
        cost = row.get("cost") or {}
        seconds = row.get("elapsed_s")
        if (type(cost.get("teacher_calls")) is not int or cost["teacher_calls"] < 0
                or not _number(seconds) or seconds <= 0):
            complete = False
        else:
            calls += cost["teacher_calls"]
            elapsed += seconds
        for key in ("input_tokens", "output_tokens"):
            if type(cost.get(key)) is not int or cost[key] < 0:
                complete = False
        input_tokens += cost["input_tokens"] if _number(cost.get("input_tokens")) else 0
        output_tokens += cost["output_tokens"] if _number(cost.get("output_tokens")) else 0
    return {"complete": complete, "decisions": len(rows), "teacher_calls": calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "observed_elapsed_s": elapsed,
            "teacher_calls_per_hour": calls * 3600 / elapsed if elapsed else None,
            "teacher_calls_per_decision": calls / len(rows) if rows else None}


def _episode_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return row["run_id"], row["episode_id"]


def _episode_cost(keys: set[tuple], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count all teacher corrections and task reward once per complete episode."""
    selected = [row for row in rows if _episode_key(row) in keys]
    summary = _cost(selected)
    if any(row.get("synthetic") is not False or (row.get("before") or {}).get("synthetic") is True
           for row in selected):
        summary["complete"] = False
    episodes = {_episode_key(row): row.get("episode_outcome") for row in selected}
    elapsed = progress = 0.0
    successes = 0
    for outcome in episodes.values():
        seconds = (outcome or {}).get("elapsed_s")
        if not outcome or not _number(seconds) or seconds <= 0:
            summary["complete"] = False
            continue
        elapsed += seconds
        if _episode_qualified(outcome):
            successes += 1
            progress += outcome["progress"]
    summary.update(episodes=len(episodes), successful_episodes=successes,
                   observed_elapsed_s=elapsed, verified_progress=progress,
                   success_rate=successes / len(episodes) if episodes else None,
                   teacher_calls_per_hour=summary["teacher_calls"] * 3600 / elapsed if elapsed else None,
                   teacher_calls_per_episode=summary["teacher_calls"] / len(episodes) if episodes else None,
                   teacher_calls_per_progress=summary["teacher_calls"] / progress if progress else None,
                   progress_per_hour=progress * 3600 / elapsed if elapsed else None)
    return summary


def _episode_gates(baseline: dict, candidate: dict) -> dict[str, bool]:
    measured = baseline["complete"] and candidate["complete"]
    return {
        "measured": measured,
        "cost": (measured and baseline["teacher_calls_per_progress"] is not None
                 and candidate["teacher_calls_per_progress"] is not None
                 and candidate["teacher_calls_per_progress"] < baseline["teacher_calls_per_progress"]
                 and candidate["teacher_calls_per_hour"] < baseline["teacher_calls_per_hour"]),
        "throughput": (measured and baseline["progress_per_hour"] is not None
                       and candidate["progress_per_hour"] is not None
                       and candidate["progress_per_hour"] >= baseline["progress_per_hour"]),
        "success": (measured and (candidate["success_rate"] or 0) >= (baseline["success_rate"] or 0)),
    }


class MotorLearner:
    """Thread/process-safe durable motor corpus, student registry and handover gates."""

    def __init__(self, directory: str | Path, *, config: LearningConfig | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.config = config or LearningConfig()
        self.clock = clock
        self.registry_path = self.directory / "registry.json"
        self._models: dict[str, dict[str, Any]] = {}

    def _registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"format": FORMAT, "generation": 0, "capabilities": {}, "models": {}}
        value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if value.get("format") != FORMAT:
            raise ValueError("unsupported motor registry format")
        return value

    def status(self) -> dict[str, Any]:
        return self._registry()

    def _save(self, registry: dict[str, Any]) -> None:
        registry["generation"] += 1
        atomic_json(self.registry_path, registry)

    def records(self) -> list[dict[str, Any]]:
        episodes = {}
        for path in (self.directory / "episodes").glob("*.json"):
            episode = json.loads(path.read_text(encoding="utf-8"))
            episodes[(episode["run_id"], episode["episode_id"])] = episode["outcome"]
        result = []
        for path in sorted((self.directory / "records").glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            # Only the independently finalized ledger supplies episode credit. An action
            # author cannot inject a successful terminal outcome into its own record.
            row["episode_outcome"] = episodes.get((row["run_id"], row["episode_id"]))
            result.append(row)
        return result

    def finish_episode(self, episode_id: str, *, run_id: str, outcome: Mapping[str, Any]) -> bool:
        """Finalize independently observed task progress, never mere input delivery.

        `progress` may be confirmed arrival/service completion or measured combat/quest
        progress. Incomplete/cancelled episodes should report success=False. Finalization
        is immutable and idempotent; useful partial traces remain available for review.
        """
        if not run_id or not episode_id:
            raise ValueError("episode identity required")
        row = {"run_id": run_id, "episode_id": episode_id, "outcome": dict(outcome)}
        path = self.directory / "episodes" / f"{_digest([run_id, episode_id])}.json"
        with file_lock(self.directory / ".lock"):
            if path.exists():
                if json.loads(path.read_text(encoding="utf-8")) != row:
                    raise ValueError("conflicting terminal outcome for episode")
                return False
            atomic_json(path, row)
            if not _episode_qualified(outcome):
                registry = self._registry()
                changed = False
                for record in self.records():
                    if (record["run_id"] != run_id or record["episode_id"] != episode_id
                            or record["author"] != "student" or record["synthetic"] is not False):
                        continue
                    state = registry["capabilities"].get(record["capability"])
                    if state and state.get("model") == record.get("model"):
                        if outcome.get("verified") is True or outcome.get("fatal") is True:
                            self._rollback(state, "student episode ended without verified useful progress", record)
                        else:
                            self._quarantine(state, "episode interrupted or terminal outcome unverified")
                        changed = True
                if changed:
                    self._save(registry)
        return True

    def ingest_run(self, directory: str | Path) -> dict[str, Any]:
        """Recover durable run evidence after trainer failure or interrupted persistence.

        Requests/accepted inputs never masquerade as results. An incomplete final JSONL
        line is ignored; corruption in a complete line is reported with its line number.
        """
        directory = Path(directory)
        report: dict[str, Any] = {"records": 0, "episodes": 0, "errors": []}
        for name in ("actions", "episodes"):
            path = directory / f"play-{name}.jsonl"
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.endswith("\n"):
                        break
                    try:
                        row = json.loads(line)
                        if name == "actions":
                            if row.pop("event", None) != "result":
                                continue
                            report["records"] += self.record(row)
                        else:
                            run_id = row.pop("run_id")
                            row.pop("schema", None)
                            report["episodes"] += self.finish_episode(
                                row["episode_id"], run_id=run_id, outcome=row)
                    except (ValueError, TypeError, KeyError) as exc:
                        report["errors"].append(f"{path.name}:{number}: {exc}")
        return report

    def record(self, record: Mapping[str, Any]) -> bool:
        """Retain every attempt; returns False for an identical already-recorded decision."""
        row = json.loads(_json(dict(record)))
        required = ("run_id", "episode_id", "decision_id", "capability", "before", "after",
                    "action", "outcome", "author", "controls_fingerprint", "knowledge_fingerprint",
                    "synthetic", "expected_effect")
        missing = [key for key in required if key not in row]
        if missing:
            raise ValueError(f"motor record missing {', '.join(missing)}")
        if not all(isinstance(row[key], str) and row[key] for key in
                   ("run_id", "episode_id", "decision_id", "capability", "controls_fingerprint",
                    "knowledge_fingerprint")):
            raise ValueError("motor record IDs and fingerprints must be nonempty strings")
        if row["author"] not in {"teacher", "student", "human", "scripted"}:
            raise ValueError("unknown action author")
        if not isinstance(row["synthetic"], bool):
            raise ValueError("synthetic provenance must be explicit")
        from jev.play.observation import EFFECTS
        if row["expected_effect"] not in EFFECTS:
            raise ValueError("unknown expected motor effect")
        path = self.directory / "records" / f"{_digest([row['run_id'], row['decision_id']])}.json"
        with file_lock(self.directory / ".lock"):
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous != row:
                    raise ValueError("conflicting evidence for an existing motor decision")
                return False
            atomic_json(path, row)
            registry = self._registry()
            state = registry["capabilities"].get(row["capability"])
            if (state and row["author"] == "student" and state.get("model") == row.get("model")
                    and row["synthetic"] is False and not _effect_qualified(row)):
                if _failed(row) or row["outcome"].get("fatal") is True:
                    self._rollback(state, "student expected effect failed", row)
                else:
                    self._quarantine(state, "student effect interrupted or unverified")
                self._save(registry)
        return True

    def _rollback(self, state: dict[str, Any], reason: str, row: dict | None = None) -> None:
        state.update(mode="blocked", reason=reason, rolled_back_at=self.clock(),
                     failed_decision=(row or {}).get("decision_id"))
        state.setdefault("blocked", [])
        if state.get("model") not in state["blocked"]:
            state["blocked"].append(state.get("model"))
        if row:
            state["corpus_runs"] = sorted(set(state.get("corpus_runs", [])) | {row["run_id"]})

    def _quarantine(self, state: dict[str, Any], reason: str) -> None:
        if state.get("mode") == "blocked":
            return  # An interrupted episode cannot undo an earlier observed action failure.
        state.update(mode="shadow", reason=reason, evidence_after=self.clock())

    def rollback(self, capability: str, reason: str) -> None:
        with file_lock(self.directory / ".lock"):
            registry = self._registry()
            state = registry["capabilities"].get(capability)
            if state:
                self._rollback(state, reason)
                self._save(registry)

    def _load(self, model_id: str, registry: dict[str, Any]) -> dict[str, Any]:
        entry = registry["models"][model_id]
        path = self.directory / "models" / f"{model_id}.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("motor model checksum mismatch")
        if model_id not in self._models:
            self._models[model_id] = json.loads(raw)
        return self._models[model_id]

    def predict(self, observation: Mapping[str, Any], capability: str, *, decision_id: str,
                controls_fingerprint: str, knowledge_fingerprint: str,
                allow_student: bool = True) -> MotorPrediction:
        registry = self._registry()
        state = registry["capabilities"].get(capability)
        if not state:
            return MotorPrediction()
        model_id = state.get("model")
        if state.get("mode") == "blocked":
            return MotorPrediction(model=model_id, reason=state["reason"])
        if (state.get("controls_fingerprint") != controls_fingerprint
                or state.get("knowledge_fingerprint") != knowledge_fingerprint):
            return MotorPrediction(model=model_id, reason="controls or knowledge changed")
        if observation.get("synthetic") is True:
            return MotorPrediction(model=model_id, reason="synthetic observation cannot drive live policy")
        try:
            model = self._load(model_id, registry)
            action, confidence, support, reason, expected = _proposal(
                model, observation, self.config, failures=state.get("failures", []))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return MotorPrediction(model=model_id, reason=f"student unavailable: {exc}")
        if action is None:
            return MotorPrediction(model=model_id, confidence=confidence, support=support, reason=reason)
        mode = "shadow"
        sample = int(_digest([model_id, decision_id])[:16], 16) / (16 ** 16)
        if allow_student and model["evaluation"]["eligible"]:
            if (state.get("mode") == "canary" and self.clock() < state.get("canary_until", 0)
                    and sample < self.config.canary_fraction):
                mode = "canary"
            elif state.get("mode") == "active" and sample >= self.config.audit_fraction:
                mode = "active"
        return MotorPrediction(action, model_id, mode, confidence, reason, support, expected)

    def update(self, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Fit/publish or evaluate a frozen candidate; safe to call in a worker thread.

        Cancellation is checked between capabilities and before publication. A candidate
        is frozen while collecting live evidence, so a growing run cannot continuously
        reset its evaluation. A rolled-back candidate needs a new successful teacher run.
        """
        cancelled = cancelled or (lambda: False)
        with file_lock(self.directory / ".train.lock"):
            registry = self._registry()
            groups: dict[tuple, list] = defaultdict(list)
            rows = self.records()
            for row in rows:
                groups[(row["capability"], row["controls_fingerprint"],
                        row["knowledge_fingerprint"])].append(row)
            # The latest observation pins which control/knowledge generation is current.
            latest: dict[str, tuple] = {}
            for key, records in groups.items():
                stamp = max(_stamp(r) for r in records)
                if key[0] not in latest or stamp > latest[key[0]][0]:
                    latest[key[0]] = (stamp, key)
            for _, key in sorted(latest.values(), key=lambda item: str(item[1])):
                if cancelled():
                    break
                capability, controls, knowledge = key
                records = groups[key]
                state = registry["capabilities"].get(capability)
                compatible = (state and state.get("controls_fingerprint") == controls
                              and state.get("knowledge_fingerprint") == knowledge)
                if compatible and state.get("mode") != "blocked":
                    model = self._load(state["model"], registry)
                    self._evaluate(state, model, records, all_rows=rows)
                    new_runs = {row["run_id"] for row in records if _qualified(row)} - set(state["corpus_runs"])
                    if (state["mode"] in {"canary", "blocked"}
                            or len(new_runs) < self.config.retrain_new_runs):
                        continue
                if compatible and state.get("mode") == "blocked":
                    blocked_runs = set(state.get("corpus_runs", []))
                    corrections = [r for r in records if r["run_id"] not in blocked_runs
                                   and r["author"] in {"teacher", "human"} and _qualified(r)
                                   and _stamp(r) > state["rolled_back_at"]]
                    if not corrections:
                        continue
                model, reason = _fit(records, self.config, cancelled=cancelled)
                if model is None:
                    registry.setdefault("pending", {})[capability] = reason
                    continue
                model.update(capability=capability, controls_fingerprint=controls,
                             knowledge_fingerprint=knowledge)
                model_id = "motor-" + _digest(model)[:24]
                if state and model_id in state.get("blocked", []):
                    continue
                if cancelled():
                    break
                target = self.directory / "models" / f"{model_id}.json"
                if not target.exists():
                    atomic_json(target, model)
                registry["models"][model_id] = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                                "capability": capability, "created_at": self.clock()}
                registry["capabilities"][capability] = {
                    "model": model_id, "mode": "shadow", "reason": reason,
                    "controls_fingerprint": controls, "knowledge_fingerprint": knowledge,
                    "corpus_runs": sorted({row["run_id"] for row in records}),
                    "blocked": state.get("blocked", []) if state else [],
                    "evaluation": model["evaluation"], "failures": [],
                    "previous": state.get("model") if state else None,
                }
                registry.setdefault("pending", {}).pop(capability, None)
            if not cancelled():
                with file_lock(self.directory / ".lock"):
                    # A live failure/cancellation may revoke student authority while
                    # fitting runs. Never overwrite that newer registry with a snapshot.
                    current = self._registry()
                    if current["generation"] != registry["generation"]:
                        return current
                    self._save(registry)
            return registry

    def _evaluate(self, state: dict[str, Any], model: dict[str, Any], rows: list[dict[str, Any]], *,
                  all_rows: list[dict[str, Any]] | None = None) -> None:
        excluded = set(model["train_runs"]) | set(model["holdout_runs"])
        encounters = set(model.get("encounters", []))
        live = [row for row in rows if row["run_id"] not in excluded and row["synthetic"] is False
                and (row.get("encounter_id") is None or str(row["encounter_id"]) not in encounters)
                and (row.get("before") or {}).get("synthetic") is not True]
        # New failures also constrain shadows, even when the teacher made the attempt.
        state["failures"] = [{"features": _features(row["before"],
                                                    visual=_spatial(_label(row["action"]))),
                              "action": _label(row["action"])}
                             for row in live if _failed(row) and _label(row.get("action") or {})]
        live = [row for row in live if _stamp(row) > state.get("evidence_after", -math.inf)]
        shadow_attempts = [row for row in live if row["author"] in {"teacher", "human"}
                           and (row.get("shadow") or {}).get("model") == state["model"]]
        shadow = [row for row in shadow_attempts if _label(row["shadow"].get("action") or {})]
        agreeing = [row for row in shadow if _qualified(row)
                    and _label((row.get("shadow") or {}).get("action") or {}) is not None
                    and _label(row.get("action") or {}) is not None
                    and _equivalent(model, _label(row["shadow"]["action"]), _label(row["action"]),
                                    row["expected_effect"])
                    and row["shadow"].get("expected_effect") == row["expected_effect"]]
        agreement = len(agreeing) / len(shadow) if shadow else 0.0
        student = [row for row in live if row["author"] == "student"
                   and row.get("model") == state["model"]]
        successes = [row for row in student if _qualified(row)]
        # Compare teacher costs in the same learned state/visual support, not unrelated work.
        baseline = [row for row in live if row["author"] == "teacher"
                    and _proposal(model, row["before"], self.config)[0] is not None]
        all_rows = all_rows if all_rows is not None else rows
        student_episodes = {_episode_key(row) for row in student}
        any_student_episodes = {_episode_key(row) for row in all_rows if row["author"] == "student"}
        baseline = [row for row in baseline if _episode_key(row) not in any_student_episodes
                    and row.get("episode_outcome") is not None]
        baseline_cost = _episode_cost({_episode_key(row) for row in baseline}, all_rows)
        student_cost = _episode_cost(student_episodes, all_rows)
        state["metrics"] = {
            "shadow_examples": len(shadow), "shadow_agreement": agreement,
            "shadow_attempts": len(shadow_attempts),
            "shadow_coverage": len(shadow) / len(shadow_attempts) if shadow_attempts else 0.0,
            "shadow_runs": len({r["run_id"] for r in shadow}),
            "student_examples": len(student), "student_successes": len(successes),
            "student_runs": len({r["run_id"] for r in student}),
            "teacher_baseline": baseline_cost, "student_cost": student_cost,
        }
        if any(_failed(row) or ((row.get("episode_outcome") or {}).get("verified") is True
               and not _qualified(row)) for row in student):
            self._rollback(state, "student outcome or completed episode failed during evaluation")
            return
        if any(not _effect_qualified(row) for row in student):
            self._quarantine(state, "student effect interrupted or unverified during evaluation")
            return
        if not model["evaluation"]["eligible"]:
            state["reason"] = "held-out gate failed; teacher continues"
            return
        if state["mode"] == "shadow":
            if (len(shadow) >= self.config.min_shadow_examples
                    and len({r["run_id"] for r in shadow}) >= self.config.min_shadow_runs
                    and agreement >= self.config.min_agreement):
                state.update(mode="canary", reason="independent live shadow gate passed",
                             canary_until=self.clock() + self.config.canary_duration_s)
            return
        if state["mode"] == "canary":
            if self.clock() >= state.get("canary_until", 0):
                self._rollback(state, "bounded canary expired without enough measured evidence")
                return
            checks = _episode_gates(baseline_cost, student_cost)
            state["metrics"]["promotion_checks"] = checks
            if (len(successes) == len(student) and len(student) >= self.config.min_canary_examples
                    and len({r["run_id"] for r in student}) >= self.config.min_canary_runs
                    and len(baseline) >= self.config.min_teacher_baseline
                    and all(checks.values())):
                state.update(mode="active", reason="live outcomes maintained with fewer teacher calls",
                             promoted_at=self.clock(),
                             corpus_runs=sorted({row["run_id"] for row in rows}))
        elif state["mode"] == "active":
            # Earlier canary successes cannot mask a later performance regression.
            # Fatal/effect failures already revoke authority immediately above; this
            # window catches successful but slower or teacher-dependent execution.
            observed = [row for row in student if _stamp(row) > state["promoted_at"]
                        and row.get("episode_outcome") is not None]
            if (len(observed) >= self.config.min_canary_examples
                    and len({row["run_id"] for row in observed}) >= self.config.min_canary_runs
                    and len(baseline) >= self.config.min_teacher_baseline):
                review = _episode_cost({_episode_key(row) for row in observed}, all_rows)
                checks = _episode_gates(baseline_cost, review)
                state["metrics"]["active_review"] = {"episode_cost": review, "checks": checks}
                if checks["measured"] and not all(checks.values()):
                    self._rollback(state, "observed active task throughput or teacher cost regressed")
