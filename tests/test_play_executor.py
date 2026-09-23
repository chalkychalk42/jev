from dataclasses import replace

import pytest

from jev.perceive.fields import SEQ_MODULUS
from jev.play.controls import Limits, build_manifest
from jev.play.executor import Executor, GuardState


class Clock:
    now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0, seconds)


class Hid:
    def __init__(self):
        self.sent, self.held, self.held_buttons = 0, set(), set()
        self.focused, self.accepted, self.checkpoint = True, True, None
        self.calls, self.position, self.releases = [], (60, 60), 0
        self.detail = "test refusal"
        self.on_hold = lambda: None

    def ready(self):
        return self.focused

    def key_down(self, key):
        if self.checkpoint:
            self.checkpoint()
        self.calls.append(("down", key))
        if self.accepted:
            self.held.add(key)
            self.sent += 1
        return self.accepted

    def tap(self, key):
        ok = self.key_down(key)
        self.held.discard(key)
        self.calls.append(("tap", key))
        return ok

    def hold(self, key, seconds, on_tick=None):
        if not self.key_down(key):
            return False
        self.calls.append(("hold", key, seconds))
        self.on_hold()
        if on_tick:
            on_tick()
        if self.checkpoint:
            self.checkpoint()
        self.held.discard(key)
        return self.accepted

    def move_to(self, x, y):
        if self.checkpoint:
            self.checkpoint()
        self.calls.append(("move", x, y))
        if self.accepted:
            self.position = (x, y)
            self.sent += 1
        return self.accepted

    def cursor_position(self):
        return self.position if self.focused else None

    def click(self, **kwargs):
        assert set(kwargs) == {"right"}  # button-only; never repeat pointer movement
        if self.checkpoint:
            self.checkpoint()
        self.calls.append(("click", kwargs["right"]))
        if self.accepted:
            self.sent += 2
        return self.accepted

    def button(self, down, right=False):
        if down and self.checkpoint:
            self.checkpoint()
        self.calls.append(("button", down, right))
        if self.accepted:
            self.sent += 1
            (self.held_buttons.add if down else self.held_buttons.discard)(right)
        return self.accepted

    def move_by(self, dx, dy):
        if self.checkpoint:
            self.checkpoint()
        self.calls.append(("relative", dx, dy))
        return self.accepted

    def release_all(self):
        self.releases += 1
        self.held.clear()
        self.held_buttons.clear()


class Harness:
    def __init__(self, **kwargs):
        self.clock, self.hid = Clock(), Hid()
        self.values = {"seq": 1, "vitals.dead": False, "vitals.ghost": False,
                       "ui.modal": False, "target.has": True, "target.name_id": 42,
                       "target.hp": 1.0, "cursor.has": True, "cursor.world": True,
                       "cursor.dead": False, "cursor.name_id": 42, "cursor.is_target": True,
                       "bars.ready": 4095, "bars.usable": 4095, "bars.gcd": 0.0,
                       "bars.casting": False}
        self.seq, self.fresh, self.age, self.reads = 1, True, 0.0, 0
        self.origin, self.size = (10, 20), (100, 80)
        self.on_read = lambda: None
        self.invalidations = []
        self.executor = Executor(self.hid, self.read, clock=self.clock.time,
                                 monotonic=self.clock.time, sleep=self.clock.sleep,
                                 invalidate_camera=self.invalidations.append, **kwargs)

    def read(self):
        self.reads += 1
        self.on_read()
        self.seq = (self.seq + self.fresh) % SEQ_MODULUS
        return GuardState({**self.values, "seq": self.seq}, self.clock.now - self.age,
                          self.origin, self.size, cursor=self.hid.position)

    def run(self, **action):
        return self.executor.execute(action)


def interact(**kwargs):
    return {"kind": "click", "button": "right", "intent": "interact",
            "expected_target_id": 42, "x": 0.5, "y": 0.5, **kwargs}


def test_move_and_turn_are_independent_of_click_and_always_release():
    h = Harness()
    result = h.run(kind="key", control="move_forward", duration_s=0.45)
    assert result.delivered and result.code == "delivered"
    assert ("hold", "w", 0.45) in h.hid.calls
    assert not any(call[0] == "click" for call in h.hid.calls)
    assert h.hid.releases == 1 and not h.hid.held
    result = h.run(kind="key", control="turn_right", duration_s=0.31)
    assert result.delivered and ("hold", "d", 0.31) in h.hid.calls


def test_observe_can_wait_unfocused_without_sending_a_play_action():
    h = Harness()
    h.hid.focused = False
    result = h.run(kind="observe", wait_s=0.4)
    assert result.code == "observed" and not result.delivered
    assert not h.hid.calls and h.clock.now == pytest.approx(100.4)
    assert h.hid.releases == 1


@pytest.mark.parametrize("mutation,code", [
    ({"ui.modal": True}, "interrupted"), ({"ui.modal": None}, "blind"),
    ({"vitals.dead": True}, "interrupted"), ({"vitals.ghost": None}, "blind"),
])
def test_unknown_or_blocked_state_never_sends_input(mutation, code):
    h = Harness()
    h.values.update(mutation)
    result = h.run(kind="key", control="move_forward", duration_s=0.2)
    assert result.code == code and not h.hid.calls
    assert h.hid.releases == 1


def test_observed_modal_can_be_dismissed_with_escape_only():
    h = Harness()
    h.values["ui.modal"] = True
    assert h.run(kind="key", control="escape").delivered
    assert ("tap", "esc") in h.hid.calls


@pytest.mark.parametrize("action", [
    {"kind": "key", "control": "target_next"},
    {"kind": "click", "button": "left", "intent": "ui", "ui_control": "quest_advance"},
    {"kind": "skill", "name": "ABORT_WAIT"},
])
def test_modal_contract_blocks_other_valid_actions_before_delivery(action):
    h = Harness()
    h.values["ui.modal"] = True
    result = h.run(**action)
    assert result.code == "interrupted" and not result.delivered and not h.hid.calls


def test_modal_contract_does_not_turn_unknown_state_into_escape_authority():
    h = Harness()
    h.values["ui.modal"] = None
    assert h.run(kind="key", control="escape").code == "blind"
    assert not h.hid.calls


def test_unfocused_and_stale_capture_refuse_input():
    h = Harness()
    h.hid.focused = False
    assert h.run(kind="key", control="jump").code == "refused"
    h.hid.focused = True
    h.age = 0.6
    assert h.run(kind="key", control="jump").code == "stale"
    assert not h.hid.calls


def test_changed_geometry_or_target_invalidates_policy_request():
    h = Harness()
    expected = h.read()
    h.origin = (20, 20)
    result = h.executor.execute({"kind": "key", "control": "jump"}, expected)
    assert result.code == "stale"
    h.origin = expected.origin
    h.values["target.name_id"] = 45
    result = h.executor.execute({"kind": "key", "control": "jump"}, expected)
    assert result.code == "stale" and not h.hid.calls


def test_partial_input_and_cancellation_never_leave_keys_held():
    h = Harness()
    def cancel():
        raise KeyboardInterrupt("test cancellation")
    h.hid.on_hold = cancel
    with pytest.raises(KeyboardInterrupt):
        h.run(kind="key", control="move_forward", duration_s=0.45)
    assert not h.hid.held and h.hid.releases == 1
    assert h.hid.checkpoint is None


def test_focus_loss_inside_action_releases_and_reports_refusal():
    h = Harness()
    h.hid.on_hold = lambda: setattr(h.hid, "focused", False)
    result = h.run(kind="key", control="move_forward", duration_s=0.45)
    assert result.code == "refused" and result.input_events > 0
    assert not h.hid.held


def test_configured_modifier_chord_is_executed_and_released():
    h = Harness()
    assert h.run(kind="key", control="target_previous").delivered
    assert h.hid.calls[:2] == [("down", "shift"), ("down", "tab")]
    assert not h.hid.held


def test_pointer_stays_within_actual_client_geometry():
    h = Harness()
    result = h.run(kind="pointer", x=1.0, y=1.0)
    assert result.delivered and result.point == (109, 99)


def test_world_click_needs_fresh_hover_and_delivers_button_only():
    h = Harness()
    result = h.run(**interact())
    assert result.delivered and result.point == (60, 60)
    assert h.reads >= 3
    assert h.hid.calls == [("move", 60, 60), ("click", True)]
    assert "unverified" in result.detail


@pytest.mark.parametrize("mutation", [{"cursor.world": False}, {"cursor.has": False},
                                      {"cursor.name_id": 43}, {"cursor.is_target": False},
                                      {"cursor.dead": None}, {"cursor.dead": True}])
def test_wrong_hover_cannot_authorize_button(mutation):
    h = Harness()
    h.values.update(mutation)
    result = h.run(**interact())
    assert result.code == "wrong_target"
    assert not any(call[0] == "click" for call in h.hid.calls)


def test_selected_same_name_is_insufficient_without_unit_equality():
    h = Harness()
    h.values["cursor.is_target"] = False
    assert h.run(**interact()).code == "wrong_target"
    # Selection can select a different unit whose requested name is observed.
    action = {**interact(), "button": "left", "intent": "select"}
    assert h.run(**action).delivered


def test_dead_pose_interaction_uses_observed_cursor_dead_not_model_geometry():
    h = Harness()
    h.values.update({"target.hp": 0.0, "cursor.dead": True})
    assert h.run(**interact(expected_dead=True)).delivered


def test_unchanged_paint_or_pointer_race_refuses_click():
    h = Harness()
    h.fresh = False
    assert h.run(**interact()).code == "stale"
    assert h.clock.now >= 101.0
    h = Harness()
    def move_pointer():
        if h.reads == 3:
            h.hid.position = (61, 60)
    h.on_read = move_pointer
    assert h.run(**interact()).code == "stale"
    assert not any(call[0] == "click" for call in h.hid.calls)


def test_sequence_wrap_is_fresh_but_backward_sequence_is_not():
    h = Harness()
    h.seq = 253
    assert h.run(**interact()).delivered
    h = Harness()
    h.fresh = -1
    assert h.run(**interact()).code == "stale"


def test_ui_click_comes_from_identity_and_fresh_painted_coordinates():
    h = Harness()
    h.values.update({"ui.gossip": True, "ui.list_x": 0.2, "ui.list_y2": 0.3,
                     "ui.list_hash2": 13, "cursor.world": False})
    result = h.run(kind="click", button="left", intent="ui", ui_control="gossip_line", ui_name_id=13)
    assert result.delivered and result.point == (30, 44)
    assert h.hid.calls == [("move", 30, 44), ("click", False)]


def test_ui_changed_identity_and_merchant_frame_do_not_authorize_click():
    h = Harness()
    h.values.update({"ui.quest_frame": True, "ui.advance_x": 0.4, "ui.advance_y": 0.2,
                     "cursor.world": False})
    def change():
        if h.reads == 3:
            h.values["ui.advance_y"] = 0.8
    h.on_read = change
    assert h.run(kind="click", button="left", intent="ui", ui_control="quest_advance").code == "stale"
    assert not any(call[0] == "click" for call in h.hid.calls)
    h = Harness()
    h.values.update({"ui.vendor": True, "ui.advance_x": 0.4, "ui.advance_y": 0.2})
    assert h.run(kind="click", button="left", intent="ui", ui_control="quest_advance").code == "interrupted"
    assert not h.hid.calls


def test_camera_is_single_axis_mouse_look_without_claimed_facing():
    h = Harness()
    result = h.run(kind="camera", axis="yaw", pixels=-40)
    assert result.delivered
    assert ("relative", -40, 0) in h.hid.calls
    assert h.hid.calls[-1] == ("button", False, True)
    assert not h.invalidations and not h.hid.held_buttons
    assert h.run(kind="camera", axis="pitch", pixels=10).delivered
    assert h.invalidations == ["explicit play pitch action"]


def test_camera_cannot_grab_ui_and_buttons_release_on_cancellation():
    h = Harness()
    h.values["cursor.world"] = False
    assert h.run(kind="camera", axis="yaw", pixels=10).code == "interrupted"
    assert not any(call[0] == "button" for call in h.hid.calls)
    h = Harness()
    def cancel(dx, dy):
        raise KeyboardInterrupt()
    h.hid.move_by = cancel
    with pytest.raises(KeyboardInterrupt):
        h.run(kind="camera", axis="yaw", pixels=10)
    assert not h.hid.held_buttons and h.hid.releases == 1


@pytest.mark.parametrize("mutation", [{"bars.ready": None}, {"bars.usable": 0},
                                      {"bars.casting": True}, {"bars.gcd": 0.1}])
def test_slot_only_when_client_positively_reports_ready(mutation):
    h = Harness()
    h.values.update(mutation)
    assert h.run(kind="action_slot", slot=2).code == "not_ready"
    assert not h.hid.calls


def test_repeated_toggle_does_not_turn_off_unverified_attack():
    h = Harness()
    assert h.run(kind="action_slot", slot=1).delivered
    assert h.run(kind="key", control="attack_target").code == "toggle_unverified"
    h.values["target.hp"] = 0.0
    h.run(kind="pointer", x=0.2, y=0.2)  # observed death closes the pending activation
    h.values["target.hp"] = 1.0
    assert h.run(kind="key", control="attack_target").delivered


def test_future_positive_active_mask_can_refuse_an_already_active_slot():
    h = Harness()
    h.values["bars.active"] = 1
    assert h.run(kind="action_slot", slot=1).code == "already_active"
    h.values["bars.active"] = 0
    assert h.run(kind="action_slot", slot=1).delivered


def test_trusted_skill_delegation_is_explicit_and_not_reported_as_input_success():
    calls = []
    h = Harness(manifest=build_manifest(skills=["CORPSE_RUN"]),
                execute_skill=lambda action: calls.append(action.name) or "done")
    h.values["vitals.dead"] = True
    result = h.run(kind="skill", name="CORPSE_RUN")
    assert result.code == "delegated" and not result.delivered
    assert calls == ["CORPSE_RUN"] and result.metadata["skill_result"] == "done"
    assert h.run(kind="skill", name="COMBAT_PROFILE").code == "unsupported"


def test_recursive_delegation_fails_and_releases():
    h = Harness(manifest=build_manifest(skills=["IDLE"]))
    h.executor.execute_skill = lambda action: h.run(kind="skill", name="IDLE")
    with pytest.raises(RuntimeError, match="recursive"):
        h.run(kind="skill", name="IDLE")
    assert h.hid.releases == 1


def test_tighter_runtime_limits_and_invalid_geometry_refuse():
    h = Harness(limits=Limits(max_hold_s=0.5, max_camera_pixels=20))
    assert h.run(kind="key", control="move_forward", duration_s=0.6).code == "unsupported"
    assert h.run(kind="camera", axis="yaw", pixels=21).code == "unsupported"
    h.size = (0, 80)
    assert h.run(kind="key", control="jump").code == "blind"
    assert not h.hid.calls


def test_guard_state_uses_owned_observation_capture_metadata():
    value = {"values": {"seq": 1}, "captured_at": 99.5, "origin": [5, 6],
             "size": [100, 80], "id": "abc", "cursor": [20, 30]}
    guard = GuardState.from_observation(value)
    assert guard.origin == (5, 6) and guard.cursor == (20, 30)
    assert guard.id == "abc" and guard.sequence == 1
    assert replace(guard, values={"seq": None}).sequence is None


def test_hover_uses_actual_radio_sequence_field():
    from jev.perceive.fields import FIELDS

    names = {field.name for field in FIELDS}
    h = Harness()
    assert set(h.values) <= names
    assert "seq" in names and "frame.seq" not in names
    assert h.run(**interact()).delivered


def test_interaction_cannot_be_followed_by_an_unverified_second_attack_toggle():
    h = Harness()
    assert h.run(**interact()).delivered
    assert h.run(kind="action_slot", slot=1).code == "toggle_unverified"
    h.executor.note_observation({"target.has": True, "target.hp": 0.0})
    assert h.run(kind="action_slot", slot=1).delivered


def test_release_failure_propagates_even_after_successful_delivery():
    h = Harness()
    def bad_release():
        h.hid.held.add("w")
    h.hid.release_all = bad_release
    with pytest.raises(RuntimeError, match="held keys/buttons"):
        h.run(kind="key", control="jump")
    assert h.hid.checkpoint is None


def test_delegated_result_preserves_structured_game_outcome():
    from jev.learn.episode import SkillOutcome
    from jev.run.supervisor import Result

    outcome = Result(SkillOutcome.ABORTED, "unreachable", "unreachable")
    h = Harness(manifest=build_manifest(skills=["TRAVEL_TO"]), execute_skill=lambda action: outcome)
    result = h.run(kind="skill", name="TRAVEL_TO")
    assert result.metadata["skill_result"] == {
        "outcome": SkillOutcome.ABORTED, "detail": "unreachable", "code": "unreachable"}
