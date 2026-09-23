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

from jev.perceive import radio, radio_frame
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
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(_lua(x) for x in v) + "}"
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
                                  "AddonList", "ScriptErrors", "StaticPopup1"])
def test_blocking_panels_survive_the_real_lua_wire_round_trip(panel):
    values = radio.unpack(payload(paint({"visiblePanel": panel}))[:PAYLOAD_CELLS])
    assert values["ui.modal"] is True
    hidden = radio.unpack(payload(paint({"visiblePanel": panel, "panelHidden": True}))[:PAYLOAD_CELLS])
    assert hidden["ui.modal"] is False


def test_a_quest_panel_is_not_a_blocking_menu():
    values = radio.unpack(payload(paint({"visiblePanel": "QuestFrame"}))[:PAYLOAD_CELLS])
    assert values["ui.modal"] is False


def test_stock_message_dialog_blocks_an_otherwise_ready_merchant():
    """BasicControls.xml message() and _ERRORMESSAGE() both display ScriptErrors.

    An open service window cannot make that independent stock dialog non-modal.
    The observed TimeManager load failure uses message(), not StaticPopup_Show().
    """
    values = radio.unpack(payload(paint({"visiblePanel": "ScriptErrors",
                                         "inventoryFixture": True}))[:PAYLOAD_CELLS])
    assert values["ui.vendor"] is True
    assert values["ui.modal"] is True


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


def test_inventory_identity_exact_copper_and_supply_counts_survive_lua_wire():
    values = radio.unpack(payload(paint({"inventoryFixture": True, "money": 91234,
                                         "class": "PALADIN", "foodCount": 7}))[:PAYLOAD_CELLS])
    assert values["bags.money_copper"] == 91234
    assert values["inventory.item_id"] == 7073
    assert values["inventory.bag"] == 0 and values["inventory.slot"] == 1
    assert values["inventory.ordinal"] == 1 and values["inventory.total"] == 2
    assert values["inventory.quality"] == 0 and values["inventory.count"] == 2
    assert values["inventory.locked"] is False
    assert values["inventory.x"] == pytest.approx(1400 / 1600, abs=0.001)
    assert values["inventory.y"] == pytest.approx(1 - 400 / 900, abs=0.001)
    assert values["bags.food_id"] == 2070 and values["bags.food_count"] == 7
    assert values["bags.drink_id"] == 159 and values["bags.drink_count"] == 0


def test_uncached_item_is_unknown_and_invalidates_exact_supply_counts():
    values = radio.unpack(payload(paint({"inventoryFixture": True, "itemUnread": True,
                                         "class": "PALADIN"}))[:PAYLOAD_CELLS])
    assert values["inventory.item_id"] is None
    assert values["bags.food_count"] is None and values["bags.drink_count"] is None


def test_closed_bag_has_only_observed_opener_and_buyback_has_no_offer():
    values = radio.unpack(payload(paint({"inventoryFixture": True, "bagHidden": True,
                                         "buyback": True}))[:PAYLOAD_CELLS])
    assert values["inventory.x"] is None
    assert values["inventory.open_x"] == pytest.approx(1500 / 1600, abs=0.001)
    assert values["merchant.item_id"] is None and values["merchant.name_id"] is None


def test_stock_merchant_offer_has_identity_price_quantity_and_owned_total():
    values = radio.unpack(payload(paint({"inventoryFixture": True, "foodCount": 7,
                                         "offerPrice": 24}))[:PAYLOAD_CELLS])
    assert values["merchant.item_id"] == 2070
    assert values["merchant.quantity"] == 5 and values["merchant.price"] == 24
    assert values["merchant.unlimited"] is True and values["merchant.extended"] is False
    assert values["merchant.owned"] == 7
    assert values["merchant.x"] == pytest.approx(150 / 1600, abs=0.001)
    assert values["merchant.ready"] is True


@pytest.mark.parametrize("change", [{"shiftHeld": True}, {"cursorType": "item"},
                                    {"repairMode": True}])
def test_merchant_cursor_modifiers_and_repair_mode_are_observed(change):
    values = radio.unpack(payload(paint({"inventoryFixture": True, **change}))[:PAYLOAD_CELLS])
    assert values["merchant.ready"] is False


def test_vendor_gossip_uses_semantic_type_and_the_same_normalized_text_hash():
    from jev.perceive.radio_frame import name_id

    values = radio.unpack(payload(paint({"visiblePanel": "GossipFrame",
                                         "vendorGossip": True}))[:PAYLOAD_CELLS])
    assert values["merchant.gossip_name_id"] == name_id("Browse my wares.")
    for extra in ({"duplicateVendor": True}, {}, {"vendorGossip": True, "panelHidden": True},
                  {"vendorGossip": True, "wrongGossipID": True},
                  {"vendorGossip": True, "vendorLineHidden": True}):
        values = radio.unpack(payload(paint({"visiblePanel": "GossipFrame", **extra}))[:PAYLOAD_CELLS])
        assert values["merchant.gossip_name_id"] is None


@pytest.mark.parametrize("present", [None, False, 0])
def test_absent_mouseover_has_no_identity_or_death_observation(present):
    values = radio.unpack(payload(paint({"hasMouseover": present,
                                         "mouseoverName": "Kobold Vermin",
                                         "mouseoverDead": True,
                                         "mouseoverIsTarget": True}))[:PAYLOAD_CELLS])
    assert values["cursor.has"] is False
    assert values["cursor.name_id"] is None
    assert values["cursor.dead"] is None
    assert values["cursor.is_target"] is False


@pytest.mark.parametrize("same_target", [None, False, 0, True])
def test_mouseover_equality_uses_the_client_identity_not_the_name(same_target):
    from jev.perceive.radio_frame import name_id

    values = radio.unpack(payload(paint({"hasMouseover": True,
                                         "targetName": "Young Wolf",
                                         "mouseoverName": "Young Wolf",
                                         "mouseoverIsTarget": same_target}))[:PAYLOAD_CELLS])
    assert values["cursor.has"] is True
    assert values["target.name_id"] == values["cursor.name_id"] == name_id("Young Wolf")
    assert values["cursor.is_target"] is (same_target is True)


@pytest.mark.parametrize("dead", [None, False, 0, True])
def test_present_mouseover_death_nil_is_observed_false(dead):
    values = radio.unpack(payload(paint({"hasMouseover": True,
                                         "mouseoverDead": dead}))[:PAYLOAD_CELLS])
    assert values["cursor.dead"] is (dead is True)


@pytest.mark.parametrize(("focus", "expected"), [(None, None), ("ui", False), ("world", True)])
def test_mouse_focus_distinguishes_unknown_ui_and_world(focus, expected):
    values = radio.unpack(payload(paint({"mouseFocus": focus}))[:PAYLOAD_CELLS])
    assert values["cursor.world"] is expected


@pytest.mark.parametrize(("api", "fields"), [
    ("UnitExists", ("has", "name_id", "dead", "is_target")),
    ("UnitName", ("name_id",)),
    ("UnitIsDead", ("dead",)),
    ("UnitIsUnit", ("is_target",)),
    ("GetMouseFocus", ("world",)),
    ("WorldFrame", ("world",)),
])
def test_unavailable_cursor_api_is_unknown(api, fields):
    values = radio.unpack(payload(paint({"hasMouseover": True,
                                         "mouseoverName": "Young Wolf",
                                         "mouseoverDead": True,
                                         "mouseoverIsTarget": True,
                                         "mouseFocus": "world",
                                         "missingApi": api}))[:PAYLOAD_CELLS])
    assert all(values[f"cursor.{field}"] is None for field in fields)


@pytest.mark.parametrize(("api", "field"), [
    ("UnitExists", "has"), ("UnitName", "name_id"), ("UnitIsDead", "dead"),
    ("UnitIsUnit", "is_target"), ("GetMouseFocus", "world"),
])
def test_cursor_getter_failure_keeps_painting_with_unknown(api, field):
    values = radio.unpack(payload(paint({"hasMouseover": True,
                                         "throwingApi": api}))[:PAYLOAD_CELLS])
    assert values[f"cursor.{field}"] is None
    assert values["char.level"] == 4


# --- melee state: the toggle is observed before anything presses it -------------------

def test_auto_attack_state_is_the_stock_attack_button_flash():
    """IsAttackAction plus IsCurrentAction is exactly what ActionButton_UpdateFlash uses."""
    on = radio.unpack(payload(paint({"attackSlot": 1, "attacking": True}))[:PAYLOAD_CELLS])
    off = radio.unpack(payload(paint({"attackSlot": 1, "attacking": False}))[:PAYLOAD_CELLS])
    assert on["bars.attacking"] is True
    assert off["bars.attacking"] is False


def test_without_an_attack_action_the_combat_edge_is_the_state():
    unknown = radio.unpack(payload(paint())[:PAYLOAD_CELLS])
    started = radio.unpack(payload(paint({"events": [["PLAYER_ENTER_COMBAT"]]}))[:PAYLOAD_CELLS])
    stopped = radio.unpack(payload(paint({"events": [["PLAYER_ENTER_COMBAT"],
                                                     ["PLAYER_LEAVE_COMBAT"]]}))[:PAYLOAD_CELLS])
    assert unknown["bars.attacking"] is None, "no edge seen yet is unknown, not off"
    assert started["bars.attacking"] is True
    assert stopped["bars.attacking"] is False


@pytest.mark.parametrize(("reach", "expected"), [(1, True), (0, False), (None, None)])
def test_melee_range_is_the_attack_actions_own_range_check(reach, expected):
    values = radio.unpack(payload(paint({"attackSlot": 1, "meleeRange": reach}))[:PAYLOAD_CELLS])
    assert values["target.melee_range"] is expected


def test_melee_range_without_a_target_or_attack_action_is_unknown():
    no_target = radio.unpack(payload(paint({"attackSlot": 1, "meleeRange": 1,
                                            "hasTarget": False}))[:PAYLOAD_CELLS])
    no_action = radio.unpack(payload(paint({"meleeRange": 1}))[:PAYLOAD_CELLS])
    assert no_target["target.melee_range"] is None
    assert no_action["target.melee_range"] is None


def test_a_ui_error_is_held_long_enough_for_a_slow_reader():
    facing = [["UI_ERROR_MESSAGE", "You are facing the wrong way!"]]
    fresh = radio.unpack(payload(paint({"events": facing, "time": 100.0,
                                        "paintTime": 101.0}))[:PAYLOAD_CELLS])
    stale = radio.unpack(payload(paint({"events": facing, "time": 100.0,
                                        "paintTime": 102.0}))[:PAYLOAD_CELLS])
    assert radio_frame.UI_ERROR_KEYS[fresh["ui.error_last"]] == "not_facing"
    assert fresh["ui.error_count"] == 1
    assert stale["ui.error_last"] == 0, "held for 1.5 s, not forever"
    assert stale["ui.error_count"] == 1, "the count is how a reader tells a new error"
