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
ACCOUNT_FIELD = (0.510, 0.489)
PASSWORD_FIELD = (0.510, 0.622)
LOGIN_BUTTON = (0.510, 0.698)
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

            if current is Stage.LOGIN and not self.typed_credentials:
                self._enter_credentials(account, password)
                self.typed_credentials = True
                time.sleep(4.0)
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

    def _enter_credentials(self, account: str, password: str) -> None:
        """Click the account field, clear it, type, tab, type, submit.

        Clicking first rather than trusting focus: a disconnect dialog takes focus when it
        appears, and typing an account name into a dismissed dialog's shadow puts it
        nowhere. Clearing first because the client remembers the last account.
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
        self.hid.type_text(account)
        time.sleep(0.2)
        self.hid.tap("tab")
        time.sleep(0.2)
        self.hid.type_text(password)
        time.sleep(0.2)
        self.hid.tap("enter")


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
