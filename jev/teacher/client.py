"""Transport to the teacher. Four outcomes, never collapsed into "no answer".

`DECISIONS.md` V4: the teacher is **Claude over the owner's subscription**, invoked as
`claude -p "<prompt>" --output-format json`. There is no Anthropic key and none is wanted,
so this is a subprocess module, not an HTTP client. Latency is seconds and the rate limit
is one window shared by the whole farm, which is why `queue.py` above it is single-worker.

The entire reason this file exists as its own layer is `ARCHITECTURE.md` §6:

    Absence of an answer is not an answer.

A timeout, a dead subprocess, an empty reply and a real answer are four different facts.
Collapsing them into `None` loses the only information that tells the queue whether to
retry, and loses the corpus the ability to tell "the model declined" from "the model was
never reached". So every call returns a `TeacherResult` carrying which of the four it was.

Swapping the transport
----------------------
`TeacherClient` is the whole contract: one `ask`, returning the four-way result. A GLM rung
(`GLM_API_KEY`, OpenAI-compatible, plain `httpx`) is a second implementation of this
protocol and nothing above it changes. It is deliberately **not** written yet: nothing
calls it, it cannot be tested against the real endpoint from here, and `ARCHITECTURE.md` §6
says a capability nothing calls does not exist. The seam is the deliverable; the second
implementation lands with its first caller.

What the real CLI actually returns (probed, 20 Sep 2026, claude 2.1.267)
------------------------------------------------------------------------
Both branches were run against the live binary. Success, exit 0:

    {"type":"result","subtype":"success","is_error":false,"result":"ok",
     "duration_ms":1166,"duration_api_ms":1640,"ttft_ms":1115,"num_turns":1,
     "session_id":"539af2b7-...","total_cost_usd":0.0173507,"stop_reason":"end_turn",
     "terminal_reason":"completed","api_error_status":null,
     "usage":{"input_tokens":10,"output_tokens":36,
              "cache_creation_input_tokens":7424,"cache_read_input_tokens":13607,...},
     "modelUsage":{"claude-haiku-4-5-20251001":{"inputTokens":907,"outputTokens":47,...}}}

Failure (the subscription was out of credits at probe time), exit 1:

    {"type":"result","subtype":"success","is_error":true,
     "result":"You're out of usage credits. ...",
     "api_error_status":429,"terminal_reason":"api_error","stop_reason":"stop_sequence",
     "usage":{"input_tokens":0,...},"modelUsage":{},"duration_ms":494}

Three traps in there, each of which would have been a silent bug:

1. `subtype` is `"success"` in **both** branches. It is not a success signal. `is_error` is.
2. A failed call still exits non-zero **and** still prints well-formed JSON. So parse first
   and classify second; treating a non-zero exit as unparseable would throw away the
   `api_error_status: 429` that says "rate limited, retry later" rather than "broken".
3. On failure, `result` holds the *error message*, not model text. Handing that string to
   `TeacherReply.model_validate_json` would turn a rate limit into a parse failure and hide
   the real cause. `result` is only an answer when `is_error` is false.

Token accounting: `usage.input_tokens` counts only uncached input (10), while
`modelUsage[model].inputTokens` counts the whole billed input (907) for the same call. The
`modelUsage` figure is the one that matches what the subscription window is charged, so it
wins when present; `usage` is the fallback, summed across its cached and uncached parts.

Cost floor: that 10-token prompt still carried 7,424 + 13,607 tokens of CLI system prompt
and tool definitions. Keeping our prompt small (PLAN §9.3, `prompt.py`) is necessary but it
is not sufficient — hence `--safe-mode`, which drops CLAUDE.md, skills, plugins, hooks and
MCP servers from that floor. `--bare` would cut more, but its help text says Anthropic auth
becomes strictly `ANTHROPIC_API_KEY`, and V4 says there is no key: `--bare` cannot log in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jev.coach.schema import Status

# Bounded so one hung child cannot hold the farm's single worker for the rest of the run.
DEFAULT_MODEL = "sonnet"
"""The teacher's tier. See the note in `ClaudeSubscriptionClient.__init__`: pinned so a
farm never contends with the human for the Opus allocation."""

# 180 s, and the reason it is not 45 is worth writing down, because 45 was the defensible
# number until it was measured.
#
# The original argument: `jev.coach.situation` bins step age at 60 s and
# `jev.learn.episode.grade` measures outcomes over a 60 s window, so an answer slower than
# that is about a situation which has already moved into another bucket. Sound — and it
# was set against a *trivial* probe that answered in 1.2 s.
#
# A realistic question does not behave like a trivial one. Measured end to end: a
# 5,406-character prompt about a stalled kill objective took **52 s** and returned 2,544
# in / 4,595 out. At 45 s every real teacher call timed out, which is not a conservative
# setting, it is an off switch.
#
# What the measurement actually settles is `DECISIONS.md` V11. The teacher's valuable
# output is the **durable artifact** — a combat profile, an `on_fail` edge — and an
# artifact is about the *step*, not the tick. It does not go stale in sixty seconds. Only
# the fallback `decision` is time-sensitive, and by the time it lands the scripted coach
# has long since acted anyway. So the right call is to wait for the artifact and let the
# caller discard the stale action, which is what the runtime does.
DEFAULT_TIMEOUT_S = 180.0

# Sent on every call, for reasons that are all "do not let the teacher touch the run":
#   --print / --output-format json  the contract, V4
#   --safe-mode                     no CLAUDE.md, skills, plugins, hooks or MCP servers, so
#                                   the system-prompt floor is small and identical between
#                                   calls; unlike --bare it leaves subscription auth working
#   --no-session-persistence        a farm asking thousands of questions must not leave
#                                   thousands of resumable sessions on disk
#   --permission-prompts none       anything that would prompt is denied instead of hanging;
#                                   a teacher that blocks on a dialog is a teacher that eats
#                                   the whole timeout and returns nothing
BASE_ARGS: tuple[str, ...] = (
    "--print",
    "--output-format",
    "json",
    "--safe-mode",
    "--no-session-persistence",
    "--permission-prompts",
    "none",
)


@dataclass(frozen=True)
class TeacherResult:
    """One call's outcome. `status` is the point of the whole object.

    `ok` is the only value where `text` means anything. The other three are recorded and
    counted, and three of them are retryable, but none of them is a decision.
    """

    status: Status
    text: str | None = None
    model: str | None = None
    latency_ms: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Free-text cause, e.g. "api_error_status=429" or "exit=2 stderr=...". It lands in the
    # DecisionRow, so a postmortem names the failure instead of describing its silhouette.
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class TeacherClient(Protocol):
    """The entire transport contract. One method, four outcomes."""

    model_name: str

    async def ask(self, prompt: str, *, timeout_s: float | None = None) -> TeacherResult:
        ...


def _tokens(doc: dict[str, Any]) -> tuple[int | None, int | None]:
    """Billed input/output for one call, preferring the accounting the subscription uses.

    `modelUsage` aggregates the whole turn including what `usage` splits out as cached, and
    it was 907 input where `usage.input_tokens` said 10 for the same probe. The larger one
    is the one the window is charged for, so it is the one worth recording.
    """
    usage_by_model = doc.get("modelUsage")
    if isinstance(usage_by_model, dict) and usage_by_model:
        tin = sum(int(m.get("inputTokens", 0) or 0) for m in usage_by_model.values() if isinstance(m, dict))
        tout = sum(int(m.get("outputTokens", 0) or 0) for m in usage_by_model.values() if isinstance(m, dict))
        if tin or tout:
            return tin, tout

    usage = doc.get("usage")
    if isinstance(usage, dict):
        tin = sum(
            int(usage.get(k, 0) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        tout = int(usage.get("output_tokens", 0) or 0)
        if tin or tout:
            return tin, tout
    return None, None


def _model_of(doc: dict[str, Any], fallback: str) -> str:
    """The model that actually served the call, which is not always the one asked for —
    `--fallback-model` and server-side routing both change it, and the corpus must record
    what answered rather than what we hoped would."""
    usage_by_model = doc.get("modelUsage")
    if isinstance(usage_by_model, dict) and usage_by_model:
        return next(iter(usage_by_model))
    return fallback


class ClaudeSubscriptionClient:
    """`claude -p` as a child process, with the child killed rather than abandoned."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cwd: str | None = None,
        json_schema: dict[str, Any] | None = None,
        extra_args: Sequence[str] = (),
        env: dict[str, str] | None = None,
    ) -> None:
        # Resolved from PATH rather than hard-coded: the binary lives under one user's home
        # on this machine, and a path baked into the module is a path that breaks on the
        # next one. "claude" is kept as the last resort so the failure is a clear ENOENT
        # transport error rather than an import-time crash on a machine without it.
        self.binary = binary or shutil.which("claude") or "claude"
        # Pinned, never left to the CLI's default — and pinned *down*.
        #
        # An unpinned call inherits whatever the user's default model is, which on this
        # machine is Opus: the scarcest, most rate-limited model on the plan, and the one
        # the human is actively using to build the thing. A farm of ten clients quietly
        # contending for that is a farm that takes the developer's capacity away.
        #
        # This was not theoretical. Leaving it unset produced a 429 "out of usage credits"
        # on every call while Sonnet and Haiku answered immediately — the Opus allocation
        # was exhausted, the plan was untouched, and the error message said neither.
        #
        # Sonnet is the right tier on the merits too. The teacher resolves ambiguity,
        # drafts skills and patches graph edges against a small prompt and a fixed output
        # schema. Nothing in that job is Opus work, and `ARCHITECTURE.md` §0 already says
        # quality matters less here than being affordable enough to call at all.
        self.model = model or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self.cwd = cwd
        # Unverified against the live CLI: the subscription was out of credits when this
        # was written, so `--json-schema` could not be exercised end to end and defaults
        # off. Schema-enforced output would make an unparseable reply nearly impossible,
        # which is worth having — but not worth turning on untested, since a flag the CLI
        # rejects makes every teacher call a transport failure forever.
        self.json_schema = json_schema
        self.extra_args = tuple(extra_args)
        self.env = env
        self.model_name = f"claude-sub:{self.model}"

    def argv(self, prompt: str) -> list[str]:
        """Built as a list and executed without a shell, so a prompt containing quotes,
        backticks or a newline is an argument and never a command."""
        args = [self.binary, *BASE_ARGS]
        if self.model:
            args += ["--model", self.model]
        if self.json_schema is not None:
            args += ["--json-schema", json.dumps(self.json_schema, separators=(",", ":"))]
        args += [*self.extra_args, prompt]
        return args

    async def ask(self, prompt: str, *, timeout_s: float | None = None) -> TeacherResult:
        limit = self.timeout_s if timeout_s is None else timeout_s
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(prompt),
                stdin=asyncio.subprocess.DEVNULL,  # never let the child block on a tty
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env or os.environ.copy(),
            )
        except OSError as exc:
            return TeacherResult(
                status="transport",
                model=self.model_name,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                detail=f"spawn failed: {exc}",
            )

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Kill, then reap. Skipping the wait leaves a zombie holding pipes, and after a
            # few hundred questions that is the farm out of file descriptors rather than an
            # obvious teacher bug.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                # Bridge shutdown cancels the queue. Cancellation must stop the paid
                # child just as timeout does, then propagate so the queue records its
                # transport cancellation and resolves every pending requester.
                raise
            return TeacherResult(
                status="timeout",
                model=self.model_name,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                detail=f"no reply within {limit:.1f}s; child killed",
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        return self.classify(
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
            latency_ms=latency_ms,
        )

    def classify(self, *, stdout: str, stderr: str, returncode: int, latency_ms: float) -> TeacherResult:
        """Turn one CLI invocation into one of the four outcomes.

        Parse first, judge second — see trap 2 in the module docstring. Public on purpose:
        it is the seam that lets the real output shapes be pinned down in tests without
        spawning a process or spending a call, and the shape is exactly the thing most
        likely to move under us on a CLI upgrade.
        """
        raw = stdout.strip()
        if not raw:
            return TeacherResult(
                status="transport",
                model=self.model_name,
                latency_ms=latency_ms,
                detail=f"no stdout (exit={returncode}) {stderr.strip()[:200]}",
            )
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            return TeacherResult(
                status="transport",
                model=self.model_name,
                latency_ms=latency_ms,
                detail=f"stdout was not json (exit={returncode}): {exc}",
            )
        if not isinstance(doc, dict):
            return TeacherResult(
                status="transport",
                model=self.model_name,
                latency_ms=latency_ms,
                detail=f"stdout json was {type(doc).__name__}, expected an object",
            )

        model = _model_of(doc, self.model_name)
        tokens_in, tokens_out = _tokens(doc)

        if doc.get("is_error") is True:
            # The CLI reached us but not the model. `result` here is an error string, so it
            # is put in `detail` and deliberately not in `text`.
            status_code = doc.get("api_error_status")
            reason = doc.get("terminal_reason") or "is_error"
            return TeacherResult(
                status="transport",
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                detail=f"{reason} api_error_status={status_code}: {str(doc.get('result'))[:200]}",
            )

        text = doc.get("result")
        if text is None:
            # Well-formed JSON with no result field means the CLI's output shape moved under
            # us. That is a transport problem to fix, not a model that declined to answer.
            return TeacherResult(
                status="transport",
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                detail=f"no 'result' key; keys={sorted(doc)[:12]}",
            )
        if not isinstance(text, str):
            return TeacherResult(
                status="transport",
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                detail=f"'result' was {type(text).__name__}, expected str",
            )
        if not text.strip():
            # A well-formed reply that says nothing. The model was reached and chose not to
            # answer: that is an abstention, and per ARCHITECTURE.md §6 it is retryable and
            # it is emphatically not an instruction to stop.
            return TeacherResult(
                status="abstained",
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                detail="empty result",
            )
        if returncode != 0:
            # Text arrived but the CLI is unhappy. Believing the text here would mean acting
            # on the output of a run the tool itself disowned.
            return TeacherResult(
                status="transport",
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                detail=f"exit={returncode} with is_error=false; {stderr.strip()[:200]}",
            )
        return TeacherResult(
            status="ok",
            text=text,
            model=model,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


Scripted = TeacherResult | str | Callable[[str, int], TeacherResult]


@dataclass
class FakeClient:
    """A teacher that costs nothing. No network, no subprocess, no clock of its own.

    Every test in `tests/test_teacher.py` runs through this, which is the point: the queue's
    dedup, cache, retry and drop behaviour are decisions about *when* to call, and testing
    them against a real subscription would be both slow and a bill.

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
