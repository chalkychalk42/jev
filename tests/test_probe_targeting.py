"""The diagnostic composes existing input/capture guards without a real client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev.clients.targeting import PaintCode, PaintResult, Targeting
from jev.persist import file_lock
from tools import probe_targeting as probe


@dataclass
class Result:
    code: str
    point: tuple[int, int]
    before: dict
    after: dict
    detail: str


def setup(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "input_lock_path", lambda: tmp_path / "input.lock")

    class Hid:
        checkpoint = None

        def __init__(self):
            self.held_buttons = set()

        def click(self, x, y, right=False):
            self.checkpoint()
            events.append(("click", x, y, right))
            return True

        def release_all(self):
            assert self.checkpoint is None
            self.held_buttons.clear()
            events.append("release")

    hid = Hid()

    def focused(*, checkpoint):
        events.append("focus")
        checkpoint()
        return True

    client = SimpleNamespace(
        hid=hid, origin=(11, 37), size=(1600, 900), focused=focused,
        frame=lambda: None, read=lambda: {"seq": 1, "target.name_id": 123},
        close=lambda: events.append("client close"))
    monkeypatch.setattr(probe, "attach", lambda client_id: client)

    class Screenshots:
        error = None

        def __init__(self, frame, directory, *, interval_s):
            assert frame is client.frame and interval_s == 1.0
            self.directory = directory
            events.append("screenshots constructed")

        def start(self):
            events.append("screenshots start")

        def close(self):
            events.append("screenshots close")

        def capture_event(self, label):
            events.append(("capture", label))
            return {"kind": "event", "label": label, "status": "ok",
                    "file": f"{label}.png", "t": 1.0}

    monkeypatch.setattr(probe, "Screenshots", Screenshots)

    class Targeting:
        def __init__(self, hid, read):
            assert hid is client.hid
            self.read = read

        def wait_for_paint(self):
            baseline = self.read()
            after = {**baseline, "seq": baseline["seq"] + 1, "target.has": True}
            return PaintResult(PaintCode.FRESH, baseline, after, "new paint after action")

        def probe(self, point):
            before = self.read()
            events.append(("probe", point))
            return Result("match", point, before, {"seq": 2, "hover.target": True},
                          "fresh target hover")

    monkeypatch.setattr(probe, "_targeting", Targeting)

    class Camera:
        def __init__(self, supplied_hid, origin, size):
            assert supplied_hid is hid and origin == client.origin and size == client.size

        def level(self):
            events.append("camera level")
            hid.checkpoint()
            return True

    monkeypatch.setattr(probe, "Camera", Camera)
    return client, events


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_hover_only_records_each_point_with_one_coordinate_conversion(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "ground:900:600", "--out", str(out)]) == 0
    saved = rows(out / "results.jsonl")
    assert [row["screen_point"] for row in saved] == [[811, 487], [911, 637]]
    assert [row["window_point"] for row in saved] == [[800, 450], [900, 600]]
    assert saved[0]["result"] == {
        "code": "match", "point": [811, 487], "before": {"seq": 1, "target.name_id": 123},
        "after": {"seq": 2, "hover.target": True}, "detail": "fresh target hover"}
    assert saved[0]["capture_before"]["file"] == "0-body-before.png"
    assert saved[0]["capture_after"]["file"] == "0-body-after.png"
    assert events.index(("capture", "0-body-before")) < events.index(("probe", (811, 487)))
    assert events.index(("probe", (811, 487))) < events.index(("capture", "0-body-after"))
    assert json.loads((out / "manifest.json").read_text())["capture_radio_atomic"] is False
    assert all(not isinstance(event, tuple) or event[0] != "click" for event in events)
    assert "camera level" not in events
    assert events.index("screenshots start") < events.index("focus")
    assert events[-3:] == ["release", "screenshots close", "client close"]
    assert not (tmp_path / "runs").exists()


def test_explicit_selection_is_exactly_one_left_click_after_calibration(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "model:801:452", "--select", "790:390", "--level",
                       "--out", str(out)]) == 0
    clicks = [event for event in events if isinstance(event, tuple) and event[0] == "click"]
    assert clicks == [("click", 801, 427, False)]
    assert events.index("screenshots start") < events.index("camera level") < events.index(clicks[0])
    assert [row["event"] for row in rows(out / "results.jsonl")] == ["select", "probe"]


@pytest.mark.parametrize("had_target", [True, False])
def test_selection_waits_for_post_click_paint_before_real_hover_probe(
        tmp_path, monkeypatch, had_target):
    client, events = setup(monkeypatch, tmp_path)

    def radio(seq, name, has=True):
        return {"seq": seq, "target.name_id": name, "target.has": has,
                "cursor.world": True, "cursor.has": True, "cursor.is_target": True}

    readings = iter([radio(1, 123, had_target), radio(2, 123, had_target),
                     radio(3, 123, had_target), radio(3, 123, had_target), radio(4, 456),
                     radio(4, 456), radio(5, 456), radio(6, 456)])
    client.read = lambda: next(readings)
    client.hid.move_to = lambda x, y: events.append(("move", x, y)) or True
    monkeypatch.setattr(probe, "_targeting",
                        lambda hid, read: Targeting(hid, read, wait_s=0.2, poll_s=0.001))
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--select", "800:350",
                       "--out", str(out)]) == 0
    selection, hover = rows(out / "results.jsonl")
    assert selection["before"]["target.name_id"] == 123
    assert selection["paint"]["baseline"]["seq"] == 3
    assert selection["paint"]["after"]["seq"] == 4
    assert selection["paint"]["after"]["target.name_id"] == 456
    assert hover["result"]["before"]["target.name_id"] == 456
    assert hover["result"]["code"] == "match"


@pytest.mark.parametrize("settled", [
    PaintResult(PaintCode.UNKNOWN, {"seq": 1}, {"seq": 1}, "no new paint"),
    PaintResult(PaintCode.BLIND, {"seq": 1}, None, "radio lost"),
    PaintResult(PaintCode.FRESH, {"seq": 1}, {"seq": 2, "target.has": False}, "new paint"),
])
def test_unproven_selection_stops_before_any_hover_movement(tmp_path, monkeypatch, settled):
    _client, events = setup(monkeypatch, tmp_path)
    hover = Mock(side_effect=AssertionError("probed unconfirmed selection"))
    monkeypatch.setattr(probe, "_targeting", lambda *args: SimpleNamespace(
        wait_for_paint=lambda: settled, probe=hover))
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--select", "800:350",
                       "--out", str(out)]) == 2
    saved = rows(out / "results.jsonl")
    assert len(saved) == 1 and saved[0]["event"] == "select"
    assert saved[0]["paint"]["code"] == settled.code
    assert saved[0]["error"]["type"] == "NotRunning"
    hover.assert_not_called()
    assert events[-3:] == ["release", "screenshots close", "client close"]


def test_blindness_before_optional_selection_never_clicks(tmp_path, monkeypatch):
    client, events = setup(monkeypatch, tmp_path)
    readings = iter([{"seq": 1}, None])
    client.read = lambda: next(readings)
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--select", "800:350",
                       "--out", str(out)]) == 2
    assert all(not isinstance(event, tuple) or event[0] != "click" for event in events)
    assert rows(out / "results.jsonl")[0]["sent"] is False


@pytest.mark.parametrize("phase", ["before", "after"])
def test_missing_explicit_visual_context_is_recorded_and_stops_further_points(
        tmp_path, monkeypatch, phase):
    _client, events = setup(monkeypatch, tmp_path)
    capture = probe.Screenshots.capture_event

    def capture_event(self, label):
        row = capture(self, label)
        if label.endswith(phase):
            row["status"] = "unavailable"
            row.pop("file")
        return row

    monkeypatch.setattr(probe.Screenshots, "capture_event", capture_event)
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "ground:900:600",
                       "--out", str(out)]) == 1
    saved = rows(out / "results.jsonl")
    assert len(saved) == 1
    assert saved[0][f"capture_{phase}"]["status"] == "unavailable"
    assert saved[0]["error"]["type"] == "ScreenshotError"
    assert ("result" in saved[0]) is (phase == "after")
    assert ("probe", (911, 637)) not in events
    assert events[-3:] == ["release", "screenshots close", "client close"]


def test_default_directory_is_unique_diagnostic_storage(tmp_path, monkeypatch):
    setup(monkeypatch, tmp_path)
    for _ in range(2):
        assert probe.main(["--points", "body:800:450"]) == 0
    directories = list((tmp_path / "captures" / "targeting").iterdir())
    assert len(directories) == 2
    assert all((path / "results.jsonl").is_file() for path in directories)
    assert not (tmp_path / "runs").exists()


def test_check_never_attaches_locks_calibrates_or_writes(tmp_path, monkeypatch, capsys):
    for name in ("attach", "file_lock", "input_lock_path", "Screenshots", "Camera", "_targeting"):
        monkeypatch.setattr(probe, name, Mock(side_effect=AssertionError(name)))
    before = list(tmp_path.iterdir())
    assert probe.main(["--check", "--points", "body:800:450", "--select", "900:400",
                       "--level", "--out", str(tmp_path / "absent")]) == 0
    assert list(tmp_path.iterdir()) == before
    report = json.loads(capsys.readouterr().out)
    assert report["live_tested"] is False
    assert report["screenshot_interval_s"] == 1.0


def test_existing_stop_does_not_attach(tmp_path, monkeypatch):
    stopped = tmp_path / "STOP"
    stopped.touch()
    attach = Mock(side_effect=AssertionError("attached after STOP"))
    monkeypatch.setattr(probe, "attach", attach)
    assert probe.main(["--points", "body:800:450", "--stop-file", str(stopped)]) == 130
    attach.assert_not_called()


def test_input_lease_contention_does_not_attach(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "input_lock_path", lambda: tmp_path / "input.lock")
    attach = Mock(side_effect=AssertionError("attached under another input owner"))
    monkeypatch.setattr(probe, "attach", attach)
    with file_lock(tmp_path / "input.lock", blocking=False):
        assert probe.main(["--points", "body:800:450"]) == 2
    attach.assert_not_called()


@pytest.mark.parametrize("phase", ["focus", "camera", "probe"])
def test_stop_is_checked_inside_each_input_phase_and_releases_everything(
        tmp_path, monkeypatch, phase):
    client, events = setup(monkeypatch, tmp_path)
    stopped = tmp_path / "STOP"

    def interrupt(*args, **kwargs):
        client.hid.held_buttons.add("right")
        stopped.touch()
        client.hid.checkpoint()
        raise AssertionError("stop checkpoint returned")

    if phase == "focus":
        client.focused = interrupt
    elif phase == "camera":
        monkeypatch.setattr(probe, "Camera", lambda *args: SimpleNamespace(level=interrupt))
    else:
        monkeypatch.setattr(probe, "_targeting", lambda *args: SimpleNamespace(probe=interrupt))
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--level", "--out", str(out),
                       "--stop-file", str(stopped)]) == 130
    assert not client.hid.held_buttons
    assert events[-3:] == ["release", "screenshots close", "client close"]
    if phase == "probe":
        assert rows(out / "results.jsonl")[0]["error"]["type"] == "KeyboardInterrupt"


def test_probe_exception_is_recorded_and_cleanup_completes(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    failed = Mock(side_effect=RuntimeError("probe failed"))
    monkeypatch.setattr(probe, "_targeting", lambda *args: SimpleNamespace(probe=failed))
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--out", str(out)]) == 1
    assert rows(out / "results.jsonl")[0]["error"] == {
        "type": "RuntimeError", "detail": "probe failed"}
    assert events[-3:] == ["release", "screenshots close", "client close"]


def test_screenshot_start_interruption_is_joined_before_client_close(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)

    def interrupted_start(self):
        events.append("screenshots start")
        raise KeyboardInterrupt

    monkeypatch.setattr(probe.Screenshots, "start", interrupted_start)
    assert probe.main(["--points", "body:800:450", "--out", str(tmp_path / "diagnostic")]) == 130
    assert events[-3:] == ["release", "screenshots close", "client close"]
    assert "focus" not in events


def test_screenshot_failure_stops_before_further_input(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(probe.Screenshots, "error", "disk full")
    assert probe.main(["--points", "body:800:450", "--out", str(tmp_path / "diagnostic")]) == 1
    assert "focus" not in events
    assert events[-3:] == ["release", "screenshots close", "client close"]


def test_deadline_is_checked_during_probe_reads(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    clock = [0.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def targeting(hid, read):
        def run(point):
            clock[0] = 2.0
            return read()
        return SimpleNamespace(probe=run)

    monkeypatch.setattr(probe, "_targeting", targeting)
    out = tmp_path / "diagnostic"
    assert probe.main(["--points", "body:800:450", "--run-for", "1", "--out", str(out)]) == 1
    assert rows(out / "results.jsonl")[0]["error"]["type"] == "TimeoutError"
    assert events[-3:] == ["release", "screenshots close", "client close"]


def test_point_outside_actual_window_is_refused_before_focus_or_input(tmp_path, monkeypatch):
    _client, events = setup(monkeypatch, tmp_path)
    assert probe.main(["--points", "outside:1600:450", "--out",
                       str(tmp_path / "diagnostic")]) == 1
    assert "focus" not in events
    assert all(not isinstance(event, tuple) for event in events)
    assert events[-3:] == ["release", "screenshots close", "client close"]


@pytest.mark.parametrize("arguments", [
    ["--points", "same:1:2", "same:3:4"],
    ["--points", "bad:-1:5"],
    ["--points", "bad:1.5:3"],
    ["--points", ":1:3"],
    ["--points", "a b:1:3"],
    ["--points", f"{'a' * 65}:1:3"],
    ["--points", "body:1:3", "--run-for", "nan"],
    ["--points", "body:1:3", "--run-for", "inf"],
    ["--points", "body:1:3", "--run-for", "0"],
    ["--points", *[f"p{i}:1:2" for i in range(probe.MAX_POINTS + 1)]],
])
def test_invalid_arguments_never_attach(monkeypatch, arguments):
    attached = Mock(side_effect=AssertionError("invalid arguments attached a client"))
    monkeypatch.setattr(probe, "attach", attached)
    with pytest.raises(SystemExit) as stopped:
        probe.main(arguments)
    assert stopped.value.code == 2
    attached.assert_not_called()
