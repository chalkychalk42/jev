"""Offline integration: real queues/workers/records, no client or subscription calls."""

import asyncio
import time

import pytest
from test_runtime_records import answer, runtime, seen
from test_supervisor import Body
from test_teacher import FakeClient, reply_json

from jev.learn.episode import SkillOutcome, read
from jev.run.supervisor import Result, Supervisor
from jev.run.watchdog import Watchdog
from jev.teacher.bridge import BudgetClient, TeacherBridge
from jev.world.state_v1 import Bags, Char, GuidePos, Sense, Vitals


def eventually(predicate, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def test_budget_survives_restart_and_accounts_actual_calls(tmp_path):
    client = FakeClient([reply_json()])
    first = BudgetClient(client, tmp_path / "budget.db", calls_per_hour=2, interval_s=10)
    assert first.reserve(100)
    second = BudgetClient(client, first.path, calls_per_hour=2, interval_s=10)
    assert not second.reserve(101)
    assert second.reserve(111)
    assert not first.reserve(200)
    assert first.reserve(3800)


def test_bridge_keeps_ticking_while_teacher_waits_and_persists_artifacts(tmp_path):
    client = FakeClient([reply_json()], delay_s=0.1)
    rt = runtime(tmp_path / "runs", [seen(i / 4) for i in range(5)])
    bridge = TeacherBridge(client, rt.graph, rt.recorder, frozenset({"GRIND_UNTIL"}),
                           budget_path=tmp_path / "budget.db", interval_s=0)
    try:
        rt.tick()
        state = rt.last_state
        assert bridge.ask(state, state.situation_key)
        assert not bridge.ask(state, state.situation_key)
        for _ in range(4):
            rt.tick(choose=False)
        assert rt.counters.ticks == 5
        eventually(lambda: not bridge._pending)
        # No background thread ever writes the Recorder directly.
        assert not any(r["author"] == "teacher" for r in read(rt.recorder.dir / "decisions.jsonl"))
        bridge.poll()
        rows = read(rt.recorder.dir / "decisions.jsonl")
        assert rows[-1]["artifacts"][0]["payload"]["to"] == "s1_skip"
        assert bridge.take(state.situation_key).artifacts
        assert bridge.take(state.situation_key) is None
        assert client.calls == 1
    finally:
        bridge.close()
        rt.recorder.close()
    assert not bridge._thread.is_alive()


def test_bridge_shutdown_cancels_slow_teacher_and_records_transport_failure(tmp_path):
    client = FakeClient([reply_json()], delay_s=100)
    rt = runtime(tmp_path / "runs", [seen()])
    bridge = TeacherBridge(client, rt.graph, rt.recorder, frozenset({"GRIND_UNTIL"}),
                           budget_path=tmp_path / "budget.db")
    rt.tick()
    bridge.ask(rt.last_state, rt.last_state.situation_key)
    eventually(lambda: client.calls == 1)
    bridge.close()
    assert not bridge._thread.is_alive()
    assert read(rt.recorder.dir / "decisions.jsonl")[-1]["status"] == "transport"
    rt.recorder.close()


def test_failed_budget_store_does_not_call_the_subscription(tmp_path):
    client = FakeClient([reply_json()])
    # A directory cannot be used as the SQLite file.
    budget = BudgetClient(client, tmp_path)
    response = asyncio.run(budget.ask("fixture"))
    assert response.status == "abstained" and client.calls == 0


@pytest.mark.parametrize("override", [
    {"bags": Bags(free=0)}, {"vitals": Vitals(dead=True)},
    {"vitals": Vitals(hp=0.1, combat=True)}, {"sense": Sense(addon_ok=False)},
])
def test_a_teacher_answer_cannot_override_the_protected_floor(tmp_path, override):
    rt = runtime(tmp_path, [seen(**override)], take=lambda key: answer("ACCEPT_QUEST"))
    rt.tick()
    assert rt.counters.teacher_applied == 0


def test_body_contract_refusal_keeps_teacher_artifacts_and_scripted_plan(tmp_path):
    rt = runtime(tmp_path, [seen()], take=lambda key: answer("GRIND_UNTIL"),
                 validate_action=lambda d, step: "wrong guide kind" if d.skill == "GRIND_UNTIL" else None)
    rt.tick()
    assert rt.armed.decision.skill == "ACCEPT_QUEST"
    assert read(rt.recorder.dir / "decisions.jsonl")[0]["verifier_verdict"] == "body_contract"


def test_watchdog_uses_progress_not_motion_or_unread_fields():
    watch = Watchdog(no_progress_s=10)
    s = seen(char=Char(level=5, xp_pct=0.2), guide=GuidePos(step_id="q"))
    watch.observe(s, 0)
    watch.observe(s.model_copy(update={"quests": None, "char": Char()}), 5)
    watch.observe(s, 10)
    assert watch.escalate and watch.failure is None, "the first window fails the step over"
    watch.observe(s.model_copy(update={"quests": None, "char": Char()}), 15)
    watch.observe(s, 20)
    assert "no quest or experience" in watch.failure
    watch = Watchdog(no_progress_s=10)
    watch.observe(s, 0)
    watch.observe(s.model_copy(update={"char": Char(level=5, xp_pct=0.3)}), 9)
    watch.observe(s.model_copy(update={"char": Char(level=5, xp_pct=0.3)}), 15)
    assert watch.failure is None


def test_reconnect_has_one_input_owner_and_keeps_playhead(tmp_path):
    blind = seen(sense=Sense(addon_ok=False))
    rt = runtime(tmp_path, [seen(), blind, blind, blind, seen(5)])
    body = Body()
    calls = []
    def reconnect(checkpoint):
        checkpoint()
        assert body.active == 0
        calls.append("session")
        return Result(SkillOutcome.SUCCEEDED, "restored", "reconnected")
    watch = Watchdog(blind_grace_s=1, reconnect=reconnect)
    supervisor = Supervisor(rt, body, watchdog=watch, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        first = supervisor.worker
        supervisor.step(1)
        assert first.done.wait(1)
        supervisor.step(2)
        assert supervisor.worker.done.wait(1)
        supervisor.step(3)
        supervisor.step(4)
        assert calls == ["session"]
        assert rt.tracker.step_id == "accept"
        rows = read(rt.recorder.dir / "skills.jsonl")
        assert len(rows) == 1 and rows[0]["outcome"] == "preempted"
        assert body.maximum == 1
    finally:
        supervisor.close()


def test_reconnect_budget_cannot_spin_forever():
    watch = Watchdog(blind_grace_s=1, reconnect_limit=1, reconnect_backoff_s=3,
                     reconnect=lambda cp: None)
    s = seen(sense=Sense(addon_ok=False))
    watch.observe(s, 0)
    assert watch.maintenance(0) is None
    assert watch.maintenance(1) is not None
    assert watch.maintenance(2) is None
    watch.completed(1)
    watch.observe(s, 4)
    assert "budget exhausted" in watch.failure
    assert watch.maintenance(5) is None


def test_optional_housekeeping_failure_does_not_stop_scripted_body(tmp_path):
    rt = runtime(tmp_path, [seen()])
    def fail(state):
        raise OSError("optional store unavailable")
    supervisor = Supervisor(rt, Body(), housekeeping=fail, say=lambda _: None)
    try:
        supervisor.step(0)
        assert supervisor.worker.body.started.wait(1)
        assert not supervisor.stopped.is_set()
    finally:
        supervisor.close()
