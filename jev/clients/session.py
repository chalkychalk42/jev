"""Getting from a login screen into the world.

The supervisor needs this (PLAN §11.2: *detect "still at login / stuck in queue"*), and so
does anyone resuming a run after a disconnect — which is how this came to be written, with
the client sitting on "Disconnected from server" and nobody at the keyboard.

It is deliberately not a screen classifier. The one state this can know for certain is
**in the world**, because that is exactly when `JevRadio` paints and the strip decodes;
the addon does not load before then, so there is no radio to ask at a login screen. So
the machine has two certain states and one honest "somewhere else", and the sequence that
drives it is self-verifying: it has worked when the strip appears, and nothing else counts.

Positions are fractions of the client area, not pixels. The login form is centred and
laid out proportionally, so a fraction survives a resolution change where a pixel does
not — and the whole point of this file is that it runs unattended.

Credentials never appear here. They come from the environment, and `.env` is gitignored,
because this repository is public.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from jev.clients.hid import Hid


class Stage(StrEnum):
    IN_WORLD = "in_world"      # certain: the strip decodes
    LOGIN = "login"            # a red plate where the Login button sits
    CHARACTER = "character"    # a red plate where Enter World sits
    ELSEWHERE = "elsewhere"    # none of the above, and nothing gets pressed


# Fractions of the client area, measured on a 1600x900 client at the TBC login screen.
#
# `ACCOUNT_FIELD` was 0.489 and that is the **label**, not the box. The input is at
# y 458-486 and the label sits 32 pixels above it, so every login clicked "Account Name",
# focused nothing, typed the account into no field, tabbed, typed the password into
# whatever Tab reached, and submitted. The fields in the kept frame are empty because
# nothing was ever entered into them.
#
# Re-measured off `tests/fixtures/login-empty.npz` by finding the dark input bars in the
# centre column: account 458-486, password 546-573.
#
# `LOGIN_BUTTON` stays at 0.698 and is *not* re-measured from those bars. The same sweep
# reports a dark bar at 637-644, which is the button's border rather than its plate: the
# plate is red, and red is not dark. Moving it there measured (79, 64, 53) instead of
# (91, 25, 9) and the login screen stopped being recognisable at all.
ACCOUNT_FIELD = (0.510, 0.524)
PASSWORD_FIELD = (0.510, 0.622)
LOGIN_BUTTON = (0.510, 0.698)

# A field with something in it. The glyphs are light on a dark plate, so a handful of
# bright pixels inside the box is the difference between "typed" and "typed at".
_TEXT_MIN = 140
_TEXT_PIXELS = 12

# Login tries before giving up. Two, because the honest failure modes here are "the click
# missed" and "the password is wrong", and neither improves with a third go. An idle
# disconnect during an unattended run is the case this exists for, and that one succeeds
# on the first.
MAX_ATTEMPTS = 2
# The "Okay" plate on the *Disconnected from server* dialog, which overlays the form.
OKAY_BUTTON = (0.510, 0.513)
# Character select. Measured on a live screen: (98, 23, 1) against (80, 73, 66) at the
# login button's position, so the two stages are told apart by *where* the red plate is.
ENTER_WORLD = (0.500, 0.913)

# The login and "Okay" buttons are the interface's red plates: red dominant, everything
# else low. Measured at (100, 39, 19) and (95, 16, 4).
_RED_MIN = 70
_RED_MARGIN = 45


def _is_red_button(frame: np.ndarray, at: tuple[float, float], radius: int = 60) -> bool:
    h, w, _ = frame.shape
    x, y = int(at[0] * w), int(at[1] * h)
    patch = frame[max(0, y - 10):y + 10, max(0, x - radius):x + radius].astype(np.int16)
    if patch.size == 0:
        return False
    r, g, b = patch[:, :, 0].mean(), patch[:, :, 1].mean(), patch[:, :, 2].mean()
    return r > _RED_MIN and (r - g) > _RED_MARGIN and (r - b) > _RED_MARGIN


def _has_text(frame: np.ndarray, at: tuple[float, float], radius: int = 70) -> bool:
    """Is there anything in this field? Confirmation, not assumption.

    `type_text` can report every keystroke sent and still leave a field empty, because
    sending a key is not the same as some widget having focus. That is exactly what
    happened, and nothing looked.
    """
    h, w, _ = frame.shape
    x, y = int(at[0] * w), int(at[1] * h)
    patch = frame[max(0, y - 9):y + 9, max(0, x - radius):x + radius]
    if patch.size == 0:
        return False
    return int((patch.max(axis=2) > _TEXT_MIN).sum()) >= _TEXT_PIXELS


def stage(frame: np.ndarray | None, radio_ok: bool) -> Stage:
    """Where the client is. `radio_ok` is whether the strip decoded this tick."""
    if radio_ok:
        return Stage.IN_WORLD
    if frame is None:
        return Stage.ELSEWHERE
    if _is_red_button(frame, ENTER_WORLD):
        return Stage.CHARACTER
    if _is_red_button(frame, LOGIN_BUTTON):
        return Stage.LOGIN
    return Stage.ELSEWHERE


@dataclass
class Session:
    """Drives a client from wherever it is into the world."""

    hid: Hid
    read_frame: Callable[[], np.ndarray | None]
    radio_ok: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)

    typed_credentials: bool = field(default=False, init=False)
    enters: int = field(default=0, init=False)
    attempts: int = field(default=0, init=False)
    unsendable: list[str] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)
    # The frame that stopped it, so the next screen can be measured rather than guessed.
    unknown_frame: object | None = field(default=None, init=False)
    dismissed_dialog: bool = field(default=False, init=False)
    entered_world: int = field(default=0, init=False)

    def stage(self) -> Stage:
        return stage(self.read_frame(), self.radio_ok())

    def _screen(self, at: tuple[float, float]) -> tuple[int, int]:
        ox, oy = self.window_origin
        w, h = self.window_size
        return (ox + int(at[0] * w), oy + int(at[1] * h))

    def sign_in(self, account: str, password: str, *, timeout_s: float = 180.0) -> bool:
        """Log in and enter the world. Returns whether the strip is painting at the end.

        The only success condition is the strip. Every other signal at a login screen is
        a guess about a UI this cannot see properly, and a login that "worked" without a
        character in the world is not a login.

        It drives **only stages it can recognise**. Reaching anything else stops the
        sequence and keeps the frame, because a blind keypress at an unreadable screen is
        how this closed the client the first time it ran.
        """
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            current = self.stage()
            if current is Stage.IN_WORLD:
                return True

            if current is Stage.LOGIN:
                if self.attempts >= MAX_ATTEMPTS:
                    self.detail = (
                        f"still at the login screen after {self.attempts} attempts"
                        + (f"; could not type {self.unsendable!r}" if self.unsendable
                           else "; the credentials were typed and not accepted"))
                    return False
                self.attempts += 1
                if not self._enter_credentials(account, password):
                    return False
                self.typed_credentials = True
                time.sleep(6.0)
                continue

            if current is Stage.CHARACTER:
                # Whoever is selected. Which character that is gets confirmed after the
                # fact, from the level and position the strip paints — asserting it from
                # the list here would mean measuring list rows nobody has looked at yet.
                self.hid.click(*self._screen(ENTER_WORLD))
                self.entered_world += 1
                time.sleep(6.0)
                continue

            # **Nothing is pressed at a screen this cannot read.**
            #
            # The first version pressed Escape and Enter here on the theory that realm
            # lists and character select both advance on Enter and the strip would say
            # whether it worked. It pressed them twenty-five times and closed the client:
            # the login screen has a Quit button, and blind Enter eventually finds it.
            #
            # A screen that has not been measured is not a screen this can drive. So it
            # stops, and `unknown_frame` holds the capture that would let it be measured.
            self.unknown_frame = self.read_frame()
            self.detail = ("reached a screen this cannot read; nothing was pressed. "
                           "Capture it, measure it, and add a stage")
            return False

        self.detail = (f"still not in the world after {timeout_s:.0f}s "
                       f"({self.enters} enters, credentials "
                       f"{'sent' if self.typed_credentials else 'not sent'})")
        return False

    def _enter_credentials(self, account: str, password: str) -> bool:
        """Click the account field, clear it, type, tab, type, submit.

        Clicking first rather than trusting focus: a disconnect dialog takes focus when it
        appears, and typing an account name into a dismissed dialog's shadow puts it
        nowhere. Clearing first because the client remembers the last account.

        **Every step is confirmed and nothing is submitted on a guess.** `type_text`
        returning False means a character could not be sent, and this used to throw that
        away and press Enter anyway. Worse, it pressed Enter on an *empty* field for days:
        the click landed on the "Account Name" label rather than the box below it, so the
        keystrokes went nowhere, and the only report was "reached a screen this cannot
        read" about a login screen it had just classified as a login screen.
        """
        # Dismiss the disconnect dialog **only if it is there**, by clicking its button.
        #
        # This used to press Escape unconditionally, on the reasoning that Escape closes
        # dialogs. At the login screen with no dialog, Escape opens the *quit* prompt
        # instead — and the Enter that submits the credentials a moment later confirms it.
        # That closed the client twice, the second time with zero blind Enters anywhere
        # else in the sequence, which is what finally identified it.
        #
        # The dialog is detectable: its plate is the same interface red as the Login
        # button, at a different height. Detect, then act. Never press at a screen this
        # cannot read.
        frame = self.read_frame()
        if frame is not None and _is_red_button(frame, OKAY_BUTTON):
            self.dismissed_dialog = True
            self.hid.click(*self._screen(OKAY_BUTTON))
            time.sleep(0.6)

        self.hid.click(*self._screen(ACCOUNT_FIELD))
        time.sleep(0.3)
        self.hid.chord("ctrl", "a")
        if not self.hid.type_text(account):
            self.unsendable = list(self.hid.unsendable)
            self.detail = (f"could not type the account name: {self.unsendable!r}"
                           if self.unsendable else self.hid.detail)
            return False
        time.sleep(0.4)

        # Look before tabbing. An empty box here means the click missed and the rest of
        # the sequence would type a password into whatever Tab happens to reach.
        frame = self.read_frame()
        if frame is not None and not _has_text(frame, ACCOUNT_FIELD):
            self.detail = ("clicked the account field and typed, and the field is still "
                           "empty; the click is not landing on the box")
            self.unknown_frame = frame
            return False

        self.hid.tap("tab")
        time.sleep(0.2)
        if not self.hid.type_text(password):
            self.unsendable = list(self.hid.unsendable)
            self.detail = "could not type the password"
            # Leave the form rather than submitting half a password.
            self.hid.chord("ctrl", "a")
            return False
        time.sleep(0.2)
        self.hid.tap("enter")
        self.enters += 1
        return True


def credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """Account and password from the environment. `None` when unset.

    Never a default, never a literal in this file. `.env` is gitignored and this
    repository is public.
    """
    src = env if env is not None else os.environ
    account, password = src.get("JEV_WOW_ACCOUNT"), src.get("JEV_WOW_PASSWORD")
    if not account or not password:
        return None
    return (account, password)
