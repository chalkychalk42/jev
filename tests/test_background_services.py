"""Optional setup, disk and model failures cannot hold the physical supervisor clock."""

import threading
import time
from unittest.mock import Mock

from test_background_runtime import eventually
from test_runtime_records import runtime, seen
from test_supervisor import Body

from jev.eval.counters import bracket_of
from jev.learn.registry import ModelRegistry, PolicyManager
from jev.learn.worker import CycleReport
from jev.persist import atomic_json
from jev.run.background import Background
from jev.run.supervisor import Supervisor
from jev.world.state_v1 import Char


def test_each_optional_constructor_failure_keeps_the_scripted_floor(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen()])
    monkeypatch.setattr("jev.learn.registry.PolicyManager", Mock(side_effect=ImportError("optional model dependency")))
    monkeypatch.setattr("jev.learn.worker.LearningWorker", Mock(side_effect=RuntimeError("worker setup")))
    monkeypatch.setattr("jev.teacher.bridge.TeacherBridge", Mock(side_effect=RuntimeError("teacher setup")))
    background = Background(rt, runs=tmp_path / "runs", store=tmp_path / "store",
                            learn=True, adaptive=True, teacher_client=object())
    try:
        eventually(lambda: {"policy_setup", "learner_setup", "teacher_setup"} <= background.errors.keys())
        rt.tick()
        assert rt.armed.decision.skill == "ACCEPT_QUEST"
        assert background.shadow(rt.last_state) == (None, None, 0.0)
        assert background.decide(rt.last_state, rt.graph.nodes[0], frozenset()) is None
    finally:
        background.close()
        rt.recorder.close()
    assert not background.loader.is_alive() and not background.thread.is_alive()


def test_teacher_is_closed_when_later_learner_setup_fails(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen()])
    bridge = Mock()
    monkeypatch.setattr("jev.teacher.bridge.TeacherBridge", Mock(return_value=bridge))
    monkeypatch.setattr("jev.learn.worker.LearningWorker", Mock(side_effect=RuntimeError("worker setup")))
    background = Background(rt, runs=tmp_path / "runs", store=tmp_path / "store",
                            learn=True, teacher_client=object())
    try:
        eventually(lambda: "learner_setup" in background.errors)
        assert rt.ask is bridge.ask and rt.take is bridge.take
    finally:
        background.close()
        rt.recorder.close()
    bridge.close.assert_called_once()


def test_blocked_model_refresh_and_status_disk_do_not_block_supervisor_ticks(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen(i / 4) for i in range(6)])
    entered, release = threading.Event(), threading.Event()
    owner = threading.get_ident()
    refresh_threads, status_threads = [], []
    refresh = PolicyManager.refresh
    calls = 0
    def blocked_refresh(manager):
        nonlocal calls
        calls += 1
        refresh_threads.append(threading.get_ident())
        if calls > 1:
            entered.set()
            assert release.wait(3)
        refresh(manager)
    monkeypatch.setattr(PolicyManager, "refresh", blocked_refresh)
    def failed_status(path, value):
        status_threads.append(threading.get_ident())
        raise OSError("optional status disk unavailable")
    monkeypatch.setattr("jev.run.background.atomic_json", failed_status)
    background = Background(rt, runs=tmp_path / "runs", store=tmp_path / "store", adaptive=True)
    supervisor = Supervisor(rt, Body(), housekeeping=background.poll, say=lambda _: None)
    try:
        eventually(lambda: background.manager is not None)
        background.wake.set()
        assert entered.wait(1)
        started = time.monotonic()
        for i in range(5):
            supervisor.step(i / 4)
        assert time.monotonic() - started < 0.5
        assert rt.counters.ticks == 5 and not supervisor.stopped.is_set()
        release.set()
        eventually(lambda: "status" in background.errors)
        assert refresh_threads and all(t != owner for t in refresh_threads)
        assert status_threads and all(t != owner for t in status_threads)
    finally:
        release.set()
        supervisor.close()
        background.close()
        rt.recorder.close()


def test_slow_model_constructor_is_optional_and_current_state_remains_usable(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen()])
    entered, release = threading.Event(), threading.Event()
    constructor = PolicyManager
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return constructor(*args, **kwargs)
    monkeypatch.setattr("jev.learn.registry.PolicyManager", slow)
    started = time.monotonic()
    background = Background(rt, runs=tmp_path / "runs", store=tmp_path / "store", adaptive=True)
    try:
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        rt.tick()
        assert rt.armed.decision.skill == "ACCEPT_QUEST"
        assert rt.counters.ticks == 1
    finally:
        release.set()
        background.close()
        rt.recorder.close()


def test_prediction_failure_quarantines_immediately_and_rolls_back_offthread(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen(char=Char(level=5))])
    entered, release = threading.Event(), threading.Event()
    rollback_threads = []
    def rollback(registry, bracket, model, reason):
        rollback_threads.append(threading.get_ident())
        assert model == "policy:v2" and reason == "verifier refused"
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr("jev.learn.registry.ModelRegistry.rollback", rollback)
    background = Background(rt, runs=tmp_path / "runs", store=tmp_path / "store", adaptive=True)
    try:
        eventually(lambda: background.manager is not None)
        rt.tick()
        started = time.monotonic()
        background.policy_failed(rt.last_state, "verifier refused", model="policy:v2")
        assert time.monotonic() - started < 0.5
        assert background.manager is None and "policy:v2" in background._blocked
        assert entered.wait(1)
        assert background.decide(rt.last_state, rt.graph.nodes[0], frozenset()) is None
        assert rollback_threads == [background.loader.ident]
    finally:
        release.set()
        background.close()
        rt.recorder.close()


def test_swallowed_policy_errors_and_report_errors_remain_named(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen()])
    class BrokenWorker:
        def __init__(self, *args, **kwargs):
            pass
        def run(self, stop, *, report_to):
            report_to(CycleReport())
    monkeypatch.setattr("jev.learn.worker.LearningWorker", BrokenWorker)
    monkeypatch.setattr("jev.run.background.atomic_json", Mock(side_effect=OSError("disk full")))
    store = tmp_path / "store"
    store.mkdir()
    (store / "registry.json").write_text("{malformed")
    background = Background(rt, runs=tmp_path / "runs", store=store, learn=True)
    try:
        eventually(lambda: "policy" in background.errors and "learning_report" in background.errors)
        assert "JSONDecodeError" in background.errors["policy"]
        rt.tick()
        assert rt.armed.decision.skill == "ACCEPT_QUEST"
    finally:
        background.close()
        rt.recorder.close()


def test_immediate_close_drains_queued_model_rollback_and_final_status(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "runs", [seen(char=Char(level=5))])
    entered, release = threading.Event(), threading.Event()
    calls = 0
    def refresh(manager):
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            assert release.wait(3)
        # Skip only model loading; actual durable rollback uses the real registry.
    monkeypatch.setattr(PolicyManager, "refresh", refresh)
    store = tmp_path / "store"
    bracket = bracket_of(5)
    atomic_json(store / "registry.json", {"format": 1, "generation": 0, "models": {},
                "bands": {bracket: {"active": "policy:v2", "previous": None, "status": "active"}}})
    background = Background(rt, runs=tmp_path / "runs", store=store, adaptive=True)
    try:
        eventually(lambda: background.manager is not None)
        background.wake.set()
        assert entered.wait(1)
        rt.tick()
        background.poll(rt.last_state)
        background.policy_failed(rt.last_state, "verifier refused", model="policy:v2")
        closer = threading.Thread(target=background.close)
        closer.start()
        assert background.stop.wait(1)
        release.set()
        closer.join(timeout=2)
        assert not closer.is_alive() and not background.loader.is_alive()
        band = ModelRegistry(store).read()["bands"][bracket]
        assert band["active"] is None and "policy:v2" in band["blocked"]
        assert (store / "live-c.json").is_file()
    finally:
        release.set()
        background.close()
        rt.recorder.close()
