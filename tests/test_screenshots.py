"""Visual evidence keeps its real timing, failures and capture ownership."""

from __future__ import annotations

import json
import threading
import time
from itertools import pairwise

import numpy as np
import pytest
from PIL import Image

from jev.run.screenshots import ScreenshotError, Screenshots


def until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def rows(path):
    return [json.loads(line) for line in (path / "manifest.jsonl").read_text().splitlines()]


def test_lossless_frames_and_timestamps_are_written_off_the_calling_thread(tmp_path):
    pixels = np.arange(12 * 8 * 3, dtype=np.uint8).reshape(8, 12, 3)
    calls = []

    def frame():
        calls.append((threading.get_ident(), time.time()))
        return pixels

    monitor = Screenshots(frame, tmp_path, interval_s=0.02).start()
    try:
        until(lambda: monitor.captured >= 3)
    finally:
        monitor.close()
    manifest = rows(tmp_path)
    assert len(manifest) == monitor.captured == len(calls)
    assert monitor.error is None
    assert not list(tmp_path.glob("*.tmp"))
    for row, (thread, captured_at) in zip(manifest, calls, strict=True):
        assert thread != threading.get_ident()
        assert row["t"] <= captured_at <= row["t"] + row["duration_s"]
        assert row["status"] == "ok"
        assert row["kind"] == "periodic"
        assert "label" not in row and "event_t" not in row
        with Image.open(tmp_path / row["file"]) as saved:
            assert np.array_equal(np.asarray(saved), pixels)


def test_missing_frame_is_reported_then_recovery_records_a_real_frame(tmp_path):
    pixels = np.ones((2, 2, 3), dtype=np.uint8)
    captures = iter([None, None, pixels])
    messages = []
    monitor = Screenshots(lambda: next(captures, pixels), tmp_path,
                          interval_s=0.02, say=messages.append).start()
    try:
        until(lambda: monitor.captured >= 1)
    finally:
        monitor.close()
    manifest = rows(tmp_path)
    assert [r["status"] for r in manifest[:3]] == ["unavailable", "unavailable", "ok"]
    assert all("file" not in r for r in manifest[:2])
    assert monitor.missing == 2
    assert sum("screenshot unavailable" in m for m in messages) == 1
    assert "screenshot capture restored" in messages


def test_slow_capture_skips_slots_instead_of_backfilling_burst(tmp_path):
    calls = []

    def slow_frame():
        calls.append(time.monotonic())
        time.sleep(0.045)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    monitor = Screenshots(slow_frame, tmp_path, interval_s=0.02).start()
    try:
        until(lambda: monitor.captured >= 3)
    finally:
        monitor.close()
    manifest = rows(tmp_path)
    assert manifest[1]["skipped_slots"] >= 2
    assert all(b - a >= 0.055 for a, b in pairwise(calls))


def test_storage_failure_is_fatal_visible_and_recorded_without_partial_png(tmp_path, monkeypatch):
    def fail_save(self, path, **kwargs):
        path.write_bytes(b"partial image")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    messages = []
    monitor = Screenshots(lambda: np.zeros((2, 2, 3), dtype=np.uint8), tmp_path,
                          say=messages.append).start()
    try:
        until(lambda: monitor.error is not None)
    finally:
        monitor.close()
    assert "disk full" in monitor.error
    assert any("recording stopped" in m for m in messages)
    assert rows(tmp_path)[0]["status"] == "error"
    assert not list(tmp_path.glob("*.png*"))


def test_close_waits_for_capture_before_returning_and_is_idempotent(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def frame():
        entered.set()
        assert release.wait(2)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    monitor = Screenshots(frame, tmp_path).start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (monitor.close(), closed.set()))
    closer.start()
    try:
        assert not closed.wait(0.03)
    finally:
        release.set()
        closer.join(2)
    assert closed.is_set()
    monitor.close()
    assert monitor._thread is None
    with pytest.raises(ScreenshotError, match="cannot be restarted"):
        monitor.start()


def test_event_captures_are_fresh_saved_observations_with_distinct_metadata(tmp_path):
    frames = []

    def frame():
        pixels = np.full((2, 2, 3), len(frames), dtype=np.uint8)
        frames.append(pixels)
        return pixels

    monitor = Screenshots(frame, tmp_path, interval_s=30).start()
    try:
        until(lambda: monitor.captured == 1)
        before = monitor.capture_event("0-body-before")
        after = monitor.capture_event("0-body-after")
    finally:
        monitor.close()
    manifest = rows(tmp_path)
    assert manifest == [manifest[0], before, after]
    assert [r["index"] for r in manifest] == [0, 1, 2]
    assert [r["kind"] for r in manifest] == ["periodic", "event", "event"]
    assert before["label"] == "0-body-before" and after["label"] == "0-body-after"
    for row in (before, after):
        assert row["event_t"] <= row["t"] <= row["t"] + row["duration_s"]
        assert row["status"] == "ok"
        with Image.open(tmp_path / row["file"]) as saved:
            assert np.array_equal(np.asarray(saved), frames[row["index"]])
    assert before["file"] != after["file"]


@pytest.mark.parametrize("label", ["", "../body", "has space", "newline\n", "x" * 81, None])
def test_event_labels_are_bounded_diagnostic_identifiers(tmp_path, label):
    monitor = Screenshots(lambda: None, tmp_path)
    with pytest.raises(ValueError, match="event label"):
        monitor.capture_event(label)
    assert not list(tmp_path.iterdir())


def test_event_capture_requires_an_open_recorder(tmp_path):
    monitor = Screenshots(lambda: np.zeros((2, 2, 3), dtype=np.uint8), tmp_path)
    with pytest.raises(ScreenshotError, match="not been started"):
        monitor.capture_event("before")
    monitor.start()
    monitor.close()
    count = monitor.captured
    with pytest.raises(ScreenshotError, match="closed"):
        monitor.capture_event("after")
    assert monitor.captured == count


def test_missing_event_frame_returns_an_explicit_unavailable_result(tmp_path):
    pixels = np.ones((2, 2, 3), dtype=np.uint8)
    frames = iter([pixels, None, pixels])
    monitor = Screenshots(lambda: next(frames), tmp_path, interval_s=30).start()
    try:
        until(lambda: monitor.captured == 1)
        missing = monitor.capture_event("missing")
        recovered = monitor.capture_event("recovered")
    finally:
        monitor.close()
    assert missing["status"] == "unavailable" and "file" not in missing
    assert missing["kind"] == "event" and missing["label"] == "missing"
    assert recovered["status"] == "ok"
    assert monitor.error is None
    assert (monitor.captured, monitor.missing) == (2, 1)
    assert [r["index"] for r in rows(tmp_path)] == [0, 1, 2]


def test_event_storage_failure_stops_all_recording_and_keeps_its_event_label(tmp_path, monkeypatch):
    monitor = Screenshots(lambda: np.zeros((2, 2, 3), dtype=np.uint8), tmp_path,
                          interval_s=30).start()

    def fail_save(self, path, **kwargs):
        path.write_bytes(b"partial image")
        raise OSError("event disk full")

    try:
        until(lambda: monitor.captured == 1)
        monkeypatch.setattr(Image.Image, "save", fail_save)
        with pytest.raises(ScreenshotError, match="event disk full"):
            monitor.capture_event("before")
        with pytest.raises(ScreenshotError, match="event disk full"):
            monitor.capture_event("after")
    finally:
        monitor.close()
    assert "event disk full" in monitor.error
    manifest = rows(tmp_path)
    assert len(manifest) == 2
    assert manifest[1]["kind"] == "event"
    assert manifest[1]["label"] == "before"
    assert manifest[1]["status"] == "error"
    assert not list(tmp_path.glob("*.tmp"))


def test_close_serializes_with_an_event_capture_and_rejects_later_events(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    results, errors, calls = [], [], []
    active = 0
    overlap = False
    count_lock = threading.Lock()

    def frame():
        nonlocal active, overlap
        with count_lock:
            active += 1
            overlap |= active > 1
            calls.append(threading.current_thread().name)
        try:
            if threading.current_thread().name == "event-capture":
                entered.set()
                assert release.wait(2)
            return np.zeros((2, 2, 3), dtype=np.uint8)
        finally:
            with count_lock:
                active -= 1

    monitor = Screenshots(frame, tmp_path, interval_s=0.01).start()
    until(lambda: monitor.captured >= 1)

    def capture():
        try:
            results.append(monitor.capture_event("during-close"))
        except Exception as exc:
            errors.append(exc)

    event = threading.Thread(target=capture, name="event-capture")
    event.start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (monitor.close(), closed.set()))
    closer.start()
    try:
        assert not closed.wait(0.03)
    finally:
        release.set()
        event.join(2)
        closer.join(2)
    assert closed.is_set() and not errors and not overlap
    assert results[0]["status"] == "ok"
    manifest = rows(tmp_path)
    assert len(manifest) == len(calls)
    assert [r["index"] for r in manifest] == list(range(len(manifest)))
    assert manifest[-1] == results[0]
    with pytest.raises(ScreenshotError, match="closed"):
        monitor.capture_event("too-late")
