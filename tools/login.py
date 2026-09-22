#!/usr/bin/env python3
"""Get the client from wherever it is into the world.

    /mnt/c/forever-win/Scripts/python.exe tools/login.py

Reads `JEV_WOW_ACCOUNT` and `JEV_WOW_PASSWORD` from the environment or from `.env`, which
is gitignored. Nothing is printed that would reveal either.

Success is the radio strip painting. A login that "worked" without a character in the
world is not a login, and this says so rather than exiting zero.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent

from jev.clients import win32  # noqa: E402
from jev.clients.capture import Backend, WindowCapture  # noqa: E402
from jev.clients.hid import Hid, Humaniser  # noqa: E402
from jev.clients.session import Session, credentials  # noqa: E402
from jev.perceive import radio_frame  # noqa: E402


def load_dotenv(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2

    env = {**load_dotenv(ROOT / ".env")}
    creds = credentials(env) or credentials()
    if creds is None:
        print("set JEV_WOW_ACCOUNT and JEV_WOW_PASSWORD in .env")
        return 2
    account, _ = creds
    print(f"account {account!r}, password set")

    try:
        hwnd = win32.game_window()
    except win32.GameWindowError as exc:
        print(exc)
        return 1
    win32.focus(hwnd)
    time.sleep(0.5)
    if not win32.is_foreground(hwnd):
        print("could not raise the window")
        return 1

    ox, oy, w, h = win32.client_rect(hwnd)
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)
    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("login"))

    def read_frame():
        try:
            return cap.grab().rgb
        except Exception:
            return None

    def radio_ok() -> bool:
        frame = read_frame()
        if frame is None:
            return False
        try:
            return radio_frame.read(frame).ok
        except Exception:
            return False

    session = Session(hid=hid, read_frame=read_frame, radio_ok=radio_ok,
                      window_origin=(ox, oy), window_size=(w, h))
    print(f"stage: {session.stage().value}")

    ok = session.sign_in(*creds, timeout_s=args.timeout)
    print(f"\nin the world: {ok}")
    print(f"  credentials sent: {session.typed_credentials}   enters: {session.enters}")
    if not ok:
        print(f"  {session.detail}")
        if session.unknown_frame is not None:
            out = ROOT / "captures" / "unknown-stage.npy"
            out.parent.mkdir(exist_ok=True)
            import numpy as np

            np.save(out, session.unknown_frame)
            print(f"  kept the frame it stopped on: {out}")
    else:
        frame = read_frame()
        reading = radio_frame.read(frame) if frame is not None else None
        if reading is not None and reading.ok:
            v = reading.values
            print(f"  level {v['char.level']}  hp {v['vitals.hp']:.2f}  "
                  f"zone {v['pos.zone_id']}  at ({v['pos.mx']:.4f},{v['pos.my']:.4f})")
    cap.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
