"""Process and reconnect lifecycle checks; every external actor is a local fixture."""

import asyncio
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_runtime_records import runtime, seen
from test_supervisor import Body

from jev.learn.episode import SkillOutcome
from jev.run.supervisor import Result, Supervisor
from jev.run.watchdog import Watchdog
from jev.teacher.bridge import BudgetClient
from jev.teacher.client import ClaudeSubscriptionClient, FakeClient
from jev.world.state_v1 import Char, GuidePos, Sense


def test_fail_rib_rejoin_cycles_cannot_reset_the_no_progress_deadline():
    """The first window fails the step over; step changes alone never count as progress,
    so a second window without any stops the run."""
    watch = Watchdog(no_progress_s=10)
    state = seen(char=Char(level=5, xp_pct=0.2), guide=GuidePos(step_id="objective"))
    watch.observe(state, 0)
    for now, step in ((4, "rib"), (8, "turnin"), (10, "objective")):
        watch.observe(state.model_copy(update={"guide": GuidePos(step_id=step)}), now)
    assert watch.escalate and watch.failure is None, "the first window fails the step over"
    watch.escalate = False
    for now, step in ((14, "rib"), (18, "turnin"), (20, "objective")):
        watch.observe(state.model_copy(update={"guide": GuidePos(step_id=step)}), now)
    assert "no quest or experience" in watch.failure
    assert not watch.escalate, "one fail-over per stall"


def test_progress_after_a_fail_over_earns_a_fresh_one():
    watch = Watchdog(no_progress_s=10)
    state = seen(char=Char(level=5, xp_pct=0.2), guide=GuidePos(step_id="objective"))
    watch.observe(state, 0)
    watch.observe(state, 10)
    assert watch.escalate
    watch.escalate = False
    grinding = state.model_copy(update={"char": Char(level=5, xp_pct=0.3)})
    watch.observe(grinding, 15)                               # the rib earned experience
    watch.observe(grinding, 25)
    assert watch.escalate and watch.failure is None


def test_last_reconnect_is_allowed_to_finish_before_budget_exhaustion(tmp_path):
    blind = seen(sense=Sense(addon_ok=False))
    rt = runtime(tmp_path, [blind, blind, blind, blind])
    body = Body()
    entered, release = threading.Event(), threading.Event()

    def reconnect(checkpoint):
        entered.set()
        while not release.wait(0.001):
            checkpoint()
        return Result(SkillOutcome.ABORTED, "still disconnected", "reconnect_failed")

    watch = Watchdog(blind_grace_s=0, reconnect_limit=1, reconnect_backoff_s=2,
                     reconnect=reconnect)
    supervisor = Supervisor(rt, body, watchdog=watch, say=lambda _: None)
    try:
        supervisor.step(0)
        assert entered.wait(1)
        active = supervisor.worker
        supervisor.step(5)
        assert watch.reconnecting and not active.cancelled.is_set()
        assert not supervisor.stopped.is_set()  # Backoff elapsed, but the last attempt owns input.
        release.set()
        assert active.done.wait(1)
        supervisor.step(6)
        assert not watch.reconnecting and watch.next_reconnect == 8
        supervisor.step(8)
        assert supervisor.stopped.is_set() and "budget exhausted" in supervisor.failure
        assert body.calls == 0 and body.releases >= 1
    finally:
        release.set()
        supervisor.close()
        rt.recorder.close()


def test_teacher_call_budget_serializes_competing_reservations(tmp_path):
    path = tmp_path / "budget.sqlite"

    def reserve(_):
        client = BudgetClient(FakeClient([]), path, calls_per_hour=2, interval_s=0)
        return client.reserve(100)

    with ThreadPoolExecutor(max_workers=8) as workers:
        permits = list(workers.map(reserve, range(8)))
    assert sum(permits) == 2
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from attempts").fetchone()[0] == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
def test_cancelled_teacher_process_is_killed_and_reaped(tmp_path):
    started, survived = tmp_path / "started", tmp_path / "survived"
    shim = tmp_path / "teacher-fixture"
    shim.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
        f"Path({str(started)!r}).touch()\ntime.sleep(0.3)\nPath({str(survived)!r}).touch()\n")
    shim.chmod(0o755)

    async def scenario():
        client = ClaudeSubscriptionClient(binary=str(shim), timeout_s=30)
        ask = asyncio.create_task(client.ask("offline fixture"))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.005)
        assert started.exists(), "local fixture never started"
        ask.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ask
        await asyncio.sleep(0.4)
        assert not survived.exists(), "the teacher process outlived bridge cancellation"

    asyncio.run(scenario())
