"""Executable controls, their semantics, and the evidence behind their bindings.

Saved binding files are configuration evidence, not proof the running client has loaded
them. Starting action bars come from the existing generated server data; their live
identity is explicitly unknown. Neither source is silently promoted to observation.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from jev.clients.hid import VK
from jev.play.actions import HELD_CONTROLS
from jev.world.combat import GENERIC, SELF_CAST_MODIFIER, for_class

# control -> command, repo binding, semantics. Configured overrides always win.
CONTROL_SPECS = {
    "move_forward": ("MOVEFORWARD", "W", "Hold: move along current character heading."),
    "move_backward": ("MOVEBACKWARD", "S", "Hold: move backward."),
    "turn_left": ("TURNLEFT", "A", "Hold: turn character left; does not strafe."),
    "turn_right": ("TURNRIGHT", "D", "Hold: turn character right; does not strafe."),
    "strafe_left": ("STRAFELEFT", "Q", "Hold: move sideways left."),
    "strafe_right": ("STRAFERIGHT", "E", "Hold: move sideways right."),
    "jump": ("JUMP", "SPACE", "Tap: request jump; verify resulting motion."),
    "target_next": ("TARGETNEARESTENEMY", "TAB", "Tap: cycle enemies, possibly offscreen."),
    "target_previous": ("TARGETPREVIOUSENEMY", "SHIFT-TAB", "Tap: reverse enemy cycle."),
    "escape": ("TOGGLEGAMEMENU", "ESCAPE", "Tap: dismiss current UI/target or open menu; observe."),
    "attack_target": ("ATTACKTARGET", "T", "Toggle melee auto-attack; delivery is not damage."),
    "target_self": ("TARGETSELF", "F1", "Tap: select self; verify target afterward."),
    "sit_stand": ("SITORSTAND", "X", "Toggle sitting/standing; observe afterward."),
    "toggle_enemy_nameplates": ("NAMEPLATES", "V", "Toggle hostile nameplate visibility."),
    "toggle_friendly_nameplates": ("FRIENDNAMEPLATES", "SHIFT-V", "Toggle friendly nameplates."),
    "toggle_all_nameplates": ("ALLNAMEPLATES", "CTRL-V", "Toggle all nameplates."),
    "toggle_bags": ("OPENALLBAGS", "SHIFT-B", "Toggle bags; no sale/use/delete action."),
    "toggle_quest_log": ("TOGGLEQUESTLOG", "L", "Toggle quest log."),
    "toggle_map": ("TOGGLEWORLDMAP", "M", "Toggle world map."),
}
_ALIASES = {"ESCAPE": "esc", "SPACE": "space", "-": "minus", "=": "equals"}


@dataclass(frozen=True)
class Limits:
    """Operational exposure limits, not estimates of game mechanics."""

    max_hold_s: float = 2.0
    max_wait_s: float = 2.0
    max_camera_pixels: int = 500
    max_view_age_s: float = 0.5
    hover_timeout_s: float = 1.0
    poll_s: float = 0.05

    def __post_init__(self):
        for value in asdict(self).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("operational limits must be positive and finite")
        if self.max_hold_s > 2 or self.max_wait_s > 2 or self.max_camera_pixels > 500:
            raise ValueError("operational limits cannot exceed the action schema")


def key_parts(binding: str) -> tuple[str, ...] | None:
    """Translate a saved chord only when every key has an existing HID implementation."""
    remaining = binding.upper()
    parts = []
    while any(remaining.startswith(m + "-") for m in ("SHIFT", "CTRL", "ALT")):
        modifier, remaining = remaining.split("-", 1)
        if modifier.lower() in parts:
            return None
        parts.append(modifier.lower())
    key = _ALIASES.get(remaining, remaining.lower())
    if key not in VK or key in {"shift", "ctrl", "alt"}:
        return None
    # Saved configuration cannot authorize an OS-level close/task-switch gesture.
    if (("alt" in parts and key in {"tab", "f4"})
            or ("ctrl" in parts and key == "esc")):
        return None
    return (*parts, key)


@dataclass(frozen=True)
class ControlManifest:
    bindings: dict[str, dict]
    action_slots: tuple[dict, ...]
    binding_inventory: tuple[dict, ...]
    sources: tuple[dict, ...]
    skills: tuple[str, ...]
    limits: Limits

    def resolve(self, control: str) -> tuple[str, ...] | None:
        row = self.bindings.get(control)
        return tuple(row["keys"]) if row and row["executable"] else None

    def slot(self, slot: int) -> dict | None:
        return next((row for row in self.action_slots if row["slot"] == slot), None)

    def to_dict(self) -> dict:
        return {
            "version": 1, "bindings": self.bindings,
            "action_slots": list(self.action_slots),
            "binding_inventory": list(self.binding_inventory), "sources": list(self.sources),
            "skills": list(self.skills), "limits": asdict(self.limits),
            "pointer": {
                "coordinates": "normalized client image, top-left origin",
                "select": "Left button after fresh hover identity/world verification.",
                "interact": "Right button after fresh hover is_target; facing is NOT established.",
                "camera": "Hold right button and send one-axis relative deltas; observe actual effect.",
                "yaw": "Horizontal delta; no assumed pixels-to-heading conversion.",
                "pitch": "Vertical delta; invalidates retained pitch calibration.",
                "ui": "Only freshly painted quest advance/list controls, or trusted service skills.",
            },
            "uncertainty": [
                "Saved bindings can differ from unsaved live settings.",
                "Generated starting-slot identities are not current live action-bar verification.",
                "Radio reports ready/usable, but not current auto-attack toggle state.",
                "Target name hashes distinguish names, not two units sharing a name.",
                "target.in_melee is an approximately 11-yard interaction check, not melee range.",
                "Input delivery does not establish facing, approach, attack, loot or UI success.",
            ],
        }


def build_manifest(values: dict | None = None, *, binding_paths=(), skills=(),
                   limits: Limits | None = None) -> ControlManifest:
    """Read explicit account then character overrides without modifying client settings.

    A nonempty saved file replaces fallback assumptions. Missing semantic commands remain
    unavailable rather than reintroducing a default key that may now mean something else.
    Empty character files do not erase the account configuration.
    """
    values = values or {}
    sources, effective, modified = [], {}, []
    for raw_path in binding_paths:
        path = Path(raw_path)
        data = path.read_bytes()  # a requested file failing to read is a preflight failure
        source = str(path)
        sources.append({"path": source, "sha256": hashlib.sha256(data).hexdigest(),
                        "evidence": "saved_configuration_not_live_readback"})
        for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # This is WoW's line format, not shell syntax: a lone apostrophe is a key.
            parts = line.split(maxsplit=2)
            if len(parts) != 3 or parts[0].lower() not in {"bind", "modifiedclick"}:
                raise ValueError(f"unsupported binding syntax at {source}:{number}")
            kind, physical, command = parts
            if len(physical) >= 2 and physical[0] == physical[-1] == '"':
                physical = physical[1:-1]
            if len(command) >= 2 and command[0] == command[-1] == '"':
                command = command[1:-1]
            row = {"kind": kind.lower(), "binding": physical.upper(), "command": command,
                   "source": source, "line": number}
            if kind.lower() == "bind":
                effective[physical.upper()] = row
            else:
                modified.append({**row, "executable": False,
                                 "semantics": "Client modified-click configuration; inventory only."})
    configured = bool(effective)
    if not configured:
        defaults = [(spec[1], spec[0]) for spec in CONTROL_SPECS.values()]
        defaults += [(str(n) if n < 10 else {10: "0", 11: "-", 12: "="}[n],
                      f"ACTIONBUTTON{n}") for n in range(1, 13)]
        effective = {key: {"kind": "bind", "binding": key, "command": command,
                           "source": "repo_configuration_assumption", "line": None}
                     for key, command in defaults}
    by_command: dict[str, list[dict]] = {}
    for row in effective.values():
        if row["command"]:
            by_command.setdefault(row["command"], []).append(row)

    def binding(command: str, default: str) -> dict:
        rows = by_command.get(command, [])
        ordered = sorted(rows, key=lambda row: row["binding"] != default)
        available = [(row, key_parts(row["binding"])) for row in ordered]
        chosen = next(((row, keys) for row, keys in available if keys), None)
        row, keys = chosen if chosen else ({}, None)
        return {"command": command, "keys": list(keys or ()),
                "alternatives": [r["binding"] for r in rows], "executable": keys is not None,
                "source": row.get("source"),
                "evidence": "saved_configuration" if configured else "assumed_repo_binding",
                "live_verified": False}

    bindings = {}
    for control, (command, default, semantics) in CONTROL_SPECS.items():
        bindings[control] = {**binding(command, default), "semantics": semantics,
                             "mode": "hold" if control in HELD_CONTROLS else
                             "toggle" if control.startswith("toggle_") or control in
                             {"attack_target", "sit_stand"} else "tap"}
    profile = for_class(values.get("char.class_id"), values.get("char.race_id"))
    abilities = {a.slot: a for a in profile.abilities}
    # The saved self-cast modifier, else the 2.4.3 default (`combat.SELF_CAST_MODIFIER`).
    saved = next((row["binding"].lower() for row in modified
                  if row["command"].upper() == "SELFCAST"), None)
    self_cast_key = saved if saved in {"alt", "ctrl", "shift"} else SELF_CAST_MODIFIER
    slots = []
    for slot in range(1, 13):
        key = str(slot) if slot < 10 else {10: "0", 11: "-", 12: "="}[slot]
        ability = abilities.get(slot)
        row = binding(f"ACTIONBUTTON{slot}", key)
        if ability is not None and ability.self_cast and row["keys"]:
            # A heal waits for a target click when a hostile or dead unit is selected.
            row = {**row, "keys": [self_cast_key, *row["keys"]]}
        slots.append({"slot": slot, **row,
                      "self_cast": bool(ability and ability.self_cast),
                      "name": ability.name if ability else None,
                      "role": ability.role.value if ability else None,
                      "toggle": ability.toggle if ability else None,
                      "identity_source": ("generic_repo_fallback" if profile is GENERIC
                                          else "generated_starting_profile") if ability else "unknown",
                      "live_identity_verified": False})
    commands = {row["command"] for row in bindings.values()}
    commands.update(f"ACTIONBUTTON{slot}" for slot in range(1, 13))
    inventory = tuple({**row, "executable": row["command"] in commands
                        and key_parts(row["binding"]) is not None,
                        "semantics": next((s[2] for s in CONTROL_SPECS.values()
                                           if s[0] == row["command"]),
                                          "Client command; consult exact-client knowledge.")}
                       for row in effective.values()) + tuple(modified)
    return ControlManifest(bindings, tuple(slots), inventory, tuple(sources),
                           tuple(sorted(set(skills))), limits or Limits())
