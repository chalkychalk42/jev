"""Review or run the prepared teaching configuration; default is an offline check.

Use Windows Python. --run is the explicit live entrypoint. Subsequent clean sessions
share the learning store and each character's own saved playhead; errors and operator
stops never restart.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_config(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (document.get("version") != 1 or not isinstance(document.get("args"), list)
            or not all(isinstance(value, str) for value in document["args"])
            or not isinstance(document.get("cwd"), str)):
        raise ValueError("invalid teaching launch configuration")
    if "--check" in document["args"] or "--play-mode" not in document["args"]:
        raise ValueError("configuration must describe the teaching runtime")
    return document


def option(args, name, default=None):
    try:
        index = args.index(name)
        return args[index + 1]
    except ValueError:
        return default
    except IndexError as exc:
        raise ValueError(f"missing value for {name}") from exc


def replace_option(args, name, value):
    args = list(args)
    if name in args:
        args[args.index(name) + 1] = str(value)
    else:
        args.extend((name, str(value)))
    return args


def route_done(args) -> bool:
    """Bound repeat collection by confirmed quest completion, never by process success.

    Each character keeps its own playhead, and which one is logged in is known only once
    a run reads the strip, so without an explicit `--playhead` the run itself decides.
    """
    from jev.guide import playhead
    from jev.guide.graph import Graph
    from jev.guide.route import compile_route
    from jev.run.body import LiveBody

    if option(args, "--playhead") is None:
        return False
    graph = Graph.load(Path(option(args, "--graph", str(ROOT / "content/tbc/ally_human_1_12.json"))))
    path = Path(option(args, "--playhead"))
    memory = playhead.load(graph.graph_id, path)
    if option(args, "--route-mode", "full") == "supported":
        graph = compile_route(graph, available_skills=LiveBody.available,
                              completed_quests=memory.completed).graph
        memory = playhead.load(graph.graph_id, path)
    required = {node.quest_id for node in graph.nodes if node.quest_id is not None}
    # This launcher is for the quest spine. No automatic repeat of an empty curriculum.
    return not required or required <= set(memory.completed)


def run_sessions(args, *, sessions: int = 1, run=None, is_done=route_done) -> int:
    if type(sessions) is not int or sessions < 0:
        raise ValueError("sessions must be nonnegative; zero means continuous")
    if run is None:
        from jev.run.cli import main

        run = main
    completed = 0
    while sessions == 0 or completed < sessions:
        stop = option(args, "--stop-file")
        if stop and Path(stop).exists():
            return 130
        if is_done(args):
            print("supported quest route is complete; collection finished")
            return 0
        code = run(list(args))
        completed += 1
        if code != 0:
            return code
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "var/teaching-launch.json")
    parser.add_argument("--run", action="store_true", help="attach the game and begin the supervised test")
    parser.add_argument("--mode", choices=("teach", "adaptive"))
    parser.add_argument("--dispatch", choices=("tutor", "hybrid"),
                        help="who takes an objective first (docs/plans/nine-hour-session.md)")
    parser.add_argument("--sessions", type=int, default=1, help="clean collection sessions; 0 repeats until stop/failure/route end")
    parser.add_argument("--session-seconds", type=float)
    args = parser.parse_args(argv)
    if args.sessions < 0 or (args.session_seconds is not None and (
            not math.isfinite(args.session_seconds) or args.session_seconds <= 0)):
        parser.error("sessions must be nonnegative and session-seconds finite and positive")
    document = load_config(args.config)
    command = document["args"]
    if args.mode:
        command = replace_option(command, "--play-mode", args.mode)
    if args.dispatch:
        command = replace_option(command, "--play-dispatch", args.dispatch)
    if args.session_seconds:
        command = replace_option(command, "--run-for", args.session_seconds)
    if not args.run:
        from jev.run.cli import main as check

        return check([*command, "--check"])
    if sys.platform != "win32":
        parser.error("live teaching requires the configured native Windows Python")
    directory = Path(document["cwd"])
    if not directory.is_dir():
        parser.error("configured checkout directory is unavailable")
    previous = Path.cwd()
    try:
        os.chdir(directory)
        return run_sessions(command, sessions=args.sessions)
    except KeyboardInterrupt:
        return 130
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
