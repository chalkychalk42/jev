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
                    stamp_path=path, quiet_s=kw.get("quiet_s", 600), say=kw.get("say"))


def person(desk, op, dt):
    """A hand at the desk: an input `dt` ms on, seen, and two more 150 ms apart."""
    desk.input(dt)
    for _ in range(2):
        op.active()
        desk.input(150)
    return op.active()


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
    assert person(desk, op, MARGIN_MS + 200)     # the mouse moved, and not by the bot
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
    assert not second.active(), "one input alone is not yet a person"
    desk.input(300)
    assert not second.active()
    desk.input(300)
    assert second.active(), "a third is"


def test_the_tick_wrapping_round_does_not_fool_it():
    desk = Desk(now=WRAP - 100)
    op = watch(desk)
    op.stamp()
    desk.now = desk.last = 900                   # the 32-bit tick wrapped
    for _ in range(2):
        op.active()
        desk.input(150)
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
    monkeypatch.setattr(client_module.operator, "suspected", lambda: True)
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
    assert person(desk, second, 5_000), "anything later is a person's"


def test_a_stamp_from_before_a_restart_is_ignored(tmp_path):
    desk = Desk(now=50_000)
    (tmp_path / "bot-input.tick").write_text("900000000")   # ahead of a clock that restarted
    op = watch(desk, tmp_path)
    assert person(desk, op, 2_000), "a person after the restart is still seen"


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


def test_stray_inputs_are_not_a_person():
    """Session 91: one input 391 ms after the bot's own, then nothing for minutes, and
    nobody at the desk; paused mid-fight for it, the character died. Session 102 saw
    three more, 650-750 ms after the bot's own, each alone."""
    desk = Desk()
    said = []
    op = watch(desk, say=said.append)
    op.stamp("key 0x20 up")
    desk.input(391)
    assert not op.active()
    desk.now += 20_000
    op.stamp()                                   # the bot plays on
    desk.input(700)                              # another stray, long after the first
    assert not op.active(), "strays minutes apart are not a hand at the desk"
    desk.input(150)
    assert not op.active(), "two within the window are not yet one either"
    assert said and all("not yet a person" in line for line in said)
    assert "key 0x20 up" in said[0], "the log says what the bot last sent"
    desk.input(150)
    assert op.active() and "a person" in said[-1]


def test_strays_seconds_apart_are_not_a_person():
    """V216: while the bot tapped keys fast, the machine's strays came 3 to 9 s apart and
    twice reached "2 of 3" inside ten seconds (sessions 174-175). A hand makes several a
    second."""
    desk = Desk()
    said = []
    op = watch(desk, say=said.append)
    for gap in (0, 4_000, 4_000, 4_000):
        desk.now += gap
        op.stamp("key 0x1e up")
        desk.input(500)
        assert not op.active(), "strays seconds apart"
    assert all("1 of 3" in line for line in said)
    assert person(desk, op, 500)


def test_one_stray_is_reason_enough_not_to_take_the_window_for_a_while():
    """A click into another window is one or two inputs; raising the game over it takes the
    window from whoever clicked (review, 25 September)."""
    from jev.clients.operator import SUSPECT_S

    desk = Desk()
    op = watch(desk)
    op.stamp()
    desk.input(900)
    assert not op.active() and op.suspected()
    desk.now += int(SUSPECT_S * 1000) + 1
    assert not op.suspected()


def test_a_person_typing_slowly_keeps_the_desk():
    """Confirmed once, every input of theirs keeps the pause: one key in fifteen seconds
    otherwise lost the desk ten minutes after the first burst (review, 25 September)."""
    desk = Desk()
    op = watch(desk, quiet_s=600)
    op.stamp()
    assert person(desk, op, 1_000)
    for _ in range(50):                          # a key every fifteen seconds, 12.5 minutes
        desk.input(15_000)
        assert op.active(), "the bot took the desk from a slow typist"


def test_a_pause_holds_the_steps_own_clock_and_a_refused_focus_is_not_a_stop(tmp_path):
    """A ten-minute pause ran out a four-minute hand-in's clock (review, 25 September); and
    a person arriving while the window was being taken back stopped the run."""
    from jev.run.supervisor import Result
    from jev.learn.episode import SkillOutcome

    rt = runtime(tmp_path, [seen(t) for t in range(30)])
    body = Body()
    person = threading.Event()
    person.set()
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append, operator_active=person.is_set,
                            has_focus=lambda: True,
                            focus=lambda checkpoint: False)
    try:
        for t in range(20):
            supervisor.step(t)
        assert rt.tracker.paused is True
        assert rt.tracker.memory.working_s == 0.0, "the step's clock ran through the pause"
        assert body.calls == 0
    finally:
        body.allow_finish.set()
        supervisor.close()
    suspected = threading.Event()
    suspected.set()
    focus_calls = []
    rt2 = runtime(tmp_path / "second", [seen(t) for t in range(10)])
    supervisor = Supervisor(rt2, body, say=lines.append, operator_suspected=suspected.is_set,
                            has_focus=lambda: False,
                            focus=lambda checkpoint: focus_calls.append(1) or False)
    try:
        for t in range(5):
            supervisor.step(t)
        assert focus_calls == [], "the window was taken back over a click in another window"
        assert not supervisor.stopped.is_set()
    finally:
        supervisor.close()
