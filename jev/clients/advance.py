"""Move a stock quest frame forward, whatever it is currently asking for.

Accept, Continue and Complete Quest are the same intent at different moments, so there is
one skill and one painted point (`ui.advance_x/y`) rather than a state machine tracking
which button a quest is up to. The addon returns whichever of the four is visible; this
presses it.

Three rules, and they are the whole file:

    nothing is clicked unless the radio says a frame is open, re-read before **every** press
    the click lands where the client says the button is, not where a sweep found something
    the goal is a state of the quest log, not a count of presses

The third rule is why this loops. Accepting is one press: the frame closes and the quest
appears. Turning in is two — Continue moves the progress page to the reward page, and
Complete Quest closes it — and a skill capped at one press cannot turn anything in. The
loop is not a sweep: it re-reads the radio each time, so every press is justified by a
button the client is painting at that moment, and it stops as soon as the log reaches the
goal or the frame closes.

Confirmation is the **assembled** quest log, never a single frame. The strip cycles one
entry per paint, so a partial cycle is unread — treating it as a short log is how a
tracker concludes a quest is missing from a log it never saw. That cuts both ways here:
`CLEARED` waits for a whole cycle too, or every mid-cycle read would look like a turn-in.

A reward page offering a **choice** of items. Complete Quest is painted and pressable
there, but it does nothing until an item is selected: measured 23 September, three presses
on "Wolves Across the Border" changed nothing. The addon paints its default choice
(`ui.choice_x/y`: usable first, then quality, then the earlier item) and whether one is
chosen; when none is, that point is clicked first, and the next read decides the rest.
Which reward is *best* is a real decision the default does not claim to make.

Not covered, deliberately
-------------------------
Multi-option gossip: a list, not a button.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

# Enough for the longest stock flow — progress page, a reward choice, reward page, done —
# plus one. Small on purpose: a frame that has not moved after this many fresh reads is not
# a frame this skill knows how to advance, and pressing harder has never been the answer.
MAX_PRESSES = 4


class Goal(StrEnum):
    """What the quest log should look like when the flow is finished."""

    HELD = "held"        # the quest is in the log — accepting
    CLEARED = "cleared"  # the quest is gone from the log — turning in


class Advanced(StrEnum):
    DONE = "done"              # the log reached the goal
    CLICKED = "clicked"        # buttons were pressed; the log did not reach the goal
    NO_FRAME = "no_frame"      # the radio reports no open frame to advance
    NO_BUTTON = "no_button"    # frame open, but nothing advanceable painted
    BLIND = "blind"            # nothing readable

    @property
    def ok(self) -> bool:
        return self is Advanced.DONE


@dataclass
class AdvanceQuestFrame:
    hid: object
    read: Callable[[], dict | None]                  # decoded radio values
    quest_ids: Callable[[], tuple[int, ...] | None]  # assembled log, or None if partial
    window_origin: tuple[int, int] = (0, 0)
    # The captured client size. The button arrives as a fraction of the interface, and
    # this is what turns it back into a pixel — by definition the right size, where a
    # screen height guessed in the addon was not.
    window_size: tuple[int, int] = (1600, 900)

    clicked: list[tuple[int, int]] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)

    def run(self, quest_id: int | None = None, goal: Goal = Goal.HELD, *,
            settle_s: float = 1.5, confirm_tries: int = 40,
            max_presses: int = MAX_PRESSES) -> Advanced:
        self.clicked = []
        self.detail = ""

        for _ in range(max_presses):
            v = self.read()
            if v is None:
                return Advanced.BLIND
            if v.get("ui.quest_frame") is not True and v.get("ui.gossip") is not True:
                # Not a failure once something has been pressed: the frame closing is how
                # every one of these flows ends.
                if self.clicked:
                    break
                self.detail = "the radio reports no open frame"
                return Advanced.NO_FRAME

            fx, fy = v.get("ui.advance_x"), v.get("ui.advance_y")
            if ((v.get("ui.choice_count") or 0) > 0 and v.get("ui.choice_made") is False
                    and v.get("ui.choice_x") is not None and v.get("ui.choice_y") is not None):
                # A reward must be chosen before Complete Quest does anything.
                fx, fy = v["ui.choice_x"], v["ui.choice_y"]
            if fx is None or fy is None:
                if self.clicked:
                    break
                self.detail = "frame open but no advance button painted"
                return Advanced.NO_BUTTON

            ox, oy = self.window_origin
            w, h = self.window_size
            point = (ox + round(fx * w), oy + round(fy * h))
            self.clicked.append(point)
            self.hid.click(*point)
            time.sleep(settle_s)

            if quest_id is not None and self._reached(quest_id, goal, confirm_tries):
                return Advanced.DONE

        if quest_id is None:
            return Advanced.CLICKED
        if self._reached(quest_id, goal, confirm_tries):
            return Advanced.DONE
        self.detail = (f"pressed {len(self.clicked)}x, but quest {quest_id} is still "
                       f"{'absent from' if goal is Goal.HELD else 'in'} the assembled log")
        return Advanced.CLICKED

    def _reached(self, quest_id: int, goal: Goal, tries: int) -> bool:
        """Has the log reached the goal — measured on a **whole** cycle, or not at all."""
        for _ in range(tries):
            ids = self.quest_ids()
            if ids is not None:
                return (quest_id in ids) if goal is Goal.HELD else (quest_id not in ids)
            time.sleep(0.1)
        return False
