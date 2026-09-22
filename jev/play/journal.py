"""Crash-readable motor evidence, including decisions which never became actions."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class PlayJournal:
    def __init__(self, directory: Path, *, run_id: str):
        self.directory, self.run_id = Path(directory), run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._streams = {}
        self._seen = set()
        self._closed = False

    def append(self, stream: str, row: dict) -> None:
        if stream not in {"observations", "actions", "episodes", "teacher"}:
            raise ValueError("unknown play evidence stream")
        with self._lock:
            if self._closed:
                raise RuntimeError("play journal is closed")
            handle = self._streams.get(stream)
            if handle is None:
                handle = (self.directory / f"play-{stream}.jsonl").open("a", encoding="utf-8")
                self._streams[stream] = handle
            handle.write(json.dumps({"schema": 1, "run_id": self.run_id, **row},
                                    separators=(",", ":"), allow_nan=False) + "\n")
            handle.flush()

    def observation(self, value: dict) -> None:
        with self._lock:
            if value["id"] not in self._seen:
                self.append("observations", value)
                self._seen.add(value["id"])

    def close(self):
        with self._lock:
            self._closed = True
            for handle in self._streams.values():
                handle.close()
            self._streams.clear()
