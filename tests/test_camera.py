"""Pointing the camera at the world, which nothing did."""

from __future__ import annotations

from jev.clients.camera import LEVEL_PX, TO_THE_STOP_PX, Camera


class _Hid:
    def __init__(self, moves_ok=True):
        self.log = []
        self.moves_ok = moves_ok

    def move_to(self, x, y, steps=0):
        self.log.append(("move_to", x, y))
        return self.moves_ok

    def button(self, down, right=False):
        self.log.append(("button", down, right))
        return True

    def move_by(self, dx, dy, step_px=10):
        self.log.append(("move_by", dx, dy))
        return True


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
