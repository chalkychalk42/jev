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
