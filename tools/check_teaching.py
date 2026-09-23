"""Offline teaching readiness; an optional single provider call uses a saved PNG.

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
from jev.play.providers import make_vision_client  # noqa: E402
from jev.play.teacher import VisionTeacher  # noqa: E402
from jev.play.tutor import menu, schema  # noqa: E402


def observe_schema() -> dict:
    """The model cannot request a game action or a lookup during this probe."""
    result = schema(menu({}, {}))                  # a menu of exactly one: observe
    result["properties"]["seconds"] = {"type": "number", "const": 0}
    return result


class ObserveOnlyClient:
    """The same constrained smoke contract, independent of the chosen transport."""

    def __init__(self, client):
        self.client, self.model_name = client, client.model_name

    async def ask_image(self, prompt, image_png, *, json_schema, timeout_s):
        return await self.client.ask_image(prompt, image_png, json_schema=observe_schema(),
                                           timeout_s=timeout_s)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--graph", type=Path, default=CONTENT / "ally_human_1_12.json")
    result.add_argument("--world-db", type=Path, default=ROOT / "data/knowledge/tbc-243.sqlite",
                        help="optional read-only exact-server world/DBC snapshot")
    result.add_argument("--bindings", action="append", type=Path, default=[])
    result.add_argument("--teacher-binary")
    result.add_argument("--teacher-effort", choices=("low", "medium", "high", "xhigh", "max"))
    result.add_argument("--teacher-model")
    result.add_argument("--teacher-provider", choices=("claude", "glm"), default="claude")
    result.add_argument("--teacher-base-url")
    result.add_argument("--teacher-key-env", default="GLM_API_KEY")
    result.add_argument("--teacher-env-file", type=Path, default=ROOT / ".env")
    result.add_argument("--timeout", type=float, default=30,
                        help="overall deadline for the optional single model call")
    result.add_argument("--transport-check", action="store_true",
                        help="check provider deployment/auth presence; no model call")
    result.add_argument("--smoke-image", type=Path,
                        help="make one provider request with this original PNG; never execute input")
    result.add_argument("--replay-image", type=Path,
                        help="one full tutor decision on this saved PNG; never executed")
    result.add_argument("--replay-step", help="guide step the replayed frame belongs to")
    result.add_argument("--replay-skill", default="GRIND_UNTIL",
                        help="guide routine armed for the replayed step")
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
    teacher = VisionTeacher(ObserveOnlyClient(client), knowledge=knowledge, max_lookups=0,
                            record_call=calls.append, max_reasks=0)
    controls = {"version": 1, "bindings": {}, "action_slots": [], "skills": []}
    result = await teacher.decide(observation, png, controls=controls,
                                  knowledge=knowledge.context(observation), timeout_s=timeout_s,
                                  allowed=("observe",))
    document = asdict(result)
    document["action"] = action_dict(result.action) if result.action else None
    observe_only = isinstance(result.action, ObserveAction) and result.action.wait_s == 0
    return {"ok": result.ok and observe_only and len(calls) == 1,
            "model_calls": len(calls), "game_input_executed": False,
            "original_png_sha256": observation["screen"]["sha256"],
            "observation": observation, "radio_decoded": reading.ok,
            "radio_fault": str(reading.fault), "reply": document}


async def replay(path: Path, *, client, knowledge, graph, bindings, step: str | None,
                 skill: str, timeout_s: float) -> dict:
    """A real decision on a saved frame, through the production tutor; nothing is executed."""
    from types import SimpleNamespace

    import numpy as np
    from PIL import Image

    from jev.perceive import radio_frame
    from jev.play.observation import context_for, detections
    from jev.play.runtime import delegable_skills
    from jev.play.tutor import render
    from jev.run.body import LiveBody

    png = path.read_bytes()
    with Image.open(path) as image:
        pixels = np.asarray(image.convert("RGB"))
        size = image.size
    reading = radio_frame.read(pixels)
    if not reading.ok:
        raise ValueError(f"replay frame has no readable radio: {reading.fault}")
    captured_at = path.stat().st_mtime
    state = radio_frame.to_state(reading, t=captured_at, client_id="replay")
    manifest = build_manifest(reading.values, binding_paths=bindings,
                              skills=LiveBody.available).to_dict()
    arm = SimpleNamespace(step_id=step, arm_id="replay",
                          decision=SimpleNamespace(skill=skill, goal=f"replay:{skill}", params={}))
    observation = {
        "id": "replay-" + hashlib.sha256(png).hexdigest()[:16], "captured_at": captured_at,
        "values": reading.values, "state": state.model_dump(mode="json"),
        "origin": [0, 0], "size": list(size), "synthetic": True,
        "screen": {"path": str(path), "sha256": hashlib.sha256(png).hexdigest(),
                   "width": size[0], "height": size[1]},
        "detections": detections(pixels, reading.values),
        "context": context_for(graph, arm, state, reading.values),
    }
    skills = delegable_skills(skill, LiveBody.available)
    calls = []
    teacher = VisionTeacher(client, knowledge=knowledge, max_lookups=0, record_call=calls.append)
    result = await teacher.decide(observation, png, controls=manifest, timeout_s=timeout_s,
                                  knowledge=knowledge.context(observation), skills=skills)
    document = asdict(result)
    document["action"] = action_dict(result.action) if result.action else None
    from jev.play.tutor import menu
    prompt = render(observation, choices=menu(observation, manifest, skills=skills),
                    knowledge=knowledge.context(observation))
    return {"ok": result.ok, "model_calls": len(calls), "game_input_executed": False,
            "frame": str(path), "prompt_chars": len(prompt),
            "detections": observation["detections"], "reply": document}


def run(args, *, client=None) -> dict:
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        raise ValueError("--timeout must be positive and finite")
    graph = Graph.load(args.graph)
    knowledge = LocalKnowledge(graph, world_db=args.world_db)
    manifest = build_manifest(binding_paths=args.bindings).to_dict()
    # JSON schema/dependency checks need neither the client nor a model.
    json.dumps(action_schema(), allow_nan=False)
    json.dumps(observe_schema(), allow_nan=False)
    json.dumps(schema(menu({}, manifest)), allow_nan=False)
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
    if client is None and (args.transport_check or args.smoke_image or args.replay_image):
        client = make_vision_client(provider=args.teacher_provider, binary=args.teacher_binary,
                                     model=args.teacher_model, base_url=args.teacher_base_url,
                                     env_file=args.teacher_env_file, key_env=args.teacher_key_env,
                                     effort=args.teacher_effort)
    if args.transport_check:
        report["transport"] = client.preflight()
        report["ok"] &= report["transport"]["ok"]
    if args.smoke_image:
        report["smoke"] = asyncio.run(smoke(args.smoke_image, client=client,
                                           knowledge=knowledge, timeout_s=args.timeout))
        report["model_calls"] = report["smoke"]["model_calls"]
        report["vision_roundtrip_verified"] = report["smoke"]["ok"]
        report["ok"] &= report["smoke"]["ok"]
    if args.replay_image:
        from jev.guide.route import compile_route
        from jev.run.body import LiveBody

        routed = compile_route(graph, available_skills=LiveBody.available).graph
        report["replay"] = asyncio.run(replay(
            args.replay_image, client=client, knowledge=LocalKnowledge(routed, world_db=args.world_db),
            graph=routed, bindings=args.bindings, step=args.replay_step,
            skill=args.replay_skill, timeout_s=args.timeout))
        report["model_calls"] += report["replay"]["model_calls"]
        report["ok"] &= report["replay"]["ok"]
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
        smoke_result = (report.get("smoke") or report.get("replay") or {}).get("reply", {})
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
