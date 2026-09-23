"""Optional one-second visual record of a supervised live test.

Capture uses the client's existing locked reader. Periodic encoding and disk writes stay
on the worker thread. Explicit event captures are synchronous so their returned file is
an actual observation around an action, never a nearest periodic frame. Both paths share
one lock and manifest. Already captured action observations can be persisted afterwards
without taking another frame or putting PNG writes between verification and input. Slow
samples skip elapsed slots instead of inventing pictures.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class ScreenshotError(RuntimeError):
    """Requested visual monitoring could not be started or retained."""


# Periodic frames are for watching a run; event frames are what an action was decided and
# judged on, and replays read their exact pixels. Measured on a live 1611x906 frame
# (23 September): lossless PNG 2.5 MB and 82 ms; JPEG quality 92 with full colour
# resolution 0.68 MB and 5 ms, with the radio still decoding and exactly the same plate
# candidates. Quality 85 with halved chroma invented plates, so it is not used.
PERIODIC_FORMAT = ("jpg", {"format": "JPEG", "quality": 92, "subsampling": 0})
EVENT_FORMAT = ("png", {"format": "PNG", "compress_level": 1})


_EVENT_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


class Screenshots:
    def __init__(self, frame: Callable[[], object | None], directory: Path, *,
                 interval_s: float = 1.0, say: Callable[[str], None] = print):
        if not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("screenshot interval must be positive and finite")
        self.frame, self.directory, self.interval_s, self.say = frame, directory, interval_s, say
        self.captured = self.missing = self.skipped = 0
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._image = None
        self._closed = False
        self._last_capture_error: str | None = None
        self._lock = threading.Lock()

    def start(self) -> Screenshots:
        with self._lock:
            if self._closed or self._stop.is_set():
                raise ScreenshotError("closed screenshot recorder cannot be restarted")
            if self._thread is not None:
                return self
            try:
                from PIL import Image  # Optional; --check and ordinary runs do not load it.

                self._image = Image
                self.directory.mkdir(parents=True, exist_ok=True)
                self._stream = (self.directory / "manifest.jsonl").open("x", encoding="utf-8")
                self._thread = threading.Thread(target=self._run, name="jev-screenshots", daemon=True)
                self._thread.start()
            except Exception as exc:
                if self._stream is not None:
                    self._stream.close()
                self._thread = None
                raise ScreenshotError(f"screenshots unavailable: {type(exc).__name__}: {exc}") from exc
        self.say(f"screenshots every {self.interval_s:g}s to {self.directory}")
        return self

    def capture_event(self, label: str) -> dict:
        """Save a fresh event frame and return its JSON-serializable manifest metadata.

        ``event_t`` is the request time; ``t`` is capture start after waiting for any
        periodic write. A missing frame returns ``status='unavailable'`` and no file.
        Storage failure is fatal, just as for periodic monitoring. This synchronous
        observation is not atomic with a caller's separate radio read or input.
        """
        self._validate_label(label)
        requested = time.time()
        with self._lock:
            self._require_open()
            try:
                return self._sample(kind="event", label=label, event_t=requested)
            except Exception as exc:
                self._failed(exc)
                raise ScreenshotError(self.error) from exc

    def record_frame(self, label: str, pixels: object | None, *, captured_at: float) -> dict:
        """Persist the supplied observation without calling the capture reader.

        ``captured_at`` is the caller's capture time in Unix seconds, also retained in
        ``t`` for the visual timeline. ``event_t`` is this persistence request's time;
        ``recorded_at`` starts the write after lock acquisition, and ``duration_s``
        measures persistence only. The caller may therefore save both sides of an
        action after input without relabeling those observations as later captures.

        Pixels must remain unchanged until this synchronous call returns. A missing
        observation is recorded as unavailable, never replaced by a fresh or periodic
        frame. Storage failures and close ownership match ``capture_event``.
        """
        self._validate_label(label)
        if (isinstance(captured_at, bool) or not isinstance(captured_at, (int, float))
                or not math.isfinite(captured_at) or captured_at <= 0):
            raise ValueError("capture time must be positive and finite Unix seconds")
        requested = time.time()
        with self._lock:
            self._require_open()
            started = time.monotonic()
            row = {"index": self.captured + self.missing, "t": captured_at,
                   "kind": "event", "label": label, "event_t": requested,
                   "captured_at": captured_at, "recorded_at": time.time(),
                   "source": "recorded_frame", "skipped_slots": self.skipped}
            try:
                return self._persist(pixels, row, started)
            except Exception as exc:
                self._failed(exc)
                raise ScreenshotError(self.error) from exc

    @staticmethod
    def _validate_label(label: str) -> None:
        if not isinstance(label, str) or _EVENT_LABEL.fullmatch(label) is None:
            raise ValueError("event label must be 1-80 letters, digits, dots, underscores or hyphens, "
                             "starting with a letter or digit")

    def _require_open(self) -> None:
        """Called while holding the capture/write lock."""
        if self.error is not None:
            raise ScreenshotError(self.error)
        if self._closed or self._stop.is_set():
            raise ScreenshotError("screenshot recorder is closed")
        if self._thread is None or self._stream is None or self._stream.closed:
            raise ScreenshotError("screenshot recorder has not been started")

    def close(self) -> None:
        self._stop.set()
        worker = self._thread
        if worker is not None:
            # The capture must finish before its shared GDI handles are closed.
            worker.join()
        with self._lock:
            # A synchronous event capture may still own the reader and stream even
            # after the periodic worker exits. Closing shares its lock as well.
            self._thread = None
            if self._stream is not None and not self._stream.closed:
                self._stream.close()
            self._closed = True

    def _run(self) -> None:
        due = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                if self._stop.is_set():
                    break
                try:
                    self._sample(kind="periodic")
                except Exception as exc:
                    self._failed(exc)
                    return
                due += self.interval_s
                now = time.monotonic()
                if due <= now:
                    missed = int((now - due) // self.interval_s) + 1
                    self.skipped += missed
                    due += missed * self.interval_s
            self._stop.wait(max(0.0, due - time.monotonic()))

    def _failed(self, exc: Exception) -> None:
        self._stop.set()
        if self.error is None:
            self.error = f"screenshot recording stopped: {type(exc).__name__}: {exc}"
            self.say(self.error)

    def _sample(self, *, kind: str, label: str | None = None, event_t: float | None = None) -> dict:
        started, wall = time.monotonic(), time.time()
        index = self.captured + self.missing
        row = {"index": index, "t": wall, "kind": kind, "skipped_slots": self.skipped}
        if kind == "event":
            row.update(label=label, event_t=event_t)
        try:
            pixels = self.frame()
        except Exception as exc:
            return self._persist(None, row, started, capture_error=exc)
        return self._persist(pixels, row, started)

    def _persist(self, pixels: object | None, row: dict, started: float, *,
                 capture_error: Exception | None = None) -> dict:
        """One image/index/manifest path, always under the recorder's ownership lock."""
        if capture_error is not None or pixels is None:
            exc = capture_error or ScreenshotError("client frame unavailable")
            self.missing += 1
            detail = f"{type(exc).__name__}: {exc}"
            row.update(status="unavailable", error=detail)
            if detail != self._last_capture_error:
                self.say(f"screenshot unavailable: {detail}")
            self._last_capture_error = detail
        else:
            stamp = datetime.fromtimestamp(row["t"], UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            suffix, options = PERIODIC_FORMAT if row["kind"] == "periodic" else EVENT_FORMAT
            name = f"{row['index']:06d}-{stamp}.{suffix}"
            path = self.directory / name
            pending = path.with_name(name + ".tmp")
            try:
                self._image.fromarray(pixels).save(pending, **options)
                pending.replace(path)
            except Exception as exc:
                pending.unlink(missing_ok=True)
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                self._record(row, started)
                raise
            self.captured += 1
            row.update(status="ok", file=name)
            if self._last_capture_error is not None:
                self.say("screenshot capture restored")
                self._last_capture_error = None
        self._record(row, started)
        return row

    def _record(self, row: dict, started: float) -> None:
        row["duration_s"] = time.monotonic() - started
        self._stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._stream.flush()
