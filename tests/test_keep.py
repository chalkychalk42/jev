"""The keeper, the loop and the heartbeat's status (`tools/keep.sh`, `tools/session_loop.sh`,
`tools/keep_status.py`, `tools/desk.py`), run against faked commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import desk  # noqa: E402
import keep_status  # noqa: E402
from keep_status import Facts, deaths, run_summary, sessions, verdict  # noqa: E402

needs_bash = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="needs bash and flock")


def test_the_loop_log_gives_each_finished_session():
    text = ("session 6 start 2026-09-25T19:30:00+01:00 arm=hybrid quiet=600\n"
            "session 6 exit=0 after 904s 2026-09-25T19:45:04+01:00\n"
            "session 7 start 2026-09-25T19:45:04+01:00 arm=hybrid quiet=600\n"
            "session 7 exit=130 after 31s 2026-09-25T19:45:35+01:00\n")
    assert sessions(text) == [(6, 0, 904), (7, 130, 31)]


def _green() -> Facts:
    return Facts(loops=["41 /bin/bash tools/session_loop.sh"],
                 sessions=[(6, 0, 904), (7, 0, 905)],
                 runs=[{"run": "a", "t": 1.0, "level": 13.1, "gain": 0.05, "key": "548c8582"},
                       {"run": "b", "t": 2.0, "level": 13.2, "gain": 0.04, "key": "548c8582"}],
                 ports={3724: True, 8085: True}, wow_pid="4242", free_gb=700.0,
                 campaign_key="548c8582")


def test_green_when_all_is_well_and_red_names_each_reason():
    assert verdict(_green()) == []
    cases = {
        "no session loop is running": {"loops": []},
        "2 session loops are running": {"loops": ["1 a", "2 b"]},
        "the last two sessions ended early or failed": {"sessions": [(6, 1, 904), (7, 0, 300)]},
        "the last two runs gained no XP": {"runs": [
            {"run": "a", "t": 1.0, "level": 13.1, "gain": 0.0, "key": "548c8582"},
            {"run": "b", "t": 2.0, "level": 13.1, "gain": 0.0, "key": "548c8582"}]},
        "3 deaths within 20 minutes": {"deaths_recent": 3},
        "a Traceback in live-7.log": {"traceback": "live-7.log"},
        "mangosd is not listening on 8085": {"ports": {3724: True, 8085: False}},
        "no game client is running": {"wow_pid": None},
        "the strip shows character 548c8582, the campaign's is 0badc0de": {
            "campaign_key": "0badc0de"},
        "only 12 GB free": {"free_gb": 12.0},
    }
    for reason, change in cases.items():
        facts = _green()
        for name, value in change.items():
            setattr(facts, name, value)
        assert verdict(facts) == [reason], reason
    one_short = _green()
    one_short.sessions = [(6, 0, 904), (7, 130, 300)]
    assert verdict(one_short) == [], "one early end is an operator stop, not a trend"


def _tick(t, level, pct, dead=False, key=0x548C8582):
    return json.dumps({"t": t, "state": {"char": {"level": level, "xp_pct": pct, "key": key},
                                         "vitals": {"dead": dead}}}) + "\n"


def test_a_run_gives_its_gain_its_character_and_its_deaths(tmp_path):
    run = tmp_path / "20260925T193000-abc"
    run.mkdir()
    rows = [_tick(0, 13, 0.10), _tick(60, 13, 0.12, dead=True), _tick(70, 13, 0.12, dead=True),
            _tick(80, 13, 0.12), _tick(1500, 13, 0.20, dead=True), _tick(1510, 14, 0.01)]
    (run / "ticks.jsonl").write_text("".join(rows))
    summary = run_summary(run)
    assert summary["key"] == "548c8582" and summary["t"] == 1510
    assert summary["gain"] == pytest.approx(0.91)
    assert deaths(run / "ticks.jsonl", since=0) == 2
    assert deaths(run / "ticks.jsonl", since=100) == 1


def test_only_input_that_is_not_the_bots_counts_as_a_person():
    assert desk.person_idle_s(last=10_000, ours=9_900, now=70_000) == float("inf")
    assert desk.person_idle_s(last=10_000, ours=1_000, now=70_000) == 60.0
    assert desk.person_idle_s(last=2**32 - 1_000, ours=None, now=1_000) == 2.0, "the tick wraps"
    assert desk.person_idle_s(last=None, ours=None, now=5) == 0.0, "unread: somebody may be there"


def _fakes(tmp_path: Path) -> tuple[Path, dict]:
    """A copy of the keeper and the loop in their own tree, with every outside command faked."""
    tree, fake, bin_ = tmp_path / "tree", tmp_path / "fake", tmp_path / "bin"
    for path in (tree / "tools", tree / ".venv" / "bin", fake, bin_,
                 tmp_path / "cmangos" / "bin", tmp_path / "cmangos" / "etc"):
        path.mkdir(parents=True)
    for name in ("keep.sh", "session_loop.sh"):
        shutil.copy(ROOT / "tools" / name, tree / "tools" / name)

    def script(path: Path, body: str) -> None:
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    script(bin_ / "pgrep", 'exit 1\n')             # nothing of the real machine's
    script(bin_ / "ss", 'port=${@: -1}; port=${port##*:}\n'
                        'grep -qx "$port" "$FAKE/listening" 2>/dev/null '
                        '&& echo "LISTEN 0 128 0.0.0.0:$port 0.0.0.0:*"; exit 0\n')
    script(bin_ / "tasklist.exe", '[ -f "$FAKE/wow" ] && echo \'"Wow.exe","4242","Console","1","9 K"\''
                                  ' || echo "INFO: No tasks are running"\n')
    script(bin_ / "powershell.exe", 'touch "$FAKE/wow"; echo "$*" >> "$FAKE/launched"\n')
    script(bin_ / "taskkill.exe", 'rm -f "$FAKE/wow"; echo "$*" >> "$FAKE/killed"\n')
    script(fake / "winpy", 'if [ "$1" = tools/desk.py ]; then [ -f "$FAKE/busy" ] && exit 1; exit 0; fi\n'
                           'echo "$*" >> "$FAKE/sessions"\n'
                           'if [ "$(wc -l < "$FAKE/sessions")" -ge 2 ]; then touch var/loop/stop; fi\n')
    for name, port in (("realmd", 3724), ("mangosd", 8085)):
        script(tmp_path / "cmangos" / "bin" / name,
               f'echo {port} >> "$FAKE/listening"; echo "{name} $*" >> "$FAKE/started"\n')
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE": str(fake),
           "JEV_WINPY": str(fake / "winpy"), "JEV_CMANGOS": str(tmp_path / "cmangos"),
           "JEV_SERVER_LOGS": str(tmp_path / "logs"), "JEV_KEEP_WAIT_S": "0.05",
           "JEV_KEEP_SETTLE_S": "0",
           "JEV_PAUSE_S": "0", "JEV_QUICK_S": "0", "JEV_SESSION_S": "30"}
    return tree, env


def _keep(tree: Path, env: dict, *commands: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(tree / "tools" / "keep.sh"), *commands], env=env,
                          capture_output=True, text=True, timeout=60)


@needs_bash
def test_the_keeper_starts_what_is_down_as_it_was_started(tmp_path):
    tree, env = _fakes(tmp_path)
    fake = Path(env["FAKE"])
    assert _keep(tree, env, "servers").returncode == 0
    started = sorted((fake / "started").read_text().splitlines())
    cmangos = env["JEV_CMANGOS"]
    assert started == [f"mangosd -c {cmangos}/etc/mangosd.conf -p {cmangos}/etc/aiplayerbot.conf",
                       f"realmd -c {cmangos}/etc/realmd.conf"]
    assert _keep(tree, env, "servers").returncode == 0
    assert len((fake / "started").read_text().splitlines()) == 2, "nothing started twice"


@needs_bash
def test_the_keeper_stops_restarting_a_server_that_keeps_dying(tmp_path):
    tree, env = _fakes(tmp_path)
    (tree / "var" / "loop").mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    (tree / "var" / "loop" / "server-restarts").write_text(f"{now - 60}\n{now - 30}\n{now - 10}\n")
    result = _keep(tree, env, "servers")
    assert result.returncode == 1
    assert "three restarts within the hour" in result.stdout
    assert not (Path(env["FAKE"]) / "started").exists()


@needs_bash
def test_the_client_is_launched_only_at_an_idle_desk_and_restarted_gracefully(tmp_path):
    tree, env = _fakes(tmp_path)
    fake = Path(env["FAKE"])
    (fake / "busy").touch()
    result = _keep(tree, env, "client")
    assert result.returncode == 1 and "not launching" in result.stdout
    assert not (fake / "launched").exists()
    (fake / "busy").unlink()
    assert _keep(tree, env, "client").returncode == 0
    assert "Start-Process -FilePath 'C:\\Games\\WoW243\\Wow.exe'" in (fake / "launched").read_text()
    assert _keep(tree, env, "client").returncode == 0
    assert len((fake / "launched").read_text().splitlines()) == 1, "a running client is left alone"
    assert _keep(tree, env, "client-restart").returncode == 0
    assert (fake / "killed").read_text().split() == ["/PID", "4242"], "no /F when it closes"
    assert len((fake / "launched").read_text().splitlines()) == 2


@needs_bash
def test_the_disk_and_the_stop_flag_are_respected(tmp_path):
    tree, env = _fakes(tmp_path)
    assert _keep(tree, {**env, "JEV_MIN_FREE_GB": str(10**9)}, "disk").returncode == 1
    (tree / "var" / "loop").mkdir(parents=True, exist_ok=True)
    (tree / "var" / "loop" / "stop").touch()
    result = _keep(tree, env, "loop")
    assert result.returncode == 0 and "not starting" in result.stdout


@needs_bash
def test_the_loop_numbers_on_plays_the_arm_and_stops_at_the_flag(tmp_path):
    tree, env = _fakes(tmp_path)
    (tree / "captures").mkdir()
    (tree / "captures" / "session-loop.log").write_text(
        "session 133 start 2026-09-25T08:43:54+01:00 arm=tutor quiet=600\n"
        "session 133 exit=130 after 439s 2026-09-25T08:51:13+01:00\n")
    (tree / "var" / "loop").mkdir(parents=True, exist_ok=True)
    (tree / "var" / "loop" / "arm").write_text("tutor\n")
    for port in ("3724", "8085"):
        with (Path(env["FAKE"]) / "listening").open("a") as handle:
            handle.write(port + "\n")
    (Path(env["FAKE"]) / "wow").touch()
    result = subprocess.run([str(tree / "tools" / "session_loop.sh")], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    log = (tree / "captures" / "session-loop.log").read_text()
    assert "session 134 start" in log and "arm=tutor" in log.splitlines()[3]
    assert "session 135 exit=0" in log and "session 136" not in log
    assert log.rstrip().splitlines()[-1].startswith("loop ended")
    assert (tree / "captures" / "live-134.log").exists()
    played = (Path(env["FAKE"]) / "sessions").read_text().splitlines()
    assert played == ["-u tools/start_teaching.py --run --dispatch tutor"] * 2
    second = subprocess.run([str(tree / "tools" / "session_loop.sh")], env=env,
                            capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, "a stopped loop ends at once"


def test_the_status_screen_reads_both_log_names(tmp_path, monkeypatch):
    monkeypatch.setattr(keep_status, "CAPTURES", tmp_path)
    (tmp_path / "live-testvvi-133.log").write_text("")
    assert keep_status.session_log(133).name == "live-testvvi-133.log"
    (tmp_path / "live-134.log").write_text("")
    assert keep_status.session_log(134).name == "live-134.log"
