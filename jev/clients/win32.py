"""Win32 bindings, in one place, through `ctypes`.

No new dependency. `SendInput` and `BitBlt` are two calls each and a handful of structs;
a package to wrap them would be a package to install on every machine that ever runs a
client, and a layer between us and the documentation when something behaves oddly.

Importing this module is safe on any platform. Nothing is resolved until `available()`
says so, so the rest of the codebase — and the whole test suite — imports it on Linux
without pretending a window exists.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"


def available() -> bool:
    """Can this process actually talk to a window?"""
    return IS_WINDOWS


class Unavailable(RuntimeError):
    """Raised when a Win32 call is attempted off Windows.

    A distinct type rather than a bare `RuntimeError`: "there is no window here" is a
    fact about the machine, not a failure of the thing that asked, and callers that run
    in both places need to tell them apart.
    """


if IS_WINDOWS:                                          # pragma: no cover - platform
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    user32 = gdi32 = kernel32 = None


# --------------------------------------------------------------------------- input

# `SendInput` structures. The union matters: an INPUT is a tagged union and getting the
# size wrong makes the call fail silently with zero events sent, which looks exactly like
# a window that ignored the key.

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

MAPVK_VK_TO_VSC = 0


# --------------------------------------------------------------------------- windows

SW_RESTORE = 9
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
TH32CS_SNAPPROCESS = 0x00000002
GAME_WINDOW_CLASS = "GxWindowClassD3d"
GAME_EXECUTABLE = "wow.exe"


class GameWindowError(RuntimeError):
    """No uniquely identified game window can be selected."""


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


if IS_WINDOWS:                                          # pragma: no cover - platform
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _require() -> None:
    if not IS_WINDOWS:
        raise Unavailable("this call needs Windows; there is no window on this platform")


# --------------------------------------------------------------------------- calls


def find_windows(title_contains: str = "", class_name: str | None = None) -> list[int]:
    """Every visible top-level window whose title contains `title_contains`.

    Returns a list, never one guess. Several WoW windows is the *expected* case for a
    farm, and a function that silently picked the first would bind every client to the
    same one.
    """
    _require()                                          # pragma: no cover - platform
    found: list[int] = []                               # pragma: no cover - platform

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):                             # pragma: no cover - platform
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if class_name is not None:
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value != class_name:
                return True
        if title_contains.lower() in title.lower():
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)                          # pragma: no cover - platform
    return found                                        # pragma: no cover - platform


def window_title(hwnd: int) -> str:
    _require()                                          # pragma: no cover - platform
    buf = ctypes.create_unicode_buffer(512)             # pragma: no cover - platform
    user32.GetWindowTextW(hwnd, buf, 512)               # pragma: no cover - platform
    return buf.value                                    # pragma: no cover - platform


def window_process_id(hwnd: int) -> int | None:
    _require()
    pid = wintypes.DWORD()
    thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value if thread and pid.value else None


def _process_names() -> dict[int, str]:
    """Read PID/executable names, without opening a process or reading its memory.

    The measured game rejects OpenProcess with access denied even for a limited image
    query. The read-only process snapshot supplies the required basename directly.
    """
    _require()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == ctypes.c_void_p(-1).value:
        return {}
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        names = {}
        present = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            names[entry.th32ProcessID] = entry.szExeFile
            present = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return names
    finally:
        kernel32.CloseHandle(snapshot)


def find_game(title: str = "World of Warcraft") -> list[int]:
    """Visible native game windows with measured class and executable identity.

    A title substring only narrows this identified set. A browser showing a page about
    the game must never become an input destination because it has a matching title.
    Unknown process identity is refused; there is no title-only fallback.
    """
    candidates = find_windows(title_contains=title, class_name=GAME_WINDOW_CLASS)
    if not candidates:
        return []
    processes = _process_names()
    return [hwnd for hwnd in candidates
            if processes.get(window_process_id(hwnd), "").casefold() == GAME_EXECUTABLE]


def game_window(title: str = "World of Warcraft", *, index: int | None = None) -> int:
    """Select one identified game, rejecting ambiguity unless an index is explicit."""
    if index is not None and (type(index) is not int or index < 0):
        raise GameWindowError("game window index must be a nonnegative integer")
    candidates = find_game(title)
    if not candidates:
        raise GameWindowError("no visible game window with verified class and executable")
    if index is None:
        if len(candidates) != 1:
            raise GameWindowError(f"{len(candidates)} verified game windows; select one explicitly")
        return candidates[0]
    if index >= len(candidates):
        raise GameWindowError(f"asked for game window {index}, found {len(candidates)}")
    return candidates[index]


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window's *client* area in screen coordinates: `(x, y, w, h)`.

    Client, not window: the border and title bar are not part of the game and their size
    changes with the Windows theme, so a region measured against the outer frame moves
    between machines for reasons nothing in this codebase can see.
    """
    _require()                                          # pragma: no cover - platform
    rect = RECT()                                       # pragma: no cover - platform
    user32.GetClientRect(hwnd, ctypes.byref(rect))      # pragma: no cover - platform
    origin = POINT(0, 0)                                # pragma: no cover - platform
    user32.ClientToScreen(hwnd, ctypes.byref(origin))   # pragma: no cover - platform
    return (origin.x, origin.y,                         # pragma: no cover - platform
            rect.right - rect.left, rect.bottom - rect.top)


def is_foreground(hwnd: int) -> bool:
    _require()                                          # pragma: no cover - platform
    return user32.GetForegroundWindow() == hwnd         # pragma: no cover - platform


VK_CAPITAL = 0x14
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12


def caps_lock_on() -> bool:
    """Is Caps Lock toggled on right now."""
    _require()                                          # pragma: no cover - platform
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)     # pragma: no cover - platform


def modifiers_down() -> tuple[str, ...]:
    """Which of shift/ctrl/alt are physically held, if any.

    A modifier left down is not hypothetical: `chord` presses shift, types, releases it,
    and anything that returns early in between leaves it stuck for every later keystroke.
    """
    _require()                                          # pragma: no cover - platform
    names = (("shift", VK_SHIFT), ("ctrl", VK_CONTROL), ("alt", VK_MENU))  # pragma: no cover
    return tuple(n for n, vk in names                   # pragma: no cover - platform
                 if user32.GetKeyState(vk) & 0x8000)


def clear_caps_lock() -> bool:
    """Turn Caps Lock off, and say whether it is off afterwards.

    Caps Lock is machine state, not window state: it survives the client, this process and
    the operator walking away, and nothing in the bot could see it. With it on, `type_text`
    sends shift+m for `M` and the OS hands the game `m` — every letter inverted — which is
    how `/target Marshal McBride` reached the server as the public sentence
    `?target mARSHAL mCbRIDE`.
    """
    _require()                                          # pragma: no cover - platform
    for _ in range(3):                                  # pragma: no cover - platform
        if not caps_lock_on():
            return True
        down = KEYBDINPUT(wVk=VK_CAPITAL, wScan=scan_code(VK_CAPITAL),
                          dwFlags=0, time=0, dwExtraInfo=None)
        up = KEYBDINPUT(wVk=VK_CAPITAL, wScan=scan_code(VK_CAPITAL),
                        dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
        send_inputs([INPUT(type=INPUT_KEYBOARD, ki=down),
                     INPUT(type=INPUT_KEYBOARD, ki=up)])
        time.sleep(0.08)
    return not caps_lock_on()                           # pragma: no cover - platform


def focus(hwnd: int) -> bool:
    """Bring a window to the foreground, and say whether it worked.

    `SetForegroundWindow` alone fails from a process that does not own the foreground —
    measured, not assumed: called from a console process it returned False and the window
    stayed where it was, while the input guard correctly refused to type into whatever
    *was* focused.

    The documented way round that is `AttachThreadInput`: attach to the thread that
    currently owns the foreground, which makes this process part of the same input queue
    and so permitted to raise a window, then detach. It is the mechanism the shell itself
    uses, not a trick — but it still fails in some states, so the result is returned and
    checked rather than assumed.

    This is for a supervisor restoring a window after a loading screen or a stray
    alt-tab. It is not a licence to fight the operator for the desktop: `Hid` refuses to
    act whenever the window is not focused, and that guard stays regardless of what
    happens here.
    """
    _require()                                          # pragma: no cover - platform
    user32.ShowWindow(hwnd, SW_RESTORE)                 # pragma: no cover - platform
    if user32.SetForegroundWindow(hwnd):                # pragma: no cover - platform
        return True

    current = user32.GetForegroundWindow()              # pragma: no cover - platform
    if not current:                                     # pragma: no cover - platform
        return False

    target_thread = user32.GetWindowThreadProcessId(hwnd, None)   # pragma: no cover
    current_thread = user32.GetWindowThreadProcessId(current, None)  # pragma: no cover
    ours = kernel32.GetCurrentThreadId()                # pragma: no cover - platform

    attached = []                                       # pragma: no cover - platform
    for other in {current_thread, target_thread} - {ours}:   # pragma: no cover
        if user32.AttachThreadInput(ours, other, True):
            attached.append(other)
    try:                                                # pragma: no cover - platform
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:                                            # pragma: no cover - platform
        for other in attached:
            user32.AttachThreadInput(ours, other, False)
    return is_foreground(hwnd)                          # pragma: no cover - platform


def send_inputs(inputs: list[INPUT]) -> int:
    """Send a batch. Returns how many events were accepted.

    Checked by callers: `SendInput` returning fewer than it was given is how a blocked or
    filtered input reports itself, and treating it as success means pressing keys into a
    window that never received them.
    """
    _require()                                          # pragma: no cover - platform
    n = len(inputs)                                     # pragma: no cover - platform
    arr = (INPUT * n)(*inputs)                          # pragma: no cover - platform
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))  # pragma: no cover - platform


def scan_code(vk: int) -> int:
    """Virtual key to scan code.

    Games commonly read scan codes rather than virtual keys, because that is what the
    DirectInput path gives them. Sending a virtual key alone is a keypress the game may
    simply never see, and it fails silently.
    """
    _require()                                          # pragma: no cover - platform
    return user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)   # pragma: no cover - platform
