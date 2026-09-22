"""Bounded, native Python vision transport for the official GLM APIs.

Protocol: https://docs.z.ai/api-reference/llm/chat-completion
Vision: https://docs.bigmodel.cn/cn/guide/models/vlm/glm-4.6v
Prices: https://docs.z.ai/guides/overview/pricing (Flash is free, checked 2026-09-22).

The caller supplies a credential and selects its issuing platform. This module never
loads credentials, changes providers, retries a call, or executes model tool requests.
Vision models do not document response_format support, so the tutor schema is supplied
as an instruction. The base tutor contract is independently validated here, then again
by VisionTeacher. Callers must check any further restrictions in their supplied schema.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from jev.play.teacher import SYSTEM_PROMPT, TutorReply
from jev.teacher.client import TeacherResult

DEFAULT_GLM_MODEL = "glm-4.6v-flash"
ZAI_BASE_URL = "https://api.z.ai/api/paas/v4"
BIGMODEL_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ALLOWED_BASE_URLS = frozenset({ZAI_BASE_URL, BIGMODEL_BASE_URL})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
_MODEL_NAME = re.compile(r"glm-[a-z0-9][a-z0-9._-]{0,99}", re.IGNORECASE)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _no_constant(value: str) -> Any:
    raise ValueError("nonfinite JSON number")


def _loads(data: str | bytes) -> Any:
    return json.loads(data, object_pairs_hook=_json_object, parse_constant=_no_constant)


def _token_count(value: Any) -> int | None:
    # Booleans, numeric strings, negative counts and floats are not API token counts.
    return value if type(value) is int and value >= 0 else None


class GLMVisionClient:
    """One image request with an explicit credential and no persistent HTTP session."""

    def __init__(self, *, api_key: str, model: str = DEFAULT_GLM_MODEL,
                 base_url: str = ZAI_BASE_URL, max_tokens: int = 1024) -> None:
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 4096
                or not api_key.isascii() or any(c.isspace() or ord(c) < 33 for c in api_key)):
            raise ValueError("GLM API key must be a nonempty ASCII credential without whitespace")
        if not isinstance(base_url, str) or base_url.rstrip("/") not in ALLOWED_BASE_URLS:
            raise ValueError("GLM base URL must be an explicitly selected official HTTPS endpoint")
        if not isinstance(model, str) or _MODEL_NAME.fullmatch(model) is None:
            raise ValueError("GLM model must be an explicit GLM model identifier")
        if type(max_tokens) is not int or not 64 <= max_tokens <= 4096:
            raise ValueError("GLM max_tokens must be between 64 and 4096")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens

    @property
    def model_name(self) -> str:
        # The requested alias is deliberately distinct from the server's actual model.
        return f"glm-api:{self.model}"

    def preflight(self, *, timeout_s: float = 10.0, check_auth: bool = True) -> dict[str, Any]:
        """Local configuration check only; credential validity requires a real call."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("preflight timeout must be positive and finite")
        return {"ok": True, "provider": "glm", "requested_model": self.model,
                "base_url": self.base_url, "credential_present": bool(self._api_key),
                "authentication_verified": False, "model_call_made": False,
                "vision_roundtrip_verified": False}

    def image_input(self, prompt: str, image_png: bytes,
                    json_schema: dict[str, Any]) -> bytes:
        if not isinstance(image_png, bytes) or not image_png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("vision input must be PNG bytes")
        if len(image_png) > MAX_IMAGE_BYTES:
            raise ValueError("vision PNG exceeds the 5 MiB image limit")
        if not isinstance(prompt, str):
            raise ValueError("vision prompt must be text")
        if not isinstance(json_schema, dict) or not json_schema:
            raise ValueError("GLM vision schema guidance must be a nonempty JSON object")
        schema_text = json.dumps(json_schema, separators=(",", ":"), allow_nan=False)
        if len(schema_text.encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise ValueError("GLM vision schema guidance exceeds the byte limit")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + "\nReturn exactly one JSON object, "
                 "without markdown or surrounding text, matching this JSON Schema:\n" + schema_text},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")}},
                    {"type": "text", "text": prompt}]},
            ],
            "stream": False,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "disabled"},
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("vision request exceeds the 10 MiB limit")
        return data

    async def ask_image(self, prompt: str, image_png: bytes, *,
                        json_schema: dict[str, Any], timeout_s: float) -> TeacherResult:
        started = time.perf_counter()

        def fail(detail: str, *, timeout: bool = False) -> TeacherResult:
            return TeacherResult(status="timeout" if timeout else "transport",
                                 model=self.model_name, detail=detail,
                                 latency_ms=(time.perf_counter() - started) * 1000)

        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return fail("no positive finite time budget remains", timeout=True)
        try:
            payload = self.image_input(prompt, image_png, json_schema)
        except (ValueError, TypeError, OverflowError):
            # Exceptions can contain a prompt, schema, or image: never serialize them.
            return fail("GLM vision request failed local input validation")
        remaining = timeout_s - (time.perf_counter() - started)
        if remaining <= 0:
            return fail("GLM vision request deadline expired before sending", timeout=True)
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(remaining), follow_redirects=False, trust_env=False,
                    transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                ) as client, client.stream(
                    "POST", self.base_url + "/chat/completions", content=payload,
                    headers={"Authorization": "Bearer " + self._api_key,
                             "Content-Type": "application/json", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        # In particular, never follow a redirect carrying Authorization.
                        # Error bodies and headers may echo user content or credentials.
                        return fail(f"GLM API returned HTTP {response.status_code}; no retry")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return fail("GLM API returned an unsupported response encoding")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            return fail("GLM API response exceeded the byte limit")
                        body.extend(chunk)
            return self.classify_response(bytes(body),
                                          latency_ms=(time.perf_counter() - started) * 1000)
        except (TimeoutError, httpx.TimeoutException):
            return fail("GLM vision request deadline expired; connection closed", timeout=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never expose an HTTP exception, which can retain request data and secrets.
            return fail("GLM vision transport failed; no retry")

    def classify_response(self, body: bytes, *, latency_ms: float = 0.0) -> TeacherResult:
        """Accept only a complete, single assistant JSON reply with a valid bounded action."""
        meta: dict[str, Any] = {"model": self.model_name, "latency_ms": latency_ms}
        if len(body) > MAX_RESPONSE_BYTES:
            return TeacherResult(status="transport", detail="GLM API response exceeded the byte limit",
                                 **meta)
        try:
            doc = _loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return TeacherResult(status="transport", detail="GLM API response was not valid JSON", **meta)
        if not isinstance(doc, dict):
            return TeacherResult(status="transport", detail="GLM API response was not an object", **meta)
        actual_model = doc.get("model")
        if isinstance(actual_model, str) and _MODEL_NAME.fullmatch(actual_model):
            meta["model"] = actual_model
        usage = doc.get("usage")
        if isinstance(usage, dict):
            meta["tokens_in"] = _token_count(usage.get("prompt_tokens"))
            meta["tokens_out"] = _token_count(usage.get("completion_tokens"))
        if "error" in doc:
            return TeacherResult(status="transport", detail="GLM API reported an error", **meta)
        choices = doc.get("choices")
        if (not isinstance(choices, list) or len(choices) != 1
                or not isinstance(choices[0], dict)):
            return TeacherResult(status="transport", detail="GLM API lacked one reply choice", **meta)
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            return TeacherResult(status="rejected", detail="GLM reply did not finish normally", **meta)
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return TeacherResult(status="transport", detail="GLM API lacked an assistant reply", **meta)
        if message.get("tool_calls") or message.get("function_call"):
            return TeacherResult(status="rejected", detail="GLM returned an unsupported tool request", **meta)
        content = message.get("content")
        if message.get("refusal") or not isinstance(content, str) or not content.strip():
            return TeacherResult(status="abstained", detail="GLM did not supply an action reply", **meta)
        try:
            reply = TutorReply.model_validate(_loads(content))
        except ValidationError as exc:
            # Diagnose schema incompatibility without retaining response text, values,
            # arbitrary extra-field names or exception context that might echo input.
            fields = {"observation_id", "capability", "action", "lookup", "rationale",
                      "expected_effect", "kind", "control", "duration_s", "wait_s", "slot",
                      "button", "intent", "x", "y", "expected_target_id", "expected_dead",
                      "ui_control", "ui_name_id", "axis", "pixels", "name", "params",
                      "observe", "key", "action_slot", "pointer", "click", "camera", "skill"}
            errors = [".".join(str(part) if part in fields else "field" for part in row["loc"])
                      + ":" + row["type"] for row in exc.errors(include_input=False,
                        include_context=False, include_url=False)[:6]]
            return TeacherResult(status="rejected", detail="GLM reply failed bounded tutor validation: "
                                 + "; ".join(errors), **meta)
        except (ValueError, TypeError, RecursionError):
            return TeacherResult(status="rejected", detail="GLM reply failed bounded tutor validation",
                                 **meta)
        return TeacherResult(status="ok", text=reply.model_dump_json(), **meta)
