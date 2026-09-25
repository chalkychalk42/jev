"""Seconds since a person last touched the desk, for the keeper (`tools/keep.sh`). Windows Python.

Read-only: it sends no input and raises no window. Input within the bot's own last stamp
(`jev.clients.operator`) is the bot's, not a person's. Prints the seconds ("inf" when the
last input was the bot's) and exits 0 when they are at least the first argument (default
600), 1 when a person may be there, 2 when it cannot tell.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev.clients import operator, win32


def person_idle_s(last: int | None, ours: int | None, now: int) -> float:
    """Seconds since input that was not the bot's. `last` is the last input's tick, `ours` the
    bot's own last stamp and `now` the tick now, all in milliseconds that wrap at 2**32."""
    if last is None:
        return 0.0                              # unread: somebody may be there
    slack = operator.MARGIN_MS + operator.STAMP_EVERY_S * 1000
    if ours is not None and (last - ours) % operator.WRAP <= slack:
        return float("inf")
    return ((now - last) % operator.WRAP) / 1000


def _stamp() -> int | None:
    try:
        return int(operator._stamp_path().read_text().strip()) % operator.WRAP
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    need = float(args[0]) if args else 600.0
    if not win32.available():
        print("unknown")
        return 2
    idle = person_idle_s(win32.last_input_tick(), _stamp(), win32.tick_now())
    print("inf" if idle == float("inf") else f"{idle:.0f}")
    return 0 if idle >= need else 1


if __name__ == "__main__":
    raise SystemExit(main())
