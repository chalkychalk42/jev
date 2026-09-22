"""One native Claude request containing only generated shapes and synthetic literals.

No project imports, game files, captured pixels, observations, knowledge stores or user
configuration are read. The native CLI alone handles its existing authentication. This
probe never executes a model response and never opens or controls a game client.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import math
import os
import re
import shutil
import struct
import time
import zlib
from pathlib import Path


def generated_png() -> bytes:
    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF))

    rows = []
    for y in range(64):
        row = bytearray([0])
        for x in range(64):
            color = ((255, 0, 0) if 5 <= x < 25 and 5 <= y < 25 else
                     (0, 128, 0) if 35 <= x < 59 and 7 <= y < 24 else
                     (0, 0, 255) if 20 <= x < 45 and 35 <= y < 58 else (255, 255, 255))
            row.extend(color)
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def payload() -> bytes:
    synthetic = {"observation": {"id": "synthetic-generated-shapes-v1", "synthetic": True,
                                  "goal": "Return an observe action for this synthetic transport test."},
                 "controls": {"allowed_actions": ["observe"], "game_input_available": False},
                 "knowledge": {"source": "programmatically_generated_public_example",
                               "facts": ["The image is a synthetic drawing, not a screenshot."]}}
    message = {"type": "user", "parent_tool_use_id": None,
               "message": {"role": "user", "content": [
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.b64encode(generated_png()).decode()}},
                   {"type": "text", "text": json.dumps(synthetic, separators=(",", ":"))}]}}
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


def schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"observation_id": {"type": "string", "const": "synthetic-generated-shapes-v1"},
                           "action": {"type": "string", "const": "observe"},
                           "synthetic": {"type": "boolean", "const": True}},
            "required": ["observation_id", "action", "synthetic"]}


def redacted_detail(value) -> str:
    """Keep a bounded diagnosis without retaining credentials or account identifiers."""
    text = str(value or "")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[redacted-email]", text)
    text = re.sub(r"(?i)\b(?:sk|pk)-(?:ant-)?[A-Za-z0-9_-]+", "[redacted-key]", text)
    text = re.sub(r"(?i)\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|client_secret)"
                  r"[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?[^\s,\"'}]+", "[redacted-credential]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,\"'}]+", "Bearer [redacted]", text)
    text = re.sub(r"([?&][A-Za-z0-9_-]+=)[^&\s]+", r"\1[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/-]{32,}={0,2}", "[redacted-long-value]", text)
    return text[:300]


async def probe(binary: str, model: str, timeout_s: float) -> dict:
    report = {"status": "not_started", "model_calls": 0, "requested_model": model,
              "actual_model": None, "tokens_in": None, "tokens_out": None, "latency_ms": 0.0}
    # A neutral pre-existing directory prevents the CLI from adding a checkout/user path
    # to context. The supplied full system prompt replaces the coding-agent prompt.
    neutral_cwd = r"C:\Windows\Temp" if os.name == "nt" else "/tmp"
    argv = [binary, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--safe-mode", "--no-session-persistence", "--permission-prompts", "none",
            "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--model", model,
            "--system-prompt", "Return only JSON matching the supplied schema. This request contains only "
            "programmatically generated colored shapes and synthetic public example data. No action will be executed.",
            "--json-schema", json.dumps(schema(), separators=(",", ":"))]
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=neutral_cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError:
        return {**report, "status": "spawn_failed"}
    report["model_calls"] = 1
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload()), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {**report, "status": "timeout", "latency_ms": (time.perf_counter() - started) * 1000}
    report["latency_ms"] = (time.perf_counter() - started) * 1000
    results = []
    try:
        for line in out.decode("utf-8", "replace").splitlines():
            item = json.loads(line)
            if isinstance(item, dict) and item.get("type") == "result":
                results.append(item)
    except ValueError:
        return {**report, "status": "invalid_stream"}
    if len(results) != 1:
        return {**report, "status": "missing_or_multiple_results"}
    result = results[0]
    report["diagnostic"] = {
        key: result.get(key) for key in
        ("subtype", "is_error", "api_error_status", "stop_reason", "terminal_reason")}
    report["diagnostic"]["result_error"] = redacted_detail(
        result.get("result") if result.get("is_error") is True else result.get("errors"))
    report["diagnostic"]["stderr"] = redacted_detail(err.decode("utf-8", "replace"))
    usages = result.get("modelUsage") or {}
    if usages:
        report["actual_model"] = next(iter(usages))
        report["tokens_in"] = sum(int(u.get("inputTokens", 0) or 0) for u in usages.values())
        report["tokens_out"] = sum(int(u.get("outputTokens", 0) or 0) for u in usages.values())
    structured = result.get("structured_output")
    wanted = {"observation_id": "synthetic-generated-shapes-v1", "action": "observe", "synthetic": True}
    if proc.returncode == 0 and result.get("is_error") is not True and result.get("subtype") == "success" and structured == wanted:
        report["status"] = "ok"
    elif result.get("is_error") is True:
        code = result.get("api_error_status")
        report["status"] = f"transport_api_error_{code}" if type(code) is int else "transport_error"
    elif proc.returncode != 0:
        report["status"] = f"process_exit_{proc.returncode}"
    elif structured is None:
        report["status"] = "missing_structured_output"
    else:
        report["status"] = "structured_output_mismatch"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default=shutil.which("claude") or "claude")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive and finite")
    report = asyncio.run(probe(args.binary, args.model, args.timeout))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
