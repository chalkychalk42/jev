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
from pathlib import Path

import numpy as np

from jev.clients.hid import Hid


class Stage(StrEnum):
    IN_WORLD = "in_world"      # certain: the strip decodes
    LOGIN = "login"            # a red plate where the Login button sits
    CHARACTER = "character"    # a red plate where Enter World sits
    CHARACTER_WAIT = "character_wait"  # Enter World greyed: the character list is loading
    REALM_ASSIGNED = "realm_assigned"  # red plates at Accept and View Realm List
    REALM_LIST = "realm_list"          # red plates at the list's Okay and Cancel
    REALM_WIZARD = "realm_wizard"      # a red plate at the wizard's Cancel, and its Suggest
    CREATE = "create"                  # red plates at the create screen's Accept and Back
    CREATE_REFUSED = "create_refused"  # the same, under a dialog: the name was refused
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

# The realm screens a restarted client opened on instead of character select, measured on
# 24 September after an addon install: the first-time realm wizard (tick the one location,
# "Development", then Suggest Realm), its "You have been assigned to the ... realm" popup
# (Accept), and the realm list (a double click on the selected row enters it; Okay
# brought the list straight back). Every button is the same interface red.
REALM_WIZARD_CANCEL = (0.911, 0.945)
REALM_LOCATION = (0.8025, 0.3322)
REALM_SUGGEST = (0.882, 0.612)
REALM_ACCEPT = (0.421, 0.528)
REALM_VIEW_LIST = (0.573, 0.528)
REALM_LIST_OKAY = (0.586, 0.791)
REALM_LIST_CANCEL = (0.672, 0.791)
REALM_ROW = (0.3375, 0.25)
# The Suggest Realm plate before a location is ticked: disabled grey, measured (39, 39, 40).
_GREY_MAX_SPREAD = 12
# Clicks through the realm screens before this gives up and keeps the frame.
MAX_REALM_STEPS = 4
# Character select while its list loads: Enter World a disabled grey plate, measured
# (29, 28, 27), under a "Retrieving character list" dialog on a red sunset sky that the
# login button's red test took for the login form - so credentials were typed at it, and
# the Enter that submitted them cancelled the retrieval. Looked at again, never typed at.
CHARACTER_WAIT_S = 2.0
MAX_CHARACTER_WAITS = 15

# Character select and the create screen, for the characters a campaign makes
# (`tools/character.py`). Each stays `None` until it is measured off a live screen, and a
# `None` refuses to act: nothing is pressed at a screen that has not been measured.
# Character select: the Create New Character plate, the list's first row and the distance
# between rows; the rows are in the order the server lists the characters, oldest first.
CREATE_NEW: tuple[float, float] | None = None
CHARACTER_ROW_FIRST: tuple[float, float] | None = None
CHARACTER_ROW_STEP: float | None = None
# The create screen: its Accept and Back plates tell it apart; each race's and class's
# button; the name box; and the Okay of the dialog that refuses a name.
CREATE_ACCEPT: tuple[float, float] | None = None
CREATE_BACK: tuple[float, float] | None = None
RACE_BUTTONS: dict[str, tuple[float, float]] = {}
CLASS_BUTTONS: dict[str, tuple[float, float]] = {}
NAME_BOX: tuple[float, float] | None = None
CREATE_REFUSED_OKAY: tuple[float, float] | None = None
# A new character's first entry plays its race's introduction: waited out, pressing
# nothing, until the strip paints.
FIRST_ENTRY_S = 240.0

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


def _is_grey_plate(frame: np.ndarray, at: tuple[float, float], radius: int = 60) -> bool:
    h, w, _ = frame.shape
    x, y = int(at[0] * w), int(at[1] * h)
    patch = frame[max(0, y - 10):y + 10, max(0, x - radius):x + radius].astype(np.int16)
    if patch.size == 0:
        return False
    means = patch.reshape(-1, 3).mean(axis=0)
    return means.min() > 20 and means.max() < 80 and means.max() - means.min() < _GREY_MAX_SPREAD


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
    if (CREATE_ACCEPT is not None and CREATE_BACK is not None
            and _is_red_button(frame, CREATE_ACCEPT) and _is_red_button(frame, CREATE_BACK)):
        if CREATE_REFUSED_OKAY is not None and _is_red_button(frame, CREATE_REFUSED_OKAY):
            return Stage.CREATE_REFUSED
        return Stage.CREATE
    if _is_red_button(frame, ENTER_WORLD):
        return Stage.CHARACTER
    if _is_grey_plate(frame, ENTER_WORLD):
        return Stage.CHARACTER_WAIT
    if _is_red_button(frame, LOGIN_BUTTON):
        return Stage.LOGIN
    if _is_red_button(frame, REALM_ACCEPT) and _is_red_button(frame, REALM_VIEW_LIST):
        return Stage.REALM_ASSIGNED
    if _is_red_button(frame, REALM_LIST_OKAY) and _is_red_button(frame, REALM_LIST_CANCEL):
        return Stage.REALM_LIST
    if _is_red_button(frame, REALM_WIZARD_CANCEL) and (
            _is_red_button(frame, REALM_SUGGEST) or _is_grey_plate(frame, REALM_SUGGEST)):
        return Stage.REALM_WIZARD
    return Stage.ELSEWHERE


@dataclass
class Session:
    """Drives a client from wherever it is into the world."""

    hid: Hid
    read_frame: Callable[[], np.ndarray | None]
    radio_ok: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    checkpoint: Callable[[], None] | None = None

    typed_credentials: bool = field(default=False, init=False)
    enters: int = field(default=0, init=False)
    attempts: int = field(default=0, init=False)
    unsendable: list[str] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)
    # The frame that stopped it, so the next screen can be measured rather than guessed.
    unknown_frame: object | None = field(default=None, init=False)
    dismissed_dialog: bool = field(default=False, init=False)
    entered_world: int = field(default=0, init=False)
    realm_steps: int = field(default=0, init=False)
    character_waits: int = field(default=0, init=False)
    # The server refused the last name `create_character` tried: taken, or not allowed.
    refused: bool = field(default=False, init=False)

    def stage(self) -> Stage:
        if self.checkpoint:
            self.checkpoint()
        return stage(self.read_frame(), self.radio_ok())

    def _wait(self, seconds: float) -> None:
        if self.checkpoint is None:
            time.sleep(seconds)
            return
        while seconds > 0:
            self.checkpoint()
            interval = min(0.1, seconds)
            time.sleep(interval)
            seconds -= interval
        self.checkpoint()

    def _screen(self, at: tuple[float, float]) -> tuple[int, int]:
        ox, oy = self.window_origin
        w, h = self.window_size
        return (ox + int(at[0] * w), oy + int(at[1] * h))

    def sign_in(self, account: str, password: str, *, timeout_s: float = 180.0,
                stop_at_select: bool = False) -> bool:
        """Log in and enter the world. Returns whether the strip is painting at the end.

        With `stop_at_select` it stops at character select instead, for a campaign to pick
        or make its character (`select_row`, `create_character`), and returns whether it is
        there; a client already in the world is not at character select.

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
                if stop_at_select:
                    self.detail = "already in the world, not at character select"
                    return False
                return True
            if stop_at_select and current is Stage.CHARACTER:
                return True
            if stop_at_select and current in (Stage.CREATE, Stage.CREATE_REFUSED):
                self._leave_create(current)
                continue

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
                self._wait(6.0)
                continue

            if current is Stage.CHARACTER:
                # Whoever is selected. Which character that is gets confirmed after the
                # fact, from the level and position the strip paints — asserting it from
                # the list here would mean measuring list rows nobody has looked at yet.
                self.hid.click(*self._screen(ENTER_WORLD))
                self.entered_world += 1
                self._wait(6.0)
                continue

            if current is Stage.CHARACTER_WAIT:
                if self.character_waits >= MAX_CHARACTER_WAITS:
                    self.unknown_frame = self.read_frame()
                    self.detail = (f"the character list did not load in "
                                   f"{MAX_CHARACTER_WAITS * CHARACTER_WAIT_S:.0f}s")
                    return False
                self.character_waits += 1
                self._wait(CHARACTER_WAIT_S)
                continue

            if current in (Stage.REALM_WIZARD, Stage.REALM_ASSIGNED, Stage.REALM_LIST):
                if self.realm_steps >= MAX_REALM_STEPS:
                    self.unknown_frame = self.read_frame()
                    self.detail = f"still choosing a realm after {self.realm_steps} steps"
                    return False
                self.realm_steps += 1
                self._choose_realm(current)
                self._wait(3.0)
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

    def _leave_create(self, current: Stage) -> None:
        """Back from the create screen to character select, by its measured plates."""
        if current is Stage.CREATE_REFUSED:
            self.hid.click(*self._screen(CREATE_REFUSED_OKAY))
            self._wait(1.0)
        self.hid.click(*self._screen(CREATE_BACK))
        self._wait(3.0)

    def select_row(self, row: int) -> bool:
        """At character select, pick the character in list row `row` (0 is the first)."""
        if CHARACTER_ROW_FIRST is None or CHARACTER_ROW_STEP is None:
            self.detail = "character select's rows are not measured"
            return False
        if self.stage() is not Stage.CHARACTER:
            self.unknown_frame = self.read_frame()
            self.detail = "not at character select"
            return False
        x, y = CHARACTER_ROW_FIRST
        self.hid.click(*self._screen((x, y + row * CHARACTER_ROW_STEP)))
        self._wait(1.5)
        return True

    def create_character(self, name: str, race: str, cls: str) -> bool:
        """At character select, make a character. True when the list is back, with the new
        character selected. A refused name is `refused`, and the list is back without it.

        Each press is at a measured plate, and the name is confirmed in its box before
        Accept, as the login confirms the account before the password."""
        self.refused = False
        if (CREATE_NEW is None or NAME_BOX is None or CREATE_ACCEPT is None
                or CREATE_BACK is None or race not in RACE_BUTTONS or cls not in CLASS_BUTTONS):
            self.detail = f"the create screen is not measured for a {race} {cls}"
            return False
        if self.stage() is not Stage.CHARACTER:
            self.unknown_frame = self.read_frame()
            self.detail = "not at character select"
            return False
        self.hid.click(*self._screen(CREATE_NEW))
        self._wait(3.0)
        if self.stage() is not Stage.CREATE:
            self.unknown_frame = self.read_frame()
            self.detail = "Create New Character did not open the create screen"
            return False
        self.hid.click(*self._screen(RACE_BUTTONS[race]))
        self._wait(1.0)
        self.hid.click(*self._screen(CLASS_BUTTONS[cls]))
        self._wait(1.0)
        self.hid.click(*self._screen(NAME_BOX))
        self._wait(0.3)
        self.hid.chord("ctrl", "a")
        if not self.hid.type_text(name):
            self.detail = f"could not type the name: {list(self.hid.unsendable)!r}"
            return False
        self._wait(0.4)
        frame = self.read_frame()
        if frame is None or not _has_text(frame, NAME_BOX):
            self.unknown_frame = frame
            self.detail = "typed the name and the box is still empty; nothing was accepted"
            return False
        self.hid.click(*self._screen(CREATE_ACCEPT))
        self._wait(4.0)
        current = self.stage()
        if current is Stage.CHARACTER:
            return True
        if current is Stage.CREATE_REFUSED:
            self.refused = True
            self.detail = f"the server refused the name {name!r}"
            self._leave_create(current)
            return False
        self.unknown_frame = self.read_frame()
        self.detail = f"after Accept the client is at {current.value}"
        return False

    def enter_world(self, *, timeout_s: float = FIRST_ENTRY_S) -> bool:
        """Enter World with whoever is selected, then wait for the strip, pressing nothing:
        a new character's first entry plays its race's introduction first."""
        if self.stage() is not Stage.CHARACTER:
            self.unknown_frame = self.read_frame()
            self.detail = "not at character select"
            return False
        self.hid.click(*self._screen(ENTER_WORLD))
        self.entered_world += 1
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            self._wait(2.0)
            if self.radio_ok():
                return True
        self.detail = f"the strip did not paint within {timeout_s:.0f}s of Enter World"
        return False

    def _choose_realm(self, current: Stage) -> None:
        """One step through the realm screens, by clicks on measured plates only."""
        if current is Stage.REALM_WIZARD:
            self.hid.click(*self._screen(REALM_LOCATION))
            self._wait(0.6)
            self.hid.click(*self._screen(REALM_SUGGEST))
        elif current is Stage.REALM_ASSIGNED:
            self.hid.click(*self._screen(REALM_ACCEPT))
        else:
            self.hid.click(*self._screen(REALM_ROW))
            self._wait(0.12)
            self.hid.click(*self._screen(REALM_ROW))

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
            self._wait(0.6)

        self.hid.click(*self._screen(ACCOUNT_FIELD))
        self._wait(0.3)
        self.hid.chord("ctrl", "a")
        if not self.hid.type_text(account):
            self.unsendable = list(self.hid.unsendable)
            self.detail = (f"could not type the account name: {self.unsendable!r}"
                           if self.unsendable else self.hid.detail)
            return False
        self._wait(0.4)

        # Look before tabbing. An empty box here means the click missed and the rest of
        # the sequence would type a password into whatever Tab happens to reach.
        frame = self.read_frame()
        if frame is None:
            self.detail = "account field confirmation unreadable; password was not sent"
            return False
        if not _has_text(frame, ACCOUNT_FIELD):
            self.detail = ("clicked the account field and typed, and the field is still "
                           "empty; the click is not landing on the box")
            self.unknown_frame = frame
            return False

        self.hid.tap("tab")
        self._wait(0.2)
        if not self.hid.type_text(password):
            self.unsendable = list(self.hid.unsendable)
            self.detail = "could not type the password"
            # Leave the form rather than submitting half a password.
            self.hid.chord("ctrl", "a")
            return False
        self._wait(0.2)
        self.hid.tap("enter")
        self.enters += 1
        return True


def credentials(env: dict[str, str] | None = None, *, path: Path | None = None) -> tuple[str, str] | None:
    """Account and password from the environment. `None` when unset.

    Never a default, never a literal in this file. `.env` is gitignored and this
    repository is public.
    """
    src: dict[str, str] = {}
    if path is not None and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            src[key.strip().removeprefix("export ")] = value
    src.update(env if env is not None else os.environ)
    account, password = src.get("JEV_WOW_ACCOUNT"), src.get("JEV_WOW_PASSWORD")
    if not account or not password:
        return None
    return (account, password)
