"""Launcher lifecycle proofs using only fake clients and the real offline argument path."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev.guide.coords import ZoneBounds
from jev.guide.graph import Graph, Node
from jev.persist import file_lock
from jev.run import cli
from jev.world.state_v1 import StepKind


def route_file(tmp_path):
    graph = Graph(graph_id="fixture", faction="alliance", entry="travel", coord_zone_id=1, nodes=(
        Node(id="travel", kind=StepKind.TRAVEL, zone="fixture", zone_id=1,
             coord_zone_id=1,
             pos=(0.5, 0.5), world=(50, 50, 0), map_id=0, skills=("TRAVEL_TO",)),))
    path = tmp_path / "graph.json"
    graph.save(path)
    return path


def test_legacy_navigation_frame_is_refused_before_input_or_capture(tmp_path, monkeypatch, capsys):
    path = route_file(tmp_path)
    graph = Graph.load(path).model_copy(update={"coord_zone_id": None})
    graph.save(path)
    attach = Mock(side_effect=AssertionError("unframed guide attached a client"))
    monkeypatch.setattr(cli, "attach", attach)
    assert cli.main(["--graph", str(path)]) == 2
    assert "no declared navigation frame" in capsys.readouterr().out
    attach.assert_not_called()


@pytest.mark.parametrize("route_mode", ["full", "supported"])
def test_all_optional_flags_check_remains_read_only_and_never_attaches(tmp_path, monkeypatch,
                                                                    capsys, route_mode):
    graph = route_file(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    touched = {}
    for name in ("attach", "input_lock_path", "file_lock", "Recorder", "MmapQuery", "Background", "reconnect_client", "Screenshots"):
        touched[name] = Mock(side_effect=AssertionError(f"--check invoked {name}"))
        monkeypatch.setattr(cli, name, touched[name])
    teacher = Mock(side_effect=AssertionError("--check constructed teacher"))
    monkeypatch.setattr("jev.teacher.client.ClaudeSubscriptionClient", teacher)
    credentials = Mock(side_effect=AssertionError("--check read credentials"))
    monkeypatch.setattr("jev.run.watchdog.credentials", credentials)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = cli.main(["--check", "--graph", str(graph), "--route-mode", route_mode,
                       "--client-id", "offline", "--learn", "--policy-mode", "adaptive",
                       "--teacher", "--teacher-binary", "/missing/never-execute",
                       "--teacher-model", "fixture", "--teacher-calls-per-hour", "1",
                       "--reconnect", "--env-file", str(tmp_path / "secret.env"),
                       "--learning-store", str(tmp_path / "learn"),
                       "--runs-dir", str(tmp_path / "runs"), "--blind-grace", "1",
                       "--no-progress", "2", "--reconnect-limit", "1", "--steps", "1",
                       "--run-for", "1", "--timeout", "1", "--hunt", "1", "--retries", "1",
                       "--screenshots", "--stop-file", str(tmp_path / "STOP")])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["learning"] and report["teacher"] and report["reconnect"]
    assert report["screenshots"]
    assert report["stop_file"] == str(tmp_path / "STOP")
    assert report["live_tested"] is False
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    for mock in [*touched.values(), teacher, credentials]:
        mock.assert_not_called()


def test_input_contention_never_attaches_or_constructs_background(tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "input_lock_path", lambda: tmp_path / "input.lock")
    attach = Mock(side_effect=AssertionError("contended launch attached a client"))
    monkeypatch.setattr(cli, "attach", attach)
    background = Mock(side_effect=AssertionError("contended launch started background services"))
    monkeypatch.setattr(cli, "Background", background)
    with file_lock(tmp_path / "input.lock", blocking=False):
        assert cli.main(["--graph", str(graph), "--run-for", "1"]) == 2
    assert "no client was attached" in capsys.readouterr().out
    attach.assert_not_called()
    background.assert_not_called()


def fake_live(monkeypatch, tmp_path, *, disconnected=False, character=0x26A9640B):
    events = []
    client = SimpleNamespace(
        client_id="slice", origin=(0, 0), size=(1600, 900), _capturing=threading.RLock(),
        log=SimpleNamespace(reset=lambda: events.append("quest log reset")),
        hid=SimpleNamespace(checkpoint=None, ready=lambda: True, keys_down=lambda: [],
                            release_all=Mock()),
        focused=lambda *args, **kwargs: True,
        frame=lambda: None, quest_ids=lambda: (),
        close=lambda: events.append("client closed"), restored=not disconnected)
    client.character = character
    client.read = lambda: ({"pos.zone_id": 1, "char.key": client.character}
                           if client.restored else None)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "input_lock_path", lambda: tmp_path / "input.lock")
    monkeypatch.setattr(cli, "attach", lambda client_id: client)
    monkeypatch.setattr(cli, "bounds_by_radio_id", lambda path: {1: ZoneBounds(1, 0, 100, 0, 100, 0)})
    monkeypatch.setattr(cli, "MmapQuery", Mock())
    monkeypatch.setattr(cli, "with_travel", Mock())
    class FakeBody:
        available = frozenset({"TRAVEL_TO"})
        validate = staticmethod(lambda decision, step: None)
        def __init__(self, client, *args, **kwargs):
            self.client = client
            events.append("body created")
        def has_focus(self):
            return self.client.hid.ready()
        def reconnect(self, checkpoint, *, env_file=None):
            events.append("body reconnect")
            return cli.reconnect_client(self.client, checkpoint, env_file=env_file)
    monkeypatch.setattr(cli, "LiveBody", FakeBody)
    class FakeBackground:
        def __init__(self, *args, **kwargs):
            events.append("background created")
        def poll(self, state):
            pass
        def close(self):
            events.append("background closed")
    monkeypatch.setattr(cli, "Background", FakeBackground)
    class FakeSupervisor:
        failure = None
        def __init__(self, runtime, body, **kwargs):
            events.append("supervisor created")
            client.runtime = runtime
        def run(self, seconds, **kwargs):
            events.append("supervisor ran")
        def close(self):
            events.append("supervisor closed")
    monkeypatch.setattr(cli, "Supervisor", FakeSupervisor)
    return client, events


def test_initial_reconnect_uses_session_env_file_and_restores_before_body_setup(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path, disconnected=True)
    # Test credentials are synthetic, never read from the user's environment/file.
    secret = tmp_path / "fixture.env"
    secret.write_text("JEV_WOW_ACCOUNT=fixture-account\nJEV_WOW_PASSWORD=fixture-password\n")
    monkeypatch.delenv("JEV_WOW_ACCOUNT", raising=False)
    monkeypatch.delenv("JEV_WOW_PASSWORD", raising=False)
    class FakeSession:
        def __init__(self, hid, frame, radio, origin, size, checkpoint):
            assert hid is client.hid and frame is client.frame
            assert origin == client.origin and size == client.size
            self.checkpoint = checkpoint
            self.radio = radio
        def sign_in(self, account, password):
            assert (account, password) == ("fixture-account", "fixture-password")
            self.checkpoint()
            assert not self.radio()
            events.append("session restored radio")
            client.restored = True
            assert self.radio()
            return True
    monkeypatch.setattr("jev.run.watchdog.Session", FakeSession)
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--reconnect",
                     "--env-file", str(secret), "--runs-dir", str(tmp_path / "runs")]) == 0
    assert events.index("session restored radio") < events.index("body created")
    assert events.index("quest log reset") < events.index("body created")
    assert events[-3:] == ["supervisor closed", "background closed", "client closed"]
    run = next((tmp_path / "runs").iterdir())
    assert (run / "route.json").is_file()
    assert "fixture-password" not in "".join(p.read_text() for p in run.iterdir() if p.is_file())


def test_failed_initial_reconnect_closes_client_without_body_or_corpus(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path, disconnected=True)
    monkeypatch.setattr("jev.run.watchdog.credentials", lambda **kwargs: None)
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--reconnect",
                     "--env-file", str(tmp_path / "absent.env"),
                     "--runs-dir", str(tmp_path / "runs")]) == 1
    assert events == ["client closed"]
    assert not (tmp_path / "runs").exists()


def test_unavailable_teacher_constructor_does_not_prevent_live_floor_start(tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    monkeypatch.setattr("jev.teacher.client.ClaudeSubscriptionClient",
                        Mock(side_effect=OSError("optional teacher executable unavailable")))
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--teacher",
                     "--runs-dir", str(tmp_path / "runs")]) == 0
    assert "supervisor ran" in events
    assert "scripted floor continues" in capsys.readouterr().out
    assert events[-1] == "client closed"


def test_screenshots_stop_before_capture_closes_even_when_supervisor_fails(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)

    class FakeScreenshots:
        error = None
        record_frame = Mock(return_value={})

        def __init__(self, frame, directory):
            assert frame is client.frame
            assert directory.name == "screenshots" and directory.parent.parent == tmp_path / "runs"

        def start(self):
            events.append("screenshots started")
            return self

        def close(self):
            events.append("screenshots closed")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Screenshots", FakeScreenshots)
    monkeypatch.setattr(cli.Supervisor, "run", interrupted)
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--screenshots",
                     "--runs-dir", str(tmp_path / "runs")]) == 130
    assert events.index("screenshots started") < events.index("supervisor created")
    assert events[-4:] == ["supervisor closed", "screenshots closed", "background closed", "client closed"]


def test_screenshot_storage_failure_cancels_worker_and_stops_test(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    screenshots = SimpleNamespace(error=None, start=lambda: None,
                                  record_frame=Mock(return_value={}),
                                  close=lambda: events.append("screenshots closed"))
    monkeypatch.setattr(cli, "Screenshots", lambda *args: screenshots)
    worker = SimpleNamespace(cancel=Mock())

    class MonitoringSupervisor:
        failure = None

        def __init__(self, *args, housekeeping, **kwargs):
            self.housekeeping = housekeeping
            self.stopped = threading.Event()
            self.worker = worker

        def run(self, *args, **kwargs):
            screenshots.error = "disk full"
            self.housekeeping(None)
            assert self.stopped.is_set()
            assert self.failure == "disk full"

        def close(self):
            events.append("supervisor closed")

    monkeypatch.setattr(cli, "Supervisor", MonitoringSupervisor)
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--screenshots",
                     "--runs-dir", str(tmp_path / "runs")]) == 1
    worker.cancel.assert_called_once_with("disk full")
    assert events[-1] == "client closed"


def test_existing_stop_file_prevents_attachment_and_is_preserved(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    stop = tmp_path / "STOP"
    stop.touch()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    attach = Mock(side_effect=AssertionError("operator already stopped the test"))
    monkeypatch.setattr(cli, "attach", attach)
    assert cli.main(["--graph", str(graph), "--stop-file", str(stop)]) == 130
    attach.assert_not_called()
    assert stop.exists()


def test_new_stop_file_uses_termination_cleanup_without_screenshots(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    stop = tmp_path / "STOP"
    base = cli.Supervisor

    class StoppingSupervisor(base):
        def __init__(self, *args, housekeeping, **kwargs):
            super().__init__(*args, **kwargs)
            self.housekeeping = housekeeping

        def run(self, *args, **kwargs):
            stop.touch()
            self.housekeeping(None)
            raise AssertionError("stop did not interrupt the live loop")

    monkeypatch.setattr(cli, "Supervisor", StoppingSupervisor)
    monkeypatch.setattr(cli, "Screenshots", Mock(side_effect=AssertionError("screenshots not requested")))
    assert cli.main(["--graph", str(graph), "--run-for", "1", "--stop-file", str(stop),
                     "--runs-dir", str(tmp_path / "runs")]) == 130
    assert events[-3:] == ["supervisor closed", "background closed", "client closed"]
    assert stop.exists()


def test_explicit_learning_store_does_not_resolve_platform_default(tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "default_learning_store",
                        Mock(side_effect=AssertionError("explicit store should win")))
    assert cli.main(["--check", "--graph", str(graph),
                     "--learning-store", str(tmp_path / "custom-store")]) == 0


@pytest.mark.parametrize("error", [KeyboardInterrupt, BrokenPipeError])
def test_interrupted_screenshot_start_joins_the_launched_thread(tmp_path, monkeypatch, error):
    import numpy as np

    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)
    client.frame = lambda: np.zeros((2, 2, 3), dtype=np.uint8)
    monitors = []
    original = cli.Screenshots

    def interrupted_log(message):
        if message.startswith("screenshots every"):
            raise error("interrupted after screenshot thread started")

    def monitor(frame, directory):
        result = original(frame, directory, say=interrupted_log)
        monitors.append(result)
        return result

    def close_client():
        assert monitors[0]._thread is None
        assert monitors[0]._stream.closed
        events.append("client closed")

    monkeypatch.setattr(cli, "Screenshots", monitor)
    client.close = close_client
    args = ["--graph", str(graph), "--screenshots", "--runs-dir", str(tmp_path / "runs")]
    if error is KeyboardInterrupt:
        assert cli.main(args) == 130
    else:
        with pytest.raises(error):
            cli.main(args)
    client.hid.release_all.assert_called_once()
    assert events == ["client closed"]


@pytest.mark.parametrize("stage", ["focus", "reconnect"])
@pytest.mark.parametrize("screenshots", [False, True])
def test_startup_stop_checkpoint_stops_before_body_and_releases_input(tmp_path, monkeypatch, stage,
                                                                    screenshots):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path, disconnected=stage == "reconnect")
    stop = tmp_path / "STOP"
    held = {"shift"}
    client.hid.release_all.side_effect = held.clear

    def focus(*args, checkpoint, **kwargs):
        if stage == "focus":
            stop.touch()
        checkpoint()
        return True

    def reconnect(client, checkpoint, **kwargs):
        stop.touch()
        checkpoint()
        raise AssertionError("STOP did not interrupt initial reconnect")

    client.focused = focus
    monkeypatch.setattr(cli, "reconnect_client", reconnect)
    args = ["--graph", str(graph), "--reconnect", "--stop-file", str(stop),
            "--runs-dir", str(tmp_path / "runs")]
    if screenshots:
        args.append("--screenshots")
    assert cli.main(args) == 130
    assert not held
    assert events == ["client closed"]
    assert (tmp_path / "runs").exists() is screenshots


@pytest.mark.parametrize("failure", ["focus", "reconnect"])
def test_screenshots_cover_startup_and_close_after_early_failure(tmp_path, monkeypatch, failure):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path, disconnected=failure == "reconnect")

    class Monitoring:
        error = None

        def __init__(self, frame, directory):
            assert frame is client.frame
            assert directory.parent.is_dir()

        def start(self):
            events.append("screenshots started")

        def close(self):
            events.append("screenshots closed")

    def focus(*args, checkpoint, **kwargs):
        assert events == ["screenshots started"]
        events.append("focus attempted")
        checkpoint()
        return failure != "focus"

    def reconnect(client, checkpoint, **kwargs):
        assert events == ["screenshots started", "focus attempted"]
        checkpoint()
        return SimpleNamespace(code="reconnect_failed", detail="fixture login rejected")

    client.focused = focus
    monkeypatch.setattr(cli, "Screenshots", Monitoring)
    monkeypatch.setattr(cli, "reconnect_client", reconnect)
    assert cli.main(["--graph", str(graph), "--screenshots", "--reconnect",
                     "--runs-dir", str(tmp_path / "runs")]) == 1
    assert events[-2:] == ["screenshots closed", "client closed"]
    client.hid.release_all.assert_called_once()
    assert len(list((tmp_path / "runs").iterdir())) == 1


def test_screenshot_failure_during_startup_prevents_focus_and_input(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)
    screenshots = SimpleNamespace(error=None, close=lambda: events.append("screenshots closed"))

    def start():
        screenshots.error = "fixture storage failed"

    screenshots.start = start
    monkeypatch.setattr(cli, "Screenshots", lambda *args: screenshots)
    client.focused = Mock(side_effect=AssertionError("monitoring failed before focus"))
    assert cli.main(["--graph", str(graph), "--screenshots",
                     "--runs-dir", str(tmp_path / "runs")]) == 1
    client.focused.assert_not_called()
    client.hid.release_all.assert_called_once()
    assert events == ["screenshots closed", "client closed"]


@pytest.mark.parametrize("enabled", [False, True])
def test_live_body_receives_the_active_screenshot_event_writer(tmp_path, monkeypatch, enabled):
    graph = route_file(tmp_path)
    _, events = fake_live(monkeypatch, tmp_path)
    monitor = SimpleNamespace(error=None, start=lambda: events.append("monitor started"),
                              close=lambda: None, record_frame=Mock(return_value={"file": "event.png"}))
    monkeypatch.setattr(cli, "Screenshots", lambda *args: monitor)
    original = cli.LiveBody
    supplied = []
    class RecordingBody(original):
        def __init__(self, *args, **kwargs):
            supplied.append(kwargs["record_frame"])
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(cli, "LiveBody", RecordingBody)
    args = ["--graph", str(graph), "--run-for", "1", "--runs-dir", str(tmp_path / "runs")]
    if enabled:
        args.append("--screenshots")
    assert cli.main(args) == 0
    assert supplied == [monitor.record_frame if enabled else None]
    if enabled:
        assert events.index("monitor started") < events.index("body created")


@pytest.mark.parametrize("reconnect_enabled", [False, True])
def test_supervision_observes_focus_and_reconnects_through_the_shared_body_owner(
        tmp_path, monkeypatch, reconnect_enabled):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)
    restored = SimpleNamespace(code="reconnected", detail="fixture session restored")
    reconnect = Mock(return_value=restored)
    monkeypatch.setattr(cli, "reconnect_client", reconnect)
    original = cli.Supervisor
    checkpoint = Mock()
    env_file = tmp_path / "fixture.env"

    class LifecycleSupervisor(original):
        def __init__(self, runtime, body, *, has_focus, watchdog, **kwargs):
            super().__init__(runtime, body, **kwargs)
            assert has_focus.__self__ is body
            self.has_focus, self.watchdog = has_focus, watchdog

        def run(self, *args, **kwargs):
            client.hid.ready = lambda: False
            assert self.has_focus() is False
            client.hid.ready = lambda: True
            assert self.has_focus() is True
            if reconnect_enabled:
                assert self.watchdog.reconnect(checkpoint) is restored
            else:
                assert self.watchdog.reconnect is None

    monkeypatch.setattr(cli, "Supervisor", LifecycleSupervisor)
    args = ["--graph", str(graph), "--run-for", "1", "--env-file", str(env_file),
            "--runs-dir", str(tmp_path / "runs")]
    if reconnect_enabled:
        args.append("--reconnect")
    assert cli.main(args) == 0
    if reconnect_enabled:
        assert events.count("body reconnect") == 1
        reconnect.assert_called_once_with(client, checkpoint, env_file=env_file)
    else:
        reconnect.assert_not_called()


def test_each_character_keeps_its_own_playhead(tmp_path, monkeypatch, capsys):
    """A fresh level 1 launched on one saved position would have started at another
    character's quest 21, Northshire's first five quests marked done."""
    from jev.guide import playhead

    graph = route_file(tmp_path)
    client, _events = fake_live(monkeypatch, tmp_path)
    veteran = playhead.for_character(0x26A9640B, tmp_path / playhead.CHARACTERS)
    playhead.save(Graph.load(graph).graph_id, None, {7, 33}, veteran)
    assert cli.main(["--graph", str(graph), "--run-for", "1"]) == 0
    assert client.runtime.completed == {7, 33}
    assert client.runtime.character_key == 0x26A9640B
    assert "character 26a9640b" in capsys.readouterr().out

    client.character = 0x1234                     # a fresh character: nothing remembered
    assert cli.main(["--graph", str(graph), "--run-for", "1"]) == 0
    assert client.runtime.completed == set() and client.runtime.character_key == 0x1234
    client.runtime.on_progress("step", {21}, None, 0)
    assert playhead.load(Graph.load(graph).graph_id, veteran).completed == {7, 33}, \
        "one character's progress was written into another's"
    fresh = playhead.for_character(0x1234, tmp_path / playhead.CHARACTERS)
    assert playhead.load(Graph.load(graph).graph_id, fresh).completed == {21}


def test_a_strip_that_does_not_name_its_character_is_not_guessed_at(tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    fake_live(monkeypatch, tmp_path, character=None)
    assert cli.main(["--graph", str(graph), "--run-for", "1"]) != 0
    assert "which character" in capsys.readouterr().out
