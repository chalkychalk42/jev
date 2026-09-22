"""Optional services with off-thread loading/persistence and one recorder owner.

The supervisor only predicts from prepared in-memory policies, drains teacher rows and
hands over status snapshots. Slow disk/model setup cannot hold its preemption clock.
"""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

from jev.persist import atomic_json


class Background:
    def __init__(self, runtime, *, runs: Path, store: Path, learn: bool = False,
                 adaptive: bool = False, teacher_client=None, calls_per_hour: int = 12):
        self.runtime, self.store = runtime, Path(store)
        self.stop, self.wake = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self.thread = self.loader = self.manager = self.teacher = None
        self.next_report = 0.0
        self.errors: dict[str, str] = {}
        self._latest_report = None
        self._failures: queue.SimpleQueue = queue.SimpleQueue()
        self._blocked: set[str] = set()
        self._policy_model = self._shadow_model = None
        self._closed = False
        # Even registry existence/imports happen off-thread. These stable hooks abstain
        # until loading succeeds; unsupported optional dependencies cannot remove floor.
        self._want_policy = learn or adaptive
        self._digest = hashlib.sha256(json.dumps(runtime.graph.model_dump(mode="json"),
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        runtime.shadow, runtime.shadow_model = self.shadow, lambda: self._shadow_model
        if adaptive:
            runtime.learned, runtime.policy_model = self.decide, lambda: self._policy_model
            runtime.policy_failed = self.policy_failed
        if teacher_client is not None:
            try:
                from jev.teacher.bridge import TeacherBridge
                self.teacher = TeacherBridge(teacher_client, runtime.graph, runtime.recorder,
                                             runtime.available_skills,
                                             budget_path=self.store / "teacher-budget.sqlite",
                                             calls_per_hour=calls_per_hour)
                runtime.ask, runtime.take = self.teacher.ask, self.teacher.take
            except Exception as exc:
                self._error("teacher_setup", exc)
        try:
            self.loader = threading.Thread(target=self._load_loop, name="jev-policy-loader", daemon=True)
            self.loader.start()
        except Exception as exc:
            self.loader = None
            self._error("policy_setup", exc)
        if learn:
            try:
                self.thread = threading.Thread(target=self._learn_loop, args=(Path(runs), adaptive),
                                                name="jev-learner", daemon=True)
                self.thread.start()
            except Exception as exc:
                self.thread = None
                self._error("learner_setup", exc)

    def _error(self, key, error=None):
        with self._lock:
            if error is None:
                self.errors.pop(key, None)
            else:
                self.errors[key] = (f"{type(error).__name__}: {error}"
                                    if isinstance(error, BaseException) else str(error))

    def _manager(self):
        with self._lock:
            return self.manager

    def _refresh(self):
        if not self._want_policy and not (self.store / "registry.json").exists():
            return
        from jev.learn.registry import PolicyManager
        active = self._manager()
        if active is None:
            prepared = PolicyManager(self.store, graph_digests={self.runtime.graph.graph_id: self._digest})
        else:
            # Reuse verified models without mutating the foreground adapter. Its
            # snapshot and policies are replaced, not edited, by refresh().
            prepared = copy.copy(active)
            prepared._verified = dict(active._verified)
            prepared.refresh()
        # Prediction's own exception path must quarantine immediately but persist its
        # rollback on this loader, never inside PolicyManager.decide on the hot path.
        prepared.report_failure = lambda state, reason: self.policy_failed(
            state, reason, model=prepared.model_name)
        with self._lock:
            prepared.policies = {key: policy for key, policy in prepared.policies.items()
                                 if key not in self._blocked}
            self.manager = prepared
        self._error("policy", prepared.last_error)

    def _rollbacks(self):
        while True:
            try:
                state, model, reason = self._failures.get_nowait()
            except queue.Empty:
                return
            try:
                from jev.eval.counters import bracket_of
                from jev.learn.registry import ModelRegistry
                bracket = bracket_of(state.char.level)
                if bracket:
                    ModelRegistry(self.store).rollback(bracket, model, reason)
                self._error("rollback")
            except Exception as exc:
                # Local quarantine survives an unavailable registry. Never re-enable a
                # failed model merely because its rollback could not be persisted.
                self._error("rollback", exc)

    def _load_loop(self):
        try:
            while not self.stop.is_set():
                self._rollbacks()
                try:
                    self._refresh()
                    self._error("policy_setup")
                except Exception as exc:
                    with self._lock:
                        self.manager = None
                    self._error("policy_setup", exc)
                self._write_status()
                self.wake.wait(5)
                self.wake.clear()
        finally:
            # A stop may arrive immediately after foreground quarantine. Persist those
            # queued rollbacks before the loader exits so restart cannot resurrect them.
            self._rollbacks()
            self._write_status()

    def _write_status(self):
        with self._lock:
            report, self._latest_report = self._latest_report, None
            errors = dict(self.errors)
        if report is not None:
            try:
                atomic_json(self.store / f"live-{self.runtime.client_id}.json",
                            dict(report, errors=errors))
                self._error("status")
            except Exception as exc:
                self._error("status", exc)

    def _learn_loop(self, runs, adaptive):
        try:
            from jev.learn.worker import LearningWorker, WorkerConfig
            worker = LearningWorker(runs, self.store, config=WorkerConfig(allow_canary=adaptive))
            def report(value):
                try:
                    atomic_json(self.store / "latest-cycle.json", asdict(value))
                    self._error("learning_report")
                except Exception as exc:
                    self._error("learning_report", exc)
            worker.run(self.stop, report_to=report)
        except Exception as exc:
            self._error("learner_setup", exc)

    def shadow(self, state):
        manager = self._manager()
        if manager is None:
            self._shadow_model = None
            return None, None, 0.0
        try:
            prediction = manager.shadow(state)
            self._shadow_model = manager.shadow_model
            return prediction.intent, prediction.skill, prediction.confidence
        except Exception as exc:
            self._shadow_model = None
            self._error("shadow", exc)
            return None, None, 0.0

    def decide(self, state, node, available_skills):
        manager = self._manager()
        if manager is None:
            self._policy_model = None
            return None
        try:
            decision = manager.decide(state, node, available_skills)
            self._policy_model = manager.model_name
            with self._lock:
                blocked = self._policy_model in self._blocked
            return None if blocked else decision
        except Exception as exc:
            self.policy_failed(state, f"prediction: {type(exc).__name__}: {exc}",
                               model=manager.model_name)
            return None

    def policy_failed(self, state, reason, *, model=None):
        model = model or self._policy_model
        with self._lock:
            self.manager = None
            if model:
                self._blocked.add(model)
        if model:
            self._failures.put((state, model, reason))
            self.wake.set()
        self._error("prediction", reason)

    def poll(self, state):
        if self.teacher:
            try:
                self.teacher.poll()  # the only thread allowed to append recorder rows
            except Exception as exc:
                self._error("teacher_poll", exc)
        now = time.monotonic()
        if now < self.next_report:
            return
        self.next_report = now + 5
        report = {"t": state.t, "run": self.runtime.recorder.run_id,
                  "step": state.guide.step_id, "addon_ok": state.sense.addon_ok,
                  "armed": self.runtime.armed.decision.skill if self.runtime.armed else None,
                  "counters": asdict(self.runtime.counters)}
        with self._lock:
            self._latest_report = report

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop.set()
        self.wake.set()
        try:
            if self.teacher:
                try:
                    self.teacher.close()
                except Exception as exc:
                    self._error("teacher_close", exc)
        finally:
            for thread in (self.loader, self.thread):
                if thread:
                    thread.join(timeout=5)
                    if thread.is_alive():
                        self._error(thread.name, "shutdown deadline; daemon still finishing optional work")
