"""One bounded action, guarded delivery, and unconditional input cleanup.

The caller owns observed outcomes. This executor never treats accepted input as an
approach, attack, kill, loot, or successful UI transaction.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass

from jev.clients.camera import GRAB_S
from jev.perceive.fields import SEQ_MODULUS
from jev.play.actions import (
    Action,
    ActionSlotAction,
    CameraAction,
    ClickAction,
    KeyAction,
    ObserveAction,
    PointerAction,
    SkillAction,
    action_dict,
    modal_action_allowed,
    parse_action,
)
from jev.play.controls import ControlManifest, Limits, build_manifest


@dataclass(frozen=True)
class GuardState:
    values: dict | None
    captured_at: float
    origin: tuple[int, int] = (0, 0)
    size: tuple[int, int] = (1600, 900)
    id: str = ""
    cursor: tuple[int, int] | None = None

    @classmethod
    def from_observation(cls, observation: dict) -> GuardState:
        return cls(observation.get("values"), observation["captured_at"],
                   tuple(observation["origin"]), tuple(observation["size"]),
                   observation.get("id", ""),
                   tuple(observation["cursor"]) if observation.get("cursor") else None)

    @property
    def sequence(self) -> int | None:
        value = (self.values or {}).get("seq")
        return value if type(value) is int and 0 <= value < SEQ_MODULUS else None


@dataclass(frozen=True)
class ExecutionResult:
    code: str
    delivered: bool
    detail: str
    action: dict
    input_events: int = 0
    point: tuple[int, int] | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class _Refusal(Exception):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


class Executor:
    def __init__(self, hid, read_guard: Callable[[], GuardState], *,
                 manifest: ControlManifest | None = None, limits: Limits | None = None,
                 checkpoint: Callable[[], None] | None = None,
                 execute_skill: Callable[[SkillAction], object] | None = None,
                 invalidate_camera: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.hid, self.read_guard = hid, read_guard
        self.manifest = manifest or build_manifest(limits=limits)
        self.limits = limits or self.manifest.limits
        self.checkpoint = checkpoint or (lambda: None)
        self.execute_skill = execute_skill
        self.invalidate_camera = invalidate_camera or (lambda reason: None)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self._executing = False
        self._toggle_requests: set[tuple[str, int | None]] = set()

    def execute(self, action: Action | dict, expected: GuardState | None = None) -> ExecutionResult:
        """Recheck fresh state after policy latency, then deliver at most one action.

        Cancellation is never swallowed. Every exit releases all held inputs; a release
        failure propagates rather than claiming a clean stop. Recursive skill delegation
        cannot re-enter this executor.
        """
        if self._executing:
            raise RuntimeError("recursive play execution is forbidden")
        self._executing = True
        original_checkpoint = getattr(self.hid, "checkpoint", None)
        before = getattr(self.hid, "sent", 0)
        point = None
        raw = action_dict(action) if not isinstance(action, dict) else action
        try:
            action = parse_action(action)
            raw = action_dict(action)

            def guarded_checkpoint():
                self.checkpoint()
                if original_checkpoint is not None:
                    original_checkpoint()
                if not self.hid.ready():
                    raise _Refusal("refused", "client focus/input unavailable")

            self.hid.checkpoint = guarded_checkpoint
            if isinstance(action, ObserveAction):
                if action.wait_s > self.limits.max_wait_s:
                    raise _Refusal("unsupported", "observe duration exceeds operational limit")
                self._wait(action.wait_s, focus=False)
                return ExecutionResult("observed", False, "observation requested; no play action dispatched", raw)
            guarded_checkpoint()
            view = self._view()
            self._compare(view, expected)
            self._state(action, view)
            metadata = {}
            if isinstance(action, KeyAction):
                if action.duration_s > self.limits.max_hold_s:
                    raise _Refusal("unsupported", "hold duration exceeds operational limit")
                keys = self.manifest.resolve(action.control)
                if not keys:
                    raise _Refusal("unsupported", "control has no executable configured binding")
                if action.control == "attack_target":
                    self._target_alive(view)
                    self._toggle_guard("attack", view)
                accepted = self._key(keys, action.duration_s)
                if accepted and action.control == "attack_target":
                    self._toggle_requests.add(("attack", view.values.get("target.name_id")))
            elif isinstance(action, ActionSlotAction):
                row = self.manifest.slot(action.slot)
                if not row or not row["executable"]:
                    raise _Refusal("unsupported", "slot has no executable configured binding")
                self._slot_ready(action.slot, view)
                toggle = row.get("toggle") is True
                if toggle:
                    self._target_alive(view)
                    self._toggle_guard("attack", view, slot=action.slot)
                accepted = self._key(tuple(row["keys"]), 0)
                if accepted and toggle:
                    self._toggle_requests.add(("attack", view.values.get("target.name_id")))
                metadata["slot_identity_live_verified"] = row["live_identity_verified"]
            elif isinstance(action, PointerAction):
                point = self._point(view, action.x, action.y)
                accepted = self.hid.move_to(*point)
                if accepted:
                    point = self._cursor(view)
            elif isinstance(action, ClickAction):
                point = self._click(action, view)
                accepted = True
            elif isinstance(action, CameraAction):
                if abs(action.pixels) > self.limits.max_camera_pixels:
                    raise _Refusal("unsupported", "camera delta exceeds operational limit")
                point = self._point(view, 0.5, 0.5)
                accepted = self._camera(action, view, point)
            else:
                if action.name not in self.manifest.skills or self.execute_skill is None:
                    raise _Refusal("unsupported", "skill is not available for trusted delegation")
                outcome = self.execute_skill(action)
                # A callback's game result is separate from our input delivery contract.
                metadata["skill_result"] = (outcome.to_dict() if hasattr(outcome, "to_dict")
                                            else asdict(outcome) if is_dataclass(outcome)
                                            else outcome if isinstance(outcome, dict)
                                            else str(outcome))
                return ExecutionResult("delegated", False, "trusted skill returned; inspect its result",
                                       raw, getattr(self.hid, "sent", before) - before,
                                       metadata=metadata)
            events = getattr(self.hid, "sent", before) - before
            if not accepted:
                return ExecutionResult("refused", False, getattr(self.hid, "detail", "input refused"),
                                       raw, events, point, metadata)
            return ExecutionResult("delivered", True, "input accepted; game effect remains unverified",
                                   raw, events, point, metadata)
        except _Refusal as exc:
            return ExecutionResult(exc.code, False, exc.detail, raw,
                                   getattr(self.hid, "sent", before) - before, point)
        finally:
            try:
                self.hid.release_all()
                if getattr(self.hid, "held", ()) or getattr(self.hid, "held_buttons", ()):
                    raise RuntimeError("input release returned with held keys/buttons")
            finally:
                self.hid.checkpoint = original_checkpoint
                self._executing = False

    def _view(self) -> GuardState:
        self.checkpoint()
        view = self.read_guard()
        if not isinstance(view, GuardState) or view.values is None:
            raise _Refusal("blind", "fresh decoded observation unavailable")
        age = self.clock() - view.captured_at
        if not math.isfinite(age) or age < 0 or age > self.limits.max_view_age_s:
            raise _Refusal("stale", "observation is outside the permitted capture age")
        if (len(view.origin) != 2 or len(view.size) != 2
                or any(type(n) is not int for n in (*view.origin, *view.size))
                or min(view.size) <= 0):
            raise _Refusal("blind", "client geometry is unknown or invalid")
        self.note_observation(view.values)
        return view

    def note_observation(self, values: dict) -> None:
        """Consume lifecycle evidence even when the outer loop owns outcome captures."""
        if (values.get("target.has") is False or values.get("target.hp") == 0
                or values.get("vitals.dead") is True or values.get("vitals.ghost") is True):
            self._toggle_requests.clear()

    @staticmethod
    def _compare(view: GuardState, expected: GuardState | None):
        if expected is None:
            return
        if view.origin != expected.origin or view.size != expected.size:
            raise _Refusal("stale", "client geometry changed after the policy observation")
        if expected.values:
            fields = ("target.has", "target.name_id", "ui.modal", "vitals.dead", "vitals.ghost")
            if any(view.values.get(k) != expected.values.get(k) for k in fields):
                raise _Refusal("stale", "target or critical state changed after policy observation")

    @staticmethod
    def _state(action: Action, view: GuardState):
        values = view.values
        if modal_action_allowed(action):
            # Esc can close an observed modal, but blind UI state still authorizes nothing.
            if values.get("ui.modal") is None:
                raise _Refusal("blind", "modal state is unknown")
            return
        if values.get("ui.modal") is not False:
            raise _Refusal("interrupted" if values.get("ui.modal") is True else "blind",
                           "blocking modal is present or unknown")
        if isinstance(action, SkillAction):
            # Recovery and service implementations own their own preconditions.
            return
        dead, ghost = values.get("vitals.dead"), values.get("vitals.ghost")
        if dead is None or ghost is None:
            raise _Refusal("blind", "alive/ghost state is unknown")
        if dead and not ghost:
            raise _Refusal("interrupted", "dead player requires recovery skill")
        if ghost and isinstance(action, ActionSlotAction | ClickAction):
            raise _Refusal("interrupted", "ghost requires recovery for world interactions")
        if ghost and isinstance(action, KeyAction) and action.control == "attack_target":
            raise _Refusal("interrupted", "ghost cannot attack")

    def _wait(self, seconds: float, *, focus: bool = True):
        deadline = self.monotonic() + seconds
        while self.monotonic() < deadline:
            self.checkpoint()
            if focus and not self.hid.ready():
                raise _Refusal("refused", "client lost focus")
            self.sleep(min(self.limits.poll_s, deadline - self.monotonic()))

    def _key(self, keys: tuple[str, ...], duration_s: float) -> bool:
        for modifier in keys[:-1]:
            if not self.hid.key_down(modifier):
                return False
        if duration_s:
            return self.hid.hold(keys[-1], duration_s, on_tick=self.checkpoint)
        return self.hid.tap(keys[-1])

    @staticmethod
    def _target_alive(view: GuardState):
        values = view.values
        hp = values.get("target.hp")
        if (values.get("target.has") is not True or values.get("target.name_id") is None
                or not isinstance(hp, int | float) or hp <= 0):
            raise _Refusal("interrupted", "attack needs an observed living target")

    def _toggle_guard(self, control: str, view: GuardState, slot: int | None = None):
        active = view.values.get("bars.active")
        if slot and type(active) is int:
            if active & (1 << (slot - 1)):
                raise _Refusal("already_active", "observed toggle is already active")
            return
        if (control, view.values.get("target.name_id")) in self._toggle_requests:
            raise _Refusal("toggle_unverified", "activation already delivered; toggle state is unknown")

    @staticmethod
    def _slot_ready(slot: int, view: GuardState):
        values = view.values
        if values.get("bars.casting") is not False:
            raise _Refusal("not_ready", "casting is active or unknown")
        gcd = values.get("bars.gcd")
        if not isinstance(gcd, int | float) or gcd != 0:
            raise _Refusal("not_ready", "global cooldown is active or unknown")
        for key in ("bars.ready", "bars.usable"):
            mask = values.get(key)
            if type(mask) is not int or not mask & (1 << (slot - 1)):
                raise _Refusal("not_ready", f"slot is not positively {key}")

    @staticmethod
    def _point(view: GuardState, x: float, y: float) -> tuple[int, int]:
        return (view.origin[0] + min(view.size[0] - 1, round(x * view.size[0])),
                view.origin[1] + min(view.size[1] - 1, round(y * view.size[1])))

    def _cursor(self, view: GuardState) -> tuple[int, int]:
        point = self.hid.cursor_position()
        if point is None or not (view.origin[0] <= point[0] < view.origin[0] + view.size[0]
                                 and view.origin[1] <= point[1] < view.origin[1] + view.size[1]):
            raise _Refusal("stale", "actual cursor is unavailable or outside the client")
        return point

    def _fresh_hover(self, action: Action, view: GuardState,
                     point: tuple[int, int]) -> GuardState:
        # The immediate post-arrival capture is a BASELINE, never hover evidence.
        baseline = self._view()
        self._compare(baseline, view)
        sequence = baseline.sequence
        if sequence is None:
            raise _Refusal("blind", "hover paint sequence is unavailable")
        deadline = self.monotonic() + self.limits.hover_timeout_s
        while self.monotonic() < deadline:
            self._wait(min(self.limits.poll_s, deadline - self.monotonic()))
            if self._cursor(view) != point:
                raise _Refusal("stale", "pointer moved during hover verification")
            current = self._view()
            self._compare(current, view)
            self._state(action, current)
            if current.sequence is not None and 0 < (current.sequence - sequence) % SEQ_MODULUS < 128:
                return current
        raise _Refusal("stale", "no newer hover paint arrived")

    @staticmethod
    def _ui_point(action: ClickAction, view: GuardState) -> tuple[float, float]:
        values = view.values
        if (values.get("ui.quest_frame") is not True and values.get("ui.gossip") is not True):
            raise _Refusal("interrupted", "no observed quest or gossip frame")
        if action.ui_control == "quest_advance":
            # Modal confirmation and merchant transactions are never authorized here.
            if values.get("ui.quest_frame") is not True:
                raise _Refusal("interrupted", "quest advance needs an observed quest frame")
            x, y = values.get("ui.advance_x"), values.get("ui.advance_y")
        elif action.ui_control == "quest_reward":
            if values.get("ui.quest_frame") is not True or values.get("ui.choice_made") is not False:
                raise _Refusal("interrupted", "reward choice needs an open, unchosen reward page")
            x, y = values.get("ui.choice_x"), values.get("ui.choice_y")
        else:
            matches = [i for i in range(5) if values.get(f"ui.list_hash{i}") == action.ui_name_id]
            if len(matches) != 1:
                raise _Refusal("interrupted", "gossip identity is absent or ambiguous")
            x, y = values.get("ui.list_x"), values.get(f"ui.list_y{matches[0]}")
        if (not isinstance(x, int | float) or not isinstance(y, int | float)
                or not 0 <= x <= 1 or not 0 <= y <= 1):
            raise _Refusal("blind", "painted UI control coordinates are unavailable")
        return x, y

    def _click(self, action: ClickAction, view: GuardState) -> tuple[int, int]:
        if action.intent == "ui":
            coordinates = self._ui_point(action, view)
        else:
            coordinates = (action.x, action.y) if action.x is not None else None
        if coordinates is not None and not self.hid.move_to(*self._point(view, *coordinates)):
            raise _Refusal("refused", "pointer movement was refused")
        point = self._cursor(view)
        hover = self._fresh_hover(action, view, point)
        values = hover.values
        if action.intent == "ui":
            if coordinates != self._ui_point(action, hover):
                raise _Refusal("stale", "painted UI control moved or changed")
            if values.get("cursor.world") is not False:
                raise _Refusal("blind", "UI mouse focus is not observed")
        else:
            if (values.get("cursor.has") is not True or values.get("cursor.world") is not True
                    or values.get("cursor.name_id") != action.expected_target_id
                    or values.get("cursor.dead") is not action.expected_dead):
                raise _Refusal("wrong_target", "fresh hover does not match requested world unit")
            if action.intent == "interact" and (
                values.get("target.has") is not True
                or values.get("target.name_id") != action.expected_target_id
                or values.get("cursor.is_target") is not True
            ):
                raise _Refusal("wrong_target", "hover unit is not exactly the selected target")
        # Never move again between the owned hover frame and the button event.
        self.checkpoint()
        if self._cursor(hover) != point or self.clock() - hover.captured_at > self.limits.max_view_age_s:
            raise _Refusal("stale", "hover frame or pointer became stale before click")
        if not self.hid.click(right=action.button == "right"):
            raise _Refusal("refused", "button delivery was refused")
        if action.intent == "interact" and not action.expected_dead:
            # TURNORACTION may activate attack. Never immediately press an unknown
            # attack toggle again merely because no damage has arrived yet.
            self._toggle_requests.add(("attack", action.expected_target_id))
        return point

    def _camera(self, action: CameraAction, view: GuardState, point: tuple[int, int]) -> bool:
        if not self.hid.move_to(*point):
            return False
        self._wait(GRAB_S)
        actual = self._cursor(view)
        current = self._fresh_hover(action, view, actual)
        if current.values.get("cursor.world") is not True or self._cursor(current) != actual:
            raise _Refusal("interrupted", "camera grab needs a fresh observed world surface")
        if action.axis == "pitch":
            self.invalidate_camera("explicit play pitch action")
        if not self.hid.button(True, right=True):
            return False
        try:
            self._wait(GRAB_S)
            accepted = self.hid.move_by(action.pixels if action.axis == "yaw" else 0,
                                        action.pixels if action.axis == "pitch" else 0)
            self._wait(GRAB_S)
        finally:
            released = self.hid.button(False, right=True)
        return accepted and released
