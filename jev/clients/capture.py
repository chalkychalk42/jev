"""Screen capture of one game window, through GDI.

Two backends, because neither works everywhere and the failure is silent:

  * **`PRINT_WINDOW`** asks the window to render itself, so it works while occluded or
    behind another window. Many DirectX applications answer it with a black bitmap.
  * **`SCREEN`** copies the pixels the desktop is actually showing, which is what a
    DirectX game reliably produces — but only while the window is visible and unobscured.

The right default for a game is `SCREEN`, with `PRINT_WINDOW` available for the case
where it turns out to work, because that one additionally frees the window from having to
be on top.

**A black frame is reported, never returned as pixels.** A capture that silently yields
zeros feeds a decoder that will duly find no strip and a vision head that will duly see no
windows, and the run then fails somewhere three layers away from the cause.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from jev.clients import win32


class Backend(StrEnum):
    SCREEN = "screen"            # BitBlt from the desktop DC
    PRINT_WINDOW = "print_window"


class CaptureError(RuntimeError):
    """The frame could not be taken, or could not be believed."""


@dataclass(frozen=True)
class Frame:
    """One captured client area. `rgb` is `(h, w, 3)`, uint8, origin top-left."""

    rgb: np.ndarray
    origin: tuple[int, int]      # screen coordinates of the client area's top-left
    backend: Backend

    @property
    def size(self) -> tuple[int, int]:
        return (self.rgb.shape[1], self.rgb.shape[0])

    @property
    def mean(self) -> float:
        return float(self.rgb.mean())


class WindowCapture:
    """Captures one HWND's client area.

    Device contexts and bitmaps are created once and reused. Not for speed — although a
    fresh DC per frame at 20 Hz leaks handles until the desktop compositor gives up —
    but because a reused bitmap makes a geometry change *visible*: the window resizing
    is a thing the caller must handle, not something to paper over by silently
    reallocating and returning a differently-shaped array than last tick.
    """

    def __init__(self, hwnd: int, backend: Backend = Backend.SCREEN,
                 black_threshold: float = 1.0) -> None:
        if not win32.available():
            raise win32.Unavailable("capture needs Windows")
        self.hwnd = hwnd
        self.backend = backend
        self.black_threshold = black_threshold
        self._size: tuple[int, int] | None = None
        self._dc = None
        self._mem = None
        self._bmp = None

    # -- lifecycle -----------------------------------------------------------

    def _ensure(self, w: int, h: int) -> None:          # pragma: no cover - platform
        if self._size == (w, h):
            return
        self.close()
        src = (win32.user32.GetDC(0) if self.backend is Backend.SCREEN
               else win32.user32.GetDC(self.hwnd))
        self._dc = src
        self._mem = win32.gdi32.CreateCompatibleDC(src)
        self._bmp = win32.gdi32.CreateCompatibleBitmap(src, w, h)
        win32.gdi32.SelectObject(self._mem, self._bmp)
        self._size = (w, h)

    def close(self) -> None:                            # pragma: no cover - platform
        if self._bmp:
            win32.gdi32.DeleteObject(self._bmp)
        if self._mem:
            win32.gdi32.DeleteDC(self._mem)
        if self._dc:
            win32.user32.ReleaseDC(0, self._dc)
        self._bmp = self._mem = self._dc = None
        self._size = None

    def __enter__(self) -> WindowCapture:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the frame -----------------------------------------------------------

    def grab(self) -> Frame:                            # pragma: no cover - platform
        x, y, w, h = win32.client_rect(self.hwnd)
        if w <= 0 or h <= 0:
            raise CaptureError(f"window has no client area ({w}x{h}); minimised?")
        self._ensure(w, h)

        if self.backend is Backend.SCREEN:
            ok = win32.gdi32.BitBlt(self._mem, 0, 0, w, h, self._dc, x, y, win32.SRCCOPY)
        else:
            ok = win32.user32.PrintWindow(self.hwnd, self._mem, win32.PW_RENDERFULLCONTENT)
        if not ok:
            raise CaptureError(f"{self.backend} returned failure "
                               f"(win32 error {ctypes.get_last_error()})")

        header = win32.BITMAPINFOHEADER(
            biSize=ctypes.sizeof(win32.BITMAPINFOHEADER), biWidth=w,
            # Negative height requests a top-down bitmap. Without it GDI hands back the
            # rows bottom-up and every measured region is mirrored vertically — which
            # looks like a calibration problem rather than a row-order one.
            biHeight=-h, biPlanes=1, biBitCount=32, biCompression=win32.BI_RGB,
        )
        info = win32.BITMAPINFO(bmiHeader=header)
        buf = ctypes.create_string_buffer(w * h * 4)
        got = win32.gdi32.GetDIBits(self._mem, self._bmp, 0, h, buf,
                                    ctypes.byref(info), win32.DIB_RGB_COLORS)
        if got == 0:
            raise CaptureError("GetDIBits copied no scanlines")

        bgra = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        rgb = np.ascontiguousarray(bgra[:, :, 2::-1])   # BGRA -> RGB

        frame = Frame(rgb=rgb, origin=(x, y), backend=self.backend)
        if frame.mean < self.black_threshold:
            raise CaptureError(
                f"{self.backend} produced a black frame (mean {frame.mean:.2f}). "
                "DirectX commonly refuses PrintWindow; try Backend.SCREEN and make sure "
                "the window is visible and unobscured."
            )
        return frame


def find_game(title: str = "World of Warcraft") -> list[int]:
    """Identified native game windows; title alone never establishes game identity."""
    return win32.find_game(title)
