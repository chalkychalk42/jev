"""A vision tutor proposes one bounded action; the runtime owns all game input.

Transport follows Claude Code's documented streaming user-message/image format:
https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
https://code.claude.com/docs/en/headless#get-structured-output

CLI 2.1.280 help was inspected locally. Images are embedded in stdin, never represented
by a path the model is expected to open. Subscription authentication is retained; model
tools, customizations, MCP servers and session persistence are disabled. Process tests
exercise this protocol without spending a model call.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from jev.play import tutor
from jev.play.actions import Action, modal_action_allowed
from jev.play.knowledge import LocalKnowledge
from jev.play.tutor import SYSTEM_PROMPT
from jev.teacher.client import ClaudeSubscriptionClient, TeacherResult

# Transient provider failures (an overloaded free tier, a rate limit) are retried inside
# the same decision deadline, each attempt charged and recorded, waiting longer each time.
# Measured on 23 Sep 2026: the free GLM tier answered "overloaded" to three requests two
# seconds apart and served the next scene normally; in the first live teaching session it
# refused four in a row, while the old 2/4/6 s schedule left thirteen seconds of the
# thirty unused. A retry starts only while a reply can still arrive: answers that came
# took 3.8-7.5 s. Never beyond the decision deadline.
BACKOFF_S = (2.0, 2.0, 3.0, 3.0, 4.0, 4.0)
MAX_TRANSIENT_RETRIES = len(BACKOFF_S)
REPLY_RESERVE_S = 5.0
# A reply that breaks the contract is asked for once more, quoting the rejection. It is
# the model's own answer, corrected against its own error - never repaired locally.
MAX_REASKS = 1


class VisionClient(Protocol):
    model_name: str

    async def ask_image(self, prompt: str, image_png: bytes, *,
                        json_schema: dict[str, Any], timeout_s: float) -> TeacherResult: ...


class ClaudeVisionClient(ClaudeSubscriptionClient):
    """Same subscription as the strategic teacher, with real vision and no tool access."""

    def preflight(self, *, timeout_s: float = 10.0, check_auth: bool = True) -> dict[str, Any]:
        """Read CLI capabilities and authentication presence without contacting a model.

        Authentication output is captured and reduced to booleans/provider labels;
        account identifiers, credentials and raw command output are never returned.
        This checks deployment, not visual reasoning, schema delivery or model routing.
        """
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("preflight timeout must be positive and finite")
        binary = shutil.which(self.binary) or self.binary
        report: dict[str, Any] = {"binary": binary, "requested_model": self.model,
                                  "requested_effort": self.effort,
                                  "platform": os.name, "model_call_made": False,
                                  "vision_roundtrip_verified": False}
        try:
            self.image_argv(tutor.schema(tutor.menu({}, {})))  # Also checks the wrapper rule.
        except ValueError as exc:
            return {**report, "ok": False, "detail": str(exc)}
        commands = {"version": [binary, "--version"], "help": [binary, "--help"]}
        if check_auth:
            commands["auth"] = [binary, "auth", "status", "--json"]
        results = {}
        for name, argv in commands.items():
            try:
                results[name] = subprocess.run(argv, capture_output=True, text=True,
                                               encoding="utf-8", errors="replace", timeout=timeout_s,
                                               cwd=self.cwd, env=self.env or os.environ.copy())
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {**report, "ok": False,
                        "detail": f"{name} preflight failed: {type(exc).__name__}"}
        required = ("--print", "--input-format", "--output-format", "--verbose", "--safe-mode",
                    "--no-session-persistence", "--permission-prompts", "--tools",
                    "--strict-mcp-config", "--mcp-config", "--model", "--system-prompt",
                    "--json-schema", *(("--effort",) if self.effort else ()))
        report["version"] = results["version"].stdout.strip()[:120]
        report["required_flags"] = {flag: flag in results["help"].stdout for flag in required}
        report["command_exit_codes"] = {name: result.returncode for name, result in results.items()}
        auth_ok = True
        if check_auth:
            try:
                auth = json.loads(results["auth"].stdout)
            except ValueError:
                auth = {}
            if not isinstance(auth, dict):
                auth = {}
            # Values are fixed labels rather than arbitrary credential-bearing strings.
            report["authenticated"] = auth.get("loggedIn") is True
            report["subscription_auth"] = auth.get("authMethod") == "claude.ai"
            report["first_party_provider"] = auth.get("apiProvider") == "firstParty"
            auth_ok = report["authenticated"] and report["subscription_auth"]
        report["ok"] = (all(r.returncode == 0 for r in results.values())
                        and all(report["required_flags"].values()) and auth_ok)
        if not report["ok"]:
            report["detail"] = "CLI capabilities or subscription authentication are unavailable"
        return report

    def image_argv(self, json_schema: dict[str, Any]) -> list[str]:
        if self.extra_args:
            raise ValueError("vision transport does not accept arbitrary CLI arguments")
        binary = shutil.which(self.binary) or self.binary
        if os.name == "nt" and Path(binary).suffix.casefold() in {".cmd", ".bat"}:
            raise ValueError("vision transport needs the native claude.exe, not a cmd.exe wrapper; "
                             "set --teacher-binary to the installed native executable")
        return [binary, "--print", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--safe-mode",
                "--no-session-persistence", "--permission-prompts", "none",
                "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--model", self.model, *(("--effort", self.effort) if self.effort else ()),
                "--system-prompt", SYSTEM_PROMPT,
                "--json-schema", json.dumps(json_schema, separators=(",", ":"))]

    @staticmethod
    def image_input(prompt: str, image_png: bytes) -> bytes:
        if not image_png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("vision input must be PNG bytes")
        if len(image_png) > 5 * 1024 * 1024:
            raise ValueError("vision PNG exceeds the 5 MiB image limit")
        message = {"type": "user", "parent_tool_use_id": None,
                   "message": {"role": "user", "content": [
                       {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                       "data": base64.b64encode(image_png).decode()}},
                       {"type": "text", "text": prompt}]}}
        data = (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("vision request exceeds the CLI's 10 MiB stdin limit")
        return data

    async def ask_image(self, prompt: str, image_png: bytes, *,
                        json_schema: dict[str, Any], timeout_s: float) -> TeacherResult:
        started = time.perf_counter()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return TeacherResult(status="timeout", model=self.model_name,
                                 detail="no positive finite time budget remains")
        try:
            payload = self.image_input(prompt, image_png)
            argv = self.image_argv(json_schema)
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.cwd, env=self.env or os.environ.copy())
        except (OSError, ValueError) as exc:
            return TeacherResult(status="transport", model=self.model_name,
                                 latency_ms=(time.perf_counter() - started) * 1000,
                                 detail=f"vision request could not start: {exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout=timeout_s)
        except (TimeoutError, asyncio.CancelledError) as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return TeacherResult(status="timeout", model=self.model_name,
                                 latency_ms=(time.perf_counter() - started) * 1000,
                                 detail=f"vision reply exceeded {timeout_s:.2f}s; child killed")
        return self.classify_stream(stdout=out.decode("utf-8", "replace"),
                                    stderr=err.decode("utf-8", "replace"),
                                    returncode=proc.returncode if proc.returncode is not None else -1,
                                    latency_ms=(time.perf_counter() - started) * 1000)

    def classify_stream(self, *, stdout: str, stderr: str, returncode: int,
                        latency_ms: float) -> TeacherResult:
        """Only a final successful result with structured_output can be an action."""
        result = None
        try:
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                doc = json.loads(line)
                if isinstance(doc, dict) and doc.get("type") == "result":
                    if result is not None:
                        raise ValueError("multiple final results for one observation")
                    result = doc
        except (json.JSONDecodeError, ValueError) as exc:
            return TeacherResult(status="transport", model=self.model_name, latency_ms=latency_ms,
                                 detail=f"invalid vision stream: {exc}")
        if result is None:
            return TeacherResult(status="transport", model=self.model_name, latency_ms=latency_ms,
                                 detail=f"no final vision result (exit={returncode}); {stderr[:200]}")
        if result.get("is_error") is not True and result.get("subtype") == "success":
            structured = result.get("structured_output")
            if not isinstance(structured, dict):
                result = {**result, "is_error": True,
                          "result": "successful CLI result had no structured_output object"}
            else:
                result = {**result, "result": json.dumps(structured, separators=(",", ":"))}
        elif result.get("is_error") is not True:
            result = {**result, "is_error": True,
                      "result": f"incomplete structured result: {result.get('subtype')}"}
        return self.classify(stdout=json.dumps(result), stderr=stderr,
                             returncode=returncode, latency_ms=latency_ms)


@dataclass(frozen=True)
class PlayTeacherResult:
    status: str
    observation_id: str
    action: Action | None = None
    capability: str | None = None
    rationale: str | None = None
    expected_effect: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: float = 0.0
    detail: str | None = None
    calls: tuple[TeacherResult, ...] = ()
    lookups: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.action is not None


class VisionTeacher:
    def __init__(self, client: VisionClient | None = None, *,
                 knowledge: LocalKnowledge | None = None, max_lookups: int = 2,
                 reserve_call: Callable[[], bool] | None = None,
                 record_call: Callable[[TeacherResult], None] | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 max_reasks: int = MAX_REASKS) -> None:
        if not 0 <= max_lookups <= 4:
            raise ValueError("max_lookups must be between 0 and 4")
        if not 0 <= max_reasks <= MAX_REASKS:
            raise ValueError("max_reasks is bounded by the shared re-ask limit")
        self.max_reasks = max_reasks
        self.client = client or ClaudeVisionClient()
        self.knowledge = knowledge
        self.max_lookups = max_lookups
        self.reserve_call = reserve_call
        self.record_call = record_call
        self.sleep = sleep

    async def decide(self, observation: dict[str, Any], screenshot: bytes | Path | str, *,
                     controls: dict[str, Any], knowledge: dict[str, Any] | None = None,
                     recent: Sequence[dict[str, Any]] = (), timeout_s: float = 30.0,
                     skills: Sequence[str] = (),
                     allowed: Sequence[str] | None = None) -> PlayTeacherResult:
        """One bounded action for this observation, chosen from the state's own menu.

        `allowed` can only narrow that menu (an observe-only probe), never widen it.
        """
        started = time.perf_counter()
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not observation_id:
            raise ValueError("observation requires a nonempty id")
        calls: list[TeacherResult] = []
        lookups: list[dict[str, Any]] = []

        def finish(status: str, *, action: Action | None = None, why: str | None = None,
                   detail: str | None = None) -> PlayTeacherResult:
            actual = next((r.model for r in reversed(calls) if r.ok), None)
            if actual is None and calls:
                actual = calls[-1].model
            # Transport failures without server model usage carry the requested alias;
            # that is not evidence of which model actually served a request.
            if actual == self.client.model_name:
                actual = None
            counted = [r for r in calls if r.tokens_in is not None]
            return PlayTeacherResult(
                status=status, observation_id=observation_id, action=action,
                capability=None, rationale=why, expected_effect=None,
                requested_model=self.client.model_name, actual_model=actual,
                tokens_in=sum(r.tokens_in for r in counted) if counted else None,
                tokens_out=sum(r.tokens_out or 0 for r in calls)
                if any(r.tokens_out is not None for r in calls) else None,
                latency_ms=(time.perf_counter() - started) * 1000, detail=detail,
                calls=tuple(calls), lookups=tuple(lookups))

        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return finish("timeout", detail="no positive finite time budget remains")
        try:
            image_png = screenshot if isinstance(screenshot, bytes) else Path(screenshot).read_bytes()
            expected_hash = (observation.get("screen") or {}).get("sha256")
            if expected_hash and hashlib.sha256(image_png).hexdigest() != expected_hash:
                return finish("invalid", detail="screenshot hash does not match the observation")
            # Reject unusable images before reserving/spending a teacher call.
            ClaudeVisionClient.image_input("", image_png)
        except (OSError, ValueError) as exc:
            return finish("invalid", detail=f"screenshot unavailable: {exc}")
        context = knowledge if knowledge is not None else (
            self.knowledge.context(observation) if self.knowledge else {})
        transient_retries = reasks = 0
        rejected: str | None = None
        while True:
            remaining = timeout_s - (time.perf_counter() - started)
            if remaining <= 0:
                return finish("timeout", detail="overall tutor decision deadline expired")
            lookups_left = self.max_lookups - len(lookups) if self.knowledge is not None else 0
            choices = tutor.menu(observation, controls, skills=skills, lookup=lookups_left > 0)
            if allowed is not None:
                choices = [c for c in choices if c.name in allowed]
            prompt = tutor.render(observation, choices=choices, knowledge=context,
                                  recent=recent, lookups=lookups,
                                  lookups_remaining=lookups_left, rejected=rejected)
            if self.reserve_call is not None and not self.reserve_call():
                return finish("budget", detail="motor teacher call budget unavailable")
            call_started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self.client.ask_image(prompt, image_png, json_schema=tutor.schema(choices),
                                          timeout_s=remaining), timeout=remaining)
            except asyncio.CancelledError:
                if self.record_call is not None:
                    self.record_call(TeacherResult(status="transport", model=self.client.model_name,
                                                   latency_ms=(time.perf_counter() - call_started) * 1000,
                                                   detail="vision request cancelled"))
                raise
            except TimeoutError:
                result = TeacherResult(status="timeout", model=self.client.model_name,
                                       latency_ms=(time.perf_counter() - call_started) * 1000,
                                       detail="overall vision decision deadline expired")
            except Exception as exc:
                result = TeacherResult(status="transport", model=self.client.model_name,
                                       latency_ms=(time.perf_counter() - call_started) * 1000,
                                       detail=f"vision transport raised {type(exc).__name__}: {exc}")
            calls.append(result)
            if self.record_call is not None:
                self.record_call(result)
            if not result.ok:
                if result.retry_after_s is not None and transient_retries < MAX_TRANSIENT_RETRIES:
                    wait = max(result.retry_after_s, BACKOFF_S[transient_retries])
                    remaining = timeout_s - (time.perf_counter() - started)
                    if math.isfinite(wait) and wait + REPLY_RESERVE_S < remaining:
                        transient_retries += 1
                        await self.sleep(wait)
                        continue
                return finish(result.status, detail=result.detail)
            try:
                choice = tutor.parse(result.text or "")
                if choice.observation_id != observation_id:
                    return finish("stale", detail="reply observation_id does not match the request")
                action = tutor.to_action(choice, choices, observation)
            except (tutor.ChoiceError, ValidationError) as exc:
                reason = _reason(exc)
                if reasks < self.max_reasks:
                    reasks += 1
                    rejected = reason
                    continue
                return finish("invalid", detail=f"invalid tutor reply: {reason}")
            if isinstance(action, str):
                lookups.append(self.knowledge.search(action))
                rejected = None
                continue
            if ((observation.get("values") or {}).get("ui.modal") is True
                    and not modal_action_allowed(action)):
                return finish("invalid", detail="action is unavailable while a blocking modal "
                              "is observed; only observe or tap escape is permitted")
            return finish("ok", action=action, why=choice.why or None)


def _reason(exc: Exception) -> str:
    """A validation failure named by field and rule; the reply text is retained elsewhere."""
    if isinstance(exc, ValidationError):
        return "; ".join(".".join(str(part) for part in row["loc"]) + ":" + row["type"]
                         for row in exc.errors(include_input=False, include_context=False,
                                               include_url=False)[:6])
    return str(exc)
