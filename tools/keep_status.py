"""One screen of what the heartbeat checks, for `tools/keep.sh status`. WSL, read-only.

Green means leave the loop alone. Red names each reason, from the plan's list
(docs/plans/forty-eight-hour-session.md, section 0): no loop, or two; the last two sessions
each short or failed; the last two runs with no XP; three deaths within 20 minutes; a
Traceback in the last session; a server port closed; no game client; the strip's character
not the campaign's; under 50 GB free. And three a loop can hide behind (review, 25
September): a loop that has started no session for 35 minutes, a deploy's hold older than
30, and a character switch tried more than 15 minutes ago and not made. Exits 0 when
green, 1 when red.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "captures"
LOOP_LOG = CAPTURES / "session-loop.log"
RUNS = ROOT / "runs"
CAMPAIGN = ROOT / "var" / "campaign.json"
# A session the loop's 900 s limit ended runs about 904 s; shorter ended early.
SHORT_S = 850
DEATH_WINDOW_S = 20 * 60
DEATHS_RED = 3
MIN_FREE_GB = 50
PORTS = {3724: "realmd", 8085: "mangosd"}
STALLED_S = 35 * 60
HOLD_S = 30 * 60
SWITCH_S = 15 * 60


@dataclass
class Facts:
    loops: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    sessions: list[tuple[int, int, int]] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    deaths_recent: int = 0
    traceback: str | None = None
    ports: dict[int, bool] = field(default_factory=dict)
    wow_pid: str | None = None
    free_gb: float = 0.0
    campaign_key: str | None = None
    now: float = 0.0
    last_start: float | None = None          # when the loop last started a session
    hold_age_s: float | None = None
    switch_tried: float | None = None


# The loop's log and each session's own log are read here and only here: `session_report.py`
# and `session_check.py` import these (V228).
def sessions(text: str) -> list[tuple[int, int, int]]:
    """(number, exit code, seconds) of each finished session in the loop's log, oldest first."""
    return [(int(n), int(c), int(s)) for n, c, s in
            re.findall(r"^session (\d+) exit=(\d+) after (\d+)s", text, re.MULTILINE)]


def starts(text: str) -> list[tuple[int, str]]:
    """(number, start time as written) of each session the loop's log started, oldest first."""
    return [(int(n), when) for n, when in
            re.findall(r"^session (\d+) start (\S+)", text, re.MULTILINE)]


def session_log(number: int) -> Path:
    """A session's own log: `live-N.log` from `tools/session_loop.sh`, `live-testvvi-N.log`
    from the loops before it."""
    ours = CAPTURES / f"live-{number}.log"
    return ours if ours.exists() else CAPTURES / f"live-testvvi-{number}.log"


def _tick(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _edge_lines(path: Path) -> tuple[str | None, str | None]:
    """The first and the last whole line of a file, without reading all of it."""
    with path.open("rb") as handle:
        first = handle.readline()
        if not first:
            return None, None
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - 65536))
        lines = handle.read().splitlines()
    return first.decode("utf-8", "replace"), lines[-1].decode("utf-8", "replace") if lines else None


def progress(tick: dict | None) -> float | None:
    """Level and the fraction of it done, as one number: 13.05 is level 13, 5% in."""
    char = ((tick or {}).get("state") or {}).get("char") or {}
    level, pct = char.get("level"), char.get("xp_pct")
    if not isinstance(level, int) or not isinstance(pct, (int, float)):
        return None
    return level + float(pct)


def run_summary(run: Path) -> dict | None:
    ticks = run / "ticks.jsonl"
    if not ticks.exists():
        return None
    first, last = (_tick(line) if line else None for line in _edge_lines(ticks))
    if last is None:
        return None
    start, end = progress(first), progress(last)
    char = (last.get("state") or {}).get("char") or {}
    key = char.get("key")
    return {"run": run.name, "t": last.get("t"), "level": end,
            "gain": (end - start) if start is not None and end is not None else None,
            "key": f"{key:08x}" if isinstance(key, int) else None}


def deaths(ticks: Path, since: float) -> int:
    """Times the character died in a run at or after `since` (tick time)."""
    count, dead = 0, None
    with ticks.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            row = _tick(line)
            if row is None:
                continue
            now = ((row.get("state") or {}).get("vitals") or {}).get("dead")
            if now is True and dead is False and isinstance(row.get("t"), (int, float)) \
                    and row["t"] >= since:
                count += 1
            if isinstance(now, bool):
                dead = now
    return count


def verdict(facts: Facts) -> list[str]:
    """Why the heartbeat is red; empty when it is green."""
    reasons = []
    if not facts.loops:
        reasons.append("no session loop is running" + (" (var/loop/stop is set)"
                                                       if "stop" in facts.flags else ""))
    elif len(facts.loops) > 1:
        reasons.append(f"{len(facts.loops)} session loops are running")
    last_two = facts.sessions[-2:]
    if len(last_two) == 2 and all(code != 0 or seconds < SHORT_S for _, code, seconds in last_two):
        reasons.append("the last two sessions ended early or failed")
    gains = [run["gain"] for run in facts.runs[-2:]]
    if len(gains) == 2 and all(gain is not None and gain <= 0 for gain in gains):
        reasons.append("the last two runs gained no XP")
    if facts.deaths_recent >= DEATHS_RED:
        reasons.append(f"{facts.deaths_recent} deaths within 20 minutes")
    if facts.traceback:
        reasons.append(f"a Traceback in {facts.traceback}")
    reasons += [f"{name} is not listening on {port}" for port, name in PORTS.items()
                if not facts.ports.get(port)]
    if facts.wow_pid is None:
        reasons.append("no game client is running")
    newest = facts.runs[-1]["key"] if facts.runs else None
    if facts.campaign_key and newest and newest != facts.campaign_key:
        reasons.append(f"the strip shows character {newest}, the campaign's is {facts.campaign_key}")
    if facts.free_gb < MIN_FREE_GB:
        reasons.append(f"only {facts.free_gb:.0f} GB free")
    if (facts.loops and facts.last_start is not None
            and facts.now - facts.last_start > STALLED_S):
        reasons.append(f"no session started for {(facts.now - facts.last_start) / 60:.0f} minutes")
    if facts.hold_age_s is not None and facts.hold_age_s > HOLD_S:
        reasons.append(f"var/loop/hold has stood for {facts.hold_age_s / 60:.0f} minutes")
    if facts.switch_tried is not None and facts.now - facts.switch_tried > SWITCH_S:
        reasons.append("a character switch was tried "
                       f"{(facts.now - facts.switch_tried) / 60:.0f} minutes ago and not made")
    return reasons


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def collect() -> Facts:
    facts = Facts()
    facts.loops = [line for line in _run(["pgrep", "-af", r"session_loop[0-9]*\.sh"]).splitlines()
                   if "pgrep" not in line and "bash -c" not in line]
    loop_dir = ROOT / "var" / "loop"
    for name in ("quiet_s", "hold", "stop"):
        path = loop_dir / name
        if path.exists():
            facts.flags[name] = path.read_text(errors="replace").strip()
    facts.now = time.time()
    if LOOP_LOG.exists():
        text = LOOP_LOG.read_text(encoding="utf-8", errors="replace")
        facts.sessions = sessions(text)
        started = starts(text)
        if started:
            with suppress(ValueError):
                facts.last_start = datetime.fromisoformat(started[-1][1]).timestamp()
    hold = loop_dir / "hold"
    if hold.exists():
        facts.hold_age_s = facts.now - hold.stat().st_mtime
    if facts.sessions:
        log = session_log(facts.sessions[-1][0])
        if log.exists() and "Traceback" in log.read_text(encoding="utf-8", errors="replace"):
            facts.traceback = log.name
    runs = sorted(p for p in RUNS.iterdir() if p.is_dir()) if RUNS.is_dir() else []
    facts.runs = [s for run in runs[-3:] if (s := run_summary(run)) is not None]
    if facts.runs and isinstance(facts.runs[-1]["t"], (int, float)):
        since = facts.runs[-1]["t"] - DEATH_WINDOW_S
        facts.deaths_recent = sum(deaths(RUNS / s["run"] / "ticks.jsonl", since)
                                  for s in facts.runs[-2:])
    listening = _run(["ss", "-ltnH"])
    facts.ports = {port: bool(re.search(rf":{port}\s", listening)) for port in PORTS}
    tasks = _run(["tasklist.exe", "/FI", "IMAGENAME eq Wow.exe", "/FO", "CSV", "/NH"])
    found = re.search(r'"wow\.exe","(\d+)"', tasks, re.IGNORECASE)
    facts.wow_pid = found.group(1) if found else None
    facts.free_gb = shutil.disk_usage(ROOT).free / 2**30
    try:
        campaign = json.loads(CAMPAIGN.read_text(encoding="utf-8"))
        facts.campaign_key = campaign["characters"][campaign["active"]]["key"]
        facts.switch_tried = campaign.get("switch_tried")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    return facts


def render(facts: Facts, reasons: list[str]) -> str:
    lines = ["GREEN" if not reasons else "RED: " + "; ".join(reasons)]
    flags = " ".join(f"{k}={v}" if v else k for k, v in sorted(facts.flags.items()))
    lines.append(f"loop: {len(facts.loops)} running" + (f"; {flags}" if flags else ""))
    lines += [f"  session {n}: exit={c} after {s}s" for n, c, s in facts.sessions[-3:]]
    for run in facts.runs[-2:]:
        gain = f"{run['gain']:+.3f}" if run["gain"] is not None else "?"
        level = f"{run['level']:.3f}" if run["level"] is not None else "?"
        lines.append(f"  run {run['run']}: level {level} ({gain}), character {run['key']}")
    lines.append(f"deaths in the last 20 minutes: {facts.deaths_recent}")
    lines.append("servers: " + ", ".join(f"{name} {'up' if facts.ports.get(port) else 'DOWN'}"
                                          for port, name in PORTS.items()))
    lines.append(f"client: {'Wow.exe ' + facts.wow_pid if facts.wow_pid else 'none'}; "
                 f"disk: {facts.free_gb:.0f} GB free; campaign: {facts.campaign_key or 'none'}")
    return "\n".join(lines)


def main() -> int:
    facts = collect()
    reasons = verdict(facts)
    print(render(facts, reasons))
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
