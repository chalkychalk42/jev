"""Real supervisor and worker threads, fake bodies only: never open a game window."""
import threading

import pytest
from test_runtime_records import runtime, seen

from jev.clients import win32
from jev.clients.hid import Hid
from jev.learn.episode import SkillOutcome, read
from jev.run.cli import main
from jev.run.supervisor import Cancelled, Result, Supervisor, interruption
from jev.world.state_v1 import Ui, Vitals


class Body:
    available = frozenset({"ACCEPT_QUEST", "TURNIN_QUEST", "TRAVEL_TO", "ABORT_WAIT", "IDLE"})
    travelling = False

    def __init__(self, *, result=None, error=False):
        self.started = threading.Event()
        self.allow_finish = threading.Event()
        self.result = result
        self.error = error
        self.calls = self.active = self.maximum = self.releases = 0

    def execute(self, arm, state, checkpoint):
        self.calls += 1
        self.active += 1
        self.maximum = max(self.active, self.maximum)
        self.started.set()
        try:
            if self.error:
                raise ValueError("body failed")
            while not self.allow_finish.wait(0.001):
                checkpoint()
            return self.result or Result(SkillOutcome.SUCCEEDED, "confirmed", "done")
        finally:
            self.active -= 1

    def release(self):
        self.releases += 1


def test_blocking_body_does_not_block_tracker_recording_or_overlap_input(tmp_path):
    rt = runtime(tmp_path, [seen(t / 4) for t in range(10)])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        for t in range(1, 8):
            supervisor.step(t / 4)
        assert body.calls == body.maximum == 1
        assert rt.counters.ticks == 8
        ticks = read(rt.recorder.dir / "ticks.jsonl")
        assert len(ticks) == 4
        assert len({t["decision_id"] for t in ticks}) == 1
        assert ticks[-1]["state"]["guide"]["age_s"] == 1.5
    finally:
        supervisor.close()
    assert body.active == 0 and body.releases >= 1


def test_modal_cancels_before_a_replacement_can_start(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(0.25, ui=Ui(modal=True)), seen(0.5, ui=Ui(modal=True))])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert worker.done.wait(1)
        supervisor.step(0.5)
        assert body.calls == 1 and body.maximum == 1
        assert rt.armed.decision.skill == "ABORT_WAIT"
        rows = read(rt.recorder.dir / "skills.jsonl")
        assert rows[0]["outcome"] == "preempted"
        assert "modal" in rows[0]["detail"]
    finally:
        supervisor.close()


def test_combat_interrupts_travel_inside_a_composite(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(0.25, vitals=Vitals(hp=0.8, combat=True))])
    body = Body()
    body.travelling = True
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert worker.done.wait(1)
        assert "combat" in worker.result.detail
    finally:
        supervisor.close()


def test_body_exception_releases_input_and_is_reported(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(0.5)])
    body = Body(error=True)
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert supervisor.worker.done.wait(1)
        supervisor.step(0.5)
        assert supervisor.stopped.is_set()
        assert "body failed" in supervisor.failure
        assert body.releases >= 1
        assert read(rt.recorder.dir / "skills.jsonl")[0]["outcome"] == "aborted"
    finally:
        supervisor.close()


def test_offline_check_never_attaches_a_client(monkeypatch, capsys):
    import jev.run.cli
    def forbidden(*args, **kwargs):
        pytest.fail("offline check attempted to attach the game")
    monkeypatch.setattr(jev.run.cli, "attach", forbidden)
    assert main(["--check"]) == 0
    assert '"executors"' in capsys.readouterr().out


def test_a_catalog_timeout_is_a_counted_failure_not_a_retrying_preemption(tmp_path):
    rt = runtime(tmp_path, [seen(0), seen(61), seen(62)])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        supervisor.step(61)
        assert supervisor.worker.done.wait(1)
        supervisor.step(62)
        assert supervisor.stopped.is_set()
        assert body.calls == 1
        result = read(rt.recorder.dir / "skills.jsonl")[0]
        assert result["outcome"] == "timed_out"
    finally:
        supervisor.close()


def test_an_open_loot_window_does_not_deadlock_combat_at_the_supervisor(tmp_path):
    rt = runtime(tmp_path, [seen(vitals=Vitals(hp=1, combat=True), ui=Ui(loot=True))])
    state = rt.tick()
    assert rt.armed.decision.skill == "LOOT"
    assert interruption(rt.armed, state) is None


def fake_hid(monkeypatch):
    sent = []
    monkeypatch.setattr(win32, "available", lambda: True)
    monkeypatch.setattr(win32, "scan_code", lambda key: key)
    monkeypatch.setattr(win32, "send_inputs", lambda inputs: sent.extend(inputs) or len(inputs))
    hid = Hid(require_focus=False)
    hid._sleep = lambda delay: None
    return hid, sent


def test_cancellation_stops_new_presses_but_releases_owned_keys(monkeypatch):
    hid, sent = fake_hid(monkeypatch)
    assert hid.key_down("w")
    def cancelled():
        raise Cancelled("stop")
    hid.checkpoint = cancelled
    with pytest.raises(Cancelled):
        hid.key_down("a")
    assert hid.key_up("w")
    assert not hid.keys_down()
    assert len(sent) == 2


def test_failed_key_release_stays_visible_in_held_state(monkeypatch):
    hid, _ = fake_hid(monkeypatch)
    hid.key_down("w")
    monkeypatch.setattr(win32, "send_inputs", lambda inputs: 0)
    assert not hid.key_up("w")
    assert hid.keys_down() == ["w"]


def test_cancellation_in_mouse_drag_releases_the_button(monkeypatch):
    hid, sent = fake_hid(monkeypatch)
    assert hid.button(True, right=True)
    def cancelled():
        raise Cancelled("stop")
    hid.checkpoint = cancelled
    with pytest.raises(Cancelled):
        hid.move_by(0, 50)
    hid.release_all()
    assert not hid.held_buttons
    flags = [i.mi.dwFlags for i in sent if i.type == win32.INPUT_MOUSE]
    assert flags == [win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP]


def test_focus_is_rechecked_inside_a_mouse_move(monkeypatch):
    hid, sent = fake_hid(monkeypatch)
    ready = iter([True, True, False])
    hid.ready = lambda: next(ready, False)
    assert not hid.move_by(0, 30, step_px=10)
    assert len(sent) == 1
