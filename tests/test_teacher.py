"""The teacher is expensive, shared and occasionally absent — and none of that stops a run.

Every test here is a claim about one of three things:

  * **cost** — that a question does not become a call when it does not have to
    (`situation_key` dedup and the answer cache, `ARCHITECTURE.md` §2)
  * **honesty** — that a timeout, a transport failure, an abstention and a refusal stay four
    different facts all the way into the corpus (`ARCHITECTURE.md` §6)
  * **survivability** — that none of those four ever looks like the teacher saying stop

No plugin: async scenarios are coroutines handed to `asyncio.run`, so `pytest` alone runs
this file. No network and no subprocess either, apart from one test that spawns a shim
script because "the child is killed on expiry" is the single behaviour a fake cannot prove.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from jev.coach.schema import Artifact, ArtifactKind, Decision, Intent, TeacherReply
from jev.coach.situation import situation_key
from jev.learn.episode import DecisionRow, Recorder, Stream, read
from jev.teacher.cache import AnswerCache
from jev.teacher.client import ClaudeSubscriptionClient, TeacherResult
from jev.teacher.prompt import (
    PROMPT_CHAR_CEILING,
    PromptContext,
    PromptTooLarge,
    SkillCard,
    StepNode,
    build_prompt,
    estimate_tokens,
    trim_state,
)
from jev.teacher.queue import TeacherQueue
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    Classification,
    Control,
    Flags,
    GuidePos,
    Objective,
    Pos,
    PowerType,
    Quest,
    Reaction,
    Sense,
    State,
    StepKind,
    Target,
    Ui,
    Vitals,
)

CATALOG = frozenset({"GRIND_UNTIL", "TRAVEL_TO", "VENDOR_REPAIR", "ACCEPT_QUEST", "LOOT"})


Scripted = TeacherResult | str | Callable[[str, int], TeacherResult]


@dataclass
class FakeClient:
    """A teacher that costs nothing. No network, no subprocess, no clock of its own.

    Every test in this file runs through this, which is the point: the queue's dedup,
    cache, retry and drop behaviour are decisions about *when* to call, and testing them
    against a real subscription would be both slow and a bill. It lived in
    `jev/teacher/client.py` until V229; only tests ever used it.

    A bare `str` is sugar for a successful reply. Replies are consumed in order; once the
    script runs out the last one repeats, so "always times out" is a one-element script.
    """

    replies: Sequence[Scripted] = ()
    model_name: str = "fake"
    delay_s: float = 0.0
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    async def ask(self, prompt: str, *, timeout_s: float | None = None) -> TeacherResult:
        self.prompts.append(prompt)
        n = self.calls
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not self.replies:
            return TeacherResult(status="transport", model=self.model_name, detail="no script")
        item = self.replies[min(n, len(self.replies) - 1)]
        if callable(item):
            return item(prompt, n)
        if isinstance(item, str):
            return TeacherResult(status="ok", text=item, model=self.model_name, latency_ms=1.0)
        return item


def run(coro):
    return asyncio.run(coro)


def reply_json(**over: Any) -> str:
    """A well-formed `TeacherReply` with both an artifact and an armable decision."""
    doc: dict[str, Any] = {
        "artifacts": [
            {
                "kind": "on_fail_edge",
                "target": "s1",
                "payload": {"to": "s1_skip", "after_s": 420},
                "rationale": "the step has timed out twice on two clients",
            }
        ],
        "decision": {
            "goal": "advance:s1",
            "intent": "grind_rib",
            "skill": "GRIND_UNTIL",
            "params": {"until": "step_or_level_12"},
            "abort_if": ["dead", "stuck_s>8"],
            "confidence": 0.72,
            "why": "objective not ticking; grind two minutes then recheck",
        },
        "confidence": 0.7,
    }
    doc.update(over)
    return json.dumps(doc)


ARTIFACT_ONLY = json.dumps(
    {
        "artifacts": [
            {
                "kind": "skill_draft",
                "target": "PULL_SINGLE_CASTER",
                "payload": {"steps": ["face", "cast", "back off"]},
                "rationale": "the adds come from the caster pair, not the patrol",
            }
        ],
        "decision": None,
    }
)

EMPTY_REPLY = json.dumps({"artifacts": [], "decision": None})

UNCATALOGUED = reply_json(
    decision={
        "goal": "advance:s1",
        "intent": "advance",
        "skill": "TELEPORT_TO_STORMWIND",
        "params": {},
        "abort_if": ["dead"],
        "confidence": 0.9,
        "why": "just teleport there",
    }
)


def a_queue(client: FakeClient, **kw: Any) -> TeacherQueue:
    """Retries with no backoff: the backoff is dead time for the whole farm and a test that
    sleeps to prove a retry is a test that is slow and occasionally a liar."""
    kw.setdefault("retry_backoff_s", 0.0)
    return TeacherQueue(client, catalog=CATALOG, **kw)


# --------------------------------------------------------------------------- cost


def test_three_clients_stuck_on_one_step_is_one_call(state: State):
    """Prevents the farm paying N times for a question that has one answer.

    `ARCHITECTURE.md` §2: `situation_key` exists so N clients in the same circumstance are
    one question. If this regresses, ten clients on one bad step exhaust the subscription
    window in minutes and the failure looks like a rate limit rather than a missing join.
    """

    async def scenario():
        client = FakeClient(replies=[reply_json()], delay_s=0.01)
        async with a_queue(client) as q:
            return client, q, await asyncio.gather(
                q.ask(state, client_id="c01"),
                q.ask(state, client_id="c02"),
                q.ask(state, client_id="c03"),
            )

    client, q, answers = run(scenario())

    assert client.calls == 1, "three askers, one underlying call"
    assert all(a.armable for a in answers), "all three get the same usable answer"
    assert len({a.decision_id for a in answers}) == 3, "each client owns its own row"

    origins = [r for r in q.rows if r.dedup_of is None]
    coalesced = [r for r in q.rows if r.dedup_of is not None]
    assert len(origins) == 1 and len(coalesced) == 2
    assert {r.dedup_of for r in coalesced} == {origins[0].decision_id}
    assert q.counters.dedup_coalesced == 2


def test_a_cached_answer_produces_no_call_at_all(state: State):
    """Prevents the cache being a claim in a design document rather than a number.

    A hit must be recorded as `cache_hit` — the saving from `situation_key` is only an
    argument if somebody can check it on the eval board.
    """

    async def scenario():
        client = FakeClient(replies=[reply_json()])
        async with a_queue(client) as q:
            first = await q.ask(state, client_id="c01")
            second = await q.ask(state, client_id="c02")
            return client, q, first, second

    client, q, first, second = run(scenario())

    assert client.calls == 1, "the second question never reached the transport"
    assert not first.cache_hit and second.cache_hit
    assert second.armable and second.decision == first.decision
    assert q.rows[-1].cache_hit is True
    assert q.rows[-1].dedup_of == first.decision_id, "one answer stays one answer in the corpus"
    assert q.counters.cache_hits == 1


def test_a_stale_cached_answer_is_not_served(state: State):
    """Prevents a confidently wrong answer about a step the run has already left.

    The TTL is the difference between reusing an answer and repeating one. A stale answer is
    worse than none, because none falls back to the scripted coach and stale gets armed.
    """
    clock = [0.0]
    cache = AnswerCache(ttl_s=60.0, now=lambda: clock[0])

    async def scenario():
        client = FakeClient(replies=[reply_json(), reply_json()])
        async with a_queue(client, cache=cache) as q:
            await q.ask(state)
            clock[0] = 61.0
            await q.ask(state)
            return client

    client = run(scenario())
    assert client.calls == 2, "the expired answer was asked again rather than reused"
    assert cache.stats.expired == 1


def test_the_cache_is_bounded_and_evicts_the_least_recently_used():
    """Prevents the cache quietly becoming a second corpus nobody versioned or graded."""
    cache = AnswerCache(maxsize=2, ttl_s=1000.0, now=lambda: 0.0)
    reply = TeacherReply.model_validate_json(reply_json())
    for key in ("a", "b"):
        cache.put(key, reply, decision_id=key)
    cache.get("a")                       # touching 'a' makes 'b' the least recent
    cache.put("c", reply, decision_id="c")

    assert len(cache) == 2
    assert cache.keys() == ["a", "c"]
    assert cache.stats.evicted == 1


def test_an_abstention_is_never_cached():
    """Prevents "I do not know" being served at cache speed for a minute.

    An empty reply is an absence of an answer, and `ARCHITECTURE.md` §6 says an absence is
    not an answer — so it is certainly not one worth keeping.
    """
    cache = AnswerCache()
    with pytest.raises(ValueError, match="empty reply"):
        cache.put("k", TeacherReply(), decision_id="d")


def test_the_saving_is_visible_on_the_eval_board(state: State):
    """Prevents a cost argument that cannot be audited. `asks` minus `calls` is the saving."""

    async def scenario():
        client = FakeClient(replies=[reply_json()], delay_s=0.01)
        async with a_queue(client) as q:
            await asyncio.gather(*(q.ask(state, client_id=f"c{i}") for i in range(3)))
            await q.ask(state, client_id="c04")
            return q.stats()

    stats = run(scenario())
    assert stats["asks"] == 4
    assert stats["calls"] == 1
    assert stats["calls_saved"] == 3 == stats["dedup_coalesced"] + stats["cache_hits"]


# --------------------------------------------------------------------------- honesty


def test_a_call_that_timed_out_is_not_a_decision_to_stop(state: State):
    """Prevents the worst failure in the system: silence read as an instruction.

    `ARCHITECTURE.md` §6. A model that returned nothing has not told us to stop. The row
    must say `timeout`, carry no intent, and the caller must be told to fall back.
    """

    async def scenario():
        client = FakeClient(replies=[TeacherResult(status="timeout", detail="killed at 45.0s")])
        async with a_queue(client, max_attempts=2) as q:
            return client, q, await q.ask(state)

    client, q, answer = run(scenario())

    assert answer.status == "timeout"
    assert answer.decision is None and not answer.armable
    assert answer.fallback_reason and answer.fallback_reason.startswith("timeout")
    row = q.rows[-1]
    assert row.status == "timeout"
    assert row.intent is None and row.skill is None, "a timeout proposes nothing"
    assert client.calls == 2, "a request that never completed is retried, not believed"
    assert q.counters.timeouts == 2


def test_an_empty_reply_is_an_abstention_that_is_retried_not_obeyed(state: State):
    """Prevents a model that declined to answer being read as a model that answered 'no'.

    §6 again: an abstention with no evidence yet is retryable. The second attempt here
    succeeds, which is the whole reason retrying an abstention is worth doing.
    """

    async def scenario():
        client = FakeClient(replies=[EMPTY_REPLY, reply_json()])
        async with a_queue(client, max_attempts=3) as q:
            return client, q, await q.ask(state)

    client, q, answer = run(scenario())

    assert client.calls == 2
    assert q.counters.abstentions == 1
    assert answer.status == "ok" and answer.armable


def test_an_abstention_that_never_resolves_is_recorded_as_one(state: State):
    """Prevents an unanswered question being filed as an answer, or as a different failure.

    Four failure modes, four names. Collapsing them loses the only signal that says whether
    to retry, and makes a rate limit and a refusal look identical in the corpus.
    """

    async def scenario():
        client = FakeClient(replies=[EMPTY_REPLY])
        async with a_queue(client, max_attempts=2) as q:
            return client, q, await q.ask(state)

    client, q, answer = run(scenario())
    assert answer.status == "abstained"
    assert answer.decision is None, "an abstention is not obeyed"
    assert q.rows[-1].status == "abstained"
    assert client.calls == 2


def test_a_reply_the_verifier_refuses_is_rejected_and_the_caller_falls_back(state: State):
    """Prevents a refusal ending a run, and prevents it being recorded as a model failure.

    A rejection is a well-formed answer the local rules would not arm. The run continues on
    the scripted default, and the reason in the row names the rule rather than a vibe.
    """

    async def scenario():
        client = FakeClient(replies=[UNCATALOGUED])
        async with a_queue(client, max_attempts=3) as q:
            return client, q, await q.ask(state)

    client, q, answer = run(scenario())

    assert answer.status == "rejected"
    assert not answer.armable and answer.decision is None
    assert answer.fallback_reason and "skill_exists" in answer.fallback_reason
    row = q.rows[-1]
    assert row.status == "rejected"
    assert row.verifier_verdict == "refuse" and "TELEPORT_TO_STORMWIND" in (row.verifier_reason or "")
    assert row.skill == "TELEPORT_TO_STORMWIND", "the row says what was refused"
    assert client.calls == 1, "a refusal is ours, not the model's; asking again buys nothing"


def test_a_refused_decision_does_not_throw_away_its_artifacts(state: State):
    """Prevents `DECISIONS.md` V11's whole point being lost to a rule about the action.

    The verifier judges the immediate action. A skill draft or an `on_fail` edge in the same
    reply is the durable half, and it is the half that was worth the call.
    """

    async def scenario():
        async with a_queue(FakeClient(replies=[UNCATALOGUED])) as q:
            return await q.ask(state)

    answer = run(scenario())
    assert answer.status == "rejected"
    assert [a.kind for a in answer.artifacts] == [ArtifactKind.ON_FAIL_EDGE]


def test_an_artifact_only_reply_is_a_success_not_an_abstention(state: State):
    """Prevents the preferred outcome being filed as a failure.

    V11 asks for a durable artifact *first* and an action only as a fallback. A reply with a
    skill draft and no decision is the teacher doing exactly what was asked.
    """

    async def scenario():
        async with a_queue(FakeClient(replies=[ARTIFACT_ONLY])) as q:
            return q, await q.ask(state)

    q, answer = run(scenario())
    assert answer.status == "ok"
    assert not answer.armable, "there is no action to arm, and that is fine"
    assert answer.fallback_reason == "artifact only: no immediate action was proposed"
    assert [a.target for a in answer.artifacts] == ["PULL_SINGLE_CASTER"]
    assert q.rows[-1].why and "skill_draft->PULL_SINGLE_CASTER" in q.rows[-1].why


def test_a_reply_that_does_not_parse_is_a_transport_failure_not_a_choice(state: State):
    """Prevents a mangled answer being recorded as the model declining.

    An abstention is a choice we observed. A reply that did not survive the trip is not a
    choice at all, and the two must not share a bucket in the training corpus.
    """

    async def scenario():
        client = FakeClient(replies=["I think you should probably grind for a bit."])
        async with a_queue(client, max_attempts=2) as q:
            return client, q, await q.ask(state)

    client, q, answer = run(scenario())
    assert answer.status == "transport"
    assert q.counters.transport_failures == 2 and q.counters.abstentions == 0
    assert client.calls == 2


def test_a_fenced_reply_is_still_read(state: State):
    """Prevents paying for a correct answer and discarding it over punctuation.

    The prompt says no markdown fence. "The contract said not to" is not a guarantee, and
    the call has already been spent by the time the fence arrives.
    """

    async def scenario():
        async with a_queue(FakeClient(replies=["```json\n" + reply_json() + "\n```"])) as q:
            return await q.ask(state)

    assert run(scenario()).armable


# --------------------------------------------------------------------------- survivability


def test_queue_overflow_drops_instead_of_growing(state: State):
    """Prevents unbounded backlog, and prevents answering questions nobody is asking any more.

    One worker for the whole farm means a queue that grows is a queue that answers the
    stalest question in the building. Overflow drops the oldest, every asker still gets a
    reply, and the depth never exceeds the bound.
    """
    maxsize = 2
    n = 6

    async def scenario():
        client = FakeClient(replies=[reply_json()], delay_s=0.02)
        async with a_queue(client, maxsize=maxsize) as q:
            states = [
                state.model_copy(update={"guide": state.guide.model_copy(update={"step_id": f"s{i}"})})
                for i in range(n)
            ]
            answers = await asyncio.gather(*(q.ask(s, client_id=f"c{i}") for i, s in enumerate(states)))
            return client, q, answers

    client, q, answers = run(scenario())

    assert len({a.situation_key for a in answers}) == n, "distinct questions, so nothing deduplicated"
    assert len(answers) == n, "every asker got an answer object; nobody waits forever"
    assert q.depth <= maxsize, "the bound held"
    assert q.counters.drops > 0
    assert q.counters.drops + client.calls == n, "every question was either asked or dropped"

    dropped = [a for a in answers if a.dropped]
    assert len(dropped) == q.counters.drops
    assert all(a.status == "transport" for a in dropped)
    assert all(a.fallback_reason and a.fallback_reason.startswith("dropped") for a in dropped)
    dropped_rows = [r for r in q.rows if r.why and r.why.startswith("dropped")]
    assert len(dropped_rows) == q.counters.drops, "a dropped question is still a recorded fact"
    assert all(r.status == "transport" and r.intent is None for r in dropped_rows)


def test_every_outcome_writes_a_row_including_the_calls_never_made(state: State):
    """Prevents a corpus that only knows about the questions that worked.

    A call that was never made is still a fact about the run: it is how `unresolved/h` gets
    a denominator and how a rate-limited hour is told apart from a quiet one.
    """

    async def scenario():
        client = FakeClient(
            replies=[
                TeacherResult(status="timeout", detail="killed"),
                reply_json(),
                UNCATALOGUED,
            ]
        )
        async with a_queue(client, max_attempts=1) as q:
            timeout = await q.ask(state, client_id="c01")
            ok = await q.ask(state, client_id="c02")
            cached = await q.ask(state, client_id="c03")
            other = state.model_copy(
                update={"guide": state.guide.model_copy(update={"step_id": "s9"})}
            )
            rejected = await q.ask(other, client_id="c04")
            return q, (timeout, ok, cached, rejected)

    q, answers = run(scenario())
    assert [a.status for a in answers] == ["timeout", "ok", "ok", "rejected"]
    assert len(q.rows) == 4, "one row per outcome, not one row per success"
    assert [r.author for r in q.rows] == [ArmedBy.TEACHER] * 4
    assert [r.client_id for r in q.rows] == ["c01", "c02", "c03", "c04"]


def test_rows_round_trip_through_the_episode_store(state: State, tmp_path):
    """Prevents a row shape that only exists in memory.

    The decision stream is written by `jev.learn.episode`, and a `DecisionRow` the recorder
    cannot serialise is a row that vanishes at exactly the moment it is needed.
    """

    async def scenario(recorder: Recorder):
        async with a_queue(FakeClient(replies=[reply_json()]), sink=recorder) as q:
            await q.ask(state)

    with Recorder(root=tmp_path) as recorder:
        run(scenario(recorder))
        run_id = recorder.run_id

    rows = read(tmp_path / run_id / f"{Stream.DECISIONS.value}.jsonl")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["author"] == ArmedBy.TEACHER
    assert rows[0]["situation_key"] == situation_key(state)
    assert rows[0]["run_id"] == run_id, "the queue takes the recorder's run id, not its own"


def test_closing_the_queue_does_not_strand_a_waiting_client(state: State):
    """Prevents a shutdown that leaves a client blocked on a teacher that will never answer.

    A client waiting on the teacher is a client not playing the game, and a hang at shutdown
    is the version of that failure nobody sees until it is in a farm.
    """

    async def scenario():
        client = FakeClient(replies=[reply_json()], delay_s=5.0)
        q = a_queue(client, maxsize=2)
        q.start()
        task = asyncio.create_task(q.ask(state))
        await asyncio.sleep(0)
        await q.aclose()
        return await asyncio.wait_for(task, timeout=1.0)

    answer = run(scenario())
    assert answer.status == "transport" and not answer.armable
    # "closed" or "cancelled" depending on whether the worker had already picked the job
    # up; both are the queue naming itself, which is what the caller needs to log.
    assert answer.fallback_reason and "queue" in answer.fallback_reason


# --------------------------------------------------------------------------- transport


# The exact bodies the real CLI produced on 20 Sep 2026 (claude 2.1.267), trimmed of the
# fields nothing reads. Kept verbatim so a change in the CLI's output shape breaks a test
# here rather than silently turning every teacher call into a transport failure in a farm.
REAL_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "duration_ms": 1166,
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "api_error_status": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 36,
            "cache_creation_input_tokens": 7424,
            "cache_read_input_tokens": 13607,
        },
        "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 907, "outputTokens": 47}},
    }
)

REAL_RATE_LIMITED = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "You're out of usage credits. Switch to another model, or manage usage credits...",
        "api_error_status": 429,
        "terminal_reason": "api_error",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "modelUsage": {},
    }
)


def test_a_rate_limited_cli_is_a_transport_failure_not_an_answer():
    """Prevents an error message being handed to the schema as if it were model text.

    The real CLI exits non-zero *and* prints well-formed JSON with `subtype: "success"` and
    `is_error: true`. Trusting `subtype`, or `result`, turns "you are out of credits" into
    an unparseable decision and hides the only actionable cause.
    """
    client = ClaudeSubscriptionClient()
    result = client.classify(stdout=REAL_RATE_LIMITED, stderr="", returncode=1, latency_ms=494.0)

    assert result.status == "transport"
    assert result.text is None, "an error string is not model text"
    assert "429" in (result.detail or "")


def test_a_successful_cli_reply_carries_its_billed_token_counts():
    """Prevents a cost dashboard built on the wrong half of the CLI's accounting.

    `usage.input_tokens` counts only uncached input (10 on the probe) while `modelUsage`
    counts the whole billed input (907) for the same call. The bill is the second one.
    """
    result = ClaudeSubscriptionClient().classify(
        stdout=REAL_OK, stderr="", returncode=0, latency_ms=1166.0
    )
    assert result.status == "ok" and result.text == "ok"
    assert (result.tokens_in, result.tokens_out) == (907, 47)
    assert result.model == "claude-haiku-4-5-20251001", "record what answered, not what was asked"


def test_an_empty_cli_result_is_an_abstention_not_a_breakage():
    """Prevents a model that said nothing being filed alongside a model that was unreachable.

    One is retryable because the evidence may arrive; the other is retryable because the
    transport may recover. They are different postmortems.
    """
    body = json.dumps({"type": "result", "is_error": False, "result": "   "})
    result = ClaudeSubscriptionClient().classify(stdout=body, stderr="", returncode=0, latency_ms=5.0)
    assert result.status == "abstained" and result.text is None


def test_unreadable_cli_output_is_a_transport_failure():
    """Prevents a crashed or updated CLI looking like a model with nothing to say."""
    client = ClaudeSubscriptionClient()
    assert client.classify(stdout="", stderr="boom", returncode=127, latency_ms=1.0).status == "transport"
    assert client.classify(stdout="not json", stderr="", returncode=0, latency_ms=1.0).status == "transport"
    shape_moved = json.dumps({"type": "result", "is_error": False, "answer": "ok"})
    assert client.classify(stdout=shape_moved, stderr="", returncode=0, latency_ms=1.0).status == "transport"


def test_the_prompt_is_an_argument_and_never_a_command():
    """Prevents a prompt containing a backtick, a quote or a newline becoming shell input.

    The prompt carries game text and model output. Building argv as a list and never going
    through a shell is what makes that safe by construction rather than by escaping.
    """
    argv = ClaudeSubscriptionClient(binary="/usr/bin/claude").argv('a "quoted" `thing`\n; rm -rf /')
    assert argv[0] == "/usr/bin/claude"
    assert argv[-1] == 'a "quoted" `thing`\n; rm -rf /'
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--safe-mode" in argv, "no CLAUDE.md, skills or MCP servers in the teacher's context"


@pytest.mark.skipif(sys.platform == "win32", reason="posix shim script")
def test_a_timed_out_child_is_killed_rather_than_abandoned(tmp_path):
    """Prevents zombie children holding pipes open.

    An abandoned child keeps its file descriptors; after a few hundred questions that is a
    farm out of descriptors, and the postmortem points at the wrong module entirely. The
    shim writes a marker only if it survives, so the absence of the marker is the proof.
    """
    marker = tmp_path / "survived"
    shim = tmp_path / "slow"
    shim.write_text(f"#!/bin/sh\nsleep 0.4\necho alive > {marker}\n")
    shim.chmod(0o755)

    async def scenario():
        client = ClaudeSubscriptionClient(binary=str(shim), timeout_s=0.05)
        result = await client.ask("anything")
        await asyncio.sleep(0.6)   # long enough for an un-killed child to reach the marker
        return result

    result = run(scenario())
    assert result.status == "timeout"
    assert not marker.exists(), "the child kept running after the timeout"


def test_a_missing_binary_is_a_transport_failure_not_a_crash():
    """Prevents the teacher being absent taking the client process down with it.

    `ARCHITECTURE.md` §0: the system makes forward progress with zero teacher calls. That is
    only true if no teacher call can raise into the caller.
    """

    async def scenario():
        client = ClaudeSubscriptionClient(binary="/nonexistent/claude", timeout_s=1.0)
        return await client.ask("hello")

    result = run(scenario())
    assert result.status == "transport" and "spawn failed" in (result.detail or "")


# --------------------------------------------------------------------------- the prompt


def heavy_state() -> State:
    """A deliberately awkward state: full quest log, a target, a UI error, every optional
    field populated. If the ceiling holds here it holds in a run."""
    return State(
        t=1000.0,
        client_id="c01",
        char=Char(name="Jev01", cls="mage", race="human", faction="alliance", level=12, xp_pct=0.4213),
        pos=Pos(zone="Elwynn Forest", zone_id=12, sub="Goldshire", mx=0.4712, my=0.6233,
                facing=1.2, indoors=False, world=(-9449.1, 64.8, 56.0)),
        vitals=Vitals(hp=0.42, hp_max=420, power=0.1, power_max=900, power_type=PowerType.MANA,
                      combat=True, dead=False, ghost=False),
        flags=Flags(mounted=False, swimming=False),
        target=Target(has=True, name="Riverpaw Gnoll Brute", level=11, hp=0.8,
                      reaction=Reaction.HOSTILE, classification=Classification.ELITE,
                      attacking_me=True, in_melee=True, dist=4.2, screen_xy=(910.0, 220.0),
                      tapped_by_other=False),
        bags=Bags(free=1, durability_min=0.22, money_copper=41200),
        ui=Ui(loot=False, modal=False, error="You are too far away."),
        quests=tuple(
            Quest(
                quest_id=100 + i,
                title=f"A Quest With A Reasonably Long Title Number {i}",
                objectives=tuple(
                    Objective(text=f"Riverpaw Gnoll number {j} slain in the woods", have=j, need=8)
                    for j in range(4)
                ),
                complete=False,
            )
            for i in range(8)
        ),
        guide=GuidePos(graph_id="ally_human", step_id="ally_human_012_hogger",
                       kind=StepKind.QUEST_OBJECTIVE, age_s=430.0, on_route=True,
                       progress=0.25, deaths_on_step=2, attempts=3),
        sense=Sense(addon_ok=True, seq=9912, vision_conf=0.81),
        control=Control(armed_skill="GRIND_UNTIL", armed_by=ArmedBy.POLICY, armed_at=980.0,
                        s1_mode="combat"),
    )


def heavy_context() -> PromptContext:
    return PromptContext(
        active_step=StepNode("ally_human_012_hogger", "quest_objective",
                             "Kill Hogger in the Forest Canyon", "ally_human_012_skip"),
        next_nodes=tuple(StepNode(f"ally_human_{13 + i}", "travel", "Go somewhere far away")
                         for i in range(5)),
        skills=tuple(SkillCard(f"SKILL_{i}", 0.7 - i * 0.05,
                               "does a thing that takes a whole line to describe properly")
                     for i in range(12)),
        events=tuple(f"event {i}: something happened that was worth writing down" for i in range(9)),
        death_postmortem="\n".join(f"line {i}: how the character died and what was around"
                                   for i in range(40)),
        catalog=tuple(sorted(CATALOG)),
    )


def test_the_prompt_stays_under_its_ceiling_on_a_realistic_state():
    """Prevents PLAN §9.3's "no 40k-token dump" being an instruction nobody enforces.

    The prompt is the only part of the cost we control — the CLI adds ~21k tokens of system
    prompt before ours is seen — so it has to be bounded by construction, not by care.
    """
    text = build_prompt(heavy_state(), heavy_context())
    assert len(text) <= PROMPT_CHAR_CEILING
    assert estimate_tokens(text) <= PROMPT_CHAR_CEILING // 4 + 1
    assert "SCHEMA:" in text and "STATE:" in text, "the core survived the trimming"


def test_the_prompt_sheds_context_before_it_sheds_the_contract():
    """Prevents a budget met by truncating the thing that makes the reply parseable.

    A prompt without its schema buys a reply that fails validation: a call spent, nothing
    learned. The postmortem goes first, the next nodes last, and the core never goes.
    """
    text = build_prompt(heavy_state(), heavy_context(), ceiling=7000)
    assert len(text) <= 7000
    assert "SCHEMA:" in text and "STATE:" in text and "QUESTION:" in text
    assert "LAST_DEATH" not in text, "the postmortem is the first thing shed"
    assert "SKILLS:" in text and "ACTIVE_STEP:" in text, "and the rest survived it"


def test_an_impossible_budget_raises_rather_than_sending_something_enormous():
    """Prevents a silent overspend when a caller passes something huge.

    Truncating the core would produce a reply that cannot validate, so this is a bug to fix
    at the call site rather than a size to absorb at the transport.
    """
    with pytest.raises(PromptTooLarge):
        build_prompt(heavy_state(), heavy_context(), ceiling=1000)


def test_the_prompt_asks_for_a_durable_artifact_before_an_immediate_action():
    """Prevents `DECISIONS.md` V11 from being true in the schema and absent from the ask.

    The schema puts `artifacts` first, but a model reads the instruction, not the field
    order. A decision helps one client once; an `on_fail` edge helps every client forever.
    """
    text = build_prompt(heavy_state(), heavy_context())
    assert "DURABLE ARTIFACT" in text
    assert text.index("Prefer a DURABLE ARTIFACT") < text.index("Use `decision` only as the fallback")


def test_the_trimmed_state_drops_what_a_teacher_cannot_use():
    """Prevents pixels and coordinates riding along on a rate-limited window.

    Screen coordinates and map fractions are System 1's business. The teacher is asked what
    to do, never where to stand, so these are cost with no effect on the answer.
    """
    doc = trim_state(heavy_state())
    flat = json.dumps(doc)
    assert "screen_xy" not in flat and "910" not in flat
    assert "mx" not in doc["where"] and "my" not in doc["where"]
    assert "facing" not in flat and "world" not in flat
    assert "Jev01" not in flat, "the character's name changes no answer"
    assert doc["guide"]["step"] == "ally_human_012_hogger", "what the question is about survives"


def test_an_unobserved_field_is_omitted_and_never_rendered_as_false():
    """Prevents the prompt inventing an observation nobody made.

    `ARCHITECTURE.md` §6: unknown is not a negative fact. `mounted: false` in a prompt is a
    claim, and a teacher that reads it will plan around a mount that was never checked for.
    """
    blind = State(t=1.0, client_id="c01", flags=Flags(), vitals=Vitals())
    doc = trim_state(blind)
    assert "vitals" not in doc, "nothing about vitals was observed, so nothing is said"
    assert set(doc["flags_unobserved"]) >= {"MOUNTED", "SWIMMING"}
    assert "flags" not in doc, "no flag was positively observed"
    assert "NOT OBSERVED" in build_prompt(blind), "and the reader is told what absence means"


def test_the_prompt_carries_the_catalog_the_verifier_will_judge_against(state: State):
    """Prevents paying for answers that are refused on arrival.

    `skill_exists` is the first verifier rule. A teacher that cannot see the catalog invents
    plausible skill names, and every one of them costs a call and produces a rejection.
    """

    async def scenario():
        client = FakeClient(replies=[reply_json()])
        async with a_queue(client) as q:
            await q.ask(state)
            return client.prompts[0]

    prompt = run(scenario())
    assert "CATALOG:" in prompt and "GRIND_UNTIL" in prompt


def test_the_situation_key_travels_in_the_prompt_and_on_the_row(state: State):
    """Prevents a corpus that cannot be joined.

    `situation_key` is the dedup key, the cache key, the bandit's join and the agreement
    bucket at once (`DECISIONS.md` V10). A row without it is a row that cannot be graded.
    """

    async def scenario():
        client = FakeClient(replies=[reply_json()])
        async with a_queue(client) as q:
            answer = await q.ask(state)
            return client.prompts[0], answer, q.rows[-1]

    prompt, answer, row = run(scenario())
    key = situation_key(state)
    assert answer.situation_key == key == row.situation_key
    assert key in prompt


def test_a_decision_row_is_a_plain_frozen_row(state: State):
    """Prevents the queue inventing its own row shape alongside the episode store's."""

    async def scenario():
        async with a_queue(FakeClient(replies=[reply_json()])) as q:
            await q.ask(state)
            return q.rows[-1]

    row = run(scenario())
    assert isinstance(row, DecisionRow)
    assert row.intent == Intent.GRIND_RIB and row.skill == "GRIND_UNTIL"
    assert row.why == "objective not ticking; grind two minutes then recheck"
    assert row.cache_hit is False and row.dedup_of is None


def test_a_reply_is_the_schema_the_prompt_advertised(state: State):
    """Prevents the prompt's contract and the validator drifting apart.

    Both come from `jev.coach.schema`; this is the test that keeps it that way.
    """
    reply = TeacherReply.model_validate_json(reply_json())
    assert isinstance(reply.decision, Decision)
    assert reply.artifacts and isinstance(reply.artifacts[0], Artifact)
    assert not reply.is_empty()
    assert "TeacherReply" in build_prompt(state)


def test_the_teacher_never_inherits_the_default_model():
    """An unpinned call takes whatever the user's default is — on this machine, Opus.

    That is the scarcest model on the plan and the one the human is using to build the
    thing, so a farm of ten clients would quietly take the developer's own capacity. It
    was not theoretical: unpinned calls returned 429 "out of usage credits" while Sonnet
    and Haiku answered immediately. The plan was untouched; only Opus was exhausted, and
    the error message said neither.
    """
    from jev.teacher.client import DEFAULT_MODEL, ClaudeSubscriptionClient

    argv = ClaudeSubscriptionClient().argv("hello")
    assert "--model" in argv, "an unpinned teacher inherits the user's default"
    assert argv[argv.index("--model") + 1] == DEFAULT_MODEL
    assert DEFAULT_MODEL != "opus", "the teacher must not contend for the Opus allocation"


def test_the_model_is_named_in_the_corpus():
    """`DecisionRow.model` has to say which tier answered, or a later analysis cannot tell
    a cheap answer from an expensive one."""
    from jev.teacher.client import ClaudeSubscriptionClient

    assert ClaudeSubscriptionClient().model_name == "claude-sub:sonnet"
    assert ClaudeSubscriptionClient(model="haiku").model_name == "claude-sub:haiku"


def test_an_explicit_model_still_wins():
    """Pinning a default must not stop a caller choosing — escalating a hard postmortem
    to a stronger tier is a legitimate thing to want."""
    from jev.teacher.client import ClaudeSubscriptionClient

    argv = ClaudeSubscriptionClient(model="haiku").argv("hello")
    assert argv[argv.index("--model") + 1] == "haiku"
