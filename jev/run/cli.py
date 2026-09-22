"""Run the shared coach/tracker over the proven live body. --check never attaches a client."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jev.clients import win32
from jev.clients.interact import GOSSIP_YARDS
from jev.guide import playhead
from jev.guide.coords import bounds_by_radio_id
from jev.guide.graph import Graph
from jev.guide.path import MmapQuery
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.run.body import LiveBody
from jev.run.client import FOCUS_QUICK_S, ClientSource, NotRunning, attach, with_travel
from jev.run.supervisor import Supervisor
from jev.world.state_v1 import StepKind

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate composition without capture or input")
    parser.add_argument("--graph", type=Path, default=ROOT / "content/tbc/ally_human_1_12.json")
    parser.add_argument("--client-id", default="slice")
    parser.add_argument("--playhead", type=Path, default=ROOT / "var/playhead.json")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--steps", type=int, default=0, help="debug cap on completed guide steps")
    parser.add_argument("--run-for", type=float, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--hunt", type=float, default=600)
    parser.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    parser.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    args = parser.parse_args(argv)
    if args.run_for <= 0 or args.timeout <= 0 or args.hunt <= 0 or args.retries < 1 or args.steps < 0:
        parser.error("durations and retries must be positive; steps must be nonnegative")
    graph = Graph.load(args.graph)
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
                          "unsupported_targets": unsupported_targets}, indent=2))
        return 0
    try:
        client = attach(args.client_id)
    except NotRunning as exc:
        print(exc)
        return 2
    recorder = supervisor = None
    try:
        if not client.focused():
            raise NotRunning("client is not focused")
        values = client.read()
        if values is None or client.quest_ids() is None:
            raise NotRunning("radio or complete quest log unavailable")
        bounds = bounds_by_radio_id(str(ROOT / "data/zones-tbc-243.json")).get(values.get("pos.zone_id"))
        if bounds is None:
            raise NotRunning("zone has no measured coordinate bounds")
        launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
        with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher,
                    checkpoint=lambda: client.hid.checkpoint() if client.hid.checkpoint else None),
                    arrival_yards=GOSSIP_YARDS, say=print)
        body = LiveBody(client, graph, travel_timeout=args.timeout, hunt_timeout=args.hunt)
        memory = playhead.load(graph.graph_id, args.playhead)
        recorder = Recorder(root=args.runs_dir)
        runtime = ClientRuntime(
            client_id=args.client_id, graph=graph, source=ClientSource(client), recorder=recorder,
            keys_down=client.hid.keys_down, start_step=memory.step_id,
            completed=set(memory.completed),
            on_progress=lambda step, done: playhead.save(graph.graph_id, step, done, args.playhead),
        )
        supervisor = Supervisor(runtime, body, max_failures=args.retries,
                                has_focus=client.hid.ready,
                                focus=lambda checkpoint: client.focused(FOCUS_QUICK_S,
                                                                         checkpoint=checkpoint))
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
                if recorder is not None:
                    recorder.close()
            finally:
                client.close()


if __name__ == "__main__":
    raise SystemExit(main())
