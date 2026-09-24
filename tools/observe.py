"""Look at the live client without touching it: one or more frames, radio and input state.

Strictly read-only. It attaches to the game window, grabs frames, decodes the radio and
reports which keys and mouse buttons Windows currently holds down. It never focuses the
window, moves the pointer or sends a key, so it is safe to run while a supervised test
is in progress or while the operator is using the desktop.

    python tools/observe.py                       # one frame, summary on stdout
    python tools/observe.py --frames 5 --every 1  # a short sequence
    python tools/observe.py --out captures/observe/check

Use Windows Python; frames are saved as lossless PNGs under the output directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SUMMARY = ("schema", "seq", "char.level", "char.xp_pct", "pos.zone_id", "pos.mx", "pos.my",
           "vitals.hp", "vitals.power", "vitals.combat", "vitals.dead", "vitals.ghost",
           "target.has", "target.name_id", "target.hp", "target.reaction", "target.in_melee",
           "target.attacking_me", "target.melee_range", "bars.attacking", "bars.casting",
           "ui.modal", "ui.loot", "ui.gossip", "ui.vendor", "ui.quest_frame", "ui.error_id",
           "ui.error_last", "ui.error_count", "bags.free", "bags.money_copper",
           "quests.count", "quests.slot_id", "quests.o0_have", "quests.o0_need",
           "cursor.has", "cursor.world", "bars.slot", "bars.slot_spell", "spells.total",
           "spells.index", "spells.id", "ui.spellbook", "ui.trainer", "cursor.holding")


def held_inputs() -> dict:
    """Physical key/button state as Windows reports it; reading it changes nothing."""
    from jev.clients import win32
    from jev.clients.hid import VK

    return {"keys": sorted(n for n, v in VK.items() if win32.user32.GetAsyncKeyState(v) & 0x8000),
            "mouse": [n for n, v in {"left": 1, "right": 2, "middle": 4}.items()
                      if win32.user32.GetAsyncKeyState(v) & 0x8000]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--every", type=float, default=1.0, help="seconds between frames")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.frames < 1 or args.every < 0:
        parser.error("frames must be positive and every non-negative")

    import numpy as np
    from PIL import Image

    from jev.clients import win32
    from jev.perceive import radio_frame
    from jev.run.client import attach

    out = args.out or ROOT / "captures" / "observe" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    client = attach("observe")
    try:
        for index in range(args.frames):
            if index:
                time.sleep(args.every)
            with client._capturing:
                frame = client.cap.grab()
            reading = radio_frame.read(frame.rgb)
            path = out / f"{index:04d}.png"
            Image.fromarray(np.ascontiguousarray(frame.rgb)).save(path)
            values = reading.values if reading.ok else {}
            print(json.dumps({
                "frame": str(path), "t": time.time(), "size": list(frame.size),
                "origin": list(frame.origin), "radio_ok": reading.ok,
                "fault": getattr(reading.fault, "value", str(reading.fault)),
                "foreground": win32.is_foreground(client.hwnd),
                "values": {key: values.get(key) for key in SUMMARY if key in values},
                "held": held_inputs(),
            }), flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
