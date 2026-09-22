"""Offline teaching readiness; an optional single subscription call uses a saved PNG.

This program has no client attachment, focus, capture or input path. The optional smoke
request only permits an observe reply and never executes it. Default operation makes no
model call. Credentials and raw authentication output are never printed or persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev.guide.graph import Graph  # noqa: E402
from jev.persist import atomic_json  # noqa: E402
from jev.play.actions import ObserveAction, action_dict, action_schema  # noqa: E402
from jev.play.controls import build_manifest  # noqa: E402
from jev.play.knowledge import CONTENT, LocalKnowledge  # noqa: E402
from jev.play.observation import fingerprint  # noqa: E402
from jev.play.teacher import ClaudeVisionClient, VisionTeacher, reply_schema  # noqa: E402


def observe_schema() -> dict:
    """The model cannot request a game action or a second lookup during this probe."""
    schema = reply_schema()
    schema["properties"]["action"] = {"$ref": "#/$defs/ObserveAction"}
    schema["properties"]["lookup"] = {"type": "null"}
    schema["properties"]["capability"] = {"type": "string", "const": "observe"}
    schema["properties"]["expected_effect"] = {"type": "string", "const": "observed"}
    schema["$defs"]["ObserveAction"]["properties"]["wait_s"] = {"type": "number", "const": 0}
    return schema


class ObserveOnlyClient(ClaudeVisionClient):
    async def ask_image(self, prompt, image_png, *, json_schema, timeout_s):
        return await super().ask_image(prompt, image_png, json_schema=observe_schema(),
                                       timeout_s=timeout_s)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--graph", type=Path, default=CONTENT / "ally_human_1_12.json")
    result.add_argument("--world-db", type=Path, default=ROOT / "data/knowledge/tbc-243.sqlite",
                        help="optional read-only exact-server world/DBC snapshot")
    result.add_argument("--bindings", action="append", type=Path, default=[])
    result.add_argument("--teacher-binary")
    result.add_argument("--teacher-model", default="sonnet")
    result.add_argument("--timeout", type=float, default=30,
                        help="overall deadline for the optional single model call")
    result.add_argument("--transport-check", action="store_true",
                        help="check CLI flags and sanitized subscription status; no model call")
    result.add_argument("--smoke-image", type=Path,
                        help="make one subscription request with this original PNG; never execute input")
    result.add_argument("--output", type=Path)
    return result


async def smoke(path: Path, *, client, knowledge, timeout_s: float) -> dict:
    import numpy as np
    from PIL import Image

    from jev.perceive import radio_frame

    png = path.read_bytes()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("--smoke-image must be an original PNG")
    with Image.open(path) as image:
        pixels = np.asarray(image.convert("RGB"))
        size = image.size
    reading = radio_frame.read(pixels)
    captured_at = path.stat().st_mtime
    observation = {
        "id": "offline-transport-" + hashlib.sha256(png).hexdigest()[:16],
        "captured_at": captured_at, "values": reading.values or {},
        "state": radio_frame.to_state(reading, t=captured_at, client_id="offline-transport")
                 .model_dump(mode="json") if reading.ok else {},
        "origin": [0, 0], "size": list(size), "synthetic": True,
        "screen": {"path": str(path), "sha256": hashlib.sha256(png).hexdigest(),
                   "width": size[0], "height": size[1]},
        "context": {"skill": "OFFLINE_VERIFY", "step_id": None,
                    "goal": "Offline transport verification; choose observe only; no game input will be executed"},
    }
    calls = []
    teacher = VisionTeacher(client, knowledge=knowledge, max_lookups=0, record_call=calls.append)
    controls = {"version": 1, "bindings": {}, "action_slots": [], "skills": [],
                "allowed_actions": [{"kind": "observe", "wait_s": 0}],
                "mode": "offline_read_only_transport_check",
                "instruction": "Return action kind observe with wait_s 0. No game action is available."}
    result = await teacher.decide(observation, png, controls=controls,
                                  knowledge=knowledge.context(observation), timeout_s=timeout_s)
    document = asdict(result)
    document["action"] = action_dict(result.action) if result.action else None
    observe_only = (isinstance(result.action, ObserveAction) and result.action.wait_s == 0
                    and result.capability == "observe" and result.expected_effect == "observed")
    return {"ok": result.ok and observe_only and len(calls) == 1,
            "model_calls": len(calls), "game_input_executed": False,
            "original_png_sha256": observation["screen"]["sha256"],
            "observation": observation, "radio_decoded": reading.ok,
            "radio_fault": str(reading.fault), "reply": document}


def run(args, *, client=None) -> dict:
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        raise ValueError("--timeout must be positive and finite")
    graph = Graph.load(args.graph)
    knowledge = LocalKnowledge(graph, world_db=args.world_db)
    manifest = build_manifest(binding_paths=args.bindings).to_dict()
    # JSON schema/dependency checks need neither the client nor a model.
    json.dumps(action_schema(), allow_nan=False)
    json.dumps(observe_schema(), allow_nan=False)
    dependencies = {name: importlib.util.find_spec(name) is not None
                    for name in ("pydantic", "numpy", "PIL")}
    report = {"schema": 1, "checked_at": time.time(), "ok": all(dependencies.values()),
              "game_input_executed": False, "model_calls": 0,
              "offline": {"graph_id": graph.graph_id, "graph_schema": graph.schema_version,
                          "graph_nodes": len(graph.nodes), "dependencies": dependencies,
                          "knowledge_fingerprint": knowledge.fingerprint,
                          "knowledge_sources": knowledge.sources,
                          "world_database": {"path": str(args.world_db),
                                             "available": any(source.get("name") == "world_database"
                                                              and source.get("available") is True
                                                              for source in knowledge.sources)},
                          "controls_fingerprint": fingerprint(manifest),
                          "binding_sources": manifest["sources"],
                          "binding_inventory_count": len(manifest["binding_inventory"]),
                          "executable_controls": sorted(k for k, v in manifest["bindings"].items()
                                                        if v["executable"]),
                          "schemas_validated": True},
              "vision_roundtrip_verified": False}
    client = client or ObserveOnlyClient(binary=args.teacher_binary, model=args.teacher_model)
    if args.transport_check:
        report["transport"] = client.preflight()
        report["ok"] &= report["transport"]["ok"]
    if args.smoke_image:
        report["smoke"] = asyncio.run(smoke(args.smoke_image, client=client,
                                           knowledge=knowledge, timeout_s=args.timeout))
        report["model_calls"] = report["smoke"]["model_calls"]
        report["vision_roundtrip_verified"] = report["smoke"]["ok"]
        report["ok"] &= report["smoke"]["ok"]
    return report


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:
        report = {"schema": 1, "ok": False, "game_input_executed": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    if args.output:
        atomic_json(args.output, report)
        smoke_result = report.get("smoke", {}).get("reply", {})
        print(json.dumps({"ok": report["ok"], "output": str(args.output),
                          "model_calls": report.get("model_calls", 0),
                          "game_input_executed": False,
                          "requested_model": smoke_result.get("requested_model"),
                          "actual_model": smoke_result.get("actual_model"),
                          "status": smoke_result.get("status"),
                          "error": report.get("error")}, indent=2))
    else:
        print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
