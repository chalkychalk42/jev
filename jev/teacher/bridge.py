"""Nonblocking live bridge to the existing teacher queue.

Only its event-loop thread calls the subscription. The supervisor drains transport rows
on the recorder's owning thread. A SQLite reservation counts every real attempt across
restarts; a broken budget store fails closed without disturbing the scripted body.
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from jev.guide.graph import Graph
from jev.learn.episode import DecisionRow, Recorder
from jev.teacher.client import TeacherClient, TeacherResult
from jev.teacher.prompt import context_for
from jev.teacher.queue import TeacherQueue
from jev.world.state_v1 import ArmedBy


class BudgetClient:
    def __init__(self, client: TeacherClient, path: Path, *, calls_per_hour: int = 12,
                 interval_s: float = 60):
        if calls_per_hour < 1 or interval_s < 0:
            raise ValueError("positive teacher budget and nonnegative interval required")
        self.client, self.path = client, Path(path)
        self.calls_per_hour, self.interval_s = calls_per_hour, interval_s
        self.model_name = client.model_name

    def reserve(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=1) as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (t REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE t <= ?", (now - 3600,))
            count, latest = db.execute("SELECT COUNT(*), MAX(t) FROM attempts").fetchone()
            if count >= self.calls_per_hour or (latest is not None and now - latest < self.interval_s):
                return False
            db.execute("INSERT INTO attempts(t) VALUES (?)", (now,))
        return True

    async def ask(self, prompt: str, *, timeout_s: float | None = None) -> TeacherResult:
        try:
            permitted = self.reserve()
        except (OSError, sqlite3.Error):
            return TeacherResult("abstained", model=self.model_name, detail="teacher budget unavailable")
        if not permitted:
            return TeacherResult("abstained", model=self.model_name, detail="teacher call budget reached")
        return await self.client.ask(prompt, timeout_s=timeout_s)


class _Sink:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.rows: queue.SimpleQueue[DecisionRow] = queue.SimpleQueue()

    def decision(self, row: DecisionRow) -> None:
        self.rows.put(row)


class TeacherBridge:
    def __init__(self, client: TeacherClient, graph: Graph, recorder: Recorder,
                 catalog: frozenset[str], *, budget_path: Path,
                 calls_per_hour: int = 12, interval_s: float = 60,
                 max_pending: int = 2, retry_after_s: float = 300):
        if max_pending < 1 or retry_after_s < 0:
            raise ValueError("invalid bridge bounds")
        self.graph, self.recorder = graph, recorder
        self.max_pending, self.retry_after_s = max_pending, retry_after_s
        self._sink = _Sink(recorder.run_id)
        budget = BudgetClient(client, budget_path, calls_per_hour=calls_per_hour,
                              interval_s=interval_s)
        self.teacher = TeacherQueue(budget, catalog=catalog, sink=self._sink,
                                    maxsize=max_pending, max_attempts=1)
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._recent: dict[str, float] = {}
        self._answers: dict = {}
        self._closed = False
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="jev-teacher", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self._closed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
            raise RuntimeError("teacher bridge failed to start")

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def ask(self, state, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if (self._closed or key in self._pending or key in self._answers
                    or len(self._pending) + len(self._answers) >= self.max_pending
                    or now - self._recent.get(key, -float("inf")) < self.retry_after_s):
                return False
            self._recent = {k: t for k, t in self._recent.items() if now - t < self.retry_after_s}
            self._pending.add(key)
            self._recent[key] = now
        asyncio.run_coroutine_threadsafe(self._ask(state, key, self.recorder._tick_id + 1), self._loop)
        return True

    async def _ask(self, state, key, tick_id):
        try:
            node = self.graph.get(state.guide.step_id or "")
            answer = await self.teacher.ask(state, tick_id=tick_id,
                                            context=context_for(node, self.graph) if node else None)
            if answer.reply is not None:
                # A rejected action never becomes armable merely by crossing threads.
                reply = answer.reply if answer.armable else answer.reply.model_copy(update={"decision": None})
                with self._lock:
                    self._answers[key] = reply
        except Exception as exc:
            self._sink.decision(DecisionRow(
                run_id=self.recorder.run_id, decision_id=f"bridge-{uuid.uuid4().hex}",
                tick_id=tick_id, t=state.t, client_id=state.client_id, situation_key=key,
                author=ArmedBy.TEACHER, model=self.teacher.client.model_name,
                intent=None, skill=None, status="transport",
                why=f"{type(exc).__name__}: {exc}"[:280],
            ))
        finally:
            with self._lock:
                self._pending.discard(key)

    def take(self, key):
        with self._lock:
            return self._answers.pop(key, None)

    def poll(self) -> None:
        """Call from the recorder thread even while a body skill is running."""
        while True:
            try:
                row = self._sink.rows.get_nowait()
            except queue.Empty:
                return
            self.recorder.decision(row)

    async def _shutdown(self):
        await self.teacher.aclose()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
            self.poll()
