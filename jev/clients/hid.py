"""Virtual keyboard and mouse. The only module that presses anything.

System 1 talks to this and nothing else talks to Win32 input, so there is exactly one
place where "the bot acted" is true — which is what makes the timing claims in PLAN §2.1
checkable rather than aspirational.

Randomise at the actuator, not in the prompt
--------------------------------------------
PLAN §2.1 is blunt about this: do not "look human" in the planning layer and then click on
a 16 ms grid. A server watching behaviour sees the *timing*, and timing is produced here.
So the jitter, the hold durations and the mouse curve all live in this file, where every
press goes through them, rather than being sprinkled over callers who will forget.

Every delay is drawn per event. A fixed "human-looking" 50 ms is a signature exactly as
clean as 0 ms — it is simply a different constant.
"""

from __future__ import annotations

import ctypes
import random
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.clients import win32
from jev.run.evidence import event as evidence_event
from jev.run.evidence import traced

# Virtual key codes for what a leveling bot actually presses.
VK = {
    "esc": 0x1B, "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "q": 0x51, "e": 0x45,
    "x": 0x58, "f": 0x46, "t": 0x54, "g": 0x47, "r": 0x52,
    **{str(d): 0x30 + d for d in range(10)},
    # Action slots 11 and 12. Default bindings are 1-9, 0, then these two, and that is
    # where a fresh character's food and water sit — so without them EAT_DRINK presses
    # nothing and reports success.
    "minus": 0xBD, "equals": 0xBB,
    # Punctuation, because a command is not a word. Without `/` in this table `type_text`
    # skipped it and `slash("/reload")` typed **reload** into say — the addon was never
    # reloaded and the character announced itself to Northshire instead. Every character
    # a slash command can contain belongs here.
    "/": 0xBF, ".": 0xBE, ",": 0xBC, ";": 0xBA, "'": 0xDE,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
    "-": 0xBD, "=": 0xBB,
    **{c: 0x41 + i for i, c in enumerate(string.ascii_lowercase)},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}

# Characters that are a shifted key rather than a key. Without these `type_text` refuses
# anything with a bracket in it, which is every `/script` there is - and a diagnostic you
# cannot type is a diagnostic nobody runs. Refusing was still the right behaviour: the
# alternative was `(` arriving as `9`.
SHIFTED = {
    "(": "9", ")": "0", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "_": "-", "+": "=", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/", "|": "\\", "~": "`", "{": "[", "}": "]",
}

# Keys that must carry the extended-key flag or the game reads a different key entirely.
EXTENDED = frozenset({"up", "down", "left", "right"})


@dataclass
class Humaniser:
    """Timing distributions, in one object so they can be swapped and measured.

    The defaults are the plan's numbers (PLAN §8.5): 30-90 ms between events, no
    zero-delay chords. They are a starting point to be replaced by distributions fitted
    from recorded play, not a claim that this is what a human looks like.
    """

    rng: random.Random = field(default_factory=random.Random)
    gap_ms: tuple[float, float] = (30.0, 90.0)
    hold_ms: tuple[float, float] = (45.0, 110.0)
    # Per-client offset so ten bots are not a chorus. A farm pressing the same key on the
    # same millisecond is a pattern no amount of per-event jitter hides.
    stagger_ms: float = 0.0

    def gap(self) -> float:
        return self.rng.uniform(*self.gap_ms) / 1000.0

    def hold(self) -> float:
        return self.rng.uniform(*self.hold_ms) / 1000.0

    def settle(self) -> float:
        """A longer pause, for after something that changes the UI."""
        return self.rng.uniform(120.0, 260.0) / 1000.0

    @staticmethod
    def for_client(client_id: str, seed: int | None = None) -> Humaniser:
        """Deterministic per client, so a replay reproduces its own timing."""
        h = abs(hash(client_id)) % 1000
        return Humaniser(rng=random.Random(seed if seed is not None else h),
                         stagger_ms=(h % 10) * 7.0)


class Hid:
    """Keyboard and mouse for one window.

    Refuses to act when the window is not focused, rather than typing into whatever is.
    A bot that presses `1` into the wrong window has not failed to attack — it has typed
    into somebody's chat, and the failure surfaces somewhere unrelated much later.
    """

    def __init__(self, hwnd: int | None = None, humaniser: Humaniser | None = None,
                 require_focus: bool = True) -> None:
        self.hwnd = hwnd
        self.h = humaniser or Humaniser()
        self.require_focus = require_focus
        self.sent = 0
        self.refused = 0
        self.unsendable: list[str] = []
        self.detail = ""
        self.held: set[str] = set()
        self.held_buttons: set[bool] = set()
        self.checkpoint: Callable[[], None] | None = None

    # -- guards --------------------------------------------------------------

    def ready(self) -> bool:
        if not win32.available():
            return False
        if self.hwnd is None or not self.require_focus:
            return True
        return win32.is_foreground(self.hwnd)

    def _guard(self) -> bool:
        if self.checkpoint is not None:
            self.checkpoint()
        if self.ready():
            return True
        self.refused += 1
        self.detail = "input unavailable or client not focused"
        evidence_event("input.refused", detail=self.detail,
                       data={"sent": self.sent, "refused": self.refused})
        return False

    def _send(self, event: win32.INPUT) -> bool:
        """Count only delivery accepted by Windows, for every input primitive."""
        accepted = win32.send_inputs([event])
        if accepted != 1:
            self.refused += 1
            self.detail = f"SendInput accepted {accepted}/1 events"
            evidence_event("input.refused", detail=self.detail,
                           data={"accepted": accepted, "requested": 1,
                                 "sent": self.sent, "refused": self.refused})
            return False
        self.sent += 1
        return True

    def _sleep(self, seconds: float, *, stagger: bool = True) -> None:
        """A drawn pause, offset once per gesture by this client's stagger.

        The stagger separates clients' timing (PLAN 8.5); it is not a per-step delay. Added
        to every ten-pixel step of a camera drag it turned 250 steps into twenty seconds of
        calibration (measured 21 s on 23 September), so steps inside one gesture skip it.
        """
        if stagger and self.h.stagger_ms:
            seconds += self.h.stagger_ms / 1000.0
        time.sleep(max(0.0, seconds))

    # -- keyboard ------------------------------------------------------------

    def _key_event(self, key: str, up: bool) -> win32.INPUT:
        vk = VK[key.lower()]
        flags = win32.KEYEVENTF_SCANCODE | (win32.KEYEVENTF_KEYUP if up else 0)
        if key.lower() in EXTENDED:
            flags |= win32.KEYEVENTF_EXTENDEDKEY
        ki = win32.KEYBDINPUT(wVk=0, wScan=win32.scan_code(vk), dwFlags=flags,
                              time=0, dwExtraInfo=None)
        return win32.INPUT(type=win32.INPUT_KEYBOARD, ki=ki)

    def key_down(self, key: str) -> bool:
        if not self._guard():
            return False
        ok = self._send(self._key_event(key, up=False))
        if ok:
            self.held.add(key)
        return ok

    def key_up(self, key: str) -> bool:
        # Releasing an input we own must survive cancellation and focus loss.
        if key not in self.held and not self.ready():
            return False
        ok = self._send(self._key_event(key, up=True))
        if ok:
            self.held.discard(key)
        return ok

    def keys_down(self) -> list[str]:
        """What is being held right now, for the recorder.

        A tick that says the character did not move is worth very little; a tick that says
        `w` was held and the character did not move is a wedge. ARCH SS4 asks for keys on
        every tick and nothing was supplying them.
        """
        return sorted(self.held)

    def tap(self, key: str) -> bool:
        """Press and release, with a drawn hold in between.

        Down and up are separate calls with a real gap rather than one batch: a key that
        goes down and up in the same batch arrives with identical timestamps, which no
        keyboard produces and every input filter can see.
        """
        if not self.key_down(key):
            return False
        try:
            self._sleep(self.h.hold())
        finally:
            ok = self.key_up(key)
        self._sleep(self.h.gap())
        return ok

    def hold(self, key: str, seconds: float,
             on_tick: Callable[[], None] | None = None,
             tick_s: float = 0.05) -> bool:
        """Hold a key down for a measured duration.

        Movement is a held key, not a tap, and the duration is the control signal — a
        turn is "press D for 0.31 s", so the accuracy of that hold is the accuracy of the
        heading. `on_tick` lets a caller sample position *while* the key is down, which is
        the only way to measure a speed without stopping first and measuring a different
        thing.

        The key is released in a `finally`. A held movement key that survives an exception
        is a character running into the sea for as long as it takes someone to notice.
        """
        if not self.key_down(key):
            return False
        try:
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline:
                if not self._guard():
                    return False
                if on_tick is not None:
                    on_tick()
                time.sleep(min(tick_s, max(0.0, deadline - time.perf_counter())))
        finally:
            released = self.key_up(key)
        self._sleep(self.h.gap())
        return released

    # Default 2.4.3 bindings, confirmed by measurement rather than memory: W/S move,
    # **A/D turn**, and **Q/E strafe**. Holding D for a second moved the character zero
    # yards while turning it, which is why an unstick built on "strafe with D" never
    # freed anything.
    MOVEMENT_KEYS = ("w", "a", "s", "d", "q", "e", "space", "shift")
    TURN_LEFT, TURN_RIGHT = "a", "d"
    STRAFE_LEFT, STRAFE_RIGHT = "q", "e"

    def release_all(self) -> None:
        """Attempt every release, then report any error or remaining owned input."""
        error = None
        for key in set(self.MOVEMENT_KEYS) | self.held:
            try:
                self.key_up(key)
            except BaseException as exc:
                # A device or evidence failure must not strand other owned inputs.
                # Surface the error only after every release has been attempted.
                error = error or exc
        for right in tuple(self.held_buttons):
            try:
                self.button(False, right=right)
            except BaseException as exc:
                error = error or exc
        if error is not None:
            raise error
        if self.held or self.held_buttons:
            raise RuntimeError("input release was refused; held inputs remain")

    def chord(self, modifier: str, key: str) -> bool:
        """Modifier plus key, with the modifier genuinely held around it."""
        if not self.key_down(modifier):
            return False
        try:
            self._sleep(self.h.gap())
            ok = self.tap(key)
            self._sleep(self.h.gap())
        finally:
            released = self.key_up(modifier)
        return released and ok

    def type_text(self, text: str) -> bool:
        """Type a line. Used by tooling; the bot itself does not talk.

        **A character this cannot type is a failure, not something to skip.** Skipping
        silently turned `/reload` into `reload`: the slash was dropped, the addon was
        never reloaded, and the run spent its time wondering why a schema bump had not
        taken effect while the character stood in Northshire saying "reload" out loud.
        Unsendable characters are recorded and the call returns False.

        **Caps Lock and stuck modifiers are checked first, and they are not paranoia.**
        `M` is sent as shift+m; with Caps Lock on the OS delivers `m`, and every letter
        comes out inverted. Nothing downstream can tell — the keystrokes all "succeeded" —
        so `/target Marshal McBride` reached the server as the public sentence
        `?target mARSHAL mCbRIDE`. The lock is machine state that outlives this process,
        so it is cleared rather than assumed, and a modifier still held is refused because
        this cannot know who is holding it.
        """
        ok = True
        self.unsendable = []
        if win32.available() and self.hwnd is not None:
            if not win32.clear_caps_lock():
                self.refused += 1
                self.detail = "Caps Lock is on and would not clear; every letter inverts"
                return False
            held = win32.modifiers_down()
            if held:
                self.refused += 1
                self.detail = f"{'+'.join(held)} held down; typing now would be shifted"
                return False
        for ch in text:
            key = ch.lower()
            if key == " ":
                key = "space"
            shifted = ch.isupper() or ch in SHIFTED
            key = SHIFTED.get(ch, key)
            if key not in VK:
                self.unsendable.append(ch)
                ok = False
                continue
            ok = (self.chord("shift", key) if shifted else self.tap(key)) and ok
        return ok

    def slash(self, command: str) -> bool:
        """Open chat, type a slash command, send it.

        Returns False if any character could not be typed, because a half-typed command
        is a chat message.

        Enter-first rather than typing `/` blind: with chat already open the `/` is a
        character in a message, and the difference is invisible until a run ends with the
        bot having said `/target Kobold` out loud in Goldshire.
        """
        if not self.tap("enter"):
            return False
        self._sleep(self.h.settle())
        if not self.type_text(command):
            # Discard rather than send. Enter here is what turns a mangled command into a
            # public sentence, and this return value was previously thrown away — which is
            # exactly how one got said out loud. Escape is safe because the box was opened
            # two lines ago: this is a measured screen, not a blind press.
            self.tap("esc")
            self._sleep(self.h.settle())
            return False
        self._sleep(self.h.gap())
        ok = self.tap("enter")
        self._sleep(self.h.settle())
        return ok

    # -- mouse ---------------------------------------------------------------

    def cursor_position(self) -> tuple[int, int] | None:
        """Actual desktop point, including absolute-input rounding or outside movement."""
        if not self.ready():
            return None
        point = win32.POINT()
        if not win32.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return point.x, point.y

    def _abs(self, x: int, y: int) -> tuple[int, int]:
        """Screen pixels to the 0..65535 absolute space `SendInput` wants."""
        sw = win32.user32.GetSystemMetrics(0)
        sh = win32.user32.GetSystemMetrics(1)
        return (int(x * 65535 / max(1, sw - 1)), int(y * 65535 / max(1, sh - 1)))

    def move_to(self, x: int, y: int, steps: int = 0) -> bool:
        """Move along a curve, not a straight line.

        A cursor that travels in a perfect line at a constant rate is the single easiest
        synthetic-input signature to spot. `bezier` supplies the path; this only walks it.
        """
        if not self._guard():
            return False
        start = win32.POINT()
        win32.user32.GetCursorPos(ctypes.byref(start))
        path = bezier((start.x, start.y), (x, y),
                      steps or self.h.rng.randint(8, 18), self.h.rng)
        for px, py in path:
            if not self._guard():
                return False
            ax, ay = self._abs(px, py)
            mi = win32.MOUSEINPUT(dx=ax, dy=ay, mouseData=0,
                                  dwFlags=win32.MOUSEEVENTF_MOVE | win32.MOUSEEVENTF_ABSOLUTE,
                                  time=0, dwExtraInfo=None)
            if not self._send(win32.INPUT(type=win32.INPUT_MOUSE, mi=mi)):
                return False
            self._sleep(self.h.rng.uniform(4.0, 14.0) / 1000.0, stagger=False)
        return True

    def button(self, down: bool, right: bool = False) -> bool:
        """Press or release a mouse button where the cursor already is.

        Separate from `click` because holding one across a movement is a different
        gesture: a right button held while the mouse moves is mouse-look, and the client
        reads it as camera control rather than as a click on whatever is underneath.
        """
        if down and not self._guard():
            return False
        if not down and right not in self.held_buttons and not self.ready():
            return False
        if right:
            flag = win32.MOUSEEVENTF_RIGHTDOWN if down else win32.MOUSEEVENTF_RIGHTUP
        else:
            flag = win32.MOUSEEVENTF_LEFTDOWN if down else win32.MOUSEEVENTF_LEFTUP
        mi = win32.MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag,
                              time=0, dwExtraInfo=None)
        ok = self._send(win32.INPUT(type=win32.INPUT_MOUSE, mi=mi))
        if ok:
            if down:
                self.held_buttons.add(right)
            else:
                self.held_buttons.discard(right)
        return ok

    def move_by(self, dx: int, dy: int, step_px: int = 10) -> bool:
        """Relative mouse movement, in small steps.

        Mouse-look is relative: the client reads deltas, and absolute positioning says
        nothing to it once the cursor is captured. Stepping rather than one large jump
        because the client samples movement per frame and a single huge delta is both
        clipped and obvious.
        """
        if not self._guard():
            return False
        sx = 0 if dx == 0 else (1 if dx > 0 else -1)
        sy = 0 if dy == 0 else (1 if dy > 0 else -1)
        left = max(abs(dx), abs(dy))
        while left > 0:
            if not self._guard():
                return False
            take = min(step_px, left)
            mi = win32.MOUSEINPUT(dx=sx * take, dy=sy * take, mouseData=0,
                                  dwFlags=win32.MOUSEEVENTF_MOVE, time=0,
                                  dwExtraInfo=None)
            if not self._send(win32.INPUT(type=win32.INPUT_MOUSE, mi=mi)):
                return False
            self._sleep(self.h.rng.uniform(8.0, 22.0) / 1000.0, stagger=False)
            left -= take
        return True

    @traced("input.click")
    def click(self, x: int | None = None, y: int | None = None, right: bool = False) -> bool:
        self.detail = ""
        evidence_event("input.click.request", data={"x": x, "y": y, "right": right,
                                                   "sent": self.sent, "refused": self.refused})
        if x is not None and y is not None and not self.move_to(x, y):
            return False
        if not self._guard():
            return False
        if not self.button(True, right=right):
            return False
        try:
            self._sleep(self.h.hold() / 2)
        finally:
            ok = self.button(False, right=right)
        self._sleep(self.h.gap())
        return ok


def bezier(start: tuple[int, int], end: tuple[int, int], steps: int,
           rng: random.Random) -> list[tuple[int, int]]:
    """A quadratic Bezier from start to end, with the control point thrown off the line.

    Pure and separately testable, which is the point of it not living inside `move_to`:
    the curve is the part that has to be right, and it needs no Windows to check.

    The control point is displaced perpendicular to the path by up to a quarter of its
    length, so longer moves bow more — which is what a hand does, and what a linear
    interpolation conspicuously does not.
    """
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1.0 or steps < 2:
        return [end]

    # Perpendicular unit vector, times a random signed fraction of the distance.
    px, py = -dy / length, dx / length
    bow = rng.uniform(-0.25, 0.25) * length
    cx = x0 + dx * 0.5 + px * bow
    cy = y0 + dy * 0.5 + py * bow

    out: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        bx = u * u * x0 + 2 * u * t * cx + t * t * x1
        by = u * u * y0 + 2 * u * t * cy + t * t * y1
        out.append((round(bx), round(by)))
    out[-1] = (x1, y1)      # always land exactly, however the curve rounded
    return out
