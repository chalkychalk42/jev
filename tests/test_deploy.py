"""The deploy at a session boundary (`tools/deploy.sh`), run in a throwaway checkout with its
own remote, the session check and the offline check faked."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_tools = pytest.mark.skipif(any(shutil.which(c) is None for c in ("bash", "git", "timeout")),
                                 reason="needs bash, git and timeout")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, dict]:
    """A live checkout on main with a remote, a `dev` branch one commit ahead, and fakes."""
    remote, live, bin_ = tmp_path / "origin.git", tmp_path / "live", tmp_path / "bin"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    live.mkdir()
    _git(live, "init", "-q", "-b", "main")
    (live / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "deploy.sh", live / "tools" / "deploy.sh")
    (live / ".gitignore").write_text("var/\ncaptures/\nsuite.log\n")
    _git(live, "add", ".")
    _git(live, "commit", "-q", "-m", "live")
    _git(live, "remote", "add", "origin", str(remote))
    _git(live, "push", "-q", "origin", "main")
    _git(live, "checkout", "-q", "-b", "dev")
    (live / "change.txt").write_text("tested\n")
    _git(live, "add", "change.txt")
    _git(live, "commit", "-q", "-m", "dev")
    _git(live, "checkout", "-q", "main")
    bin_.mkdir()
    for name, body in (("pgrep", "exit 1\n"),                      # no session running
                       ("winpy", '[ -f "$FAKE_OFFLINE_FAILS" ] && exit 2; exit 0\n')):
        (bin_ / name).write_text("#!/bin/bash\n" + body)
        (bin_ / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "JEV_WINPY": str(bin_ / "winpy"),
           "JEV_DEPLOY_POLL_S": "0", "FAKE_OFFLINE_FAILS": str(tmp_path / "offline-fails")}
    return live, env


def _deploy(live: Path, env: dict, suite: str) -> subprocess.CompletedProcess:
    (live / "suite.log").write_text(suite)
    return subprocess.run([str(live / "tools" / "deploy.sh"), "dev", str(live / "suite.log")],
                          env=env, capture_output=True, text=True, timeout=60)


@needs_tools
def test_a_passed_suite_is_merged_checked_pushed_and_the_hold_released(tmp_path):
    live, env = _checkout(tmp_path)
    result = _deploy(live, env, "....\nexit=0\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(live, "rev-parse", "HEAD") == _git(live, "rev-parse", "dev")
    assert _git(tmp_path / "origin.git", "rev-parse", "main") == _git(live, "rev-parse", "dev")
    assert not (live / "var" / "loop" / "hold").exists()
    assert "pushed" in result.stdout and "hold released" in result.stdout


@needs_tools
def test_a_failed_suite_or_offline_check_leaves_main_as_it_was(tmp_path):
    live, env = _checkout(tmp_path)
    before = _git(live, "rev-parse", "HEAD")
    result = _deploy(live, env, "F...\nFAILED tests/test_x.py::test_y\nexit=1\n")
    assert result.returncode == 1 and "nothing merged" in result.stdout
    assert _git(live, "rev-parse", "HEAD") == before
    assert not (live / "var" / "loop" / "hold").exists(), "play never waits on a failed deploy"
    (tmp_path / "offline-fails").touch()
    result = _deploy(live, env, "exit=0\n")
    assert result.returncode == 1 and "taken back" in result.stdout
    assert _git(live, "rev-parse", "HEAD") == before
    assert _git(tmp_path / "origin.git", "rev-parse", "main") == before, "nothing pushed"
    assert not (live / "var" / "loop" / "hold").exists()
