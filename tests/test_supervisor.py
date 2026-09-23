"""Real supervisor and worker threads, fake bodies only: never open a game window."""
import threading

import pytest
from test_runtime_records import runtime, seen

from jev.clients import win32
from jev.clients.hid import Hid
from jev.learn.episode import SkillOutcome, read
from jev.run.cli import main
from jev.run.supervisor import Cancelled, Result, Supervisor, interruption
from jev.world.state_v1 import Bags, Quest, State, Ui, Vitals


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


def test_capable_modal_wait_acknowledges_closed_popup_in_combat_but_death_still_preempts(tmp_path):
    rt = runtime(tmp_path, [seen(0, ui=Ui(modal=True), vitals=Vitals(hp=1, combat=True)),
                            seen(0.25, ui=Ui(modal=False), vitals=Vitals(hp=1, combat=True)),
                            seen(0.5, ui=Ui(modal=False), vitals=Vitals(hp=0, dead=True))])
    body = Body()
    body.handles_modal = body.executes_wait = True
    supervisor = Supervisor(rt, body, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        assert worker.arm.decision.skill == "ABORT_WAIT"
        supervisor.step(0.25)
        assert worker.completion_observed
        assert not worker.cancelled.is_set(), "closed popup needs acknowledgement before combat handover"
        supervisor.step(0.5)
        assert worker.done.wait(1)
        assert worker.result.outcome is SkillOutcome.PREEMPTED
        assert "dead" in worker.result.detail
        assert body.maximum == 1
    finally:
        supervisor.close()


def test_modal_capability_does_not_make_travel_immune_to_combat(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(0.25, vitals=Vitals(hp=1, combat=True))])
    body = Body()
    body.handles_modal = body.executes_wait = True
    body.travelling = True
    supervisor = Supervisor(rt, body, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert worker.done.wait(1)
        assert worker.result.outcome is SkillOutcome.PREEMPTED
        assert "combat" in worker.result.detail
    finally:
        supervisor.close()


def test_modal_acknowledgement_does_not_override_guide_failure_edge(tmp_path):
    from jev.guide.graph import FailEdge, FailWhen
    from jev.guide.tracker import Tracker

    rt = runtime(tmp_path, [seen(0, ui=Ui(modal=True)), seen(0.25, ui=Ui(modal=False))])
    first, second = rt.graph.nodes
    first = first.model_copy(update={"on_fail": (FailEdge(when=FailWhen.TIMEOUT,
                                                          value=0.2, goto=second.id),)})
    rt.graph = rt.graph.model_copy(update={"nodes": (first, second)})
    rt.tracker = Tracker(rt.graph, first.id)
    body = Body()
    body.handles_modal = body.executes_wait = True
    supervisor = Supervisor(rt, body, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert rt.tracker.step_id == second.id
        assert worker.done.wait(1)
        assert worker.result.outcome is SkillOutcome.PREEMPTED
        assert "playhead changed" in worker.result.detail
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


def test_observed_completion_allows_the_body_to_finish_before_next_dispatch(tmp_path):
    rt = runtime(tmp_path, [seen(), seen(0.25, quests=(Quest(quest_id=1),)),
                            seen(0.5, quests=(Quest(quest_id=1),))])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert rt.tracker.step_id == "turnin"
        assert worker.completion_observed and not worker.cancelled.is_set()
        assert body.calls == 1
        body.allow_finish.set()
        assert worker.done.wait(1)
        supervisor.step(0.5)
        rows = read(rt.recorder.dir / "skills.jsonl")
        assert rows[0]["skill"] == "ACCEPT_QUEST" and rows[0]["outcome"] == "succeeded"
        assert body.maximum == 1
    finally:
        supervisor.close()


def test_full_bags_interrupt_standalone_travel_and_preserve_the_playhead(tmp_path):
    from jev.world.state_v1 import Pos

    far = Pos(zone="zone", mx=0.9, my=0.9)
    rt = runtime(tmp_path, [seen(pos=far), seen(0.25, pos=far, bags=Bags(free=0)),
                            seen(0.5, pos=far, bags=Bags(free=0))])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda _: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(0.25)
        assert worker.done.wait(1)
        assert "bags" in worker.result.detail
        supervisor.step(0.5)
        assert supervisor.stopped.is_set()  # no invented selling executor
        assert "BAG_MAKE_SPACE" in supervisor.failure
        assert rt.tracker.step_id == "accept" and rt.tracker.memory.attempts == 0
        assert body.calls == 1
    finally:
        supervisor.close()


def test_death_results_enter_recovery_without_exhausting_the_quest_retry_budget(tmp_path):
    from jev.clients.fight import Fought
    from jev.run.body import LiveBody

    rt = runtime(tmp_path, [seen()])
    events = []
    life = {"dead": False, "ghost": False, "t": 0}
    rt.source.read = lambda: seen(life["t"], vitals=Vitals(hp=1, combat=False,
                                                         dead=life["dead"], ghost=life["ghost"]))
    body = Body()
    body.available = body.available | {"RELEASE_SPIRIT", "CORPSE_RUN"}
    def execute(arm, state, checkpoint):
        checkpoint()
        skill = arm.decision.skill
        events.append(skill)
        if skill == "ACCEPT_QUEST":
            life["dead"] = True
            return LiveBody._result(Fought.DIED, "died on the quest")
        if skill == "RELEASE_SPIRIT":
            life.update(dead=False, ghost=True)
        elif skill == "CORPSE_RUN":
            life.update(dead=False, ghost=False)
        else:
            raise AssertionError(skill)
        return Result(SkillOutcome.SUCCEEDED)
    body.execute = execute
    supervisor = Supervisor(rt, body, say=lambda _: None, max_failures=1)
    try:
        for i in range(4):
            life["t"] = i * 0.5
            supervisor.step(i * 0.5)
            assert supervisor.worker is not None
            assert supervisor.worker.done.wait(1)
        assert events == ["ACCEPT_QUEST", "RELEASE_SPIRIT", "CORPSE_RUN", "ACCEPT_QUEST"]
        assert not supervisor.failure and not supervisor.failures
        assert rt.tracker.step_id == "accept" and rt.tracker.memory.attempts == 0
        assert rt.counters.deaths == 1
    finally:
        supervisor.close()


def test_offline_check_never_attaches_a_client(monkeypatch, capsys):
    import jev.run.cli
    def forbidden(*args, **kwargs):
        pytest.fail("offline check attempted to attach the game")
    monkeypatch.setattr(jev.run.cli, "attach", forbidden)
    assert main(["--check"]) == 0
    assert '"executors"' in capsys.readouterr().out


def test_focus_backoff_can_restore_a_hidden_radio_without_running_game_skills(tmp_path):
    states = [State(t=i * 0.25, client_id="c") for i in range(4)] + [seen(1)]
    rt = runtime(tmp_path, states)
    body = Body()
    focused = threading.Event()
    started = threading.Event()
    allow_focus = threading.Event()
    def focus(checkpoint):
        started.set()
        while not allow_focus.wait(0.001):
            checkpoint()
        focused.set()
        return True
    supervisor = Supervisor(rt, body, say=lambda _: None,
                            has_focus=focused.is_set, focus=focus)
    try:
        supervisor.step(0)
        assert started.wait(1)
        focus_worker = supervisor.worker
        for i in range(1, 4):
            supervisor.step(i * 0.25)
        assert rt.counters.ticks == 4 and body.calls == 0
        assert not focus_worker.cancelled.is_set()
        assert not (rt.recorder.dir / "skills.jsonl").exists()
        allow_focus.set()
        assert focus_worker.done.wait(1)
        supervisor.step(1)
        assert body.started.wait(1)
        assert body.calls == 1 and not supervisor.failure
        assert not (rt.recorder.dir / "skills.jsonl").exists(), "focus was recorded as a game skill"
    finally:
        supervisor.close()


def test_refused_focus_stops_once_without_game_input(tmp_path):
    rt = runtime(tmp_path, [State(t=0, client_id="c"), State(t=0.5, client_id="c")])
    body = Body()
    attempts = []
    supervisor = Supervisor(rt, body, say=lambda _: None, has_focus=lambda: False,
                            focus=lambda _: attempts.append("focus") or False)
    try:
        supervisor.step(0)
        assert supervisor.worker.done.wait(1)
        supervisor.step(0.5)
        assert supervisor.stopped.is_set() and "refused focus" in supervisor.failure
        assert attempts == ["focus"] and body.calls == 0
        assert not supervisor.failures
    finally:
        supervisor.close()


def test_operator_stop_cancels_a_pending_focus_backoff(tmp_path):
    rt = runtime(tmp_path, [State(t=0, client_id="c")])
    started = threading.Event()
    def focus(checkpoint):
        started.set()
        while True:
            checkpoint()
            threading.Event().wait(0.001)
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda _: None, has_focus=lambda: False, focus=focus)
    supervisor.step(0)
    assert started.wait(1)
    worker = supervisor.worker
    supervisor.close()
    assert worker.done.is_set() and not worker.thread.is_alive()
    assert body.calls == 0


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
    hid._sleep = lambda delay, **_: None
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
