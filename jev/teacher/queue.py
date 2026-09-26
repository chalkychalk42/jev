"""One queue, one worker, for the whole farm.

`ARCHITECTURE.md` §3: the teacher is `claude -p` over a subscription, latency is seconds,
and the rate limit is a single window shared by every client. So there is exactly one
worker here and there is no configuration option to add a second. Ten clients do not get
ten times the teacher; they get ten times the *questions*, which is why most of this file
is about not asking.

The four ways a question does not become a call, cheapest first:

1. **Cache** — a recent answer for this `situation_key` is reused without asking at all.
2. **Dedup** — three clients stuck on one step is one question (`ARCHITECTURE.md` §2). The
   later askers attach to the in-flight job and their `DecisionRow.dedup_of` points at the
   original, so the corpus knows it was one answer and not three agreeing models.
3. **Drop** — the queue is bounded, and overflow drops instead of growing. A stale question
   answered five minutes late is worse than one never asked, because the situation has
   moved on and the scripted coach was always available and always free.
4. **Retry, but bounded** — a request that never completed is not a decision, so it is
   retried; a request that has failed `max_attempts` times is recorded as having failed and
   the caller falls back.

Everything is recorded
----------------------
A `DecisionRow` is written for every outcome, including the calls that never happened:
cache hits, dedup coalesces, drops, timeouts, abstentions and verifier rejections. **A call
that was never made is still a fact about the run** — it is how `unresolved/h` gets its
denominator, and how the cost saving from `situation_key` becomes a number somebody can
check rather than a claim in a design document.

A verifier refusal is `status="rejected"`, and it is never a reason to stop. The caller
gets `fallback_reason` naming the rule that refused, arms its scripted default, and the run
continues (`jev/coach/verifier.py`).

Three places this deliberately does not follow the obvious design
-----------------------------------------------------------------
**Overflow drops the oldest queued question, not the newest.** The obvious version refuses
the new arrival, but then the worker spends the subscription answering the stalest question
in the building. The reason overflow drops at all is staleness, so it drops the stalest.

**The verifier runs per requester, not once per answer.** Coalesced askers and cache hits
re-verify against their own state. The bin says the same answer is correct for both states;
it does not say the same character is standing in the same place, and the verifier is
local, deterministic and free.

**A drop is recorded as `status="transport"`, not a sixth status.** `Status` is owned by
`jev.coach.schema` and the whole corpus agrees on its five values; a drop *is* a transport
failure that happened inside our own process — the question never left the building — and
`DecisionRow.why` says which one it was. Five values everything understands beat six that
only this module does. `Answer.dropped` tells a live caller apart without the schema change.

Artifact payloads are retained in every DecisionRow, including refused or stale actions.
They are proposals for review; receiving text never installs code or changes the graph.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import pydantic

from jev.coach.schema import Artifact, Decision, Status, TeacherReply, Verdict
from jev.coach.situation import situation_key as compute_situation_key
from jev.coach.verifier import verify
from jev.learn.episode import DecisionRow
from jev.teacher.cache import AnswerCache
from jev.teacher.client import TeacherClient, TeacherResult
from jev.teacher.prompt import PromptContext, build_prompt
from jev.world.state_v1 import ArmedBy, State

# Depth, not "enough". One worker at the observed few-seconds-per-call means a depth of 8 is
# roughly 40 s of backlog, and `jev.coach.situation` rebins step age at 60 s: past that, the
# answer arrives about a situation that is already in a different bucket. So the bound is
# chosen as "the most questions that can still be answered while they are still the question
# being asked", not as a memory figure.
DEFAULT_MAXSIZE = 8

# Three attempts, because the failures worth retrying are transient by nature — a killed
# child, a 429, a reply that did not parse — and a fourth attempt on a shared window costs
# every other client in the farm. Two felt like one bad moment away from giving up.
DEFAULT_MAX_ATTEMPTS = 3

# Short and linear. The worker is single, so backoff is dead time for the whole farm; long
# enough to let a rate limit breathe, short enough that three attempts stay inside the 60 s
# window the answer has to be useful in.
DEFAULT_RETRY_BACKOFF_S = 2.0

# The in-memory row window is a debugging convenience, not the corpus. The corpus is the
# recorder, which flushes per row to disk because runs normally end in a crash.
ROW_WINDOW = 512


class DecisionSink(Protocol):
    """Where rows go. `jev.learn.episode.Recorder` satisfies this structurally, which is the
    point: the queue must not import a file writer to be testable."""

    run_id: str

    def decision(self, row: DecisionRow) -> None:
        ...


@dataclass(frozen=True)
class Answer:
    """What a caller gets back. Never `None`, for any outcome, ever.

    Returning `None` on failure is the bug `ARCHITECTURE.md` §6 is about: the caller cannot
    tell a timeout from an abstention from a refusal, and every one of them looks like the
    teacher saying stop.
    """

    decision_id: str
    situation_key: str
    status: Status
    reply: TeacherReply | None = None
    decision: Decision | None = None
    verdict: Verdict | None = None
    cache_hit: bool = False
    dedup_of: str | None = None
    dropped: bool = False
    detail: str | None = None
    latency_ms: float | None = None
    attempts: int = 0

    @property
    def armable(self) -> bool:
        """There is a decision here and the verifier accepted it. Only then may it be armed."""
        return self.status == "ok" and self.decision is not None

    @property
    def artifacts(self) -> list[Artifact]:
        """Durable output, available even when the decision was refused.

        The verifier judges the immediate action, not the skill draft. `DECISIONS.md` V11
        says the artifact is the part worth having, so a rejection must not throw it away.
        """
        return list(self.reply.artifacts) if self.reply is not None else []

    @property
    def fallback_reason(self) -> str | None:
        """`None` exactly when `armable`. Otherwise a specific line the caller can log
        before arming its scripted default — "the teacher did not answer" is not specific
        enough to debug at 3am."""
        if self.armable:
            return None
        if self.dropped:
            return f"dropped: {self.detail or 'queue full'}"
        if self.status == "ok":
            return "artifact only: no immediate action was proposed"
        return f"{self.status}: {self.detail or 'no detail'}"


@dataclass
class Counters:
    """The eval board's teacher row.

    Two families, and they deliberately do not sum to one another:

      * **per call** — `calls`, `retries`, `timeouts`, `transport_failures`, `abstentions`.
        These count attempts against the shared subscription window, so a question retried
        three times is three calls. This family is the bill.
      * **per question** — `asks`, `cache_hits`, `dedup_coalesced`, `drops`, `rejections`,
        `ok`. These count questions asked by clients. This family is the workload.

    The gap between `asks` and `calls` is the entire argument for `situation_key`, which is
    why both are here rather than one derived number.
    """

    asks: int = 0
    calls: int = 0
    retries: int = 0
    cache_hits: int = 0
    dedup_coalesced: int = 0
    drops: int = 0
    ok: int = 0
    timeouts: int = 0
    transport_failures: int = 0
    abstentions: int = 0
    rejections: int = 0
    latency_ms_total: float = 0.0
    latency_samples: int = 0

    @property
    def mean_latency_ms(self) -> float:
        return self.latency_ms_total / self.latency_samples if self.latency_samples else 0.0

    @property
    def calls_saved(self) -> int:
        """Questions that did not become calls. The headline saving."""
        return self.cache_hits + self.dedup_coalesced

    def as_dict(self) -> dict[str, float | int]:
        return {
            "asks": self.asks,
            "calls": self.calls,
            "retries": self.retries,
            "cache_hits": self.cache_hits,
            "dedup_coalesced": self.dedup_coalesced,
            "calls_saved": self.calls_saved,
            "drops": self.drops,
            "ok": self.ok,
            "timeouts": self.timeouts,
            "transport_failures": self.transport_failures,
            "abstentions": self.abstentions,
            "rejections": self.rejections,
            "mean_latency_ms": round(self.mean_latency_ms, 1),
        }


@dataclass(frozen=True)
class _Outcome:
    """The shared part of an answer: what the transport produced, before any one client's
    state is applied to it."""

    status: Status
    reply: TeacherReply | None = None
    result: TeacherResult | None = None
    detail: str | None = None
    attempts: int = 0
    # Carried rather than inferred from `detail`: every requester behind a dropped job —
    # the origin and anyone coalesced onto it — has to be able to say "dropped" rather
    # than "transport failure", and matching on a message string to find that out is the
    # kind of coupling that survives until somebody improves the wording.
    dropped: bool = False


@dataclass
class _Job:
    """One question in flight, however many clients are behind it."""

    situation_key: str
    prompt: str
    origin_decision_id: str
    future: asyncio.Future[_Outcome]


def _extract_json(text: str) -> str:
    """Find the JSON object in a reply that was told not to wrap it.

    The contract says no fence, but "the contract said not to" is not a guarantee, and a
    fenced-but-correct answer is a call already paid for. Outermost braces, because a
    payload can contain nested objects and the last `}` is the real end.
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    return s[start : end + 1] if start != -1 and end > start else s


def _parse_reply(text: str) -> tuple[TeacherReply | None, str | None]:
    try:
        return TeacherReply.model_validate_json(_extract_json(text)), None
    except pydantic.ValidationError as exc:
        return None, f"reply did not validate: {str(exc)[:240]}"
    except ValueError as exc:
        return None, f"reply was not json: {str(exc)[:240]}"


class TeacherQueue:
    """Single-worker, bounded, deduplicating, caching, recording."""

    def __init__(
        self,
        client: TeacherClient,
        *,
        catalog: frozenset[str],
        cache: AnswerCache | None = None,
        sink: DecisionSink | None = None,
        run_id: str | None = None,
        maxsize: int = DEFAULT_MAXSIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        timeout_s: float | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1; a queue that holds nothing drops everything")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1; a teacher that never calls is not one")
        # Required rather than defaulted: the verifier refuses any skill outside the
        # catalog, so an empty default would silently reject every answer the teacher gave
        # and look like the model being useless.
        self.client = client
        self.catalog = catalog
        self.cache = cache if cache is not None else AnswerCache()
        self.sink = sink
        self.run_id = run_id or (sink.run_id if sink is not None else f"teacher-{uuid.uuid4().hex[:8]}")
        self.max_attempts = max_attempts
        self.retry_backoff_s = retry_backoff_s
        self.timeout_s = timeout_s
        self.counters = Counters()
        self.rows: deque[DecisionRow] = deque(maxlen=ROW_WINDOW)

        self._q: asyncio.Queue[_Job] = asyncio.Queue(maxsize=maxsize)
        self._pending: dict[str, _Job] = {}
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Idempotent. Also called lazily by `ask`, because a queue that silently hangs
        when somebody forgot to start it is exactly the class of failure this codebase
        refuses to ship."""
        if self._worker is None or self._worker.done():
            self._closed = False
            self._worker = asyncio.create_task(self._run(), name="teacher-queue")

    async def aclose(self) -> None:
        """Stop the worker and resolve everything still waiting.

        Draining matters: a caller awaiting a job whose worker was cancelled would wait
        forever, and a client blocked on the teacher is a client not playing the game.
        """
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        while True:
            try:
                job = self._q.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._resolve(job, _Outcome(status="transport", detail="queue closed"))
            self._q.task_done()
        for job in list(self._pending.values()):
            self._resolve(job, _Outcome(status="transport", detail="queue closed"))
        self._pending.clear()

    async def drain(self) -> None:
        """Wait for everything queued to be answered. Tests and shutdown, not the hot path."""
        await self._q.join()

    async def __aenter__(self) -> TeacherQueue:
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def depth(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------------------------ the ask

    async def ask(
        self,
        state: State,
        *,
        prompt: str | None = None,
        context: PromptContext | None = None,
        client_id: str | None = None,
        tick_id: int = 0,
    ) -> Answer:
        """Ask the teacher about `state`. Returns for every outcome; never raises for one."""
        self.counters.asks += 1
        key = state.situation_key or compute_situation_key(state)
        who = client_id or state.client_id

        cached = self.cache.get(key)
        if cached is not None:
            self.counters.cache_hits += 1
            return self._finish(
                state=state,
                client_id=who,
                tick_id=tick_id,
                situation_key=key,
                outcome=_Outcome(status="ok", reply=cached.reply, detail="cache"),
                cache_hit=True,
                # Points at the call that originally paid for this answer, so one answer
                # reused by four clients stays one answer in the corpus rather than looking
                # like four independent agreements.
                dedup_of=cached.decision_id,
                model=cached.model,
            )

        existing = self._pending.get(key)
        if existing is not None:
            self.counters.dedup_coalesced += 1
            outcome = await self._wait(existing)
            return self._finish(
                state=state,
                client_id=who,
                tick_id=tick_id,
                situation_key=key,
                outcome=outcome,
                dedup_of=existing.origin_decision_id,
                dropped=outcome.dropped,
            )

        if self._closed:
            return self._finish(
                state=state, client_id=who, tick_id=tick_id, situation_key=key,
                outcome=_Outcome(status="transport", detail="queue closed"),
            )

        job = _Job(
            situation_key=key,
            prompt=prompt if prompt is not None else build_prompt(state, self._context(context)),
            origin_decision_id=_new_id(),
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[key] = job
        self.start()
        self._enqueue(job)

        outcome = await self._wait(job)
        return self._finish(
            state=state, client_id=who, tick_id=tick_id, situation_key=key,
            outcome=outcome, decision_id=job.origin_decision_id, dropped=outcome.dropped,
        )

    def _context(self, context: PromptContext | None) -> PromptContext:
        """Hand the verifier's catalog to the prompt unless the caller already did. The
        verifier refuses any skill outside it, so withholding it means paying for answers
        that are rejected on arrival."""
        if context is None:
            return PromptContext(catalog=tuple(sorted(self.catalog)))
        if context.catalog:
            return context
        return PromptContext(
            active_step=context.active_step,
            next_nodes=context.next_nodes,
            skills=context.skills,
            events=context.events,
            death_postmortem=context.death_postmortem,
            question=context.question,
            catalog=tuple(sorted(self.catalog)),
        )

    def _enqueue(self, job: _Job) -> None:
        """Always admits `job`, making room by dropping the stalest question if it must.

        The newest question therefore cannot be dropped, which is the point: overflow exists
        because answers go stale, so the thing thrown away is the one that has been waiting
        longest. `drops` counts jobs, not waiters — three clients coalesced onto one dropped
        question were one question.
        """
        try:
            self._q.put_nowait(job)
            return
        except asyncio.QueueFull:
            pass
        # Safe because maxsize >= 1 is enforced in __init__, so a full queue has an item,
        # and nothing is awaited between the get and the put.
        stale = self._q.get_nowait()
        self._pending.pop(stale.situation_key, None)
        self.counters.drops += 1
        self._resolve(stale, _Outcome(status="transport", detail="queue full", dropped=True))
        self._q.task_done()
        self._q.put_nowait(job)

    async def _wait(self, job: _Job) -> _Outcome:
        return await asyncio.shield(job.future)

    # ------------------------------------------------------------------ the worker

    async def _run(self) -> None:
        while True:
            job = await self._q.get()
            try:
                outcome = await self._dispatch(job)
            except asyncio.CancelledError:
                self._pending.pop(job.situation_key, None)
                self._resolve(job, _Outcome(status="transport", detail="queue cancelled"))
                self._q.task_done()
                raise
            except Exception as exc:
                # Caught broadly on purpose: a worker that dies takes the farm's only
                # teacher with it, and every future ask then hangs on a queue nobody
                # drains. A bug here becomes one recorded transport failure.
                outcome = _Outcome(status="transport", detail=f"worker error: {exc!r}")
            # Unregistered before the waiters wake, so the next asker for this key starts a
            # fresh question instead of attaching to a job that has already finished.
            self._pending.pop(job.situation_key, None)
            self._resolve(job, outcome)
            self._q.task_done()

    async def _dispatch(self, job: _Job) -> _Outcome:
        """One question, up to `max_attempts` calls. Only an answer stops the loop."""
        last = _Outcome(status="transport", detail="no attempt was made")
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                self.counters.retries += 1
                if self.retry_backoff_s > 0:
                    await asyncio.sleep(self.retry_backoff_s * (attempt - 1))
            self.counters.calls += 1
            result = await self.client.ask(job.prompt, timeout_s=self.timeout_s)
            if result.latency_ms:
                self.counters.latency_ms_total += result.latency_ms
                self.counters.latency_samples += 1

            if result.status == "timeout":
                self.counters.timeouts += 1
                last = _Outcome("timeout", result=result, detail=result.detail, attempts=attempt)
                continue
            if result.status == "transport":
                self.counters.transport_failures += 1
                last = _Outcome("transport", result=result, detail=result.detail, attempts=attempt)
                continue
            if result.status == "abstained" or not result.text:
                # A well-formed reply that says nothing. ARCHITECTURE.md §6: an abstention
                # with no evidence yet is retryable, and it is not an instruction to stop.
                self.counters.abstentions += 1
                last = _Outcome("abstained", result=result, detail=result.detail or "empty reply",
                                attempts=attempt)
                continue

            reply, error = _parse_reply(result.text)
            if reply is None:
                # Classified transport rather than abstention on purpose: an abstention is a
                # choice the model made and we observed. A reply we could not parse is not a
                # choice we observed, it is an answer that did not survive the trip.
                self.counters.transport_failures += 1
                last = _Outcome("transport", result=result, detail=error, attempts=attempt)
                continue
            if reply.is_empty():
                self.counters.abstentions += 1
                last = _Outcome("abstained", reply=reply, result=result,
                                detail="no artifact and no decision", attempts=attempt)
                continue

            return _Outcome("ok", reply=reply, result=result, attempts=attempt)
        return last

    def _resolve(self, job: _Job, outcome: _Outcome) -> None:
        if not job.future.done():
            job.future.set_result(outcome)

    # ------------------------------------------------------------------ recording

    def _finish(
        self,
        *,
        state: State,
        client_id: str,
        tick_id: int,
        situation_key: str,
        outcome: _Outcome,
        cache_hit: bool = False,
        dedup_of: str | None = None,
        decision_id: str | None = None,
        dropped: bool = False,
        model: str | None = None,
    ) -> Answer:
        """Apply one client's state to a shared outcome, record it, and hand it back.

        Verification happens here rather than in the worker because it is per client: the
        bucket says the same answer is correct for both, not that both characters are in the
        same zone, alive, and out of combat.
        """
        did = decision_id or _new_id()
        status: Status = outcome.status
        reply = outcome.reply
        decision = reply.decision if reply is not None else None
        verdict: Verdict | None = None
        detail = outcome.detail

        if status == "ok" and decision is not None:
            verdict = verify(decision, state, self.catalog)
            if not verdict.ok:
                status = "rejected"
                self.counters.rejections += 1
                detail = f"{verdict.rule}: {verdict.reason}"
                # Nothing is cached and nothing already cached is invalidated. A refusal is
                # often about *this* client — its zone, its combat state — so caching it
                # would serve a refusal to everyone at cache speed, and invalidating on it
                # would cost every other client in the bucket a call to learn the same.
        if status == "ok":
            self.counters.ok += 1
            if reply is not None and not cache_hit and dedup_of is None:
                self.cache.put(
                    situation_key, reply, decision_id=did,
                    model=model or (outcome.result.model if outcome.result else None),
                )

        result = outcome.result
        armable = status == "ok" and decision is not None
        row = DecisionRow(
            run_id=self.run_id,
            decision_id=did,
            tick_id=tick_id,
            t=time.time(),
            client_id=client_id,
            situation_key=situation_key,
            author=ArmedBy.TEACHER,
            model=model or (result.model if result else (self.client.model_name if cache_hit else None)),
            intent=str(decision.intent) if decision is not None else None,
            skill=decision.skill if decision is not None else None,
            params=dict(decision.params) if decision is not None else {},
            confidence=(decision.confidence if decision is not None
                        else (reply.confidence if reply is not None else None)),
            why=_why(status, decision, reply, detail, dropped),
            status=status,
            verifier_verdict=(None if verdict is None else ("accept" if verdict.ok else "refuse")),
            verifier_reason=(None if verdict is None or verdict.ok else verdict.reason),
            latency_ms=result.latency_ms if result else None,
            tokens_in=result.tokens_in if result else None,
            tokens_out=result.tokens_out if result else None,
            cache_hit=cache_hit,
            dedup_of=dedup_of,
            artifacts=[a.model_dump(mode="json") for a in reply.artifacts] if reply else [],
        )
        self._emit(row)

        return Answer(
            decision_id=did,
            situation_key=situation_key,
            status=status,
            reply=reply,
            decision=decision if armable else None,
            verdict=verdict,
            cache_hit=cache_hit,
            dedup_of=dedup_of,
            dropped=dropped,
            detail=detail,
            latency_ms=result.latency_ms if result else None,
            attempts=outcome.attempts,
        )

    def _emit(self, row: DecisionRow) -> None:
        self.rows.append(row)
        if self.sink is not None:
            self.sink.decision(row)

    def stats(self) -> dict[str, float | int]:
        """One dict for the eval board: what the teacher cost and what it saved."""
        return {**self.counters.as_dict(), **self.cache.stats.as_dict(), "depth": self.depth}


def _why(
    status: Status,
    decision: Decision | None,
    reply: TeacherReply | None,
    detail: str | None,
    dropped: bool,
) -> str | None:
    """One line for the postmortem, and the only place a drop reason or an artifact list
    can live — `DecisionRow` has no column for either and it is owned elsewhere."""
    if dropped:
        return f"dropped: {detail or 'queue full'}"
    if status != "ok":
        return f"{status}: {detail}" if detail else status
    if decision is not None:
        return decision.why
    if reply is not None and reply.artifacts:
        return "artifacts: " + ", ".join(f"{a.kind}->{a.target}" for a in reply.artifacts)
    return detail


def _new_id() -> str:
    return uuid.uuid4().hex[:16]
