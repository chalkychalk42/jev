"""Atomic, bracket-scoped model lifecycle and the live, abstaining policy adapter.

The registry is local trusted data, not a model download mechanism. Training publishes
immutable model directories first and then switches one JSON pointer. A crash before the
pointer switch leaves an unused artifact; a crash after it leaves a complete model.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jev.coach.policy import _guide, _recover, preempt, service
from jev.coach.schema import Decision, Intent
from jev.eval.counters import bracket_of
from jev.guide.graph import Node
from jev.learn.distill import ABSTAIN, Policy, Prediction
from jev.persist import atomic_json, file_lock
from jev.world.state_v1 import State, StepKind


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _measured(evidence: Mapping[str, Any], *, promotion: bool) -> bool:
    checks = evidence.get("checks", {})
    needed = {"candidate_windows", "candidate_runs", "candidate_span", "baseline_windows",
              "baseline_runs", "baseline_span", "observed_progress", "good_outcomes",
              "graph_revision"}
    needed |= {"measured_improvement"} if promotion else {"coverage", "supported_actions"}
    return (evidence.get("eligible") is True and needed.issubset(checks)
            and all(value is True for value in checks.values())
            and bool(evidence.get("routes")))


class ModelRegistry:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "registry.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"format": 1, "generation": 0, "models": {}, "bands": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("format") != 1:
            raise ValueError("unsupported model registry format")
        return data

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        with file_lock(self.directory / ".registry.lock"):
            data = self.read()
            yield data
            data["generation"] += 1
            atomic_json(self.path, data)

    def next_version(self) -> int:
        versions = [m["version"] for m in self.read()["models"].values()]
        for path in (self.directory / "candidates").glob("v*"):
            if path.name[1:].isdigit():
                versions.append(int(path.name[1:]))
        return max(versions, default=0) + 1

    def publish(self, policy: Policy, metadata: Mapping[str, Any]) -> str:
        """Publish a complete immutable artifact; initially every bracket is shadow-only."""
        root = self.directory / "candidates"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"v{policy.version}"
        staging = Path(tempfile.mkdtemp(prefix=".training-", dir=root))
        try:
            path = policy.save(staging / "policy.joblib")
            with path.open("rb") as saved:
                os.fsync(saved.fileno())
            record = dict(metadata, version=policy.version, model=policy.model_name,
                          path=str((target / "policy.joblib").relative_to(self.directory)),
                          sha256=digest(path), created_at=time.time())
            atomic_json(staging / "metadata.json", record)
            staging.rename(target)
        except BaseException:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
            raise
        with self.edit() as data:
            if policy.model_name in data["models"]:
                raise ValueError("model version already published")
            data["models"][policy.model_name] = record
            for bracket in record.get("brackets", []):
                band = data["bands"].setdefault(bracket, {})
                band["shadow"] = policy.model_name
                band.setdefault("active", None)
                band.setdefault("previous", None)
                band.setdefault("status", "shadow")
        return policy.model_name

    def load(self, model: str, snapshot: Mapping[str, Any] | None = None) -> Policy:
        record = (snapshot or self.read())["models"][model]
        path = (self.directory / record["path"]).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("model path escapes local registry")
        if digest(path) != record["sha256"]:
            raise ValueError(f"model checksum changed: {model}")
        policy = Policy.load(path)
        if policy.model_name != model:
            raise ValueError("model identity disagrees with registry")
        return policy

    def canary(self, bracket: str, model: str, *, evidence: dict[str, Any],
               duration_s: float = 7200.0, fraction: float = 0.1,
               now: float | None = None) -> bool:
        if not _measured(evidence, promotion=False):
            return False
        if duration_s <= 0 or not 0 < fraction <= 1:
            raise ValueError("canary duration/fraction must be positive and bounded")
        with self.edit() as data:
            band = data["bands"].get(bracket, {})
            if (band.get("shadow") != model or band.get("status") == "canary"
                    or data["models"][model].get("synthetic")):
                return False
            # A rolled-back candidate cannot start itself again on the same corpus.
            if model in band.get("blocked", []):
                return False
            prior = band.get("active")
            band.update(active=model, previous=prior, status="canary", evidence=evidence,
                        previous_graphs=band.get("graphs", []),
                        graphs=sorted(evidence["routes"]),
                        canary_until=(time.time() if now is None else now) + duration_s,
                        fraction=fraction, reason="bounded canary; held-out outcome gate passed")
            return True

    def promote(self, bracket: str, model: str, *, evidence: dict[str, Any]) -> bool:
        if not _measured(evidence, promotion=True):
            return False
        with self.edit() as data:
            band = data["bands"].get(bracket, {})
            if band.get("active") != model or band.get("status") != "canary":
                return False
            band.update(status="active", evidence=evidence, reason="measured outcome gate passed")
            return True

    def rollback(self, bracket: str, model: str, reason: str) -> bool:
        with self.edit() as data:
            band = data["bands"].get(bracket, {})
            if band.get("active") != model:
                if band.get("previous") == model:
                    band["previous"] = None
                    band["previous_graphs"] = []
                    band["blocked"] = list(dict.fromkeys([*band.get("blocked", []), model]))
                    band["reason"] = f"fallback disabled {model}: {reason}"
                    return True
                return False  # a stale worker may not demote its replacement
            previous = band.get("previous")
            blocked = list(dict.fromkeys([*band.get("blocked", []), model]))
            if previous in blocked:
                previous = None
            band.update(active=previous, previous=None, status="active" if previous else "shadow",
                        graphs=band.get("previous_graphs", []),
                        blocked=blocked, reason=f"rollback {model}: {reason}")
            return True


def grounded_decision(prediction: Prediction, state: State, node: Node | None,
                      available_skills: frozenset[str], *, confidence: float = 0.7) -> Decision | None:
    """Resolve model labels only through existing, currently applicable skill plans.

    The classifier never learns target IDs, coordinates, quest names or raw keys. Those
    come from the current graph and observed service rules. Incompatible independently
    trained intent/skill heads abstain; neither head gets to invent a parameter.
    """
    if (prediction.intent is None or prediction.confidence < confidence
            or prediction.skill_confidence is None or prediction.skill_confidence < confidence
            or prediction.skill not in available_skills):
        return None
    if preempt(state) is not None or state.vitals.combat is not False:
        return None
    if ((not state.sense.addon_ok and (state.sense.vision_conf or 0) < 0.5)
            or state.vitals.dead is not False or state.vitals.ghost is not False):
        return None
    # Existing safety/recovery/service must be settled by the scripted floor first.
    if service(state) is not None or _recover(state) is not None:
        return None
    if node is not None and node.kind is StepKind.GRIND:
        # The tracker owns the entry-level-derived completion bound. It is not a label
        # or a current-state fact, so leave this arm to the runtime's grounded floor.
        return None
    plan = _guide(state, node)
    if plan is None:
        return None
    d = plan.decision
    if prediction.intent != d.intent.value or prediction.skill != d.skill:
        return None
    if d.intent in (Intent.SKIP, Intent.ESCALATE, Intent.WAIT):
        return None
    return d.model_copy(update={"confidence": min(prediction.confidence,
                                                  prediction.skill_confidence),
                                "why": "learned choice; parameters from current guide facts"})


class PolicyManager:
    """In-memory hot-path policy. Caller invokes refresh outside input/decision ticks."""
    def __init__(self, directory: str | Path, *, confidence: float = 0.7,
                 graph_digests: Mapping[str, str] | None = None,
                 clock=time.time) -> None:
        self.registry = ModelRegistry(directory)
        self.confidence, self.clock = confidence, clock
        self.graph_digests = dict(graph_digests or {})
        self.snapshot: dict[str, Any] = {"models": {}, "bands": {}}
        self.policies: dict[str, Policy] = {}
        self._verified: dict[str, tuple[tuple, Policy]] = {}
        self.shadow_model: str | None = None
        self.model_name: str | None = None
        self.last_error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        try:
            snapshot = self.registry.read()
            needed = {band.get(key) for band in snapshot["bands"].values()
                      for key in ("shadow", "active", "previous")} - {None}
            loaded = {}
            errors = {}
            for name in needed:
                try:
                    record = snapshot["models"][name]
                    path = (self.registry.directory / record["path"]).resolve()
                    if not path.is_relative_to(self.registry.directory.resolve()):
                        raise ValueError("model path escapes local registry")
                    stat = path.stat()
                    identity = (record["sha256"], str(path), stat.st_dev, stat.st_ino,
                                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                    cached = self._verified.get(name)
                    if cached and cached[0] == identity:
                        loaded[name] = cached[1]
                    else:
                        loaded[name] = self.registry.load(name, snapshot)
                        self._verified[name] = identity, loaded[name]
                except Exception as exc:
                    errors[name] = f"{type(exc).__name__}: {exc}"
            for bracket, band in snapshot["bands"].items():
                if band.get("previous") in errors:
                    self.registry.rollback(bracket, band["previous"], errors[band["previous"]])
                if band.get("active") in errors:
                    self.registry.rollback(bracket, band["active"], errors[band["active"]])
            if errors:
                snapshot = self.registry.read()
            self.snapshot, self.policies = snapshot, loaded
            self.last_error = "; ".join(f"{name}: {why}" for name, why in errors.items()) or None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.policies = {}  # unavailable/corrupt registry always restores scripted floor

    def _model(self, state: State, key: str) -> tuple[str | None, Policy | None]:
        band = self.snapshot["bands"].get(bracket_of(state.char.level), {})
        name = band.get(key)
        meta = self.snapshot["models"].get(name, {})
        if state.guide.graph_id not in meta.get("graphs", []):
            return None, None
        if key == "active" and state.guide.graph_id not in band.get("graphs", []):
            return None, None
        if key == "active":
            expected = meta.get("graph_digests", {}).get(state.guide.graph_id)
            if expected is None or expected != self.graph_digests.get(state.guide.graph_id):
                return None, None
        if key == "active" and band.get("status") == "canary":
            if self.clock() >= band.get("canary_until", 0):
                return None, None
            identity = f"{name}:{state.client_id}:{state.guide.step_id}:{state.situation_key}"
            fraction = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) / 2**32
            if fraction >= band.get("fraction", 0):
                prior = band.get("previous")
                if state.guide.graph_id not in self.snapshot["models"].get(prior, {}).get("graphs", []):
                    return None, None
                expected = self.snapshot["models"][prior].get("graph_digests", {}).get(state.guide.graph_id)
                if expected is None or expected != self.graph_digests.get(state.guide.graph_id):
                    return None, None
                return prior, self.policies.get(prior)
        return name, self.policies.get(name)

    def shadow(self, state: State) -> Prediction:
        self.shadow_model, policy = self._model(state, "shadow")
        return policy.shadow(state) if policy else ABSTAIN

    def decide(self, state: State, node: Node | None,
               available_skills: frozenset[str]) -> Decision | None:
        self.model_name, policy = self._model(state, "active")
        if policy is None:
            return None
        try:
            prediction = policy.predict_full(state)
            return grounded_decision(prediction, state, node, available_skills,
                                     confidence=self.confidence)
        except Exception as exc:
            self.report_failure(state, f"prediction: {type(exc).__name__}: {exc}")
            return None

    def report_failure(self, state: State, reason: str) -> None:
        bracket = bracket_of(state.char.level)
        if bracket and self.model_name:
            try:
                self.registry.rollback(bracket, self.model_name, reason)
                self.refresh()
            except Exception as exc:
                # Persistence failures still remove this process's permission to drive.
                self.policies = {}
                self.last_error = f"rollback unavailable: {type(exc).__name__}: {exc}"
