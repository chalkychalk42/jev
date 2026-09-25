"""Run the shared coach/tracker over the proven live body. --check never attaches a client."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import signal
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from jev.clients import operator, win32
from jev.clients.interact import GOSSIP_YARDS
from jev.guide import playhead, spawns
from jev.guide.coords import bounds_by_radio_id, navigation_frame
from jev.guide.graph import Graph
from jev.guide.path import MmapQuery
from jev.guide.route import compile_route
from jev.guide.route_memory import RouteMemory
from jev.learn.choices import ChoiceLog, ChoiceMemory, backfill_hunts
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.persist import atomic_json, file_lock, input_lock_path
from jev.run.background import Background
from jev.run.body import LiveBody
from jev.run.client import FOCUS_QUICK_S, ClientSource, NotRunning, attach, with_travel
from jev.run.paths import default_learning_store
from jev.run.screenshots import ScreenshotError, Screenshots
from jev.run.supervisor import Supervisor
from jev.run.watchdog import Watchdog, reconnect_client
from jev.world.state_v1 import StepKind

ROOT = Path(__file__).resolve().parents[2]
# How long an operator stop waits for a fight in progress to end before stopping anyway.
STOP_COMBAT_GRACE_S = 90.0
# Reads to wait at start for one whole quest-log cycle (about 0.13 s each). Forty failed
# sessions 97 and 98 with "complete quest log unavailable" while the radio painted.
STARTUP_LOG_TRIES = 250


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
    parser.add_argument("--screenshots", action="store_true",
                        help="record lossless client screenshots every second for supervised tests")
    parser.add_argument("--stop-file", type=Path,
                        help="stop cooperatively when this file exists; the file is never deleted")
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
    parser.add_argument("--learning-store", type=Path)
    parser.add_argument("--policy-mode", choices=("shadow", "adaptive"), default="shadow",
                        help="adaptive permits evidence-gated canaries and promotion")
    parser.add_argument("--teacher", action="store_true", help="enable bounded Claude subscription queue")
    parser.add_argument("--teacher-model", help="defaults to Sonnet for Claude or GLM-4.6V-Flash for GLM")
    parser.add_argument("--teacher-provider", choices=("claude", "glm"), default="claude")
    parser.add_argument("--teacher-base-url", help="explicit official GLM API endpoint")
    parser.add_argument("--teacher-key-env", default="GLM_API_KEY",
                        help="credential variable name, never the credential value")
    parser.add_argument("--teacher-env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--teacher-binary")
    parser.add_argument("--teacher-effort", choices=("low", "medium", "high", "xhigh", "max"),
                        help="Claude reasoning effort; unset leaves the CLI default")
    parser.add_argument("--teacher-calls-per-hour", type=int, default=12)
    parser.add_argument("--play-mode", choices=("off", "teach", "adaptive"), default="off",
                        help="visual Jev actions inside guide skills; adaptive enables evaluated motor handover")
    parser.add_argument("--play-dispatch", choices=("tutor", "hybrid"), default="tutor",
                        help="hybrid: the guide's routine first, the tutor on its failures "
                             "and a fixed sample of objectives")
    parser.add_argument("--play-teacher-calls-per-hour", type=int, default=240,
                        help="separate motor tutor budget, counting each actual request/lookup")
    parser.add_argument("--play-decision-timeout", type=float, default=30,
                        help="bounded visual tutor decision deadline in seconds")
    parser.add_argument("--bindings", type=Path, action="append", default=[],
                        help="saved bindings-cache.wtf, account first then character overrides")
    parser.add_argument("--world-db", type=Path, default=ROOT / "data/knowledge/tbc-243.sqlite",
                        help="read-only exact-server world/DBC knowledge snapshot for the visual tutor")
    parser.add_argument("--reconnect", action="store_true", help="use measured Session with credentials from environment")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env",
                        help="optional reconnect credentials; environment takes precedence")
    parser.add_argument("--blind-grace", type=float, default=20)
    parser.add_argument("--no-progress", type=float, default=900)
    parser.add_argument("--reconnect-limit", type=int, default=3)
    args = parser.parse_args(argv)
    from jev.play.providers import model_for

    args.teacher_model = model_for(args.teacher_provider, args.teacher_model)
    if args.teacher_provider != "claude" and args.play_mode == "off":
        parser.error("GLM is available for visual playing; select --play-mode teach or adaptive")
    if args.learning_store is None:
        try:
            args.learning_store = default_learning_store(ROOT)
        except ValueError as exc:
            parser.error(f"{exc} (override: --learning-store PATH)")
    if args.run_for <= 0 or args.timeout <= 0 or args.hunt <= 0 or args.retries < 1 or args.steps < 0:
        parser.error("durations and retries must be positive; steps must be nonnegative")
    if (args.blind_grace <= 0 or args.no_progress <= 0 or args.reconnect_limit < 1
            or args.teacher_calls_per_hour < 1 or args.play_teacher_calls_per_hour < 1
            or not math.isfinite(args.play_decision_timeout) or args.play_decision_timeout <= 0):
        parser.error("watchdog durations and budgets must be positive")
    if args.play_mode != "off":
        args.screenshots = args.learn = True
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.client_id):
        parser.error("client-id must contain only letters, numbers, underscores or hyphens")
    graph = Graph.load(args.graph)
    if args.check:
        # Offline there is no character, so nothing is taken as done yet.
        route = compile_route(graph, available_skills=LiveBody.available)
        if args.route_mode == "supported":
            graph = route.graph
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
                          "play_mode": args.play_mode,
                          "visual_teacher": args.play_mode != "off",
                          "motor_learning": args.play_mode != "off",
                          "motor_handover": args.play_mode == "adaptive",
                          "play_teacher_calls_per_hour": args.play_teacher_calls_per_hour,
                          "teacher_provider": args.teacher_provider,
                          "teacher_requested": args.teacher_model,
                          "teacher_effort": args.teacher_effort,
                          "bindings": [str(path) for path in args.bindings],
                          "world_db": str(args.world_db),
                          "screenshots": args.screenshots,
                          "stop_file": str(args.stop_file) if args.stop_file else None,
                          "live_tested": False}, indent=2))
        return 0
    if args.stop_file is not None and args.stop_file.exists():
        print(f"operator stop file observed: {args.stop_file}")
        return 130
    if graph.coord_zone_id is None:
        print("guide has no declared navigation frame; regenerate it before live execution")
        return 2
    try:
        with _termination_cleanup(), file_lock(input_lock_path(), blocking=False):
            return _live(args, graph)
    except BlockingIOError:
        print("another live supervisor owns this user's input; no client was attached")
        return 2


# The guide that follows each, by graph id. A character whose playhead says a guide is
# finished starts the next session on the one after it; the quests it has done carry over.
NEXT_GUIDE: dict[str, Path] = {
    "alli_human_1_12": ROOT / "content/tbc/ally_human_12_20.json",
}


def remembered(args, graph, key: int | None):
    """This character's playhead and route: its own file, found by the key the strip
    paints, so each character keeps its own place in the guide (`playhead`)."""
    if args.playhead is not None:
        path = args.playhead
    elif key is None:
        raise NotRunning("the strip does not say which character this is (an addon older "
                         "than schema 13): install it with tools/gen_addon_fields.py "
                         "--install and restart the client")
    else:
        path = playhead.for_character(key, ROOT / playhead.CHARACTERS)
    for _ in range(len(NEXT_GUIDE) + 1):
        memory = playhead.load(graph.graph_id, path)
        route = compile_route(graph, available_skills=LiveBody.available,
                              completed_quests=memory.completed)
        used = route.graph if args.route_mode == "supported" else graph
        if used is not graph:
            memory = playhead.load(used.graph_id, path)
        following = NEXT_GUIDE.get(graph.graph_id)
        if not memory.finished or following is None or not following.exists():
            return path, memory, route, used
        # This character finished the guide: the next one takes over, its quests carried.
        print(f"guide {graph.graph_id} finished; continuing with {following.name}")
        args.graph = following
        graph = Graph.load(following)
    return path, memory, route, used


def _live(args, graph) -> int:
    try:
        client = attach(args.client_id)
    except NotRunning as exc:
        print(exc)
        return 2
    recorder = supervisor = background = screenshots = playing = None

    def operator_checkpoint():
        if args.stop_file is not None and args.stop_file.exists():
            print(f"operator stop file observed: {args.stop_file}")
            raise KeyboardInterrupt

    def startup_checkpoint():
        operator_checkpoint()
        if screenshots is not None and screenshots.error:
            raise ScreenshotError(screenshots.error)

    try:
        startup_checkpoint()
        if args.screenshots:
            recorder = Recorder(root=args.runs_dir)
            # Keep ownership before start: interruption after the thread launches
            # must still join it before releasing the shared capture handles.
            screenshots = Screenshots(client.frame, recorder.dir / "screenshots")
            screenshots.start()
        startup_checkpoint()
        # A person at the desk: wait for them rather than fail. A failed start costs the
        # loop a restart and three in a row stop it; the waiting session costs nothing.
        if operator.active():
            print("operator active: waiting for the desk to be quiet before starting")
            while operator.active():
                startup_checkpoint()
                time.sleep(1)
            print("operator quiet: starting")
        if not client.focused(checkpoint=startup_checkpoint):
            raise NotRunning("client is not focused")
        values = client.read()
        if values is None and args.reconnect:
            result = reconnect_client(client, startup_checkpoint, env_file=args.env_file)
            if result.code != "reconnected":
                raise NotRunning(result.detail)
            values = client.read()
        startup_checkpoint()
        if values is None or client.quest_ids(tries=STARTUP_LOG_TRIES) is None:
            # What was seen, for the next time: four sessions in a row stopped here while the
            # strip painted, and a separate reader assembled the same log in under a second.
            seen, count = client.log.progress
            raise NotRunning(f"radio or complete quest log unavailable (reading "
                             f"{'none' if values is None else 'ok'}, strip frozen "
                             f"{client.frozen_for():.1f} s, quest slots {seen} of {count})")
        # Which character is logged in decides whose playhead this run keeps.
        character = values.get("char.key")
        path, memory, route, graph = remembered(args, graph, character)
        print(f"character {character:08x}: playhead {path}" if character is not None
              else f"playhead {path}")
        zones = bounds_by_radio_id(str(ROOT / "data/zones-tbc-243.json"))
        bounds = navigation_frame(graph.coord_zone_id, values.get("pos.zone_id"), zones)
        if bounds is None:
            raise NotRunning("zone has no measured coordinate bounds")
        launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
        with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher,
                    checkpoint=lambda: client.hid.checkpoint() if client.hid.checkpoint else None),
                    arrival_yards=GOSSIP_YARDS, say=print, zones=zones,
                    route_memory=RouteMemory(ROOT / "var/route-memory.json"))
        body = LiveBody(client, graph, travel_timeout=args.timeout, hunt_timeout=args.hunt,
                        record_frame=screenshots.record_frame if screenshots is not None else None,
                        hunt_spawns=spawns.load(args.graph),
                        gear_memory=path.with_name(path.stem + ".equipped.json"),
                        merchant_memory=ROOT / "var" / "merchant-memory.json",
                        home_memory=path.with_name(path.stem + ".home.json"),
                        taxi_memory=path.with_name(path.stem + ".taxi.json"))
        if recorder is None:
            recorder = Recorder(root=args.runs_dir)
        # What each choice has paid off before (`jev.learn.choices`), counted first from any
        # runs that predate the choices' own log; this run logs its own.
        body.choice_memory = ChoiceMemory(ROOT / "var" / "choices.json")
        counted = backfill_hunts((run for run in Path(args.runs_dir).iterdir() if run.is_dir()),
                                 body.choice_memory)
        body.choice_log = ChoiceLog(recorder.dir / "choices.jsonl")
        visits = sum(arm.tries for arm in body.choice_memory.arms("hunt.station").values())
        print(f"choices: {visits} hunt station visits remembered"
              + (f", {counted} counted from earlier runs" if counted else ""))
        if args.play_mode != "off":
            from jev.play.controller import PlayConfig
            from jev.play.runtime import PlayingBody

            playing = PlayingBody(
                body, recorder=recorder, store=args.learning_store, screenshots=screenshots,
                mode=args.play_mode, teacher_model=args.teacher_model,
                teacher_binary=args.teacher_binary,
                teacher_provider=args.teacher_provider, teacher_base_url=args.teacher_base_url,
                teacher_env_file=args.teacher_env_file, teacher_key_env=args.teacher_key_env,
                teacher_effort=args.teacher_effort,
                teacher_calls_per_hour=args.play_teacher_calls_per_hour,
                binding_paths=args.bindings, world_db=args.world_db,
                config=PlayConfig(mode=args.play_mode, teacher_timeout_s=args.play_decision_timeout),
                dispatch=args.play_dispatch)
            body = playing
        atomic_json(recorder.dir / "route.json", {
            "mode": args.route_mode, "source": route.source_graph_id, "graph": graph.graph_id,
            "graph_digest": hashlib.sha256(json.dumps(graph.model_dump(mode="json"),
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "excluded": [asdict(e) for e in route.excluded] if args.route_mode == "supported" else [],
        })
        runtime = ClientRuntime(
            client_id=args.client_id, graph=graph, source=ClientSource(client), recorder=recorder,
            keys_down=client.hid.keys_down, start_step=memory.step_id,
            start_rejoin=memory.rejoin_to, start_deaths=memory.deaths,
            start_retried=memory.retried, start_rib_until=memory.rib_until,
            completed=set(memory.completed),
            on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None,
            finished=False: playhead.save(graph.graph_id, step, done, path, rejoin_to=rejoin,
                                          deaths=deaths, retried=retried, rib_until=until,
                                          finished=finished),
            character_key=character,
            available_skills=body.available,
            validate_action=body.validate,
        )
        teacher = None
        if args.teacher and args.play_mode == "off":
            try:
                from jev.teacher.client import ClaudeSubscriptionClient
                teacher = ClaudeSubscriptionClient(binary=args.teacher_binary, model=args.teacher_model,
                                                   effort=args.teacher_effort)
            except Exception as exc:
                print(f"teacher unavailable; scripted floor continues: {type(exc).__name__}: {exc}")
        background = Background(runtime, runs=args.runs_dir, store=args.learning_store,
                                learn=args.learn, adaptive=args.policy_mode == "adaptive",
                                teacher_client=teacher, calls_per_hour=args.teacher_calls_per_hour)
        if args.learn or args.policy_mode == "adaptive" or args.teacher:
            print(f"learning store: {args.learning_store}")
        watchdog = Watchdog(blind_grace_s=args.blind_grace, no_progress_s=args.no_progress,
                            reconnect_limit=args.reconnect_limit,
                            reconnect=(lambda checkpoint: body.reconnect(checkpoint,
                                                                         env_file=args.env_file))
                            if args.reconnect else None)
        stop_seen = []

        def housekeeping(state):
            # An operator stop waits out a fight, up to `STOP_COMBAT_GRACE_S`: a session
            # stopped at 25% health mid-fight left the character standing idle through the
            # restart, and it died (run 20260924T075209-e0395d).
            if args.stop_file is not None and args.stop_file.exists():
                now = time.monotonic()
                if not stop_seen:
                    stop_seen.append(now)
                fighting = (state is not None and state.vitals.combat is True
                            and state.vitals.dead is not True and state.vitals.ghost is not True)
                if fighting and now - stop_seen[0] < STOP_COMBAT_GRACE_S:
                    if len(stop_seen) == 1:
                        stop_seen.append(now)
                        print("operator stop file observed; stopping once this fight is over")
                    return
            operator_checkpoint()
            if screenshots is not None and screenshots.error:
                supervisor.failure = screenshots.error
                supervisor.stopped.set()
                if supervisor.worker is not None:
                    supervisor.worker.cancel(screenshots.error)
                return
            background.poll(state)
            if playing is not None:
                playing.poll(state)

        supervisor = Supervisor(runtime, body, max_failures=args.retries,
                                has_focus=body.has_focus,
                                focus=lambda checkpoint: client.focused(FOCUS_QUICK_S,
                                                                         checkpoint=checkpoint),
                                housekeeping=housekeeping, watchdog=watchdog,
                                operator_active=operator.active,
                                operator_suspected=operator.suspected)
        print(f"recording to {recorder.dir}")
        supervisor.run(args.run_for, max_steps=args.steps)
        if screenshots is not None:
            screenshots.close()
            if screenshots.error:
                print(f"stopped: {screenshots.error}")
                return 1
        if supervisor.failure:
            print(f"stopped: {supervisor.failure}")
            return 1
        return 0
    except (NotRunning, ScreenshotError) as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            if supervisor is not None:
                supervisor.close()
            else:
                client.hid.checkpoint = None
                client.hid.release_all()
        finally:
            try:
                try:
                    if playing is not None:
                        playing.close()
                finally:
                    if screenshots is not None:
                        screenshots.close()
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
