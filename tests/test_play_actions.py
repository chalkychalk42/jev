import json

import pytest
from pydantic import ValidationError

from jev.play.actions import action_dict, action_schema, modal_action_allowed, parse_action
from jev.play.controls import Limits, build_manifest, key_parts


@pytest.mark.parametrize("action", [
    {"kind": "key", "control": "move_forward", "duration_s": 0.45},
    {"kind": "key", "control": "turn_right", "duration_s": 0.31},
    {"kind": "key", "control": "attack_target"},
    {"kind": "action_slot", "slot": 12},
    {"kind": "pointer", "x": 0.0, "y": 1.0},
    {"kind": "camera", "axis": "yaw", "pixels": -20},
    {"kind": "click", "button": "right", "intent": "interact", "expected_target_id": 42,
     "x": 0.5, "y": 0.4, "expected_dead": True},
    {"kind": "click", "button": "left", "intent": "ui", "ui_control": "gossip_line",
     "ui_name_id": 15},
    {"kind": "skill", "name": "TRAVEL_TO", "params": {"x": 0.5}},
    {"kind": "observe", "wait_s": 0.1},
])
def test_action_round_trip(action):
    parsed = parse_action(action)
    assert parse_action(json.loads(json.dumps(action_dict(parsed)))) == parsed


@pytest.mark.parametrize("action", [
    {"kind": "key", "control": "move_forward"},
    {"kind": "key", "control": "turn_left", "duration_s": 2.001},
    {"kind": "key", "control": "target_next", "duration_s": 0.2},
    {"kind": "key", "control": "enter"},
    {"kind": "key", "control": "jump", "text": "/script x"},
    {"kind": "action_slot", "slot": True},
    {"kind": "action_slot", "slot": "1"},
    {"kind": "action_slot", "slot": 13},
    {"kind": "pointer", "x": float("nan"), "y": 0.5},
    {"kind": "pointer", "x": 0.5, "y": 1.01},
    {"kind": "click", "button": "right", "intent": "interact"},
    {"kind": "click", "button": "left", "intent": "select", "expected_target_id": 42, "x": 0.4},
    {"kind": "click", "button": "right", "intent": "select", "expected_target_id": 42},
    {"kind": "click", "button": "left", "intent": "ui", "ui_control": "quest_advance",
     "x": 0.5, "y": 0.5},
    {"kind": "click", "button": "left", "intent": "ui", "ui_control": "gossip_line"},
    {"kind": "camera", "axis": "yaw", "pixels": 0},
    {"kind": "camera", "axis": "pitch", "pixels": 501},
    {"kind": "skill", "name": "__import__", "params": {}},
    {"kind": "observe", "wait_s": -1.0},
])
def test_action_rejects_unbounded_or_ambiguous_input(action):
    with pytest.raises(ValidationError):
        parse_action(action)


def test_schema_exposes_discriminator_and_no_arbitrary_key():
    schema = action_schema()
    assert schema["discriminator"]["propertyName"] == "kind"
    assert "enter" not in schema["$defs"]["KeyAction"]["properties"]["control"]["enum"]
    assert all(row.get("additionalProperties") is False for row in schema["$defs"].values()
               if row.get("type") == "object")


def test_modal_schema_only_advertises_observe_or_escape_and_removes_other_definitions():
    schema = action_schema({"ui.modal": True})
    assert set(schema["$defs"]) == {"ObserveAction", "KeyAction"}
    assert schema["discriminator"]["mapping"] == {
        "observe": "#/$defs/ObserveAction", "key": "#/$defs/KeyAction"}
    assert schema["oneOf"] == [{"$ref": ref}
                               for ref in schema["discriminator"]["mapping"].values()]
    key = schema["$defs"]["KeyAction"]["properties"]
    assert key["control"]["const"] == "escape" and "enum" not in key["control"]
    assert key["duration_s"]["const"] == key["duration_s"]["maximum"] == 0
    assert action_schema({"ui.modal": False}) == action_schema()
    assert action_schema({"ui.modal": None}) == action_schema()


@pytest.mark.parametrize("action,allowed", [
    ({"kind": "observe", "wait_s": 0.2}, True),
    ({"kind": "key", "control": "escape"}, True),
    ({"kind": "key", "control": "target_next"}, False),
    ({"kind": "click", "button": "left", "intent": "ui", "ui_control": "quest_advance"}, False),
    ({"kind": "skill", "name": "ABORT_WAIT"}, False),
])
def test_shared_modal_action_policy(action, allowed):
    assert modal_action_allowed(parse_action(action)) is allowed


def test_saved_bindings_override_defaults_and_keep_full_inventory(tmp_path):
    account = tmp_path / "account.wtf"
    character = tmp_path / "character.wtf"
    account.write_text("bind W MOVEFORWARD\nbind A TURNLEFT\nbind 1 ACTIONBUTTON1\n"
                       "bind ENTER OPENCHAT\nbind ' MOVIE_RECORDING_GUI\n"
                       "modifiedclick ALT SELFCAST\n")
    character.write_text('bind W OPENCHAT\nbind UP MOVEFORWARD\nbind A ""\n')
    manifest = build_manifest(binding_paths=[account, character])
    assert manifest.resolve("move_forward") == ("up",)
    assert manifest.resolve("turn_left") is None
    assert manifest.resolve("turn_right") is None
    assert manifest.slot(1)["keys"] == ["1"]
    assert manifest.slot(2)["executable"] is False
    assert any(row["command"] == "OPENCHAT" and not row["executable"]
               for row in manifest.binding_inventory)
    assert manifest.bindings["move_forward"]["evidence"] == "saved_configuration"
    assert not manifest.bindings["move_forward"]["live_verified"]
    assert len(manifest.sources) == 2
    assert manifest.sources[0]["sha256"]
    json.dumps(manifest.to_dict())


def test_empty_character_bindings_retain_account_configuration(tmp_path):
    account, character = tmp_path / "a.wtf", tmp_path / "b.wtf"
    account.write_text("bind D TURNRIGHT\n")
    character.write_text("")
    manifest = build_manifest(binding_paths=[account, character])
    assert manifest.resolve("turn_right") == ("d",)
    assert manifest.resolve("turn_left") is None


def test_missing_requested_bindings_fail_and_defaults_are_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_manifest(binding_paths=[tmp_path / "missing"])
    manifest = build_manifest({"char.class_id": 2, "char.race_id": 1})
    assert manifest.resolve("turn_right") == ("d",)
    assert manifest.resolve("strafe_right") == ("e",)
    assert manifest.bindings["turn_right"]["evidence"] == "assumed_repo_binding"
    assert manifest.slot(1)["live_identity_verified"] is False


def test_a_heal_slot_is_pressed_with_the_saved_self_cast_modifier(tmp_path):
    """Measured 23 September: Holy Light with a corpse selected waited for a target click."""
    saved = tmp_path / "bindings.wtf"
    saved.write_text("bind 1 ACTIONBUTTON1\nbind 3 ACTIONBUTTON3\nmodifiedclick CTRL SELFCAST\n")
    paladin = {"char.class_id": 2, "char.race_id": 1}
    manifest = build_manifest(paladin, binding_paths=[saved])
    assert manifest.slot(3)["keys"] == ["ctrl", "3"] and manifest.slot(3)["self_cast"]
    assert manifest.slot(1)["keys"] == ["1"] and not manifest.slot(1)["self_cast"]
    default = build_manifest(paladin)
    assert default.slot(3)["keys"] == ["alt", "3"], "the 2.4.3 default modifier"


def test_supported_modifier_bindings_and_unknown_hardware():
    assert key_parts("CTRL-SHIFT-TAB") == ("ctrl", "shift", "tab")
    assert key_parts("CTRL--") == ("ctrl", "minus")
    assert key_parts("BUTTON4") is None
    assert key_parts("SHIFT-SHIFT-W") is None
    assert key_parts("ALT-F4") is None
    assert key_parts("ALT-TAB") is None
    assert key_parts("CTRL-SHIFT-ESCAPE") is None


@pytest.mark.parametrize("kwargs", [{"max_hold_s": 3}, {"max_camera_pixels": 501},
                                    {"max_view_age_s": float("nan")}, {"poll_s": 0}])
def test_limits_are_explicit_finite_exposure_budgets(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)
