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
from dataclasses import asdict, replace
from pathlib import Path

from jev.clients import operator, win32
from jev.clients.interact import GOSSIP_YARDS
from jev.guide import playhead, spawns
from jev.guide.coords import bounds_by_radio_id, navigation_frame
from jev.guide.generate import with_rib_levels
from jev.guide.graph import Graph
from jev.guide.path import MmapQuery
from jev.guide.route import compile_route
from jev.guide.route_memory import RouteMemory
from jev.learn.choices import Choice, ChoiceLog, ChoiceMemory, backfill_hunts
from jev.learn.danger import DangerMap, count_runs, runs_in
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.persist import atomic_json, file_lock, input_lock_path
from jev.run.body import LiveBody
from jev.run.client import FOCUS_QUICK_S, ClientSource, NotRunning, attach, with_travel
from jev.run.paths import default_learning_store
from jev.run.screenshots import ScreenshotError, Screenshots
from jev.run.supervisor import Supervisor
from jev.run.watchdog import Watchdog, reconnect_client
from jev.world.state_v1 import StepKind

ROOT = Path(__file__).resolve().parents[2]
# Another's learning, read beside the character's own at a discount and never written
# (V290): the operator puts it here; deleting the directory undoes it.
PRIOR = ROOT / "var" / "prior"
# How long an operator stop waits for a fight in progress to end before stopping anyway.
STOP_COMBAT_GRACE_S = 90.0
# Reads to wait at start for one whole quest-log cycle (about 0.13 s each). Forty failed
# sessions 97 and 98 with "complete quest log unavailable" while the radio painted.
STARTUP_LOG_TRIES = 250
# Attempts a step's own skill gets in a session before the step fails over to its rib
# (`Supervisor.max_failures`). The hive plays with these too (V341): with one, its first
# unreachable walk to a quest giver was the quest's failure: 203 of the 383 quest steps failed
# over in its 4 Oct 16:26-18:30, where the live bot would have walked again.
RETRIES = 3


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
    parser.add_argument("--retries", type=int, default=RETRIES)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--hunt", type=float, default=600)
    parser.add_argument("--mmaps", default="/home/ash/cmangos/run/bin/mmaps")
    parser.add_argument("--jevpath", default="/home/ash/ForeverV2/tools/jevpath/jevpath")
    parser.add_argument("--route-mode", choices=("full", "supported"), default="full",
                        help="explicitly select the source guide or its executable subset")
    parser.add_argument("--learning-store", type=Path)
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
    parser.add_argument("--play-mode", choices=("off", "teach"), default="off",
                        help="visual Jev actions inside guide skills, from the tutor")
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
    parser.add_argument("--no-coach-model", action="store_true",
                        help="the scripted coach decides alone; Jev is not asked")
    parser.add_argument("--reconnect-limit", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.check:
        print(f"startup: arguments read at {time.strftime('%H:%M:%S')}", flush=True)
    from jev.play.providers import model_for

    args.teacher_model = model_for(args.teacher_provider, args.teacher_model)
    if args.teacher_provider != "claude" and args.play_mode == "off":
        parser.error("GLM is available for visual playing; select --play-mode teach")
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
        # Screenshots, not learning: since V158 no imitation student acts, and training
        # them inside the live process re-read every run each cycle, held the lock and cost
        # the fight and the walk their reaction time (V174). The corpus is still recorded.
        args.screenshots = True
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.client_id):
        parser.error("client-id must contain only letters, numbers, underscores or hyphens")
    graph = Graph.load(args.graph)
    if args.check:
        # Offline there is no character, so nothing is taken as done yet.
        route = compile_route(graph, available_skills=LiveBody.available,
                              worthless=worthless_quests(args.world_db, graph))
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
                          "teacher": args.teacher, "reconnect": args.reconnect,
                          "play_mode": args.play_mode,
                          "visual_teacher": args.play_mode != "off",
                          "motor_learning": False,          # recorded, not trained (V174)
                          "motor_recording": args.play_mode != "off",
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
# The level each guide with a next is outgrown at: from it, between two quests, the complete
# quests' hand-ins nearby are made and the next guide takes over (V162,
# `ClientRuntime.outgrown_at`).
OUTGROWN_AT: dict[str, int] = {
    "alli_human_1_12": 13,
}


def outgrown_at(graph_id: str) -> int | None:
    """The level a guide with a next is outgrown at: its own (V162), and never before the next
    guide takes over (V280, amended): at 13 the 1-12 guide was outgrown while the 12-20 guide
    waited for 14, and sessions 277-279 each ended within seconds of starting."""
    following = NEXT_GUIDE.get(graph_id)
    if following is None:
        return None
    own, entry = OUTGROWN_AT.get(graph_id), entry_level(following)
    if own is None or entry is None:
        return own if own is not None else entry
    return max(own, entry)


def worthless_quests(world_db: Path | None, graph: Graph) -> frozenset[int]:
    """The guide's quests that pay no experience and offer no reward to choose, from the
    world snapshot (`compile_route`'s `worthless`, V163). Nothing when it cannot be read."""
    import sqlite3

    from jev.play.world_knowledge import readonly_uri

    quests = sorted({n.quest_id for n in graph.nodes if n.quest_id is not None})
    if not quests or world_db is None or not Path(world_db).is_file():
        return frozenset()
    try:
        connection = sqlite3.connect(readonly_uri(Path(world_db)), uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT entry FROM world_quest_template WHERE RewMoneyMaxLevel = 0 "
                f"AND RewChoiceItemId1 = 0 AND entry IN ({','.join('?' * len(quests))})",
                quests).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return frozenset()
    return frozenset(row[0] for row in rows)


def _guide_on(args, graph, path: Path):
    """The guide this character's playhead names, when it is one after `graph`.

    Every session starts from the first guide, and a finished guide handed over only while
    the file still named it: once the next guide had saved its own place there, the first
    read as nothing remembered and was played again from a scan. The session after 12-20
    began (141) walked Testvvi from Westfall back toward Goldshire for a 1-12 hand-in,
    6,700 yards for four kills.
    """
    try:
        named = json.loads(path.read_text(encoding="utf-8")).get("graph_id")
    except (OSError, ValueError, AttributeError):
        return graph
    if not isinstance(named, str):
        return graph
    named = named.removesuffix(".supported")
    current = graph
    for _ in range(len(NEXT_GUIDE)):
        following = NEXT_GUIDE.get(current.graph_id)
        if following is None or not following.exists():
            break
        current = Graph.load(following)
        if current.graph_id == named:
            print(f"guide {graph.graph_id}: this character is on {following.name}")
            args.graph = following
            return current
    return graph


# A guide is started two levels above its lowest grind's (V276, V280): at that level its mobs are
# all at or above the character. The level 12 mage died five times in its first 35 minutes of the
# 12-20 guide's Westfall (sessions 263-264: a Riverpaw camp of 13-15s, Defias Smugglers in
# threes), where it had made about 3,900 XP an hour at 11 on Elwynn's Prowlers with 0.8 deaths
# a session; the paladin's Westfall at 12-13 averaged 1.2 deaths a session and about 2,800 XP an
# hour. Back on the Prowlers at 12 the mage made 5,095 and 4,630 XP an hour with no deaths
# (sessions 268-269), and at 14 it learns Arcane Explosion, its first answer to a camp.
ENTRY_LEVELS_ABOVE = 2


def own_end(graph: Graph) -> int | None:
    """The level a guide with none after it is outgrown at: one past its highest grind's
    (V295). `None` with no grind in it."""
    top = max((n.level[1] for n in graph.nodes if n.kind is StepKind.GRIND), default=None)
    return None if top is None else top + 1


def entry_level(guide: Path) -> int | None:
    """The level a guide starts at: two levels above its lowest grind's. Below its lowest
    grind's, its every grind is above the character (V262); at it, all its mobs are at or
    above it (V276, V280)."""
    try:
        ribs = [n for n in Graph.load(guide).nodes if n.kind is StepKind.GRIND]
    except (OSError, ValueError):
        return None
    lowest = min((r.level[0] for r in ribs), default=None)
    return None if lowest is None else lowest + ENTRY_LEVELS_ABOVE


def level_start(graph: Graph, level: int | None) -> str | None:
    """Where a character with no place in `graph` begins (V297): the first quest taken on its
    spine whose band reaches the character's level, or a level gate above it. A level 9 orc
    given a route from level 1 would otherwise walk back to the Valley of Trials for quests
    grey to it; `None` (the entry, as before) when nothing is below the character."""
    if level is None:
        return None
    by_id, cursor, seen = graph.by_id(), graph.entry, set()
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        node = by_id.get(cursor)
        if node is None:
            return None
        if ((node.kind is StepKind.QUEST_ACCEPT or node.kind is StepKind.DING_GATE)
                and node.level[1] > level - (0 if node.kind is StepKind.DING_GATE else 1)):
            return None if cursor == graph.entry else cursor
        cursor = node.next[0] if node.next else None
    return None


def remembered(args, graph, key: int | None, level: int | None = None):
    """This character's playhead and route: its own file, found by the key the strip
    paints, so each character keeps its own place in the guide (`playhead`). A guide run
    out below the next one's first level plays its own grind for the character until that
    level (V262)."""
    if args.playhead is not None:
        path = args.playhead
    elif key is None:
        raise NotRunning("the strip does not say which character this is (an addon older "
                         "than schema 13): install it with tools/gen_addon_fields.py "
                         "--install and restart the client")
    else:
        path = playhead.for_character(key, ROOT / playhead.CHARACTERS)
    graph = _guide_on(args, graph, path)
    # Its ribs' creatures' levels, which choose the grind for the level (V323).
    graph = with_rib_levels(graph, getattr(args, "world_db", None))
    for _ in range(len(NEXT_GUIDE) + 1):
        memory = playhead.load(graph.graph_id, path)
        route = compile_route(graph, available_skills=LiveBody.available,
                              completed_quests=memory.completed,
                              worthless=worthless_quests(getattr(args, "world_db", None), graph))
        used = route.graph if args.route_mode == "supported" else graph
        if used is not graph:
            memory = playhead.load(used.graph_id, path)
        if memory.step_id is None and not memory.finished:
            start = level_start(used, level)
            if start is not None:
                print(f"level {level}, new to {used.graph_id}: beginning at {start}")
                memory = replace(memory, step_id=start)
        following = NEXT_GUIDE.get(graph.graph_id)
        last = following is None or not following.exists()
        if not memory.finished:
            return path, memory, route, used
        # With no guide after it, a guide run out grinds its own until it is outgrown (V295):
        # the generated draenei guide ends with Azuremyst's last quest, near level 7, and three
        # of the hive's characters stood finished at levels 2 to 7, each session doing nothing;
        # the 12-20 guide has none after it either.
        entry = own_end(used) if last else entry_level(following)
        if level is not None and entry is not None and level < entry:
            # Run out early: V245 passed a dead chain by, and the level 9 mage's 1-12 route
            # ended at 9.85. In the 12-20 guide's Westfall its walk in met a level 14-15
            # Harvest Watcher and an 18-19 Dust Devil, dead both times (session 235). Its
            # own grind for its level until the next guide's first, the level kept so a
            # grind runs on across sessions (V214).
            rib = used.rib_for(level, key=key)
            # A grind still suited to the level is kept, not traded (V272): at 11 the level
            # 9-11 Prowlers' rib gave way to the 11-12 one, eight Riverpaw Gnolls 5 to 9 yards
            # apart, where every pull is two or three.
            kept = used.get(memory.step_id) if memory.step_id else None
            first = entry - ENTRY_LEVELS_ABOVE          # the next guide's lowest grind's level
            # The guide's last grinds wait with the next guide (V276, V280) when it has no grind
            # of its own for the wait: the 1-12 guide's Westfall ribs for 12 to 14 (V333).
            own = [r for r in used.ribs() if first <= r.level[0] <= level <= r.level[1]
                   and not r.route_blocked_reason]
            if (kept is not None and kept.kind is StepKind.GRIND and not kept.route_blocked_reason
                    and kept.level[0] <= level
                    and (level <= kept.level[1]
                         or (not own and kept.level[1] >= first - 1
                             and level <= kept.level[1] + ENTRY_LEVELS_ABOVE))):
                rib = kept
            if rib is not None:
                below = f"its own end, {entry}" if last else f"{following.name}'s {entry}"
                print(f"guide {graph.graph_id} finished at level {level}, below {below}: "
                      f"grinding {rib.id} until then")
                return path, replace(memory, step_id=rib.id, finished=True, rejoin_to=None,
                                     rib_until=None, entry_level=entry - 1), route, used
        if last:
            return path, memory, route, used
        # This character finished the guide: the next one takes over, its quests carried.
        print(f"guide {graph.graph_id} finished; continuing with {following.name}")
        args.graph = following
        graph = with_rib_levels(Graph.load(following), getattr(args, "world_db", None))
    return path, memory, route, used


def _live(args, graph) -> int:
    print(f"startup: attaching at {time.strftime('%H:%M:%S')}", flush=True)
    try:
        client = attach(args.client_id)
    except NotRunning as exc:
        print(exc)
        return 2
    recorder = supervisor = bridge = screenshots = playing = None

    def operator_checkpoint():
        if args.stop_file is not None and args.stop_file.exists():
            print(f"operator stop file observed: {args.stop_file}")
            raise KeyboardInterrupt

    def startup_checkpoint():
        operator_checkpoint()
        if screenshots is not None and screenshots.error:
            raise ScreenshotError(screenshots.error)

    def stamp(label: str) -> None:
        # Where a session's start goes: about 30 s from launch to the first action, the
        # character standing uncontrolled meanwhile; sessions 212 and 223 began in a fight.
        print(f"startup: {label} at {time.strftime('%H:%M:%S')}", flush=True)

    try:
        stamp("attached")
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
        stamp("focused")
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
        stamp("quest log read")
        # Which character is logged in decides whose playhead this run keeps.
        character = values.get("char.key")
        level = values.get("char.level")
        path, memory, route, graph = remembered(args, graph, character,
                                                level if isinstance(level, int) else None)
        print(f"character {character:08x}: playhead {path}" if character is not None
              else f"playhead {path}")
        zones = bounds_by_radio_id(str(ROOT / "data/zones-tbc-243.json"))
        bounds = navigation_frame(graph.coord_zone_id, values.get("pos.zone_id"), zones)
        if bounds is None:
            raise NotRunning("zone has no measured coordinate bounds")
        launcher = ("wsl.exe", "-d", "Ubuntu-24.04", "-e") if win32.IS_WINDOWS else ()
        # Where the character keeps being attacked, counted from every run not yet counted
        # (`jev.learn.danger`, V161): routes keep clear of it at the character's level. A
        # prior beside it (V290), when the operator has put one there, is read at a discount.
        danger = DangerMap(ROOT / "var" / "danger.json", prior=PRIOR / "danger.json")
        if danger.lent:
            print(f"danger: a prior of {len(danger.lent)} cells from {PRIOR / 'danger.json'}")
        by_area = {zone.area_id: zone for zone in zones.values()}
        runs_dir = Path(args.runs_dir)
        this_run = recorder.dir.name if recorder is not None else None
        attacks = count_runs(runs_in(runs_dir, danger, skip=this_run), danger, by_area.get)
        print(f"danger: {len(danger.cells)} cells learned"
              + (f", {attacks} attacks counted from earlier runs" if attacks else ""))
        stamp("danger counted")
        # What walks steer by (V178): confirmed blocked spots; escapes are not taken.
        route_memory = RouteMemory(ROOT / "var/route-memory.json")
        print(f"route memory: {len(route_memory.blocks(bounds.map_id))} blocked spots kept "
              f"clear of; {len(route_memory.passages)} old passages not taken")
        # The way in from the door, when the last session ended inside (V232).
        if hasattr(client, "restore_trail"):
            client.trail_memory = path.with_name(path.stem + ".trail.json")
            client.restore_trail()
        with_travel(client, bounds, MmapQuery(args.jevpath, args.mmaps, launcher=launcher,
                    checkpoint=lambda: client.hid.checkpoint() if client.hid.checkpoint else None),
                    arrival_yards=GOSSIP_YARDS, say=print, zones=zones,
                    route_memory=route_memory, danger=danger)
        body = LiveBody(client, graph, travel_timeout=args.timeout, hunt_timeout=args.hunt,
                        record_frame=screenshots.record_frame if screenshots is not None else None,
                        hunt_spawns=spawns.load(args.graph),
                        gear_memory=path.with_name(path.stem + ".equipped.json"),
                        merchant_memory=ROOT / "var" / "merchant-memory.json",
                        home_memory=path.with_name(path.stem + ".home.json"),
                        purse_memory=path.with_name(path.stem + ".purse.json"),
                        taxi_memory=path.with_name(path.stem + ".taxi.json"))
        stamp("body built")
        if recorder is None:
            recorder = Recorder(root=args.runs_dir)
        # What each choice has paid off before (`jev.learn.choices`), counted first from any
        # runs that predate the choices' own log; this run logs its own.
        choices = ChoiceMemory(ROOT / "var" / "choices.json", prior=PRIOR / "choices.json")
        counted = backfill_hunts((run for run in Path(args.runs_dir).iterdir() if run.is_dir()),
                                 choices)
        choice_log = ChoiceLog(recorder.dir / "choices.jsonl")
        # Jev, the coach's model (PLAN §9, `jev.coach.judge`): what to arm next and the learned
        # choices are its picks wherever there is more than one. Without its key, or switched
        # off in var/coach-model.json, the scripted coach decides alone.
        judge = None
        if not args.no_coach_model:
            from jev.coach.judge import Judge
            from jev.coach.model import open_model

            coach_model = open_model(ROOT / "var", ROOT / ".env")
            if coach_model is not None:
                # What the hive measured each quest and grind to be worth, lent beside the
                # other priors and read once (V312); without it Jev's options are as before.
                from jev.learn.values import Values

                values = Values.load(PRIOR / "values.json")
                if values is not None:
                    print(f"values: {len(values.quests)} quests and {len(values.grinds_by)} "
                          f"grinds measured, from {PRIOR / 'values.json'}")
                judge = Judge(coach_model, state=client.state, where=body.here_world,
                              record=recorder.dir / "coach-model.jsonl", values=values)
                from jev.coach.judge import CombatJudge
                from jev.coach.model import settings as model_settings

                if model_settings(ROOT / "var")["combat"]:
                    body.fight.judge = CombatJudge(coach_model,
                                                   record=recorder.dir / "coach-model.jsonl")
        body.judge = judge
        body.learn(choices, choice_log)
        visits = sum(arm.tries for arm in choices.arms("hunt.station", lent=False).values())
        print(f"choices: {visits} hunt station visits remembered"
              + (f", {counted} counted from earlier runs" if counted else ""))
        if choices.lent:
            lent = sum(arm.tries for arm in (choices.lent.get("hunt.station") or {}).values())
            print(f"choices: a prior of {lent} hunt station visits from {PRIOR / 'choices.json'}")
        if args.play_mode != "off":
            from jev.play.controller import PlayConfig
            from jev.play.runtime import PlayingBody

            playing = PlayingBody(
                body, recorder=recorder, store=args.learning_store, screenshots=screenshots,
                teacher_model=args.teacher_model, teacher_binary=args.teacher_binary,
                teacher_provider=args.teacher_provider, teacher_base_url=args.teacher_base_url,
                teacher_env_file=args.teacher_env_file, teacher_key_env=args.teacher_key_env,
                teacher_effort=args.teacher_effort,
                teacher_calls_per_hour=args.play_teacher_calls_per_hour,
                binding_paths=args.bindings, world_db=args.world_db,
                config=PlayConfig(teacher_timeout_s=args.play_decision_timeout))
            # After a routine fails: the tutor, or the routine again, learned (V158).
            playing.recovery = Choice(choices, "recover.after_failure", log=choice_log,
                                      judge=judge)
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
            start_entry_level=memory.entry_level,
            # A finished guide's grind (V262): resumed, and finished again when done.
            start_grind_then_finish=bool(memory.finished and graph.get(memory.step_id or "")
                                         is not None
                                         and graph.get(memory.step_id).kind is StepKind.GRIND),
            completed=set(memory.completed),
            on_progress=lambda step, done, rejoin, deaths, retried=frozenset(), until=None,
            finished=False, entry_level=None: playhead.save(
                graph.graph_id, step, done, path, rejoin_to=rejoin, deaths=deaths,
                retried=retried, rib_until=until, finished=finished, entry_level=entry_level),
            character_key=character,
            outgrown_at=outgrown_at(route.source_graph_id),
            available_skills=body.available,
            validate_action=body.validate,
            judge=judge,
        )
        if runtime.outgrown_at is not None:
            print(f"guide {route.source_graph_id}: outgrown at level {runtime.outgrown_at}, "
                  f"then {NEXT_GUIDE[route.source_graph_id].name}")
        # The strategic teacher's queue, without visual play: the runtime asks it and takes
        # its answers on later ticks, and only the supervisor's thread appends its rows (`poll`).
        if args.teacher and args.play_mode == "off":
            try:
                from jev.teacher.bridge import TeacherBridge
                from jev.teacher.client import ClaudeSubscriptionClient
                teacher = ClaudeSubscriptionClient(binary=args.teacher_binary, model=args.teacher_model,
                                                   effort=args.teacher_effort)
                bridge = TeacherBridge(teacher, runtime.graph, runtime.recorder,
                                       runtime.available_skills,
                                       budget_path=Path(args.learning_store) / "teacher-budget.sqlite",
                                       calls_per_hour=args.teacher_calls_per_hour)
                runtime.ask, runtime.take = bridge.ask, bridge.take
            except Exception as exc:
                print(f"teacher unavailable; scripted floor continues: {type(exc).__name__}: {exc}")
        if args.teacher:
            print(f"learning store: {args.learning_store}")
        watchdog = Watchdog(blind_grace_s=args.blind_grace, no_progress_s=args.no_progress,
                            reconnect_limit=args.reconnect_limit,
                            reconnect=(lambda checkpoint: body.reconnect(checkpoint,
                                                                         env_file=args.env_file))
                            if args.reconnect else None)
        stop_seen = []
        teacher_faults: set[str] = set()

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
            if bridge is not None:
                try:
                    bridge.poll()
                except Exception as exc:
                    # An optional queue never stops the scripted floor; each new fault is said once.
                    fault = f"teacher: {type(exc).__name__}: {exc}"
                    if fault not in teacher_faults:
                        teacher_faults.add(fault)
                        print(fault)

        supervisor = Supervisor(runtime, body, max_failures=args.retries,
                                has_focus=body.has_focus,
                                focus=lambda checkpoint: client.focused(FOCUS_QUICK_S,
                                                                         checkpoint=checkpoint),
                                housekeeping=housekeeping, watchdog=watchdog,
                                operator_active=operator.active,
                                operator_suspected=operator.suspected)
        print(f"recording to {recorder.dir}")
        stamp("supervising")
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
                    if bridge is not None:
                        bridge.close()
                except Exception as exc:
                    print(f"teacher: {type(exc).__name__}: {exc}")
                finally:
                    try:
                        if recorder is not None:
                            recorder.close()
                    finally:
                        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
