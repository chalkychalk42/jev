#!/usr/bin/env python3
"""The campaign: which character plays, and when the next one is made or picked and entered
(docs/plans/forty-eight-hour-session.md, section 8).

The campaign is `var/campaign.json`, which git ignores: the characters' names and the realm's
live only there. The active character plays until its level or its deadline; the loop then
restarts the client (`tools/keep.sh client-restart`) and this enters the next.

With the repo's own Python in WSL (files, and the character database read-only for a name):
    tools/character.py plan --realm REALM --active NAME --class paladin --row 2
                            --until-level 20 --until 2026-09-26T10:30:00+01:00
                            --next-class mage --next-row 3
    tools/character.py name     a pronounceable name no character has, kept as the next's
    tools/character.py due [--mark]
                                "switch" once the active character is at its level or past its
                                deadline, at most once an hour after a marked try; else "stay"
    tools/character.py status
With Windows Python, the client at its login screen or at character select, the desk idle:
    tools/character.py enter [--name NAME]
                                sign in to character select; make the next character, or
                                pick its row; enter the world; wait out the introduction,
                                pressing nothing; check name, class and race by the strip.
                                The campaign then names it the active one
    tools/character.py measure [--focus] [--click FX,FY]
                                keep the client's frame as a PNG, to measure a screen from;
                                with a click first at a fraction of the client

Nothing here prints a credential. Nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from jev.perceive.radio_frame import character_key  # noqa: E402
from jev.persist import atomic_json  # noqa: E402

CAMPAIGN = ROOT / "var" / "campaign.json"
RUNS = ROOT / "runs"
MANGOSD_CONF = Path.home() / "cmangos" / "run" / "etc" / "mangosd.conf"
FORMAT = 1
# A switch that failed is tried again no sooner than this (the plan: within two hours).
RETRY_S = 3600.0
# How long nobody must have touched the desk before this takes the window (`tools/desk.py`).
IDLE_S = 600.0
# The server refuses a name taken or not allowed; this many are tried before it stops.
NAME_TRIES = 3
# Names: a capital, then lower case, 6 to 9 letters in all (the server allows 2 to 12).
NAME = re.compile(r"[A-Z][a-z]{5,8}")
STARTS = ("Ael", "Bran", "Cal", "Dor", "Ela", "Fen", "Gar", "Hal", "Ith", "Jor", "Kel", "Lor",
          "Mar", "Nor", "Orin", "Per", "Quel", "Ral", "Sel", "Tor", "Ul", "Val", "Wen", "Yor")
MIDDLES = ("a", "e", "i", "o", "ai", "ea", "io")
ENDS = ("dor", "lin", "mar", "nis", "ran", "rel", "rin", "ros", "sen", "thas", "van", "wen",
        "wyn", "zar")
CLASS_IDS = {"warrior": 1, "paladin": 2, "hunter": 3, "rogue": 4, "priest": 5, "shaman": 7,
             "mage": 8, "warlock": 9, "druid": 11}
RACE_IDS = {"human": 1, "orc": 2, "dwarf": 3, "nightelf": 4, "undead": 5, "tauren": 6,
            "gnome": 7, "troll": 8, "bloodelf": 10, "draenei": 11}


def load(path: Path | None = None) -> dict:
    path = CAMPAIGN if path is None else path
    campaign = json.loads(path.read_text(encoding="utf-8"))
    if campaign.get("format") != FORMAT:
        raise ValueError(f"{path} is not a campaign of format {FORMAT}")
    return campaign


def save(campaign: dict, path: Path | None = None) -> None:
    atomic_json(CAMPAIGN if path is None else path, campaign)


def key_of(name: str, realm: str) -> str:
    """The key the strip paints for a character, as its playhead's file names it."""
    return f"{character_key(name, realm):08x}"


def make_name(rng: random.Random) -> str:
    """A pronounceable name: a start, sometimes a vowel, an end; 6 to 9 letters."""
    while True:
        name = rng.choice(STARTS) + (rng.choice(MIDDLES) if rng.random() < 0.5 else "") \
            + rng.choice(ENDS)
        if NAME.fullmatch(name) and not re.search(r"(.)\1\1", name.lower()):
            return name


def _query(sql: str) -> str | None:
    """A read-only query of the character database, as mangosd reaches it. The credentials
    are read here and handed to the client in a file only this user can read; never printed."""
    try:
        conf = MANGOSD_CONF.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.search(r'^\s*CharacterDatabaseInfo\s*=\s*"([^"]*)"', conf, re.MULTILINE)
    if found is None or len(found.group(1).split(";")) != 5:
        return None
    host, port, user, password, database = found.group(1).split(";")
    handle, options = tempfile.mkstemp(suffix=".cnf")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(f"[client]\nhost={host}\nport={port}\nuser={user}\npassword={password}\n")
        done = subprocess.run(["mysql", f"--defaults-extra-file={options}", "-N", "-B",
                               database, "-e", sql], capture_output=True, text=True, timeout=30)
        return done.stdout if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        Path(options).unlink()


def name_in_use(name: str) -> bool | None:
    """Whether a character has this name; `None` when the database cannot be asked."""
    if not NAME.fullmatch(name):
        raise ValueError(f"not a name this makes: {name!r}")
    answer = _query(f"SELECT COUNT(*) FROM characters WHERE name = '{name}'")
    return None if answer is None else answer.strip() != "0"


def newest_level(key: str, runs: Path = RUNS) -> int | None:
    """The level the newest run of the character with `key` last read, if any."""
    from keep_status import run_summary

    for run in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True)[:8] \
            if runs.is_dir() else ():
        summary = run_summary(run)
        if summary is not None and summary["key"] == key and summary["level"] is not None:
            return int(summary["level"])
    return None


def due(campaign: dict, now: float, level: int | None) -> tuple[str, str]:
    """("switch" or "stay", why), for the active character at `level` (None: unread)."""
    active = campaign["active"]
    if active + 1 >= len(campaign["characters"]):
        return "stay", "no character after this one"
    tried = campaign.get("switch_tried")
    if tried is not None and now - tried < RETRY_S:
        return "stay", "a switch was tried within the hour"
    character = campaign["characters"][active]
    until_level, until = character.get("until_level"), character.get("until")
    if until_level is not None and level is not None and level >= until_level:
        return "switch", f"{character['name']} is level {level}"
    if until is not None and now >= datetime.fromisoformat(until).timestamp():
        return "switch", f"{character['name']}'s time was up at {until}"
    return "stay", f"{character['name']} plays on" + (f" at level {level}" if level else "")


def _target(campaign: dict, name: str | None) -> int:
    if name is not None:
        for i, character in enumerate(campaign["characters"]):
            if character["name"] == name:
                return i
        raise SystemExit(f"no character called {name} in the campaign")
    return min(campaign["active"] + 1, len(campaign["characters"]) - 1)


def cmd_plan(args) -> int:
    if CAMPAIGN.exists() and not args.force:
        print(f"{CAMPAIGN} exists; --force to replace it")
        return 1
    until = datetime.fromisoformat(args.until).isoformat() if args.until else None
    campaign = {"format": FORMAT, "realm": args.realm, "active": 0, "switch_tried": None,
                "characters": [
                    {"name": args.active, "class": args.cls, "race": args.race,
                     "key": key_of(args.active, args.realm), "row": args.row, "created": True,
                     "until_level": args.until_level, "until": until},
                    {"name": None, "class": args.next_class, "race": args.next_race,
                     "key": None, "row": args.next_row, "created": False}]}
    save(campaign)
    print(f"campaign: {args.active} until level {args.until_level} or {until}, "
          f"then a {args.next_race} {args.next_class}")
    return 0


def cmd_name(args) -> int:
    campaign = load()
    i = _target(campaign, None)
    character = campaign["characters"][i]
    if character.get("created"):
        print(f"{character['name']} already exists")
        return 1
    rng = random.Random(args.seed)
    for _ in range(50):
        name = make_name(rng)
        taken = name_in_use(name)
        if taken:
            continue
        if taken is None:
            print("the character database could not be asked; the server's answer decides",
                  file=sys.stderr)
        character["name"], character["key"] = name, key_of(name, campaign["realm"])
        save(campaign)
        print(name)
        return 0
    print("no free name in 50 tries")
    return 1


def cmd_due(args) -> int:
    if not CAMPAIGN.exists():
        print("stay")
        return 0
    campaign = load()
    character = campaign["characters"][campaign["active"]]
    answer, why = due(campaign, time.time(), newest_level(character["key"]))
    if answer == "switch" and args.mark:
        # Only the loop, which goes on to switch, marks the try: a look by hand must not
        # hold the loop's switch off for an hour.
        campaign["switch_tried"] = time.time()
        save(campaign)
    print(answer)
    print(f"campaign: {why}", file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    campaign = load()
    for i, c in enumerate(campaign["characters"]):
        mark = "*" if i == campaign["active"] else " "
        print(f"{mark} {c['name'] or '(no name yet)'}: {c['race']} {c['class']}, key {c['key']}, "
              f"row {c['row']}, {'made' if c.get('created') else 'not made'}"
              + (f", until level {c['until_level']} or {c['until']}" if c.get("until_level") else ""))
    return 0


def _window():
    """The game window, its capture and its input, raised only at an idle desk."""
    import desk

    from jev.clients import win32
    from jev.clients.capture import Backend, WindowCapture
    from jev.clients.hid import Hid, Humaniser

    idle = desk.person_idle_s(win32.last_input_tick(), desk._stamp(), win32.tick_now())
    if idle < IDLE_S:
        raise SystemExit(f"someone used the desk {idle:.0f}s ago: not taking the window")
    hwnd = win32.game_window()
    win32.focus(hwnd)
    time.sleep(0.5)
    if not win32.is_foreground(hwnd):
        raise SystemExit("could not raise the game window")
    origin_x, origin_y, width, height = win32.client_rect(hwnd)
    return (hwnd, WindowCapture(hwnd, backend=Backend.SCREEN),
            Hid(hwnd=hwnd, humaniser=Humaniser.for_client("login")),
            (origin_x, origin_y), (width, height))


def _keep(frame, label: str) -> Path | None:
    if frame is None:
        return None
    from PIL import Image

    out = ROOT / "captures" / "glue" / f"{time.strftime('%Y%m%dT%H%M%S')}-{label}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frame).save(out)
    return out


def cmd_enter(args) -> int:
    from jev.clients import win32
    from jev.clients.session import Session, credentials
    from jev.perceive import radio_frame

    if not win32.available():
        print("run this with Windows Python")
        return 2
    campaign = load()
    i = _target(campaign, args.name)
    character = campaign["characters"][i]
    if character["name"] is None:
        print("the next character has no name yet: tools/character.py name")
        return 2
    creds = credentials(path=ROOT / ".env")
    if creds is None:
        print("set JEV_WOW_ACCOUNT and JEV_WOW_PASSWORD in .env")
        return 2
    try:
        _hwnd, cap, hid, origin, size = _window()
    except Exception as exc:
        print(exc)
        return 1

    def read_frame():
        try:
            return cap.grab().rgb
        except Exception:
            return None

    def read_values() -> dict | None:
        frame = read_frame()
        if frame is None:
            return None
        try:
            reading = radio_frame.read(frame)
        except Exception:
            return None
        return reading.values if reading.ok else None

    session = Session(hid=hid, read_frame=read_frame, radio_ok=lambda: read_values() is not None,
                      window_origin=origin, window_size=size)
    try:
        if not session.sign_in(*creds, stop_at_select=True):
            print(f"not at character select: {session.detail}")
            if (kept := _keep(session.unknown_frame, "select")) is not None:
                print(f"kept the frame: {kept}")
            return 1
        if character.get("created"):
            if character.get("row") is None or not session.select_row(character["row"]):
                print(f"could not pick {character['name']}'s row: {session.detail}")
                return 1
        else:
            for _ in range(NAME_TRIES):
                if session.create_character(character["name"], character["race"],
                                            character["class"]):
                    character["created"] = True
                    save(campaign)
                    print(f"made {character['name']}, a {character['race']} {character['class']}")
                    break
                if not session.refused:
                    print(f"could not make {character['name']}: {session.detail}")
                    if (kept := _keep(session.unknown_frame, "create")) is not None:
                        print(f"kept the frame: {kept}")
                    return 1
                name = make_name(random.Random())
                print(f"the server refused {character['name']}; trying {name}")
                character["name"], character["key"] = name, key_of(name, campaign["realm"])
                save(campaign)
            else:
                print(f"the server refused {NAME_TRIES} names")
                return 1
        if not session.enter_world():
            print(f"did not reach the world: {session.detail}")
            _keep(read_frame(), "enter")
            return 1
        values = read_values() or {}
        seen = (values.get("char.key"), values.get("char.class_id"), values.get("char.race_id"))
        wanted = (int(character["key"], 16), CLASS_IDS[character["class"]],
                  RACE_IDS[character["race"]])
        if seen != wanted:
            print(f"in the world as the wrong character: key/class/race {seen}, wanted {wanted}")
            return 1
        campaign["active"], campaign["switch_tried"] = i, None
        character["entered"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save(campaign)
        print(f"in the world as {character['name']}, level {values.get('char.level')}")
        return 0
    finally:
        cap.close()


def cmd_measure(args) -> int:
    from jev.clients import win32
    from jev.clients.capture import Backend, WindowCapture
    from jev.clients.session import stage

    if not win32.available():
        print("run this with Windows Python")
        return 2
    if args.focus or args.click:
        _hwnd, cap, hid, origin, size = _window()
    else:
        cap = WindowCapture(win32.game_window(), backend=Backend.SCREEN)
    try:
        if args.click:
            # One click at a fraction of the client, to step through a screen being
            # measured; the frame after it is kept.
            fx, fy = (float(v) for v in args.click.split(","))
            hid.click(origin[0] + int(fx * size[0]), origin[1] + int(fy * size[1]))
            time.sleep(2.5)
        frame = cap.grab().rgb
    finally:
        cap.close()
    out = _keep(frame, "measure")
    print(f"{frame.shape[1]}x{frame.shape[0]}, stage {stage(frame, False).value}: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--realm", required=True)
    plan.add_argument("--active", required=True)
    plan.add_argument("--class", dest="cls", default="paladin", choices=sorted(CLASS_IDS))
    plan.add_argument("--race", default="human", choices=sorted(RACE_IDS))
    plan.add_argument("--row", type=int, required=True)
    plan.add_argument("--until-level", type=int, default=20)
    plan.add_argument("--until", help="ISO time with its offset")
    plan.add_argument("--next-class", default="mage", choices=sorted(CLASS_IDS))
    plan.add_argument("--next-race", default="human", choices=sorted(RACE_IDS))
    plan.add_argument("--next-row", type=int, required=True)
    plan.add_argument("--force", action="store_true")
    name = commands.add_parser("name")
    name.add_argument("--seed", type=int)
    due_ = commands.add_parser("due")
    due_.add_argument("--mark", action="store_true",
                      help="record the try (the loop, which switches on the answer)")
    commands.add_parser("status")
    enter = commands.add_parser("enter")
    enter.add_argument("--name")
    measure = commands.add_parser("measure")
    measure.add_argument("--focus", action="store_true")
    measure.add_argument("--click", metavar="FX,FY",
                         help="click here first, as fractions of the client (implies --focus)")
    args = parser.parse_args(argv)
    return {"plan": cmd_plan, "name": cmd_name, "due": cmd_due, "status": cmd_status,
            "enter": cmd_enter, "measure": cmd_measure}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
