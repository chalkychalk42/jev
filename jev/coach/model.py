"""Jev, the coach's decision model: typed choices over HTTP, never in the way of the floor.

`docs/PLAN.md` §9 puts Jev at the centre of the coach: at every event it picks what to do
next among what is possible now, and "Jev only arms skills and moves the playhead". The
scripted coach (`jev.coach.policy`) was built as the floor under it and Jev itself was never
called until 28 September: this is the call. The model is TypeSafe's System One,
`POST /v1/systemone` with `{state, model, questions}`, each question a `choice` among named
options whose descriptions are the prompt; its answer names one option with the
probabilities of all (probed 28 September: `jev-1.13.0`, 386 ms, 450 input tokens).

Every call has one of these outcomes, never collapsed into "no answer" (as the teacher's,
`jev.teacher.client`): `ok`, `timeout`, `error` (transport, HTTP, an answer that names no
option offered), `off` (no credential, or switched off) and `budget` (the day's spend is
used up). Anything but `ok` leaves the decision to the scripted floor, which is always
valid on its own (`jev.coach.policy`): the model upgrades a decision, it is never needed
for one. Nothing is retried on the hot path; a decision that waits is a character standing.

The spend is capped per day across every process sharing one `var/` (the hive's bots, the
live session): each call's tokens are added to a counter under a file lock, at the list
price. Each call is also written, with the state it saw and what it said, beside the run's
other records, so the outcome it led to can grade it later (V9: label by outcome).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jev.persist import atomic_json, file_lock

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
USD_PER_MTOK = 0.04
# A decision the coach waits for stands the character still: one that has not come in this
# long is left to the floor. Measured 386 ms for a small state (28 September).
TIMEOUT_S = 1.5
# What the model is allowed to cost a day, in dollars, unless the settings say otherwise.
USD_PER_DAY = 10.0
KEY_NAME = "TYPESAFE_API_KEY"
SETTINGS = "coach-model.json"
USAGE = "coach-model-usage.json"


@dataclass(frozen=True)
class Answer:
    """What one call came to. `choice` is set only when `status` is "ok"."""

    status: str                       # ok | timeout | error | off | budget
    choice: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    tokens: int = 0
    model: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.choice is not None


class Budget:
    """The day's spend, shared by every process writing `path`, under a file lock (the
    live session runs on Windows, the hive on Linux: `jev.persist.file_lock` is both)."""

    def __init__(self, path: Path, usd_per_day: float):
        self.path, self.usd_per_day = Path(path), float(usd_per_day)
        self.lock = self.path.with_suffix(".lock")
        self._local = threading.Lock()

    def _today(self) -> dict:
        day = time.strftime("%Y-%m-%d")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = {}
        return document if document.get("day") == day else {"day": day}

    def spent(self) -> float:
        return float(self._today().get("usd", 0.0))

    def allows(self) -> bool:
        return self.spent() < self.usd_per_day

    def add(self, tokens: int, status: str) -> None:
        try:
            with file_lock(self.lock):
                self._add(tokens, status)
        except OSError:
            # A lock the file system cannot give: Windows Python over the WSL share answers
            # "Resource deadlock avoided" (28 September), and the live session is the one
            # process writing its `var/`. Its own lock then; the count is the same.
            with self._local:
                self._add(tokens, status)

    def _add(self, tokens: int, status: str) -> None:
        document = self._today()
        document["calls"] = document.get("calls", 0) + 1
        document["tokens"] = document.get("tokens", 0) + max(0, int(tokens))
        document["usd"] = round(document["tokens"] / 1_000_000 * USD_PER_MTOK, 6)
        counts = document.setdefault("status", {})
        counts[status] = counts.get(status, 0) + 1
        atomic_json(self.path, document)


class CoachModel:
    """One client for every decision a process asks of Jev."""

    def __init__(self, api_key: str | None, *, budget: Budget | None = None,
                 model: str = MODEL, url: str = URL, timeout_s: float = TIMEOUT_S,
                 record: Path | None = None, transport: httpx.BaseTransport | None = None,
                 clock=time.time):
        self.api_key, self.model, self.url = api_key, model, url
        self.timeout_s, self.budget, self.clock = timeout_s, budget, clock
        self.record_path = Path(record) if record is not None else None
        self._record_lock = threading.Lock()
        self._http = (httpx.Client(timeout=timeout_s, transport=transport, headers={
            "authorization": f"Bearer {api_key}", "content-type": "application/json"})
            if api_key else None)

    @property
    def available(self) -> bool:
        return self._http is not None and (self.budget is None or self.budget.allows())

    def choose(self, point: str, state: dict[str, Any], instructions: str,
               options: dict[str, str], *, record: Path | None = None,
               note: dict | None = None) -> Answer:
        """One choice among `options` (name -> what it means, with its evidence) at
        decision point `point`, given `state`; written to `record` (else the client's own)
        with `note` beside it (what the scripted coach would have done: never sent)."""
        done = lambda answer: self._done(point, state, options, answer, record, note)  # noqa: E731
        if self._http is None:
            return done(Answer("off", detail="no credential"))
        if self.budget is not None and not self.budget.allows():
            return done(Answer("budget", detail="day's spend used"))
        body = {"state": state, "model": self.model, "questions": {
            "choice": {"type": "choice", "instructions": instructions, "criteria": options}}}
        began = time.perf_counter()
        try:
            response = self._http.post(self.url, json=body)
        except httpx.TimeoutException:
            return done(Answer(
                "timeout", latency_ms=_ms(began), detail=f"no answer in {self.timeout_s} s"))
        except httpx.HTTPError as exc:
            return done(Answer(
                "error", latency_ms=_ms(began), detail=f"transport: {type(exc).__name__}"))
        latency = _ms(began)
        if response.status_code != 200:
            return done(Answer(
                "error", latency_ms=latency, detail=f"HTTP {response.status_code}"))
        try:
            document = response.json()
        except ValueError:
            return done(Answer(
                "error", latency_ms=latency, detail="the reply is not JSON"))
        usage = document.get("usage") if isinstance(document, dict) else None
        tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        tokens = tokens if type(tokens) is int and tokens >= 0 else 0
        answer = ((document.get("answers") or {}).get("choice")
                  if isinstance(document, dict) else None)
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if choice not in options:
            return done(Answer(
                "error", latency_ms=latency, tokens=tokens,
                detail="the answer names no option offered"))
        probabilities = {key: float(value) for key, value in
                         (answer.get("probabilities") or {}).items()
                         if key in options and isinstance(value, (int, float))}
        confidence = answer.get("confidence")
        return done(Answer(
            "ok", choice=choice, latency_ms=latency, tokens=tokens,
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            probabilities=probabilities, model=str(document.get("model") or self.model)))

    def _done(self, point: str, state: dict, options: dict, answer: Answer,
              record: Path | None = None, note: dict | None = None) -> Answer:
        if self.budget is not None and answer.status in ("ok", "error", "timeout"):
            try:
                self.budget.add(answer.tokens, answer.status)
            except (OSError, ValueError):
                pass              # the count is lost, the decision is not (the cap reads on)
        path = Path(record) if record is not None else self.record_path
        if path is not None:
            row = {"t": self.clock(), "point": point, "state": state,
                   "options": options, **(note or {}), **asdict(answer)}
            with self._record_lock, path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        return answer

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


def _ms(began: float) -> float:
    return round((time.perf_counter() - began) * 1000.0, 1)


def settings(var: Path) -> dict[str, Any]:
    """`var/coach-model.json`: whether Jev decides (`enabled`), what it may spend a day
    (`usd_per_day`), and whether it picks a fight's attacks (`combat`). No file is Jev
    switched on at the default cap, fights included."""
    path = Path(var) / SETTINGS
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        document = {}
    return {"enabled": bool(document.get("enabled", True)),
            "usd_per_day": float(document.get("usd_per_day", USD_PER_DAY)),
            "model": str(document.get("model", MODEL)),
            # Whether it picks the attacks in a fight too (V298).
            "combat": bool(document.get("combat", True))}


def open_model(var: Path, env_file: Path | None, *, record: Path | None = None,
               say=print) -> CoachModel | None:
    """The coach's model as `var`'s settings have it, or `None` when switched off. A missing
    credential is said once and is no model: the floor plays on."""
    from jev.play.providers import credential

    config = settings(var)
    if not config["enabled"]:
        say("coach model: switched off (coach-model.json)")
        return None
    try:
        key = credential(KEY_NAME, env_file)
    except ValueError:
        say(f"coach model: no {KEY_NAME}; the scripted coach decides alone")
        return None
    budget = Budget(Path(var) / USAGE, config["usd_per_day"])
    say(f"coach model: {config['model']}, ${budget.spent():.2f} of "
        f"${config['usd_per_day']:.2f} spent today")
    return CoachModel(key, budget=budget, model=config["model"], record=record)
