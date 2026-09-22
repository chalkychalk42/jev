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
