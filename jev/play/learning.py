"""The motor corpus, and the shadow proposal of the students training left behind.

Every tutor action is kept with its observed outcome and its episode's, whoever chose it,
so the corpus can still be used offline. A student was an inspectable nearest-neighbour
classifier over complete bounded actions (duration, direction, slot), learned from
observed successful effects, not from teacher authorship; failed attempts veto repeating
the same action in a covered context, and screenshots add a separate distance term, so
similar radio state cannot make two different scenes equivalent.

No student is trained inside a session (V174), and none acts (V225): its training, its
live evaluation and the handover through shadow, canary and active modes went. A model
training already published is still asked for a proposal, which is recorded beside the
tutor's action as its shadow and never executed.

Models and records are JSON, atomically published, with no executable pickle payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping
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
    """What a proposal needs from its neighbours: support from independent runs, and
    agreement among them. The training and handover gates went with V225."""

    min_support: int = 3
    min_support_runs: int = 2
    min_agreement: float = 0.90

    def __post_init__(self) -> None:
        for name in ("min_support", "min_support_runs"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.min_agreement <= 1:
            raise ValueError("min_agreement must be in (0, 1]")


@dataclass(frozen=True)
class MotorPrediction:
    """A student's proposal (`shadow`), recorded beside the tutor's action; never executed."""

    action: dict[str, Any] | None = None
    model: str | None = None
    mode: str = "teacher"
    confidence: float = 0.0
    reason: str = "no trained student"
    support: int = 0
    expected_effect: str | None = None

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


class MotorLearner:
    """Thread/process-safe durable motor corpus, and the students training published."""

    def __init__(self, directory: str | Path, *, config: LearningConfig | None = None) -> None:
        self.directory = Path(directory)
        self.config = config or LearningConfig()
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

    def remember_controls(self, fingerprint: str, manifest: Mapping[str, Any]) -> None:
        """Keep a controls generation's manifest, so later ones can tell what still holds."""
        path = self.directory / "controls" / f"{fingerprint}.json"
        if not path.exists():
            atomic_json(path, dict(manifest))

    def _remember_run_controls(self, directory: Path) -> None:
        """A run's controls, from the manifest it played under, when it matches its print."""
        from jev.play.observation import fingerprint

        try:
            played = json.loads((directory / "play-config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        controls, printed = played.get("controls"), played.get("controls_fingerprint")
        if isinstance(controls, dict) and isinstance(printed, str) and fingerprint(controls) == printed:
            self.remember_controls(printed, controls)

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
        # No student acts (V225), so an episode answers for no student and reads no
        # records: reading every one under the lock made other writers wait past Windows'
        # ten-second lock (PermissionError [Errno 13]).
        with file_lock(self.directory / ".lock"):
            if path.exists():
                if json.loads(path.read_text(encoding="utf-8")) != row:
                    raise ValueError("conflicting terminal outcome for episode")
                return False
            atomic_json(path, row)
        return True

    def ingest_run(self, directory: str | Path) -> dict[str, Any]:
        """Recover durable run evidence after trainer failure or interrupted persistence.

        Requests/accepted inputs never masquerade as results. An incomplete final JSONL
        line is ignored; corruption in a complete line is reported with its line number.
        """
        directory = Path(directory)
        report: dict[str, Any] = {"records": 0, "episodes": 0, "errors": []}
        self._remember_run_controls(directory)
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
        return True

    def _load(self, model_id: str, registry: dict[str, Any]) -> dict[str, Any]:
        entry = registry["models"][model_id]
        path = self.directory / "models" / f"{model_id}.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("motor model checksum mismatch")
        if model_id not in self._models:
            self._models[model_id] = json.loads(raw)
        return self._models[model_id]

    def predict(self, observation: Mapping[str, Any], capability: str, *,
                controls_fingerprint: str) -> MotorPrediction:
        """The shadow proposal of the student training published for `capability`, if any.

        The controller records it beside the tutor's action and never executes it (V225).
        """
        registry = self._registry()
        state = registry["capabilities"].get(capability)
        if not state:
            return MotorPrediction()
        model_id = state.get("model")
        if state.get("mode") == "blocked":
            return MotorPrediction(model=model_id, reason=state["reason"])
        # The key bindings an action is pressed with are what a motor model depends on. The
        # tutor's knowledge - guide, profiles, catalogue - shaped which actions it chose, but
        # every label is graded by its observed outcome (V9), and none of it is a model
        # input; tying models to it restarted the corpus at every guide change (V71).
        if state.get("controls_fingerprint") != controls_fingerprint:
            return MotorPrediction(model=model_id, reason="controls changed")
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
        return MotorPrediction(action, model_id, "shadow", confidence, reason, support, expected)
