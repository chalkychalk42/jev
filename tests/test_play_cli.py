"""The visual-playing CLI must preserve the read-only preflight and one-owner launch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_run_cli import fake_live, route_file

from jev.run import cli


def test_play_check_is_read_only_and_forces_evidence_not_learning(tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    prohibited = []
    for name in ("attach", "input_lock_path", "file_lock", "Recorder", "MmapQuery",
                 "reconnect_client", "Screenshots", "atomic_json"):
        mock = Mock(side_effect=AssertionError(f"--check invoked {name}"))
        monkeypatch.setattr(cli, name, mock)
        prohibited.append(mock)
    for name in ("PlayingBody", "make_vision_client", "VisionTeacher", "MotorLearner", "PlayJournal"):
        mock = Mock(side_effect=AssertionError(f"--check constructed {name}"))
        monkeypatch.setattr(f"jev.play.runtime.{name}", mock)
        prohibited.append(mock)
    credentials = Mock(side_effect=AssertionError("--check read reconnect credentials"))
    monkeypatch.setattr("jev.run.watchdog.credentials", credentials)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = cli.main(["--check", "--graph", str(graph), "--play-mode", "teach",
                       "--teacher-binary", "/missing/never-execute", "--teacher-model", "fixture",
                       "--play-teacher-calls-per-hour", "3", "--play-decision-timeout", "2",
                       "--learning-store", str(tmp_path / "learning"),
                       "--runs-dir", str(tmp_path / "runs"), "--reconnect",
                       "--bindings", str(tmp_path / "account-bindings.wtf"),
                       "--bindings", str(tmp_path / "character-bindings.wtf")])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["play_mode"] == "teach" and report["motor_recording"] and report["visual_teacher"]
    # Evidence, not learning: no student is trained inside a live session (V174) or acts (V225).
    assert report["screenshots"] and not report["motor_learning"]
    assert "motor_handover" not in report
    assert report["play_teacher_calls_per_hour"] == 3
    assert report["bindings"] == [str(tmp_path / "account-bindings.wtf"),
                                  str(tmp_path / "character-bindings.wtf")]
    assert report["live_tested"] is False
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    for mock in [*prohibited, credentials]:
        mock.assert_not_called()


@pytest.mark.parametrize(("option", "value"), [
    ("--play-decision-timeout", "0"), ("--play-decision-timeout", "-1"),
    ("--play-decision-timeout", "nan"), ("--play-decision-timeout", "inf"),
    ("--play-teacher-calls-per-hour", "0"), ("--play-mode", "adaptive"),
])
def test_invalid_play_exposure_or_budget_is_rejected_before_attachment(tmp_path, monkeypatch, option, value):
    graph = route_file(tmp_path)
    attach = Mock(side_effect=AssertionError("invalid setup attached client"))
    monkeypatch.setattr(cli, "attach", attach)
    with pytest.raises(SystemExit) as error:
        cli.main(["--check", "--graph", str(graph), "--play-mode", "teach", option, value])
    assert error.value.code == 2
    attach.assert_not_called()


def install_playing_launcher(monkeypatch, events):
    arguments = {}

    class FakeScreenshots:
        error = None
        record_frame = Mock()
        def __init__(self, frame, directory):
            events.append("screenshots created")
        def start(self):
            events.append("screenshots started")
            return self
        def close(self):
            events.append("screenshots closed")

    class FakePlayingBody:
        def __init__(self, spine, **kwargs):
            events.append("playing created")
            arguments.update(kwargs)
            self.spine, self.available, self.validate = spine, spine.available, spine.validate
            self.has_focus, self.reconnect = spine.has_focus, spine.reconnect
        def close(self):
            events.append("playing closed")

    monkeypatch.setattr(cli, "Screenshots", FakeScreenshots)
    monkeypatch.setattr("jev.play.runtime.PlayingBody", FakePlayingBody)
    strategic = Mock(side_effect=AssertionError("parallel strategic teacher constructed during visual play"))
    monkeypatch.setattr("jev.teacher.client.ClaudeSubscriptionClient", strategic)
    return arguments, strategic, FakePlayingBody


def test_launch_wraps_one_spine_after_screenshots_and_uses_motor_tutor_budget(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)
    arguments, strategic, body_class = install_playing_launcher(monkeypatch, events)
    seen = {}
    class Supervisor:
        failure = None
        def __init__(self, runtime, body, **kwargs):
            assert isinstance(body, body_class)
            assert body.spine.client is client
            seen["body"] = body
            seen["runtime"] = runtime
            seen["housekeeping"] = kwargs["housekeeping"]
            events.append("supervisor created")
        def run(self, *args, **kwargs):
            seen["housekeeping"](None)
            events.append("supervisor ran")
        def close(self):
            events.append("supervisor closed")
    monkeypatch.setattr(cli, "Supervisor", Supervisor)
    assert cli.main(["--graph", str(graph), "--play-mode", "teach", "--teacher",
                     "--teacher-model", "fixture", "--play-teacher-calls-per-hour", "7",
                     "--play-decision-timeout", "4", "--bindings", str(tmp_path / "bindings.wtf"),
                     "--runs-dir", str(tmp_path / "runs"),
                     "--learning-store", str(tmp_path / "learning"), "--run-for", "1"]) == 0
    assert "mode" not in arguments and arguments["config"].mode == "teach"
    assert arguments["teacher_calls_per_hour"] == 7
    assert arguments["config"].teacher_timeout_s == 4
    assert arguments["teacher_model"] == "fixture"
    assert arguments["binding_paths"] == [tmp_path / "bindings.wtf"]
    assert arguments["screenshots"] is not None
    assert events.index("screenshots started") < events.index("playing created")
    assert events.index("playing created") < events.index("supervisor created")
    assert events.index("supervisor closed") < events.index("playing closed")
    assert events.index("playing closed") < events.index("client closed")
    assert events.count("body created") == 1 and events.count("playing created") == 1
    strategic.assert_not_called()


def test_interrupt_stops_worker_then_visual_learning_and_capture(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    install_playing_launcher(monkeypatch, events)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli.Supervisor, "run", interrupted)
    assert cli.main(["--graph", str(graph), "--play-mode", "teach", "--run-for", "1",
                     "--runs-dir", str(tmp_path / "runs"),
                     "--learning-store", str(tmp_path / "learning")]) == 130
    assert events[-4:] == ["supervisor closed", "playing closed", "screenshots closed",
                           "client closed"]


def test_visual_setup_failure_releases_input_and_capture_without_starting_worker(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    client, events = fake_live(monkeypatch, tmp_path)
    install_playing_launcher(monkeypatch, events)
    def broken(*args, **kwargs):
        raise ValueError("invalid control configuration")
    monkeypatch.setattr("jev.play.runtime.PlayingBody", broken)
    with pytest.raises(ValueError, match="invalid control configuration"):
        cli.main(["--graph", str(graph), "--play-mode", "teach", "--run-for", "1",
                  "--runs-dir", str(tmp_path / "runs"),
                  "--learning-store", str(tmp_path / "learning")])
    assert "supervisor created" not in events
    assert events[-2:] == ["screenshots closed", "client closed"]
    client.hid.release_all.assert_called_once()
    assert client.hid.checkpoint is None


def test_screenshot_failure_stops_playing_worker_before_another_decision(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    arguments, _, _ = install_playing_launcher(monkeypatch, events)
    worker = SimpleNamespace(cancel=Mock())
    class Supervisor:
        failure = None
        def __init__(self, *args, housekeeping, **kwargs):
            import threading
            self.worker, self.stopped, self.housekeeping = worker, threading.Event(), housekeeping
        def run(self, *args, **kwargs):
            arguments["screenshots"].error = "disk full"
            self.housekeeping(None)
            assert self.stopped.is_set()
        def close(self):
            events.append("supervisor closed")
    monkeypatch.setattr(cli, "Supervisor", Supervisor)
    assert cli.main(["--graph", str(graph), "--play-mode", "teach", "--run-for", "1",
                     "--runs-dir", str(tmp_path / "runs"),
                     "--learning-store", str(tmp_path / "learning")]) == 1
    worker.cancel.assert_called_once_with("disk full")
    assert events[-1] == "client closed"


def test_glm_check_uses_provider_default_without_credentials_network_or_client(
        tmp_path, monkeypatch, capsys):
    graph = route_file(tmp_path)
    fixture = tmp_path / "fixture.env"
    fixture.write_text("JEV_TEST_GLM_CREDENTIAL=fixture-private-key\n")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    prohibited = []
    for target in (
        "jev.run.cli.attach", "jev.run.cli.input_lock_path", "jev.run.cli.Screenshots",
        "jev.play.providers.credential", "jev.play.providers.make_vision_client",
        "jev.play.runtime.make_vision_client", "jev.play.runtime.PlayingBody",
        "httpx.Client", "httpx.AsyncClient", "subprocess.Popen",
    ):
        mock = Mock(side_effect=AssertionError(f"--check invoked {target}"))
        monkeypatch.setattr(target, mock)
        prohibited.append(mock)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert cli.main([
        "--check", "--graph", str(graph), "--play-mode", "teach", "--teacher",
        "--teacher-provider", "glm", "--teacher-env-file", str(fixture),
        "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
        "--teacher-base-url", "https://open.bigmodel.cn/api/paas/v4",
        "--learning-store", str(tmp_path / "learning"),
    ]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["teacher_provider"] == "glm"
    assert report["teacher_requested"] == "glm-4.6v-flash"
    assert report["visual_teacher"] and report["screenshots"]
    assert report["live_tested"] is False
    assert "fixture-private-key" not in output
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    for mock in prohibited:
        mock.assert_not_called()


def test_glm_requires_visual_play_before_attachment_or_credentials(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    attach = Mock(side_effect=AssertionError("unsupported mode attached client"))
    read = Mock(side_effect=AssertionError("unsupported mode read credentials"))
    monkeypatch.setattr(cli, "attach", attach)
    monkeypatch.setattr("jev.play.providers.credential", read)
    with pytest.raises(SystemExit) as caught:
        cli.main(["--check", "--graph", str(graph), "--teacher-provider", "glm", "--teacher"])
    assert caught.value.code == 2
    attach.assert_not_called()
    read.assert_not_called()


def test_glm_launch_forwards_explicit_settings_without_a_second_teacher(tmp_path, monkeypatch):
    graph = route_file(tmp_path)
    _client, events = fake_live(monkeypatch, tmp_path)
    arguments, strategic, _body_class = install_playing_launcher(monkeypatch, events)
    fixture = tmp_path / "fixture.env"
    fixture.write_text("JEV_TEST_GLM_CREDENTIAL=fixture-private-key\n")
    assert cli.main([
        "--graph", str(graph), "--play-mode", "teach", "--teacher",
        "--teacher-provider", "glm", "--teacher-env-file", str(fixture),
        "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
        "--teacher-base-url", "https://open.bigmodel.cn/api/paas/v4",
        "--runs-dir", str(tmp_path / "runs"),
        "--learning-store", str(tmp_path / "learning"), "--run-for", "1",
    ]) == 0
    assert arguments["teacher_provider"] == "glm"
    assert arguments["teacher_model"] == "glm-4.6v-flash"
    assert arguments["teacher_env_file"] == fixture
    assert arguments["teacher_key_env"] == "JEV_TEST_GLM_CREDENTIAL"
    assert arguments["teacher_base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert events.count("playing created") == events.count("body created") == 1
    strategic.assert_not_called()
