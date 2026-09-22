"""Standalone learning uses the live checkout's storage defaults without touching runs."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev.learn import worker
from jev.run import paths


def fake_worker(monkeypatch):
    constructor = Mock(return_value=SimpleNamespace(cycle=lambda: worker.CycleReport()))
    monkeypatch.setattr(worker, "LearningWorker", constructor)
    return constructor


def test_defaults_follow_checkout_independently_of_launcher_directory(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(paths, "_WINDOWS", False)
    constructor = fake_worker(monkeypatch)

    assert worker.main(["--once"]) == 0
    assert constructor.call_args.args == (root / "runs", root / "var/learning")
    assert not (launcher / "var").exists()
    assert not root.exists()


def test_windows_unc_default_shares_live_native_store(tmp_path, monkeypatch):
    root = Path(r"\\wsl.localhost\Ubuntu-24.04\home\ash\ForeverV2")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Tester/AppData/Local")
    constructor = fake_worker(monkeypatch)

    assert worker.main(["--once"]) == 0
    assert constructor.call_args.args == (root / "runs", paths.default_learning_store(root))
    assert constructor.call_args.args[1].name == "foreverv2-3c309d13f4025150"


def test_explicit_paths_override_defaults_without_platform_resolution(monkeypatch):
    resolver = Mock(side_effect=AssertionError("explicit store resolved a default"))
    monkeypatch.setattr(worker, "default_learning_store", resolver)
    constructor = fake_worker(monkeypatch)

    assert worker.main(["--once", "--runs", "other-runs", "--store", "other-store"]) == 0
    assert constructor.call_args.args == (Path("other-runs"), Path("other-store"))
    resolver.assert_not_called()


def test_missing_native_storage_reports_worker_override_before_construction(monkeypatch, capsys):
    root = Path(r"\\wsl.localhost\Ubuntu\home\ash\project")
    monkeypatch.setattr(worker, "ROOT", root)
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    constructor = fake_worker(monkeypatch)

    with pytest.raises(SystemExit) as error:
        worker.main(["--once"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "LOCALAPPDATA" in stderr and "--store" in stderr
    assert "--learning-store" not in stderr
    constructor.assert_not_called()
