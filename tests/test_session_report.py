"""The scoreboard's blocks (`tools/session_report.py --blocks`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from session_report import Session, blocks  # noqa: E402


def test_sessions_are_gathered_into_blocks_of_play_and_rated_per_hour():
    sessions = [Session(number=n, minutes=15.0, level_start=13, level_end=13, xp=500.0, kills=10,
                        deaths=int(n == 3), stuck=2, steps=1, tutor_calls=4, stations=3,
                        stations_won=1) for n in range(1, 11)]
    board = blocks(sessions, 2.0)
    assert [b["sessions"] for b in board] == ["1-8", "9-10"]
    first = board[0]
    assert (first["hours"], first["xp"], first["xp_h"]) == (2.0, 4000, 2000)
    assert (first["kills_h"], first["deaths_h"], first["stuck_h"]) == (40.0, 0.5, 8.0)
    assert (first["tutor_h"], first["stations"]) == (16.0, "8/24")
    assert board[1]["hours"] == 0.5, "the last block is what there is"
