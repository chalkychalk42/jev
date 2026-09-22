"""Clean session rotation creates independent actual recordings, never retries failure."""

import json

import pytest

from tools import start_teaching


def test_repeated_clean_sessions_preserve_identical_playhead_store_and_stop_configuration():
    calls = []
    args = ["--play-mode", "adaptive", "--playhead", "progress.json", "--learning-store", "models"]
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


def test_explicit_adaptive_and_session_length_flow_through_same_check(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path),
                               "args": ["--play-mode", "teach", "--run-for", "180"]}))
    checked = []
    monkeypatch.setattr("jev.run.cli.main", lambda values: checked.append(values) or 0)
    assert start_teaching.main(["--config", str(path), "--mode", "adaptive", "--session-seconds", "3600"]) == 0
    assert start_teaching.option(checked[0], "--play-mode") == "adaptive"
    assert float(start_teaching.option(checked[0], "--run-for")) == 3600


def test_nonwindows_live_launch_refused_before_game_access(tmp_path, monkeypatch):
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"version": 1, "cwd": str(tmp_path), "args": ["--play-mode", "teach"]}))
    monkeypatch.setattr(start_teaching.sys, "platform", "linux")
    with pytest.raises(SystemExit) as error:
        start_teaching.main(["--config", str(path), "--run"])
    assert error.value.code == 2
