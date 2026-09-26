"""Real supervisor and worker threads, fake bodies only: never open a game window."""
import threading

import pytest
from test_runtime_records import runtime, seen

from jev.clients import win32
from jev.clients.hid import Hid
from jev.learn.episode import SkillOutcome, read
from jev.run.cli import main
from jev.run.supervisor import Cancelled, Result, Supervisor, interruption
from jev.world.state_v1 import Bags, Flags, Quest, State, Ui, Vitals


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


def test_the_bodys_own_jump_does_not_preempt_the_skill_that_jumped(tmp_path):
    # Measured 23 September: every unstick jump read as falling for 0.5-1.06 s, and
    # cancelling mid-air kept the character on the near side of a fence for 45 s.
    airborne = seen(flags=Flags(falling=True))
    rt = runtime(tmp_path, [seen(0), airborne.model_copy(update={"t": 0.5}),
                            airborne.model_copy(update={"t": 1.0}), seen(1.5), seen(2.0)])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        for t in (0.5, 1.0, 1.5, 2.0):
            supervisor.step(t)
        assert supervisor.worker is worker and not worker.cancelled.is_set()
    finally:
        supervisor.close()


def test_a_fall_longer_than_any_jump_still_preempts(tmp_path):
    airborne = seen(flags=Flags(falling=True))
    rt = runtime(tmp_path, [seen(0)] + [airborne.model_copy(update={"t": t / 2})
                                        for t in range(1, 8)])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        for t in (0.5, 1.0, 1.5, 2.0, 2.5):
            supervisor.step(t)
        assert not worker.cancelled.is_set(), "a 2 s airtime was measured on a slope"
        supervisor.step(3.0)                       # 2.5 s airborne: longer than any jump
        assert worker.cancelled.is_set() and worker.reason == "falling"
    finally:
        supervisor.close()
    assert interruption(worker.arm, airborne) == "falling", "untracked callers stay conservative"
    assert interruption(worker.arm, airborne, falling_s=2.0) != "falling"


def test_a_body_that_keeps_its_routine_clock_is_timed_from_it(tmp_path):
    import math

    rt = runtime(tmp_path, [seen(0), seen(61), seen(62), seen(125)])
    body = Body()
    body.routine_clock = math.inf                   # a tutor's self-bounded episode
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(61)
        assert not worker.cancelled.is_set(), "the catalog timed the tutor's episode"
        body.routine_clock = 62.0                   # the scripted routine took over
        supervisor.step(62)
        assert not worker.cancelled.is_set()
        supervisor.step(125)
        assert worker.cancelled.is_set() and worker.reason == "skill timeout"
    finally:
        supervisor.close()


def test_the_walk_to_an_npc_is_not_charged_to_the_accept(tmp_path):
    """A step is reached a hundred yards out; the accept then walked out of Goldshire's
    inn cellar and spent its sixty seconds on the stairs (session 90)."""
    rt = runtime(tmp_path, [seen(t) for t in (0, 10, 40, 60, 61, 105, 112)])
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        assert worker.arm.decision.skill == "ACCEPT_QUEST"
        body.travelling = True                   # the accept walks to its NPC
        for now in (10, 40, 60):
            supervisor.step(now)
        body.travelling = False                  # there: the work in front of the NPC
        supervisor.step(61)
        assert not worker.cancelled.is_set(), "the walk was charged to the accept"
        supervisor.step(105)
        assert not worker.cancelled.is_set(), "55 s of its own work"
        supervisor.step(112)
        assert worker.cancelled.is_set() and worker.reason == "skill timeout"
    finally:
        body.allow_finish.set()
        supervisor.close()


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


@pytest.mark.parametrize(("combat", "stops"), [(True, False), (False, True)])
def test_a_lost_defensive_fight_is_a_death_not_a_failed_attempt_at_the_step(
        tmp_path, combat, stops):
    """Run 20260923T173347-590b06 stopped with `('..._turnin', 'COMBAT_PROFILE'): 1 failed
    attempts` because the fight it was defending in ended with the character dead. The
    tracker counts deaths against the step; the step's own skill failing still stops."""
    fighting = Vitals(hp=1, power=1, combat=combat, dead=False, ghost=False)
    rt = runtime(tmp_path, [seen(0, vitals=fighting), seen(1, vitals=fighting),
                            seen(2, vitals=fighting)])
    body = Body()
    body.available = body.available | {"COMBAT_PROFILE"}
    body.execute = lambda arm, state, checkpoint: Result(SkillOutcome.ABORTED, "lost", "too_hurt")
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert supervisor.worker.done.wait(1)
        rule = supervisor.worker.arm.rule
        supervisor.step(1)
        assert rule.startswith("fight.") is combat
        assert supervisor.stopped.is_set() is stops
    finally:
        supervisor.close()


def test_a_stalled_step_is_failed_over_before_the_run_is_stopped(tmp_path):
    """Run 20260923T191946-2b79ed: a character stood on crates in Echo Ridge Mine while
    every fight came back unreachable, and the watchdog stopped the run with the quest
    at 1/12. The first window without progress now fails the step into its own edge,
    and only a second one stops the run."""
    from jev.clients.source import ScriptedSource
    from jev.guide.graph import FailEdge, FailWhen, Graph, Node
    from jev.learn.episode import Recorder
    from jev.orch.runtime import ClientRuntime
    from jev.run.watchdog import Watchdog
    from jev.world.state_v1 import StepKind

    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    g = Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1, next=("turnin",),
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), timeout_s=600.0,
             on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=600, goto="rib"),), **base),
        Node(id="turnin", kind=StepKind.QUEST_TURNIN, quest_id=1,
             skills=("TRAVEL_TO", "TURNIN_QUEST"), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(1, 10), **base),
    ))
    rt = ClientRuntime("c", g, ScriptedSource([seen(t) for t in range(40)]), Recorder(tmp_path))
    body = Body()
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append, watchdog=Watchdog(no_progress_s=10))
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        for t in range(1, 12):
            supervisor.step(t)
        assert rt.tracker.step_id == "rib", "the stalled step did not fail over"
        assert supervisor.failure is None and not supervisor.stopped.is_set()
        assert any("failing it over" in line for line in lines)
        for t in range(12, 23):
            supervisor.step(t)
        assert "no quest or experience" in (supervisor.failure or "")
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_a_service_the_step_waits_on_is_not_a_stall(tmp_path):
    """Session 101: a 389-yard walk to a merchant was failed over as "no quest or
    experience progress". A meal, a merchant or a trainer is bounded by its own timeout."""
    from jev.clients.source import ScriptedSource
    from jev.guide.graph import Graph, Node
    from jev.learn.episode import Recorder
    from jev.orch.runtime import ClientRuntime
    from jev.run.watchdog import Watchdog
    from jev.world.state_v1 import StepKind, Vitals

    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    g = Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),
        Node(id="rib", kind=StepKind.GRIND, level=(1, 10), **base),
    ))
    hurt = Vitals(hp=0.5, power=1, combat=False, dead=False, ghost=False)
    rt = ClientRuntime("c", g, ScriptedSource([seen(t, vitals=hurt) for t in range(40)]),
                       Recorder(tmp_path))
    body = Body()
    body.available = Body.available | {"EAT_DRINK"}
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append, watchdog=Watchdog(no_progress_s=10))
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        for t in range(1, 30):
            supervisor.step(t)
        assert rt.armed.decision.skill == "EAT_DRINK"
        assert not any("failing it over" in line for line in lines), "a meal was taken for a stall"
        assert rt.tracker.step_id == "accept" and supervisor.failure is None
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_another_character_logging_in_stops_the_run_with_this_ones_playhead_saved(tmp_path):
    """Each character keeps its own playhead. A state from another character - logged
    out, and someone else logged in - is not tracked or saved, and the run stops."""
    from jev.world.state_v1 import Char

    mine, theirs = Char(key=1, level=4), Char(key=2, level=1)
    states = [seen(0, char=mine), seen(0.25, char=mine), seen(0.5, char=theirs),
              seen(0.75, char=theirs)]
    saved = []
    rt = runtime(tmp_path, states, character_key=1,
                 on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None, **_: saved.append(step))
    body = Body()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    try:
        supervisor.step(0)
        supervisor.step(0.25)
        before = list(saved)
        supervisor.step(0.5)
        assert supervisor.stopped.is_set() and "another character" in supervisor.failure
        assert rt.foreign == 2 and saved == before, "the other character's state was saved"
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_full_bags_with_nothing_to_sell_are_not_a_failed_attempt(tmp_path):
    """Every session stopped at the merchant on one full backpack, and the next session
    walked straight back to it (runs 20260924T011327-6e5f4b to ...012802)."""
    full = Bags(free=0, durability_min=1.0)
    rt = runtime(tmp_path, [seen(0, bags=full), seen(1, bags=full), seen(2, bags=full)])
    body = Body()
    body.available = body.available | {"BAG_MAKE_SPACE"}
    body.execute = lambda arm, state, checkpoint: Result(
        SkillOutcome.ABORTED, "no confirmed sale-eligible junk", "no_junk")
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert supervisor.worker.arm.decision.skill == "BAG_MAKE_SPACE"
        assert supervisor.worker.done.wait(1)
        supervisor.step(1)
        assert not supervisor.stopped.is_set()
        assert rt.policy_context.bags_blocked
    finally:
        supervisor.close()


def test_combat_takes_a_hunt_from_a_tutor_with_no_fight_of_its_own_running(tmp_path):
    """A level 7 paladin lost 51% to 0 in 30 s to a Defias Thug it had not selected while
    the tutor played the hunt (run 20260924T055951-0c4439): a fighting skill the tutor
    holds is no defence."""
    from dataclasses import replace

    rt = runtime(tmp_path, [seen(vitals=Vitals(hp=0.5, combat=True))])
    state = rt.tick()
    hunt = replace(rt.armed, decision=rt.armed.decision.model_copy(update={"skill": "GRIND_UNTIL"}))
    assert interruption(hunt, state) is None, "the scripted hunt fights for itself"
    assert interruption(hunt, state, exposed=True) == "combat interrupted the leg or service"


def test_a_tutor_is_exposed_only_while_it_holds_the_objective_without_a_fight():
    import math

    from jev.play.runtime import PlayingBody

    rt = object.__new__(PlayingBody)
    rt.routine_clock, rt._delegating = math.inf, None
    assert rt.tutor_exposed
    rt._delegating = "COMBAT_PROFILE"
    assert not rt.tutor_exposed, "its own delegated fight is fighting back"
    rt._delegating = "EAT_DRINK"
    assert rt.tutor_exposed
    rt.routine_clock, rt._delegating = 12.0, None
    assert not rt.tutor_exposed, "a scripted routine holds it"


def test_a_trainer_out_of_reach_waits_a_level_and_never_stops_the_run(tmp_path):
    """One nameplate Brother Wilhelm did not answer from inside Goldshire's smithy stopped
    session 66: training is optional, and its failure waits for the next level."""
    from jev.world.state_v1 import Char

    class TrainingBody(Body):
        available = Body.available | {"TRAIN_CLASS"}

    rt = runtime(tmp_path, [seen(0, char=Char(level=9)), seen(1, char=Char(level=9)),
                            seen(2, char=Char(level=9))])
    body = TrainingBody(result=Result(SkillOutcome.ABORTED, "Brother Wilhelm: hover: ground",
                                      "not_visible"))
    body.allow_finish.set()
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    rt.policy_context.trainable = lambda state: True
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        assert supervisor.worker.arm.decision.skill == "TRAIN_CLASS"
        assert supervisor.worker.done.wait(1)
        supervisor.step(1)
        assert not supervisor.stopped.is_set(), supervisor.failure
        assert rt.policy_context.train_blocked_level == 9
    finally:
        supervisor.close()


def test_a_routine_taking_over_from_the_tutor_has_only_its_own_walk_taken_off(tmp_path):
    """The walk allowance subtracted the tutor's walking too, and the routine that took
    over got its sixty seconds and more (review, 25 September)."""
    rt = runtime(tmp_path, [seen(t) for t in (0, 10, 40, 41, 42, 102, 104)])
    body = Body()
    body.routine_clock = float("inf")                # the tutor holds the objective
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        body.travelling = True
        for now in (10, 40):                         # the tutor walks thirty seconds
            supervisor.step(now)
        body.travelling = False
        body.routine_clock = 41.0                    # the routine takes over, not walking
        supervisor.step(41)
        supervisor.step(42)
        assert worker.walked_s == 0.0, "the tutor's walk came off the routine's budget"
        supervisor.step(102)
        assert worker.cancelled.is_set() and worker.reason == "skill timeout"
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_a_meal_that_runs_out_of_time_does_not_stop_the_run(tmp_path):
    """One drink too many for its budget, from low mana on level-1 water, stopped session
    107 with the run's one retry spent on a meal. A meal is armed again while needed."""
    from jev.clients.source import ScriptedSource
    from jev.guide.graph import Graph, Node
    from jev.learn.episode import Recorder
    from jev.orch.runtime import ClientRuntime
    from jev.world.state_v1 import StepKind, Vitals

    base = dict(zone="zone", zone_id=1, pos=(0.5, 0.5))
    g = Graph(graph_id="g", faction="alliance", entry="accept", nodes=(
        Node(id="accept", kind=StepKind.QUEST_ACCEPT, quest_id=1,
             skills=("TRAVEL_TO", "ACCEPT_QUEST"), **base),))
    low = Vitals(hp=0.5, power=0.1, combat=False, dead=False, ghost=False)
    rt = ClientRuntime("c", g, ScriptedSource([seen(t, vitals=low) for t in (0, 60, 121, 122, 123)]),
                       Recorder(tmp_path))
    body = Body()
    body.available = Body.available | {"EAT_DRINK"}
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        worker = supervisor.worker
        supervisor.step(60)
        assert not worker.cancelled.is_set(), "a meal's budget covers more than one drink"
        supervisor.step(121)
        assert worker.cancelled.is_set() and worker.reason == "skill timeout"
        assert worker.done.wait(1)
        supervisor.step(122)
        assert not supervisor.stopped.is_set() and supervisor.failure is None
    finally:
        body.allow_finish.set()
        supervisor.close()


def test_a_run_whose_time_is_up_waits_out_the_fight_it_is_in(tmp_path):
    """Session 121 ran out of time at 41% health in a fight, the next session's first read
    was five seconds later at 18%, and the character died before its first swing."""
    fight = Vitals(hp=0.4, combat=True, dead=False, ghost=False)
    states = [seen(t, vitals=fight) for t in range(4)] + [seen(t) for t in range(4, 8)]
    rt = runtime(tmp_path, states, available_skills=Body.available | {"COMBAT_PROFILE"})
    body = Body()
    body.available = Body.available | {"COMBAT_PROFILE"}
    body.allow_finish.set()
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append)
    supervisor.step(0)                          # in the fight when the time runs out
    assert not supervisor.stopped.is_set()
    supervisor.run(0)
    assert rt.counters.ticks == 5, "stepped through the fight, and stopped once it was over"
    assert "run time is up; stopping once this fight is over" in lines


def test_a_run_whose_guide_is_finished_fights_what_attacks_it_before_it_stops(tmp_path):
    """Session 141's guide finished as a fight began: the run ended at once, the character
    stood in the fight through the restart and died (session 142 began by releasing its
    spirit)."""
    fight = Vitals(hp=0.6, combat=True, dead=False, ghost=False)
    states = [seen(t, vitals=fight) for t in range(4)] + [seen(t) for t in range(4, 8)]
    rt = runtime(tmp_path, states, available_skills=Body.available | {"COMBAT_PROFILE"})
    rt.finished = True
    body = Body()
    body.available = Body.available | {"COMBAT_PROFILE"}
    body.allow_finish.set()
    lines = []
    supervisor = Supervisor(rt, body, say=lines.append)
    supervisor.run(60)
    assert rt.counters.ticks == 5, "stepped through the fight, and stopped once it was over"
    assert body.calls >= 1, "the fight was fought, not waited through"
    assert "the guide is finished; stopping once this fight is over" in lines


def test_a_run_whose_time_is_up_out_of_a_fight_stops_at_once(tmp_path):
    rt = runtime(tmp_path, [seen(t) for t in range(4)])
    body = Body()
    body.allow_finish.set()
    supervisor = Supervisor(rt, body, say=lambda line: None)
    supervisor.step(0)
    supervisor.run(0)
    assert rt.counters.ticks == 1


def test_a_repairer_out_of_reach_is_not_walked_to_again_on_the_step_and_never_stops_the_run(
        tmp_path):
    """V185: the walk out of Sentinel Hill's inn to William MacGregor stuck in its doorway,
    and one failed repair stopped session 144."""
    from jev.world.state_v1 import Bags

    class RepairingBody(Body):
        available = Body.available | {"VENDOR_REPAIR"}

    worn = Bags(free=20, durability_min=0.2, money_copper=5000)
    rt = runtime(tmp_path, [seen(t, bags=worn) for t in (0, 1, 2)])
    body = RepairingBody(result=Result(SkillOutcome.ABORTED, "approach_failed",
                                       "the planner could not stand us on the node"))
    body.allow_finish.set()
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        assert supervisor.worker.arm.decision.skill == "VENDOR_REPAIR"
        assert supervisor.worker.done.wait(1)
        supervisor.step(1)
        assert not supervisor.stopped.is_set(), supervisor.failure
        step = rt.tracker.step_id
        assert rt.policy_context.repair_unreachable_step == step
        assert not rt.policy_context.can_repair(5000, step)
        assert rt.policy_context.can_repair(5000, "another step")
    finally:
        supervisor.close()


def test_a_merchant_out_of_reach_is_not_walked_to_again_on_the_step_and_never_stops_the_run(
        tmp_path):
    """V175: Goldshire's innkeeper, upstairs of whom the walk kept ending, stopped two
    sessions at T-0 on one timed-out restock each."""
    from jev.world.state_v1 import Bags

    class ShoppingBody(Body):
        available = Body.available | {"BUY_AMMO_REAGENT_FOOD"}

    empty = Bags(free=20, durability_min=1.0, money_copper=500, food_id=2070, food_count=0,
                 drink_id=159, drink_count=5)
    rt = runtime(tmp_path, [seen(t, bags=empty) for t in (0, 1, 2)])
    body = ShoppingBody(result=Result(SkillOutcome.TIMED_OUT, "skill timeout", "timeout"))
    body.allow_finish.set()
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=1)
    try:
        supervisor.step(0)
        assert body.started.wait(1)
        assert supervisor.worker.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD"
        assert supervisor.worker.done.wait(1)
        supervisor.step(1)
        assert not supervisor.stopped.is_set(), supervisor.failure
        step = rt.tracker.step_id
        assert rt.policy_context.supplies_unreachable_step == step
        assert not rt.policy_context.can_restock(500, step)
        assert rt.policy_context.can_restock(500, "another step")
    finally:
        supervisor.close()
