"""Getting into the world, and refusing to claim it happened.

The one state this can know for certain is *in the world*, because that is exactly when
the addon paints. There is no radio at a login screen, so everything else is either a
structural read of the frame or an honest "somewhere else".
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from jev.clients.hid import Hid
from jev.clients.session import (
    ACCOUNT_FIELD,
    LOGIN_BUTTON,
    PASSWORD_FIELD,
    Session,
    Stage,
    credentials,
    stage,
)


def _frame(red_at: tuple[float, float] | None = None) -> np.ndarray:
    f = np.zeros((900, 1600, 3), dtype=np.uint8)
    if red_at is not None:
        x, y = int(red_at[0] * 1600), int(red_at[1] * 900)
        f[y - 12:y + 12, x - 70:x + 70] = (120, 20, 10)
    return f


def _session(frame=None, radio=False) -> Session:
    return Session(hid=Hid(hwnd=1), read_frame=lambda: frame,
                   radio_ok=lambda: radio, window_size=(1600, 900))


# --- what it can know -------------------------------------------------------

def test_the_strip_is_the_only_certain_signal():
    """A login that "worked" without a character in the world is not a login."""
    assert stage(_frame(), radio_ok=True) is Stage.IN_WORLD
    assert stage(None, radio_ok=True) is Stage.IN_WORLD, "the strip outranks the pixels"


def test_the_login_form_is_read_from_its_red_button():
    """Measured on a live TBC login screen: the Login plate is (100, 39, 19) and the
    Okay plate (95, 16, 4) — red dominant, everything else low."""
    assert stage(_frame(LOGIN_BUTTON), radio_ok=False) is Stage.LOGIN


def test_anything_else_is_admitted_to_be_unknown():
    """Realm lists and character select are not invented as states. They are advanced
    through, and the strip says whether it worked."""
    assert stage(_frame(), radio_ok=False) is Stage.ELSEWHERE
    assert stage(None, radio_ok=False) is Stage.ELSEWHERE


def test_a_dark_frame_is_not_a_login_screen():
    """The login screen is dark overall, so darkness cannot be the signal."""
    assert stage(np.zeros((900, 1600, 3), np.uint8), radio_ok=False) is Stage.ELSEWHERE


# --- credentials ------------------------------------------------------------

def test_credentials_come_from_the_environment_and_have_no_default():
    """This repository is public. A password must never be a literal in it."""
    assert credentials({}) is None
    assert credentials({"JEV_WOW_ACCOUNT": "a"}) is None
    assert credentials({"JEV_WOW_ACCOUNT": "a", "JEV_WOW_PASSWORD": "b"}) == ("a", "b")


def test_no_credential_is_hardcoded_in_the_module():
    import inspect

    from jev.clients import session

    src = inspect.getsource(session)
    assert "forever" not in src.lower()
    assert src.count("JEV_WOW_") == 2, "credentials should be read in exactly one place"


# --- the sequence -----------------------------------------------------------

def test_it_stops_the_moment_the_strip_appears():
    s = _session(frame=_frame(), radio=True)
    assert s.sign_in("acct", "pw", timeout_s=5.0) is True
    assert s.typed_credentials is False, "it typed a password it did not need to"
    assert s.enters == 0


def test_it_presses_nothing_at_a_screen_it_cannot_read():
    """The first version pressed Escape and Enter on the theory that realm lists and
    character select both advance on Enter, and the strip would say whether it worked. It
    pressed them twenty-five times and closed the client — the login screen has a Quit
    button, and blind Enter eventually finds it."""
    s = _session(frame=_frame(), radio=False)      # not the login form, not in world
    assert s.sign_in("acct", "pw", timeout_s=5.0) is False
    assert s.enters == 0, "it pressed keys at a screen it could not read"
    assert "cannot read" in s.detail
    assert s.unknown_frame is not None, "the frame that stopped it was not kept"


def test_the_form_positions_are_fractions_not_pixels():
    """The whole point of this file is running unattended, and a pixel offset does not
    survive a resolution change where a fraction does."""
    for at in (ACCOUNT_FIELD, PASSWORD_FIELD, LOGIN_BUTTON):
        assert all(0.0 < v < 1.0 for v in at)
    assert ACCOUNT_FIELD[1] < PASSWORD_FIELD[1] < LOGIN_BUTTON[1], "form order is wrong"


def test_it_clicks_the_account_field_rather_than_trusting_focus():
    """A disconnect dialog takes focus when it appears, and an account name typed into a
    dismissed dialog's shadow goes nowhere."""
    import inspect

    body = inspect.getsource(Session._enter_credentials)
    assert "click(" in body
    assert 'chord("ctrl", "a")' in body, "the client remembers the last account"


@pytest.mark.parametrize("field_name", ["typed_credentials", "enters", "detail"])
def test_it_reports_what_it_did(field_name):
    """A supervisor needs to tell 'stuck at login' from 'stuck in a queue'."""
    assert hasattr(_session(), field_name)


def test_the_disconnect_dialog_is_dismissed_by_clicking_it_not_by_escape():
    """Escape at the login screen with no dialog opens the *quit* prompt, and the Enter
    that submits the credentials a moment later confirms it. That closed the client twice.

    The dialog's plate is the same interface red as the Login button at a different
    height, so it can be detected — and nothing is pressed when it is absent.
    """
    import inspect

    from jev.clients.session import OKAY_BUTTON

    body = inspect.getsource(Session._enter_credentials)
    assert 'tap("esc")' not in body, "it presses Escape blind again"
    assert "OKAY_BUTTON" in body and "_is_red_button" in body
    assert OKAY_BUTTON[1] < LOGIN_BUTTON[1], "the dialog sits above the Login button"


def test_character_select_is_told_from_login_by_where_the_plate_is():
    """Both screens show an interface-red plate. They are distinguished by position, not
    colour: measured live, Enter World is (98, 23, 1) while the login button's position on
    that same frame is (80, 73, 66)."""
    from jev.clients.session import ENTER_WORLD

    assert stage(_frame(ENTER_WORLD), radio_ok=False) is Stage.CHARACTER
    assert stage(_frame(LOGIN_BUTTON), radio_ok=False) is Stage.LOGIN
    assert ENTER_WORLD[1] > LOGIN_BUTTON[1], "Enter World sits lower than Login"


def test_entering_the_world_is_a_click_not_a_keypress():
    """Enter at a screen this could not read is what closed the client twice."""
    import inspect

    body = inspect.getsource(Session.sign_in)
    assert "ENTER_WORLD" in body and "click(" in body
    assert 'tap("enter")' not in body


# --- the login that never typed anything ------------------------------------

LOGIN_EMPTY = pathlib.Path(__file__).parent / "fixtures" / "login-empty.npz"


def test_the_account_click_lands_on_the_box_and_not_its_label():
    """A real TBC login screen, kept from a run that reported success at typing and left
    both fields empty. `ACCOUNT_FIELD` was 0.489, which is the "Account Name" *label*:
    the input sits at y 458-486 and the label 32 pixels above it. Every login clicked the
    label, focused nothing, and typed into no field at all."""
    frame = np.load(LOGIN_EMPTY)["frame"]
    h, _w, _ = frame.shape

    def darkness(at):
        y = int(at[1] * h)
        patch = frame[y - 8:y + 8, 700:900].astype(np.int16)
        return patch.max(axis=2).mean()

    assert darkness(ACCOUNT_FIELD) < 90, "the account coordinate is not on a dark box"
    assert darkness(PASSWORD_FIELD) < 90
    assert darkness((0.510, 0.489)) > 90, "0.489 was the label, and it is still the label"


def test_an_empty_field_after_typing_is_not_submitted():
    """`type_text` can report every keystroke sent and still leave a field empty, because
    sending a key is not the same as a widget having focus. Nothing looked, so the form
    was submitted empty and the failure was reported as an unreadable screen."""
    frame = np.load(LOGIN_EMPTY)["frame"]
    hid = _CountingHid()
    s = Session(hid=hid, read_frame=lambda: frame, radio_ok=lambda: False,
                window_size=(1600, 900))
    assert s._enter_credentials("ash", "secret") is False
    assert "still empty" in s.detail
    assert hid.taps.count("enter") == 0, "submitted a form it could not fill"


def test_a_character_that_cannot_be_typed_stops_before_the_password():
    hid = _CountingHid(unsendable=["é"])
    s = Session(hid=hid, read_frame=lambda: None, radio_ok=lambda: False)
    assert s._enter_credentials("café", "secret") is False
    assert hid.taps.count("enter") == 0
    assert hid.typed == ["café"], "typed the password after failing the account"


def test_a_login_screen_it_recognises_is_never_reported_as_unreadable():
    """The old loop guarded on `not typed_credentials`, so the second look at a login
    screen fell through to "reached a screen this cannot read" - about the one screen it
    had just classified. That sends you looking for a stage that does not exist."""
    frame = np.load(LOGIN_EMPTY)["frame"]
    hid = _CountingHid()
    s = Session(hid=hid, read_frame=lambda: frame, radio_ok=lambda: False,
                window_size=(1600, 900))
    assert s.sign_in("ash", "secret", timeout_s=2.0) is False
    assert "cannot read" not in s.detail, s.detail
    assert "login screen" in s.detail or "still empty" in s.detail, s.detail


class _CountingHid(Hid):
    def __init__(self, unsendable: list[str] | None = None):
        super().__init__(hwnd=1)
        self.taps: list[str] = []
        self.typed: list[str] = []
        self._cannot = set(unsendable or ())

    def click(self, x=None, y=None, right=False):
        return True

    def chord(self, modifier, key):
        return True

    def tap(self, key):
        self.taps.append(key)
        return True

    def type_text(self, text):
        self.typed.append(text)
        self.unsendable = [c for c in text if c in self._cannot]
        return not self.unsendable
