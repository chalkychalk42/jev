#!/usr/bin/env python3
"""First actuation: press one harmless key and watch the strip notice.

    /mnt/c/forever-win/Scripts/python.exe tools/probe_input.py

Jump is the right first key. It is harmless anywhere, it cannot aggro anything, it needs
no target, and — the point — `flags.falling` goes true for a moment afterwards. So a
single press closes the whole loop with no vision involved at all: input reached the
game, the game changed, the addon painted the change, and the decoder read it back.

Also probes which of the facing APIs this client actually has, by running `/run` and
having the addon report through a field rather than through chat, which nothing can read
yet.

Presses only `space` and, with `--reload`, types `/reload`. Nothing else.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jev.clients import win32  # noqa: E402
from jev.clients.capture import Backend, WindowCapture  # noqa: E402
from jev.clients.hid import Hid, Humaniser  # noqa: E402
from jev.perceive import radio_frame  # noqa: E402


def sample(cap: WindowCapture) -> dict | None:
    try:
        reading = radio_frame.read(cap.grab().rgb)
    except Exception as exc:                      # noqa: BLE001 - diagnostic tool
        print(f"  capture/read raised: {exc}")
        return None
    return reading.values if reading.ok else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reload", action="store_true",
                    help="type /reload first, to pick up changed addon Lua")
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    if not win32.available():
        print("run this with Windows Python")
        return 2

    windows = win32.find_windows("World of Warcraft")
    if not windows:
        print("no game window")
        return 1
    hwnd = windows[0]

    if not win32.is_foreground(hwnd):
        print("focusing the window...")
        win32.focus(hwnd)
        time.sleep(0.5)
    print(f"foreground: {win32.is_foreground(hwnd)}")

    hid = Hid(hwnd=hwnd, humaniser=Humaniser.for_client("probe"))
    cap = WindowCapture(hwnd, backend=Backend.SCREEN)

    if args.reload:
        print("typing /reload ...")
        hid.slash("/reload")
        time.sleep(4.0)

    before = sample(cap)
    if before is None:
        print("could not read the strip before pressing anything; stopping")
        return 1
    print(f"before: seq={before['seq']} falling={before['flags.falling']} "
          f"level={before['char.level']} hp={before['vitals.hp']:.2f}")

    print("pressing space ...")
    pressed = hid.tap("space")
    print(f"  tap returned {pressed}; sent={hid.sent} refused={hid.refused}")

    seen_falling = False
    seqs = []
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        v = sample(cap)
        if v is not None:
            seqs.append(v["seq"])
            if v["flags.falling"] is True:
                seen_falling = True
                print(f"  falling=True at seq {v['seq']}")
        time.sleep(0.05)

    after = sample(cap)
    print()
    print(f"strip advanced: {len(set(seqs))} distinct seq values over {args.seconds}s")
    print(f"INPUT REACHED THE GAME: {seen_falling}")
    if after:
        print(f"facing field: {after['pos.facing']}  "
              f"({'GetPlayerFacing works' if after['pos.facing'] is not None else 'no facing API in this client'})")
    cap.close()
    return 0 if seen_falling else 1


if __name__ == "__main__":
    raise SystemExit(main())
