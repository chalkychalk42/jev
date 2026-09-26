"""Clean session rotation creates independent actual recordings, never retries failure."""

import json

import pytest

from tools import start_teaching


def test_repeated_clean_sessions_preserve_identical_playhead_store_and_stop_configuration():
    calls = []
    args = ["--play-mode", "teach", "--playhead", "progress.json", "--learning-store", "models"]
    assert start_teaching.run_sessions(args, sessions=3,
                                       run=lambda values: calls.append(values) or 0,
                                       is_done=lambda _: False) == 0
    assert calls == [args] * 3


@pytest.mark.parametrize("failure", [1, 2, 130])
def test_failure_or_stop_is_never_relaunched(failure):
    calls = []
    assert start_teaching.run_sessions([], sessions=0, run=lambda _: calls.append(1) or failure,
                                       is_done=lambda _: False) == failure
    assert len(calls) == 1


def test_finished_route_stops_continuous_collection_between_sessions():
    calls = []
    assert start_teaching.run_sessions([], sessions=0, run=lambda _: calls.append(1) or 0,
                                       is_done=lambda _: len(calls) >= 2) == 0
    assert len(calls) == 2


def test_stop_file_prevents_first_or_later_attachment(tmp_path):
    stop = tmp_path / "STOP"
    calls = []

    def run(_):
        calls.append(1)
        stop.touch()
        return 0

    assert start_teaching.run_sessions(["--stop-file", str(stop)], sessions=0,
                                       run=run, is_done=lambda _: False) == 130
    assert len(calls) == 1


def test_launcher_defaults_to_check_and_never_starts_a_live_session(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    args = ["--play-mode", "teach", "--run-for", "180"]
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path), "args": args}))
    checked = []
    monkeypatch.setattr("jev.run.cli.main", lambda values: checked.append(values) or 0)
    monkeypatch.setattr(start_teaching, "run_sessions", lambda *a, **k: pytest.fail("started live session"))
    assert start_teaching.main(["--config", str(path)]) == 0
    assert checked == [[*args, "--check"]]


def test_the_loops_dispatch_and_session_length_flow_through_same_check(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path),
                               "args": ["--play-mode", "teach", "--run-for", "180"]}))
    checked = []
    monkeypatch.setattr("jev.run.cli.main", lambda values: checked.append(values) or 0)
    assert start_teaching.main(["--config", str(path), "--dispatch", "hybrid",
                                "--session-seconds", "3600"]) == 0
    assert start_teaching.option(checked[0], "--play-mode") == "teach"
    assert float(start_teaching.option(checked[0], "--run-for")) == 3600
    assert "--play-dispatch" not in checked[0], "hybrid is the only dispatch (V223)"


@pytest.mark.parametrize("retired", [["--dispatch", "tutor"], ["--mode", "adaptive"]])
def test_the_retired_arms_are_refused_before_the_check(tmp_path, monkeypatch, retired):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path), "args": ["--play-mode", "teach"]}))
    monkeypatch.setattr("jev.run.cli.main", lambda values: pytest.fail("checked a retired arm"))
    with pytest.raises(SystemExit) as error:
        start_teaching.main(["--config", str(path), *retired])
    assert error.value.code == 2


def test_the_loops_launch_flags_pass_the_real_offline_check(tmp_path, capsys):
    """The flags the loop's launch configuration names (26 September), through the real
    `jev.run.cli --check`, as the deploy's offline check runs them: the consolidation
    (V222-V229) removed options, and none of these."""
    args = ["--client-id", "fixture", "--play-mode", "teach", "--route-mode", "supported",
            "--reconnect", "--run-for", "900", "--retries", "1", "--no-progress", "300",
            "--play-decision-timeout", "30", "--stop-file", str(tmp_path / "STOP"),
            "--learning-store", str(tmp_path / "learning"),
            "--bindings", str(tmp_path / "bindings-cache.wtf"),
            "--teacher-provider", "claude", "--teacher-model", "fixture",
            "--teacher-effort", "medium", "--teacher-binary", str(tmp_path / "never-run")]
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path), "args": args}))
    before = sorted(tmp_path.rglob("*"))
    assert start_teaching.main(["--config", str(path), "--dispatch", "hybrid"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["play_mode"] == "teach" and report["visual_teacher"] and report["reconnect"]
    assert report["motor_recording"] and not report["motor_learning"]
    assert report["route_mode"] == "supported" and report["live_tested"] is False
    assert sorted(tmp_path.rglob("*")) == before, "the check writes nothing"


def test_nonwindows_live_launch_refused_before_game_access(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path), "args": ["--play-mode", "teach"]}))
    monkeypatch.setattr(start_teaching.sys, "platform", "linux")
    with pytest.raises(SystemExit) as error:
        start_teaching.main(["--config", str(path), "--run"])
    assert error.value.code == 2
