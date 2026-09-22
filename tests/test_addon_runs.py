"""Execute the real addon and decode what it paints.

This is the only test that runs the Lua itself. Everything else about the wire format is
checked against a Python model of it, which proves the *format* and cannot prove the
*addon* — a nil index, a wrong arity or a helper renamed on one side would pass every
other test in this suite and fail on a client that has to be restarted to try again.

Needs `lua5.1` on PATH and skips without it, because a developer machine without Lua
should still be able to run the suite.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from jev.perceive import radio
from jev.perceive.fields import (
    CALIBRATION_SWATCHES,
    GRID_COLS,
    MARKER_L,
    MARKER_R,
    layout,
)

PAYLOAD_CELLS = layout()["payload_cells"]

LUA = shutil.which("lua5.1") or shutil.which("lua")
pytestmark = pytest.mark.skipif(LUA is None, reason="needs lua5.1 to run the real addon")


def paint(state: dict | None = None) -> list[tuple[int, int, int]]:
    """Run the addon under the stubbed client and return every painted cell."""
    literal = "return {" + ", ".join(f"{k}={_lua(v)}" for k, v in (state or {}).items()) + "}"
    proc = subprocess.run(
        [LUA, "tests/lua/paint_once.lua"],
        capture_output=True, text=True, env={"JEV_STATE": literal, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"the addon raised:\n{proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines[0].startswith("CELLS"), proc.stdout[:200]
    return [tuple(int(v) for v in line.split()) for line in lines[1:]]


def _lua(v) -> str:
    """Python value to a Lua literal.

    `False` becomes Lua `false`, not `nil`. In a Lua table `nil` means *absent*, so the
    stub's default would be handed back and a test that asked for "no target" would get
    the default target instead — quietly testing the opposite of what it says.
    `None` is how a test asks for genuine absence.
    """
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "1" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def payload(cells: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Everything after the calibration row."""
    return cells[GRID_COLS:]


# --- it runs at all ---------------------------------------------------------

def test_the_addon_loads_and_paints_without_raising():
    """The largest residual risk in the whole radio design: a runtime error in Lua only
    surfaces on a client, and a client has to be restarted to try again."""
    cells = paint()
    assert len(cells) >= GRID_COLS, "nothing was painted"


@pytest.mark.parametrize("panel", ["GameMenuFrame", "OptionsFrame", "InterfaceOptionsFrame",
                                  "VideoOptionsFrame", "AudioOptionsFrame", "KeyBindingFrame",
                                  "AddonList", "ScriptErrorsFrame", "StaticPopup1"])
def test_blocking_panels_survive_the_real_lua_wire_round_trip(panel):
    values = radio.unpack(payload(paint({"visiblePanel": panel}))[:PAYLOAD_CELLS])
    assert values["ui.modal"] is True
    hidden = radio.unpack(payload(paint({"visiblePanel": panel, "panelHidden": True}))[:PAYLOAD_CELLS])
    assert hidden["ui.modal"] is False


def test_a_quest_panel_is_not_a_blocking_menu():
    values = radio.unpack(payload(paint({"visiblePanel": "QuestFrame"}))[:PAYLOAD_CELLS])
    assert values["ui.modal"] is False


def test_the_calibration_row_is_what_the_decoder_expects():
    """Both sides read this row from the same generated table, so a mismatch here means
    the generator and the addon have drifted."""
    cells = paint()
    assert cells[0] == MARKER_L
    assert cells[GRID_COLS - 1] == MARKER_R
    assert cells[1:GRID_COLS - 1] == list(CALIBRATION_SWATCHES)


# --- it paints what it was told ---------------------------------------------

def test_what_the_client_knows_comes_back_out_of_the_decoder():
    """The end-to-end claim: values set on the stubbed client survive the addon's packer,
    the twelve-bit cells and the Python decoder unchanged."""
    cells = paint({"level": 23, "hp": 45, "hpMax": 90, "freePerBag": 3, "dur": 60})
    values = radio.unpack(payload(cells)[:PAYLOAD_CELLS])

    assert values["char.level"] == 23
    assert values["vitals.hp"] == pytest.approx(0.5, abs=0.01)
    assert values["vitals.hp_max"] == 90
    assert values["bags.free"] == 3 * 5          # five bag slots in the stub
    assert values["bags.durability_min"] == pytest.approx(0.6, abs=0.01)


def test_a_position_survives_the_round_trip():
    """Map fractions are the highest-resolution thing on the wire; if anything loses
    precision in the packer it shows here first."""
    cells = paint({"mx": 0.4817, "my": 0.4294})
    values = radio.unpack(payload(cells)[:PAYLOAD_CELLS])
    assert values["pos.mx"] == pytest.approx(0.4817, abs=0.0005)
    assert values["pos.my"] == pytest.approx(0.4294, abs=0.0005)


def test_the_sequence_counter_advances_between_paints():
    """A frozen sequence is how a hung addon is told apart from a misread, so it has to
    actually move."""
    a = radio.unpack(payload(paint())[:PAYLOAD_CELLS])["seq"]
    b = radio.unpack(payload(paint({"time": 2000.0}))[:PAYLOAD_CELLS])["seq"]
    assert a is not None and b is not None


def test_an_unobservable_field_is_painted_as_unknown_not_as_false():
    """`UnitAffectingCombat` returns nil out of combat, which is a real observation, but a
    getter that cannot evaluate at all must paint the NA code. Unknown is not a negative
    fact, all the way down to the wire."""
    values = radio.unpack(payload(paint({"hasTarget": False}))[:PAYLOAD_CELLS])
    assert values["target.has"] is False
    assert values["target.hp"] is None, "an absent target has no health, not zero health"
    assert values["target.level"] is None


def test_the_addon_only_touches_a_small_api_surface():
    """Paint only (PLAN §2.2). Nothing here may actuate."""
    # Comments are stripped first. Both files explain at length that they do not call
    # these functions, and a naive substring search finds the explanation.
    import re

    src = ""
    for name in ("Helpers.lua", "JevRadio.lua"):
        text = pathlib.Path(f"addons/JevRadio/{name}").read_text(encoding="utf-8")
        text = re.sub(r"--\[\[.*?\]\]", "", text, flags=re.S)
        src += "\n".join(line.split("--")[0] for line in text.splitlines())
    for forbidden in ("UseAction", "CastSpellByName", "MoveForwardStart", "SetCVar",
                      "TurnLeftStart", "JumpOrAscendStart", "RunBinding", "SendChatMessage"):
        assert forbidden not in src, f"the addon calls {forbidden}, which actuates"


@pytest.mark.parametrize(("flag", "complete"), [(1, True), (0, False), (-1, False)])
def test_quest_completion_is_a_positive_one_not_lua_truthiness(flag, complete):
    values = radio.unpack(payload(paint({"questComplete": flag}))[:PAYLOAD_CELLS])
    assert values["quests.slot_complete"] is complete
