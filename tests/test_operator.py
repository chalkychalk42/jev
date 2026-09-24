"""A person at the desk pauses the bot (`jev.clients.operator`)."""

import threading
import time

from test_runtime_records import runtime, seen
from test_supervisor import Body

from jev.clients.operator import MARGIN_MS, WRAP, Operator
from jev.run.supervisor import Supervisor


class Desk:
    """Windows' two clocks, driven by the test: now, and the last input from anyone."""

    def __init__(self, now=100_000):
        self.now, self.last = now, now

    def input(self, dt=0):
        self.now += dt
        self.last = self.now


def watch(desk, tmp_path=None, **kw):
    path = None if tmp_path is None else tmp_path / "bot-input.tick"
    return Operator(last_input_tick=lambda: desk.last, tick_now=lambda: desk.now,
                    stamp_path=path, quiet_s=kw.get("quiet_s", 600))


def test_the_bots_own_input_is_not_a_person():
    desk = Desk()
    op = watch(desk)
    for _ in range(5):
        desk.input(40)
        op.stamp()
    assert op.age_s() == float("inf") and not op.active()


def test_input_newer_than_the_bots_is_a_person_until_the_desk_is_quiet():
    desk = Desk()
    op = watch(desk, quiet_s=30)
    op.stamp()
    desk.input(MARGIN_MS + 200)                  # the mouse moved, and not by the bot
    assert op.active()
    desk.now += 29_000
    assert op.active(), "still within the quiet window"
    desk.now += 2_000
    assert not op.active(), "quiet for longer than the window"


def test_input_before_the_process_started_is_not_taken_for_a_person():
    desk = Desk()
    desk.input(0)
    op = watch(desk)
    desk.now += 5_000
    assert not op.active()


def test_a_new_session_reads_the_last_ones_stamp(tmp_path):
    """Sessions are separate processes: the operator between them is still seen."""
    desk = Desk()
    first = watch(desk, tmp_path)
    first.stamp()                                # the previous session's last key
    desk.input(10_000)                           # a person, between the sessions
    desk.now += 1_000
    second = watch(desk, tmp_path)
    assert second.active()


def test_the_tick_wrapping_round_does_not_fool_it():
    desk = Desk(now=WRAP - 100)
    op = watch(desk)
    op.stamp()
    desk.now = 900                               # the 32-bit tick wrapped
    desk.last = 900
    assert op.active(), "input 1,000 ms after the bot's, across the wrap"


def test_the_supervisor_pauses_for_a_person_and_resumes_after(tmp_path):
    rt = runtime(tmp_path, [seen(t / 4) for t in range(20)])
    body = Body()
    person = threading.Event()
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append, operator_active=person.is_set)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        running = supervisor.worker
        person.set()
        supervisor.step(0.25)
        assert running.cancelled.is_set() and running.reason == "operator active"
        assert running.done.wait(1)
        for t in range(2, 6):
            supervisor.step(t / 4)
        assert body.calls == 1, "nothing started while the person was there"
        person.clear()
        for t in range(6, 10):
            supervisor.step(t / 4)
        # The resumed skill runs on the worker thread; give it a moment to reach the body.
        deadline = time.monotonic() + 1
        while body.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert body.calls == 2, "resumed once the desk was quiet"
        assert "operator active: paused until the desk is quiet" in lines
        assert "operator quiet: resuming" in lines
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_focus_is_never_taken_while_a_person_is_at_the_desk(monkeypatch):
    from types import SimpleNamespace

    from jev.run import client as client_module

    taken = []
    monkeypatch.setattr(client_module.operator, "active", lambda: True)
    monkeypatch.setattr(client_module.win32, "focus", lambda hwnd: taken.append(hwnd) or True)
    monkeypatch.setattr(client_module.win32, "is_foreground", lambda hwnd: False)
    fake = SimpleNamespace(hwnd=42)
    assert client_module.Client.focused(fake, 5.0) is False
    assert taken == [], "the window was raised over the person's"


def test_the_last_sessions_final_input_behind_its_stamp_is_still_the_bots(tmp_path):
    """The file is written at most every half second; its last input may be newer."""
    desk = Desk()
    first = watch(desk, tmp_path)
    first.stamp()                                # written
    desk.input(400)
    first.stamp()                                # too soon to write: the file lags by 400 ms
    second = watch(desk, tmp_path)
    assert not second.active(), "the previous session's own last key"
    desk.input(5_000)
    assert second.active(), "anything later is a person's"


def test_a_stamp_from_before_a_restart_is_ignored(tmp_path):
    desk = Desk(now=50_000)
    (tmp_path / "bot-input.tick").write_text("900000000")   # ahead of a clock that restarted
    op = watch(desk, tmp_path)
    desk.input(2_000)
    assert op.active(), "a person after the restart is still seen"


def test_flush_writes_the_stamp_the_throttle_held_back(tmp_path):
    desk = Desk()
    op = watch(desk, tmp_path)
    op.stamp()
    desk.input(100)
    op.stamp()
    op.flush()
    assert int((tmp_path / "bot-input.tick").read_text()) == desk.now


def test_a_paused_session_is_not_a_stalled_one():
    from types import SimpleNamespace

    from jev.run.watchdog import Watchdog

    state = SimpleNamespace(
        sense=SimpleNamespace(addon_ok=True), quests=None, guide=None,
        char=SimpleNamespace(level=5, xp_pct=0.5),
        vitals=SimpleNamespace(dead=False, ghost=False))
    dog = Watchdog(no_progress_s=300)
    for t in range(0, 1300, 10):
        dog.observe(state, t, paused=True)
    assert not dog.escalate and dog.failure is None
    for t in range(1300, 1300 + 610, 10):
        dog.observe(state, t)
    assert dog.failure is not None, "the window still runs once the person has gone"
