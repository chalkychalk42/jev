"""Run the shared coach/tracker over the proven live body. --check never attaches a client."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from jev.clients import win32
from jev.clients.interact import GOSSIP_YARDS
from jev.guide import playhead
from jev.guide.coords import bounds_by_radio_id, navigation_frame
from jev.guide.graph import Graph
from jev.guide.path import MmapQuery
from jev.guide.route import compile_route
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.persist import atomic_json, file_lock, input_lock_path
from jev.run.background import Background
from jev.run.body import LiveBody
from jev.run.client import FOCUS_QUICK_S, ClientSource, NotRunning, attach, with_travel
from jev.run.supervisor import Supervisor
from jev.run.watchdog import Watchdog, reconnect_client
from jev.world.state_v1 import StepKind

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _termination_cleanup():
    """Service termination takes the same cooperative release path as Ctrl-C."""
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate composition without capture or input")
    parser.add_argument("--graph", type=Path, default=ROOT / "content/tbc/ally_human_1_12.json")
    parser.add_argument("--client-id", default="slice")
    parser.add_argument("--playhead", type=Path)
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--steps", type=int, default=0, help="debug cap on completed guide steps")
    parser.add_argument("--run-for", type=float, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--hunt", type=float, default=600)
    parser.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    parser.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    parser.add_argument("--route-mode", choices=("full", "supported"), default="full",
                        help="explicitly select the source guide or its executable subset")
    parser.add_argument("--learn", action="store_true", help="grade and train in the background")
    parser.add_argument("--learning-store", type=Path, default=ROOT / "var/learning")
    parser.add_argument("--policy-mode", choices=("shadow", "adaptive"), default="shadow",
                        help="adaptive permits evidence-gated canaries and promotion")
    parser.add_argument("--teacher", action="store_true", help="enable bounded Claude subscription queue")
    parser.add_argument("--teacher-model", default="sonnet")
    parser.add_argument("--teacher-binary")
    parser.add_argument("--teacher-calls-per-hour", type=int, default=12)
    parser.add_argument("--reconnect", action="store_true", help="use measured Session with credentials from environment")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env",
                        help="optional reconnect credentials; environment takes precedence")
    parser.add_argument("--blind-grace", type=float, default=20)
    parser.add_argument("--no-progress", type=float, default=900)
    parser.add_argument("--reconnect-limit", type=int, default=3)
    args = parser.parse_args(argv)
    if args.run_for <= 0 or args.timeout <= 0 or args.hunt <= 0 or args.retries < 1 or args.steps < 0:
        parser.error("durations and retries must be positive; steps must be nonnegative")
    if (args.blind_grace <= 0 or args.no_progress <= 0 or args.reconnect_limit < 1
            or args.teacher_calls_per_hour < 1):
        parser.error("watchdog durations and budgets must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.client_id):
        parser.error("client-id must contain only letters, numbers, underscores or hyphens")
    if args.playhead is None:
        args.playhead = ROOT / ("var/playhead.json" if args.client_id == "slice"
                                else f"var/playheads/{args.client_id}.json")
    graph = Graph.load(args.graph)
    memory = playhead.load(graph.graph_id, args.playhead)
    route = compile_route(graph, available_skills=LiveBody.available,
                          completed_quests=memory.completed)
    if args.route_mode == "supported":
        graph = route.graph
        memory = playhead.load(graph.graph_id, args.playhead)
    if args.check:
        unavailable = sorted({s for n in graph.nodes for s in n.skills} - LiveBody.available)
        target_kinds = {StepKind.QUEST_ACCEPT, StepKind.QUEST_TURNIN,
                        StepKind.QUEST_OBJECTIVE, StepKind.GRIND, StepKind.REPAIR}
        unsupported_targets = [{"step_id": n.id, "kind": n.target_kind,
                                "name": n.target_name}
                               for n in graph.nodes if n.kind in target_kinds
                               and (n.target_kind != "creature" or not n.target_name)]
        print(json.dumps({"graph": graph.graph_id, "entry": graph.entry,
                          "executors": sorted(LiveBody.available),
                          "unimplemented_catalog_skills": unavailable,
                          "unsupported_targets": unsupported_targets,
                          "route_mode": args.route_mode,
                          "coordinate_frame": graph.coord_zone_id,
                          "supported_quests": len({n.quest_id for n in route.graph.nodes if n.quest_id}),
                          "route_exclusions": [asdict(e) for e in route.excluded],
                          "learning": args.learn, "policy_mode": args.policy_mode,
                          "teacher": args.teacher, "reconnect": args.reconnect,
                          "live_tested": False}, indent=2))
        return 0
    if graph.coord_zone_id is None:
        print("guide has no declared navigation frame; regenerate it before live execution")
        return 2
    try:
        with _termination_cleanup(), file_lock(input_lock_path(), blocking=False):
            return _live(args, graph, memory, route)
    except BlockingIOError:
        print("another live supervisor owns this user's input; no client was attached")
        return 2


def _live(args, graph, memory, route) -> int:
    try:
        client = attach(args.client_id)
    except NotRunning as exc:
        print(exc)
        return 2
    recorder = supervisor = background = None
    try:
        if not client.focused():
            raise NotRunning("client is not focused")
        values = client.read()
        if values is None and args.reconnect:
            result = reconnect_client(client, lambda: None, env_file=args.env_file)
            if result.code != "reconnected":
                raise NotRunning(result.detail)
            values = client.read()
        if values is None or client.quest_ids() is None:
            raise NotRunning("radio or complete quest log unavailable")
        zones = bounds_by_radio_id(str(ROOT / "data/zones-tbc-243.json"))
        bounds = navigation_frame(graph.coord_zone_id, values.get("pos.zone_id"), zones)
        if bounds is None:
            raise NotRunning("zone has no measured coordinate bounds")
        launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
        with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher,
                    checkpoint=lambda: client.hid.checkpoint() if client.hid.checkpoint else None),
                    arrival_yards=GOSSIP_YARDS, say=print, zones=zones)
        body = LiveBody(client, graph, travel_timeout=args.timeout, hunt_timeout=args.hunt)
        recorder = Recorder(root=args.runs_dir)
        atomic_json(recorder.dir / "route.json", {
            "mode": args.route_mode, "source": route.source_graph_id, "graph": graph.graph_id,
            "graph_digest": hashlib.sha256(json.dumps(graph.model_dump(mode="json"),
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "excluded": [asdict(e) for e in route.excluded] if args.route_mode == "supported" else [],
        })
        runtime = ClientRuntime(
            client_id=args.client_id, graph=graph, source=ClientSource(client), recorder=recorder,
            keys_down=client.hid.keys_down, start_step=memory.step_id,
            completed=set(memory.completed),
            on_progress=lambda step, done: playhead.save(graph.graph_id, step, done, args.playhead),
            available_skills=body.available,
            validate_action=body.validate,
        )
        teacher = None
        if args.teacher:
            try:
                from jev.teacher.client import ClaudeSubscriptionClient
                teacher = ClaudeSubscriptionClient(binary=args.teacher_binary, model=args.teacher_model)
            except Exception as exc:
                print(f"teacher unavailable; scripted floor continues: {type(exc).__name__}: {exc}")
        background = Background(runtime, runs=args.runs_dir, store=args.learning_store,
                                learn=args.learn, adaptive=args.policy_mode == "adaptive",
                                teacher_client=teacher, calls_per_hour=args.teacher_calls_per_hour)
        watchdog = Watchdog(blind_grace_s=args.blind_grace, no_progress_s=args.no_progress,
                            reconnect_limit=args.reconnect_limit,
                            reconnect=(lambda checkpoint: reconnect_client(client, checkpoint,
                                                                            env_file=args.env_file))
                            if args.reconnect else None)
        supervisor = Supervisor(runtime, body, max_failures=args.retries,
                                has_focus=client.hid.ready,
                                focus=lambda checkpoint: client.focused(FOCUS_QUICK_S,
                                                                         checkpoint=checkpoint),
                                housekeeping=background.poll, watchdog=watchdog)
        print(f"recording to {recorder.dir}")
        supervisor.run(args.run_for, max_steps=args.steps)
        if supervisor.failure:
            print(f"stopped: {supervisor.failure}")
            return 1
        return 0
    except NotRunning as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            if supervisor is not None:
                supervisor.close()
        finally:
            try:
                if background is not None:
                    background.close()
            finally:
                try:
                    if recorder is not None:
                        recorder.close()
                finally:
                    client.close()


if __name__ == "__main__":
    raise SystemExit(main())
