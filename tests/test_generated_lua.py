"""Anything two components must agree on is generated from one definition."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_addon_field_table_is_not_stale():
    """If this fails, run `python tools/gen_addon_fields.py`.

    The decoder reads the Python table and the addon reads the generated Lua one. A hand
    edit to either is a silent disagreement inside a wire format with no way to notice,
    so staleness is a test failure rather than a convention.
    """
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_addon_fields.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_field_says_how_it_is_measured():
    """A field cannot exist without a way to observe it."""
    from jev.perceive.fields import FIELDS

    for f in FIELDS:
        assert f.lua.strip(), f"{f.name} has no measurement"
        assert "return" in f.lua, f"{f.name} never returns a value"
