"""Atomic replacement of the small documents the run keeps (`jev.persist`)."""


def test_a_refused_rename_is_tried_again_before_the_error_stands(tmp_path, monkeypatch):
    """Windows over the WSL share refused `choices.json`'s rename with "Access is denied"
    and ended sessions 176 and 177: a file open a moment longer lets go."""
    from pathlib import Path

    from jev import persist

    monkeypatch.setattr(persist, "REPLACE_WAIT_S", 0.0)
    real = Path.replace
    refusals = [PermissionError(13, "Access is denied")] * 2

    def replace(self, target):
        if refusals:
            raise refusals.pop()
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    persist.atomic_json(tmp_path / "choices.json", {"visits": 1})
    assert (tmp_path / "choices.json").read_text().strip().endswith("}")
    refusals.extend([PermissionError(13, "Access is denied")] * persist.REPLACE_TRIES)
    import pytest

    with pytest.raises(PermissionError):
        persist.atomic_json(tmp_path / "choices.json", {"visits": 2})
    assert not list(tmp_path.glob(".choices.json.*")), "no temporary left behind"
