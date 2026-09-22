"""Continuous, restart-safe outcome learning without a client or a teacher dependency.

    python -m jev.learn.worker --runs runs --store var/learning --once

Only derived grades/model artifacts are written. One OS lock bounds concurrent training,
registry edits are atomic, and unchanged graded examples never produce another candidate.
Live tails remain pending; model activation requires observed outcomes and explicit mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from jev.learn.dataset import DEFAULT_AUTHORS, Dataset, Dropped, build_from_rows
from jev.learn.distill import evaluate, train
from jev.learn.episode import read
from jev.learn.evidence import (
    EvidenceGates,
    compare,
    independent,
    metrics,
    observations,
    shadow_evidence,
    split_runs,
)
from jev.learn.grade import grade_run
from jev.learn.registry import ModelRegistry, atomic_json, file_lock


@dataclass(frozen=True)
class WorkerConfig:
    interval_s: float = 60.0
    retrain_s: float = 900.0
    min_training_rows: int = 200
    min_training_runs: int = 3
    min_new_rows: int = 50
    heldout_fraction: float = 0.25
    max_runs: int = 500
    max_run_bytes: int = 256 * 1024 * 1024
    window_s: float = 60.0
    max_gap_s: float = 2.0
    canary_s: float = 86400.0
    canary_fraction: float = 0.1
    allow_canary: bool = False
    include_synthetic: bool = False
    gates: EvidenceGates = field(default_factory=EvidenceGates)

    def __post_init__(self) -> None:
        positive = (self.interval_s, self.min_training_rows, self.min_training_runs,
                    self.min_new_rows, self.max_runs, self.max_run_bytes,
                    self.window_s, self.max_gap_s, self.canary_s)
        if min(positive) <= 0 or self.retrain_s < 0:
            raise ValueError("worker bounds must be positive (retrain interval may be zero)")
        if not 0 < self.heldout_fraction < 1 or not 0 < self.canary_fraction <= 1:
            raise ValueError("fractions must lie in their probability bounds")
        if self.include_synthetic and self.allow_canary:
            raise ValueError("synthetic test corpus may not activate a live canary")


@dataclass
class CycleReport:
    runs: int = 0
    graded_runs: int = 0
    pending: int = 0
    good: int = 0
    examples: int = 0
    unobserved_outcomes: int = 0
    candidate: str | None = None
    transitions: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    reason: str = "no eligible corpus"


def fingerprint(directory: Path) -> str:
    files = []
    for name in ("ticks", "decisions", "skills"):
        path = directory / f"{name}.jsonl"
        stat = path.stat() if path.exists() else None
        files.append((name, stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0))
    route = directory / "route.json"
    if route.exists():
        stat = route.stat()
        files.append(("route", stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()


def dataset_identity(data: Dataset) -> str:
    # Feature or outcome corrections invalidate the fingerprint as well as new IDs.
    # NaN is deterministic for hashing; it is never written into manifests.
    rows = sorted((asdict(e) for e in data.examples),
                  key=lambda r: (r["run_id"], r["decision_id"]))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


class LearningWorker:
    def __init__(self, runs_root: str | Path, directory: str | Path, *,
                 config: WorkerConfig | None = None, clock=time.time) -> None:
        self.runs_root, self.registry = Path(runs_root), ModelRegistry(directory)
        self.config, self.clock = config or WorkerConfig(), clock
        self.state_path = self.registry.directory / "worker.json"

    def cycle(self) -> CycleReport:
        report = CycleReport()
        try:
            with file_lock(self.registry.directory / ".worker.lock", blocking=False):
                self._cycle(report)
        except BlockingIOError:
            report.reason = "another learning worker owns this store"
        except Exception as exc:
            report.errors["worker"] = f"{type(exc).__name__}: {exc}"
            report.reason = "cycle failed; see per-run errors and the durable model registry"
        return report

    def _cycle(self, report: CycleReport) -> None:
        cfg = self.config
        try:
            state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if not isinstance(state, dict) or not isinstance(state.get("sources", {}), dict):
                raise ValueError("invalid worker cache shape")
        except (ValueError, OSError) as exc:
            # This file caches work; the immutable model registry is authoritative.
            # Losing a cache must not require somebody to nurse the learner back up.
            state = {}
            report.errors["worker_cache"] = f"rebuilt unreadable cache: {exc}"
        known = state.setdefault("sources", {})
        if not self.runs_root.exists():
            report.reason = "run store does not exist yet"
            return
        candidates = [p for p in self.runs_root.iterdir()
                      if p.is_dir() and (p / "ticks.jsonl").exists()]
        candidates.sort(key=lambda p: (p / "ticks.jsonl").stat().st_mtime_ns, reverse=True)
        if len(candidates) > cfg.max_runs:
            report.errors["run_limit"] = f"newest {cfg.max_runs} of {len(candidates)} runs selected"
        examples, observed, dropped = [], [], []
        revisions = {}
        current_digests = {}
        seen_runs = set()
        for directory in candidates[:cfg.max_runs]:
            try:
                if sum(p.stat().st_size for p in directory.glob("*.jsonl")) > cfg.max_run_bytes:
                    raise ValueError("run exceeds configured byte limit; split/raise bound explicitly")
                source = fingerprint(directory)
                key = str(directory.resolve())
                missing_grades = ((directory / "decisions.jsonl").exists()
                                  and not (directory / "grades.jsonl").exists())
                if known.get(key) != source or missing_grades:
                    result = grade_run(directory, window_s=cfg.window_s, max_gap_s=cfg.max_gap_s)
                    report.graded_runs += 1
                    report.pending += result.pending
                    report.good += result.good
                    if fingerprint(directory) == source:
                        known[key] = source
                t = read(directory / "ticks.jsonl")
                d = read(directory / "decisions.jsonl") if (directory / "decisions.jsonl").exists() else []
                g = read(directory / "grades.jsonl") if (directory / "grades.jsonl").exists() else []
                run_ids = {row["run_id"] for row in t}
                if seen_runs & run_ids:
                    raise ValueError("duplicate run identity in another corpus directory")
                seen_runs |= run_ids
                chunk = build_from_rows(t, d, g, include_synthetic=cfg.include_synthetic)
                route_path = directory / "route.json"
                route = json.loads(route_path.read_text()) if route_path.exists() else {}
                digests = {route["graph"]: route["graph_digest"]} if (
                    isinstance(route.get("graph"), str)
                    and isinstance(route.get("graph_digest"), str)
                    and len(route["graph_digest"]) == 64
                    and all(c in "0123456789abcdef" for c in route["graph_digest"])) else {}
                for run_id in run_ids:
                    revisions[run_id] = digests
                # Runs are newest first. Older revisions remain useful training
                # history; only held-out outcomes on the latest observed revision can
                # grant permission to drive that revision.
                for graph, revision in digests.items():
                    current_digests.setdefault(graph, revision)
                outcomes = observations(t, d, g, include_synthetic=cfg.include_synthetic,
                                        graph_digests=digests)
                eligible = {(r.run_id, r.decision_id) for r in outcomes
                            if r.good and r.exclusive_model}
                kept = [e for e in chunk.examples if (e.run_id, e.decision_id) in eligible]
                report.unobserved_outcomes += len(chunk.examples) - len(kept)
                examples.extend(kept)
                dropped.append(chunk.dropped)
                observed.extend(outcomes)
                report.runs += 1
            except Exception as exc:
                report.errors[directory.name] = f"{type(exc).__name__}: {exc}"

        dataset = Dataset(tuple(examples), DEFAULT_AUTHORS, Dropped(**{
            f.name: sum(getattr(d, f.name) for d in dropped) for f in fields(Dropped)
        }))
        report.examples = len(dataset)
        training, testing = split_runs(dataset, fraction=cfg.heldout_fraction)
        train_runs = {e.run_id for e in training.examples}
        test_runs = {e.run_id for e in testing.examples}
        identity = hashlib.sha256((dataset_identity(dataset)
                                   + json.dumps(revisions, sort_keys=True)).encode()).hexdigest()
        snapshot = self.registry.read()
        existing = next((m for m in snapshot["models"].values()
                         if m.get("corpus_fingerprint") == identity), None)
        enough = (len(training) >= cfg.min_training_rows
                  and len(train_runs) >= cfg.min_training_runs and bool(test_runs))
        elapsed = self.clock() - state.get("last_training_at", -1e30)
        new_rows = len(dataset) - state.get("last_training_rows", 0)
        if existing:
            report.reason = "graded corpus unchanged; candidate already published"
        elif enough and elapsed >= cfg.retrain_s and (new_rows >= cfg.min_new_rows or new_rows <= 0):
            policy = train(training, version=self.registry.next_version())
            graph_ids = sorted({r.graph for r in observed if r.run_id in train_runs})
            graph_digests = {graph: current_digests[graph] for graph in graph_ids
                             if graph in current_digests}
            scoped = [r for r in observed if r.graph_digest is not None
                      and r.graph_digest == graph_digests.get(r.graph)]
            evaluations = {}
            for bracket in training.brackets():
                evidence = shadow_evidence(policy, scoped, bracket, test_runs, cfg.gates)
                evidence["checks"]["graph_revision"] = bool(graph_digests)
                evidence["eligible"] = all(evidence["checks"].values())
                evaluations[bracket] = evidence
            meta = {
                "corpus_fingerprint": identity,
                "training_runs": sorted(train_runs), "heldout_runs": sorted(test_runs),
                "training_rows": len(training), "heldout_rows": len(testing),
                "graphs": graph_ids, "graph_digests": graph_digests,
                "brackets": training.brackets(), "synthetic": cfg.include_synthetic,
                "evaluation": asdict(evaluate(policy, testing, held_out=True)),
                "shadow_evidence": evaluations,
                "gates": asdict(cfg.gates),
            }
            report.candidate = self.registry.publish(policy, meta)
            state.update(last_training_at=self.clock(), last_training_rows=len(dataset))
            report.reason = "candidate trained on outcome labels and published for shadow evaluation"
        elif not enough:
            report.reason = (f"waiting for evidence: {len(training)} training rows from "
                             f"{len(train_runs)} runs; {len(test_runs)} whole held-out runs")
        else:
            report.reason = "training interval/new-row gate pending"
        self._lifecycle(observed, report)
        state["last_cycle_at"] = self.clock()
        state["last_report"] = asdict(report)
        atomic_json(self.state_path, state)

    def _lifecycle(self, observed, report: CycleReport) -> None:
        cfg = self.config
        snapshot = self.registry.read()
        for bracket, band in snapshot["bands"].items():
            active = band.get("active")
            if active:
                metadata = snapshot["models"][active]
                seen = set(metadata["training_runs"]) | set(metadata["heldout_runs"])
                scoped = [r for r in observed if r.graph_digest is not None
                          and r.graph_digest == metadata.get("graph_digests", {}).get(r.graph)]
                candidate = [r for r in scoped if r.bracket == bracket and r.model == active
                             and r.run_id not in seen]
                prior = band.get("previous")
                baseline = [r for r in scoped if r.bracket == bracket and r.model != active
                            and r.exclusive_model
                            and (r.model == prior if prior else r.model.startswith("scripted:"))]
                # Old good nights cannot dilute a current regression forever. Keep
                # independent windows, then compare recent measured work.
                limit = max(120, cfg.gates.min_windows)
                candidate = sorted(independent(candidate), key=lambda r: r.t)[-limit:]
                baseline = sorted(independent(baseline), key=lambda r: r.t)[-limit:]
                evidence = compare(candidate, baseline, cfg.gates, improvement=True)
                evidence["checks"]["graph_revision"] = bool(metadata.get("graph_digests"))
                evidence["eligible"] = all(evidence["checks"].values())
                cm, bm = metrics(candidate), metrics(baseline)
                # Turning a candidate off needs less evidence than entrusting it with
                # a bracket. A regression on one long-running client must not wait for
                # three restarts merely to satisfy the promotion reproduction gate.
                enough = (cm.windows >= min(5, cfg.gates.min_windows)
                          and cm.observed_s >= min(300, cfg.gates.min_observed_s)
                          and bm.windows >= cfg.gates.min_windows
                          and bm.runs >= cfg.gates.min_runs
                          and bm.observed_s >= cfg.gates.min_observed_s)
                regression = enough and any(not ok for name, ok in evidence["checks"].items()
                                            if name.endswith((":deaths", ":stalls", ":steps", ":xp")))
                expired = band.get("status") == "canary" and self.clock() >= band["canary_until"]
                if regression or (expired and not evidence["eligible"]):
                    reason = "measured regression" if regression else "canary expired without promotion evidence"
                    if self.registry.rollback(bracket, active, reason):
                        report.transitions.append(f"{bracket}: rollback {active}: {reason}")
                elif band.get("status") == "canary" and evidence["eligible"]:
                    if self.registry.promote(bracket, active, evidence=evidence):
                        report.transitions.append(f"{bracket}: promoted {active}")
            shadow = band.get("shadow")
            if cfg.allow_canary and shadow and shadow != active and band.get("status") != "canary":
                meta = snapshot["models"][shadow]
                evidence = meta.get("shadow_evidence", {}).get(bracket, {})
                if not meta.get("synthetic") and self.registry.canary(
                        bracket, shadow, evidence=evidence, duration_s=cfg.canary_s,
                        fraction=cfg.canary_fraction, now=self.clock()):
                    report.transitions.append(f"{bracket}: bounded canary {shadow}")

    def run(self, stop: threading.Event, *, report_to=None) -> None:
        """Periodic work; Event.wait permits prompt stop between cycles."""
        while not stop.is_set():
            report = self.cycle()
            if report_to:
                try:
                    report_to(report)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("learning report callback failed")
            stop.wait(self.config.interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument("--store", type=Path, default=Path("var/learning"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--allow-canary", action="store_true",
                        help="enable evidence-gated sampled trials; runtime must also be adaptive")
    args = parser.parse_args()
    worker = LearningWorker(args.runs, args.store,
                            config=WorkerConfig(interval_s=args.interval,
                                                allow_canary=args.allow_canary))
    if args.once:
        result = worker.cycle()
        print(json.dumps(asdict(result)))
        return int(bool(result.errors))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker.run(stop, report_to=lambda r: print(json.dumps(asdict(r)), flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
