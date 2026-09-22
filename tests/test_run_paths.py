from pathlib import Path

import pytest

from jev.run import paths


def test_linux_checkout_retains_repo_store(monkeypatch):
    monkeypatch.setattr(paths, "_WINDOWS", False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = Path("/home/ash/ForeverV2")
    assert paths.default_learning_store(root) == root / "var/learning"


def test_native_windows_checkout_retains_repo_store(monkeypatch):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = Path("C:/Projects/ForeverV2")
    assert paths.default_learning_store(root) == root / "var/learning"


def test_unc_store_is_native_and_stable_across_wsl_aliases(monkeypatch):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Tester/AppData/Local")
    original = paths.default_learning_store(
        Path(r"\\wsl.localhost\Ubuntu-24.04\home\ash\ForeverV2"))
    alias = paths.default_learning_store(
        Path(r"\\WSL$\UBUNTU-24.04\home\ash\extra\..\ForeverV2" + "\\"))
    assert original == alias
    assert original.parent == Path("C:/Users/Tester/AppData/Local/Jev/learning")
    assert original.name.startswith("foreverv2-")
    assert len(original.name.rsplit("-", 1)[1]) == 16


def test_same_named_checkouts_have_distinct_stores(monkeypatch):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Tester/AppData/Local")
    first = paths.default_learning_store(Path(r"\\wsl.localhost\Ubuntu\home\ash\project"))
    second = paths.default_learning_store(Path(r"\\wsl.localhost\Ubuntu\other\project"))
    assert first != second


@pytest.mark.parametrize("other", [
    r"\\wsl.localhost\Ubuntu\home\ash\Project",
    r"\\wsl.localhost\Ubuntu\home\Ash\project",
])
def test_case_sensitive_wsl_directories_have_distinct_stores(monkeypatch, other):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Tester/AppData/Local")
    first = paths.default_learning_store(Path(r"\\wsl.localhost\Ubuntu\home\ash\project"))
    assert first != paths.default_learning_store(Path(other))


@pytest.mark.parametrize("local", [None, "", "relative", r"\\wsl.localhost\Ubuntu\tmp"])
def test_unc_checkout_requires_native_application_storage(monkeypatch, local):
    monkeypatch.setattr(paths, "_WINDOWS", True)
    if local is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", local)
    with pytest.raises(ValueError, match="native learning store"):
        paths.default_learning_store(Path(r"\\wsl.localhost\Ubuntu\home\ash\project"))
