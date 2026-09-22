"""The complete motor vocabulary; no text, shell, scripts or open-ended input."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

Control = Literal[
    "move_forward", "move_backward", "turn_left", "turn_right", "strafe_left",
    "strafe_right", "jump", "target_next", "target_previous", "escape", "attack_target",
    "target_self", "sit_stand", "toggle_enemy_nameplates", "toggle_friendly_nameplates",
    "toggle_all_nameplates", "toggle_bags", "toggle_quest_log", "toggle_map",
]
HELD_CONTROLS = frozenset({"move_forward", "move_backward", "turn_left", "turn_right",
                           "strafe_left", "strafe_right"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ObserveAction(Strict):
    kind: Literal["observe"] = "observe"
    wait_s: float = Field(default=0.0, ge=0.0, le=2.0)


class KeyAction(Strict):
    kind: Literal["key"] = "key"
    control: Control
    duration_s: float = Field(default=0.0, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def duration_matches_control(self):
        if self.control in HELD_CONTROLS and self.duration_s <= 0:
            raise ValueError("movement and turning require a positive duration_s")
        if self.control not in HELD_CONTROLS and self.duration_s != 0:
            raise ValueError("tap/toggle controls cannot be held")
        return self


class ActionSlotAction(Strict):
    kind: Literal["action_slot"] = "action_slot"
    slot: int = Field(ge=1, le=12)


class PointerAction(Strict):
    kind: Literal["pointer"] = "pointer"
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class ClickAction(Strict):
    kind: Literal["click"] = "click"
    button: Literal["left", "right"]
    intent: Literal["select", "interact", "ui"]
    x: float | None = Field(default=None, ge=0.0, le=1.0)
    y: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_target_id: int | None = Field(default=None, ge=0, le=65534)
    expected_dead: bool = False
    ui_control: Literal["quest_advance", "gossip_line"] | None = None
    ui_name_id: int | None = Field(default=None, ge=0, le=65534)

    @model_validator(mode="after")
    def complete_grounding(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("click coordinates must be supplied as an x,y pair")
        if self.intent != "ui":
            if self.expected_target_id is None:
                raise ValueError("unit clicks require expected_target_id")
            if self.ui_control is not None or self.ui_name_id is not None:
                raise ValueError("unit click cannot carry UI grounding")
            if ((self.intent == "select" and self.button != "left")
                    or (self.intent == "interact" and self.button != "right")):
                raise ValueError("select uses left; interact uses right")
        else:
            if self.ui_control is None or self.button != "left":
                raise ValueError("UI click requires a painted ui_control and left button")
            if self.x is not None or self.expected_target_id is not None or self.expected_dead:
                raise ValueError("UI coordinates derive from fresh painted controls")
            if (self.ui_control == "gossip_line") != (self.ui_name_id is not None):
                raise ValueError("only gossip_line requires ui_name_id")
        return self


class CameraAction(Strict):
    kind: Literal["camera"] = "camera"
    axis: Literal["yaw", "pitch"]
    pixels: int = Field(ge=-500, le=500)

    @model_validator(mode="after")
    def nonzero(self):
        if not self.pixels:
            raise ValueError("camera movement requires a nonzero delta")
        return self


class SkillAction(Strict):
    kind: Literal["skill"] = "skill"
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    params: dict[str, JsonValue] = Field(default_factory=dict)


Action = Annotated[
    ObserveAction | KeyAction | ActionSlotAction | PointerAction | ClickAction | CameraAction
    | SkillAction,
    Field(discriminator="kind"),
]
_ADAPTER = TypeAdapter(Action)


def parse_action(value: dict | Action) -> Action:
    return _ADAPTER.validate_python(value)


def action_dict(action: Action) -> dict:
    return _ADAPTER.dump_python(action, mode="json", exclude_none=True)


def action_schema() -> dict:
    return _ADAPTER.json_schema()
