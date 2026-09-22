"""Pointing the camera at the world, which nothing did."""

from __future__ import annotations

import pytest

from jev.clients.camera import GRAB_S, LEVEL_PX, SETTLE_S, TO_THE_STOP_PX, Camera


class _Hid:
    def __init__(self, moves_ok=True, drags=(True, True), grab_ok=True, release_ok=True):
        self.log = []
        self.moves_ok = moves_ok
        self.drags = iter(drags)
        self.grab_ok = grab_ok
        self.release_ok = release_ok

    def move_to(self, x, y, steps=0):
        self.log.append(("move_to", x, y))
        return self.moves_ok

    def button(self, down, right=False):
        self.log.append(("button", down, right))
        return self.grab_ok if down else self.release_ok

    def move_by(self, dx, dy, step_px=10):
        self.log.append(("move_by", dx, dy))
        result = next(self.drags)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_real_wait(monkeypatch):
    monkeypatch.setattr("jev.clients.camera.time.sleep", lambda _: None)


def _camera(hid=None):
    return Camera(hid=hid or _Hid(), window_origin=(10, 20), window_size=(1600, 900))


def test_levelling_drags_into_the_stop_before_it_measures_anything():
    """Mouse-look is relative, so there is no absolute pitch to set - except at the
    clamp. Going to the stop first is what makes the result the same from any start."""
    hid = _Hid()
    assert _camera(hid).level()

    drags = [e for e in hid.log if e[0] == "move_by"]
    assert drags == [("move_by", 0, TO_THE_STOP_PX), ("move_by", 0, -LEVEL_PX)]
    assert TO_THE_STOP_PX > LEVEL_PX, "the stop must be reachable from below level"


def test_the_drag_never_moves_sideways():
    """Sideways is yaw, and yaw turns the character. Levelling must not steer."""
    hid = _Hid()
    _camera(hid).level()
    assert all(dx == 0 for kind, dx, _dy in
               (e for e in hid.log if e[0] == "move_by"))


def test_the_button_is_held_across_the_drag_and_always_released():
    hid = _Hid()
    _camera(hid).level()
    buttons = [e for e in hid.log if e[0] == "button"]
    assert buttons == [("button", True, True), ("button", False, True)]

    order = [e[0] for e in hid.log]
    assert order.index("button") < order.index("move_by")
    assert order[-1] == "button", "the right button was left held down"


def test_a_cursor_that_will_not_move_is_not_a_camera_grab():
    """Grabbing mouse-look with the cursor somewhere unknown points it at whatever is
    there. If the move failed, nothing is pressed."""
    hid = _Hid(moves_ok=False)
    assert not _camera(hid).level()
    assert not [e for e in hid.log if e[0] == "button"]


def test_the_grab_happens_at_the_middle_of_the_window():
    hid = _Hid()
    _camera(hid).level()
    assert hid.log[0] == ("move_to", 10 + 800, 20 + 450)


@pytest.mark.parametrize(("drags", "expected_drags"), [
    ((False,), [("move_by", 0, TO_THE_STOP_PX)]),
    ((True, False), [("move_by", 0, TO_THE_STOP_PX), ("move_by", 0, -LEVEL_PX)]),
])
def test_a_partial_drag_is_failure_and_always_releases_the_grab(drags, expected_drags):
    hid = _Hid(drags=drags)
    assert _camera(hid).level() is False
    assert [e for e in hid.log if e[0] == "move_by"] == expected_drags
    assert hid.log[-1] == ("button", False, True)


def test_an_undelivered_release_is_not_successful_levelling():
    hid = _Hid(release_ok=False)
    assert _camera(hid).level() is False
    assert hid.log[-1] == ("button", False, True)


def test_a_refused_grab_does_not_start_a_drag():
    hid = _Hid(grab_ok=False)
    assert _camera(hid).level() is False
    assert not [e for e in hid.log if e[0] == "move_by"]


@pytest.mark.parametrize("error", [RuntimeError("movement failed"), KeyboardInterrupt()])
def test_an_interrupted_drag_still_releases_the_grab(error):
    hid = _Hid(drags=(error,))
    with pytest.raises(type(error)):
        _camera(hid).level()
    assert hid.log[-1] == ("button", False, True)


def test_success_keeps_the_measured_wait_sequence(monkeypatch):
    sleeps = []
    monkeypatch.setattr("jev.clients.camera.time.sleep", sleeps.append)
    assert _camera().level() is True
    assert sleeps == [GRAB_S, GRAB_S, GRAB_S, SETTLE_S]


def _calibration_inputs(origin=(10, 20), size=(1600, 900)):
    return [("move_to", origin[0] + size[0] // 2, origin[1] + size[1] // 2),
            ("button", True, True), ("move_by", 0, TO_THE_STOP_PX),
            ("move_by", 0, -LEVEL_PX), ("button", False, True)]


def test_ensure_level_runs_the_measured_sequence_once_then_sends_no_input():
    hid = _Hid()
    camera = _camera(hid)
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs()
    for _ in range(3):
        assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs()


def test_explicit_level_still_forces_a_full_calibration():
    hid = _Hid(drags=(True,) * 4)
    camera = _camera(hid)
    assert camera.ensure_level() is True
    assert camera.level() is True
    assert hid.log == _calibration_inputs() * 2
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs() * 2


def test_explicit_invalidation_requires_the_measured_sequence_again():
    hid = _Hid(drags=(True,) * 4)
    camera = _camera(hid)
    assert camera.ensure_level() is True
    camera.invalidate("focus ownership lost")
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs() * 2


@pytest.mark.parametrize("failure", ["move", "grab", "stop", "return", "release"])
def test_an_incomplete_calibration_is_never_reused(failure):
    hid = _Hid(moves_ok=failure != "move", grab_ok=failure != "grab",
               release_ok=failure != "release",
               drags=(failure != "stop", failure != "return"))
    camera = _camera(hid)
    assert camera.ensure_level() is False
    failed_inputs = list(hid.log)
    hid.moves_ok = hid.grab_ok = hid.release_ok = True
    hid.drags = iter((True, True))
    assert camera.ensure_level() is True
    assert hid.log == failed_inputs + _calibration_inputs()


def test_failed_forced_calibration_also_discards_the_previous_success():
    hid = _Hid(drags=(True,) * 4)
    camera = _camera(hid)
    assert camera.ensure_level() is True
    hid.moves_ok = False
    assert camera.level() is False
    hid.moves_ok = True
    assert camera.ensure_level() is True
    assert hid.log == [*_calibration_inputs(), ("move_to", 810, 470), *_calibration_inputs()]


@pytest.mark.parametrize("phase", ["stop", "return", "settle"])
def test_cancelled_calibration_never_becomes_reusable(phase, monkeypatch):
    from jev.run.supervisor import Cancelled

    error = Cancelled("stop requested")
    drags = (error,) if phase == "stop" else (True, error) if phase == "return" else (True, True)
    hid = _Hid(drags=drags)
    camera = _camera(hid)
    def sleep(seconds):
        if phase == "settle" and seconds == SETTLE_S:
            raise error
    monkeypatch.setattr("jev.clients.camera.time.sleep", sleep)
    with pytest.raises(Cancelled, match="stop requested"):
        camera.ensure_level()
    assert hid.log[-1] == ("button", False, True)
    failed_inputs = list(hid.log)
    monkeypatch.setattr("jev.clients.camera.time.sleep", lambda _: None)
    hid.drags = iter((True, True))
    assert camera.ensure_level() is True
    assert hid.log == failed_inputs + _calibration_inputs()


@pytest.mark.parametrize("origin, size", [((30, 40), (1600, 900)), ((10, 20), (1280, 720))])
def test_changed_window_geometry_requires_calibration_for_the_new_client_area(origin, size):
    hid = _Hid(drags=(True,) * 4)
    camera = _camera(hid)
    assert camera.ensure_level() is True
    camera.window_origin, camera.window_size = origin, size
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs() + _calibration_inputs(origin, size)


def test_invalidation_during_settle_cannot_be_overwritten_by_calibration_success(monkeypatch):
    hid = _Hid(drags=(True,) * 4)
    camera = _camera(hid)
    invalidated = False
    def sleep(seconds):
        nonlocal invalidated
        if seconds == SETTLE_S and not invalidated:
            invalidated = True
            camera.invalidate("focus lost while calibration settled")
    monkeypatch.setattr("jev.clients.camera.time.sleep", sleep)
    assert camera.ensure_level() is False
    assert hid.log == _calibration_inputs()
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs() * 2
    assert camera.ensure_level() is True
    assert hid.log == _calibration_inputs() * 2
