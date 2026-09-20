"""Move a stock frame forward. One click, at a point the client itself painted.

Accept, Complete and Continue are the same intent at different moments in a quest, so
there is one skill and one painted point (`ui.advance_x/y`) rather than a state machine
tracking which button a quest is up to.

Two rules, and they are the whole file:

    nothing is clicked unless the radio says the frame is open
    the click lands where the client says the button is, not where a sweep found something

Confirmation is the **assembled** quest log, not a single frame. The strip cycles one
entry per paint, so a partial cycle is unread — treating it as a short log is how a
tracker concludes a quest is missing from a log it never saw.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Advanced(StrEnum):
    ACCEPTED = "accepted"          # the quest appeared in the log
    CLICKED = "clicked"            # the button was pressed; the log did not change
    NO_FRAME = "no_frame"          # the radio does not report an open frame
    NO_BUTTON = "no_button"        # frame open, but nothing advanceable painted
    BLIND = "blind"                # nothing readable

    @property
    def ok(self) -> bool:
        return self is Advanced.ACCEPTED


@dataclass
class AdvanceQuestFrame:
    hid: object
    read: Callable[[], dict | None]                 # decoded radio values
    quest_ids: Callable[[], tuple[int, ...] | None]  # assembled log, or None if partial
    window_origin: tuple[int, int] = (0, 0)
    # The captured client size. The button arrives as a fraction of the interface, and
    # this is what turns it back into a pixel — by definition the right size, where a
    # screen height guessed in the addon was not.
    window_size: tuple[int, int] = (1600, 900)

    clicked: tuple[int, int] | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def run(self, quest_id: int | None = None, *, settle_s: float = 1.5,
            confirm_tries: int = 40) -> Advanced:
        self.clicked = None
        self.detail = ""

        v = self.read()
        if v is None:
            return Advanced.BLIND
        if v.get("ui.quest_frame") is not True and v.get("ui.gossip") is not True:
            self.detail = "the radio reports no open frame"
            return Advanced.NO_FRAME

        fx, fy = v.get("ui.advance_x"), v.get("ui.advance_y")
        if fx is None or fy is None:
            self.detail = "frame open but no advance button painted"
            return Advanced.NO_BUTTON

        ox, oy = self.window_origin
        w, h = self.window_size
        self.clicked = (ox + round(fx * w), oy + round(fy * h))
        self.hid.click(*self.clicked)
        time.sleep(settle_s)

        if quest_id is None:
            return Advanced.CLICKED

        # A whole cycle, or nothing. A partial one is unread, and reading it as a short
        # log is how a quest looks missing from a log nobody finished reading.
        for _ in range(confirm_tries):
            ids = self.quest_ids()
            if ids is not None and quest_id in ids:
                return Advanced.ACCEPTED
            time.sleep(0.1)
        self.detail = f"clicked, but quest {quest_id} is not in the assembled log"
        return Advanced.CLICKED
