"""Pick one line out of a list the client is drawing, by name.

A gossip is not a button, and that is the whole difficulty. `AdvanceQuestFrame` works
because Accept, Continue and Complete Quest are the same intent wearing different names,
so "whichever is showing" is a complete answer. A list has no "whichever": an NPC with two
quests to hand in draws two lines that are identical in every respect a camera can see,
and clicking the first is a coin toss that opens the wrong quest half the time.

So the line is chosen by **identity**, not position. The addon paints `fnv1a16` of each
line's text — the same hash, from the same function, that identifies a target — and the
guide already knows the title it wants, because a quest's name is a fact about the quest
and the graph carries it. Matching is a comparison. Nothing here reads text off the
screen, recognises a quest, or counts pixels.

Two frames, one skill: `GossipTitleButton` for a gossip and `QuestTitleButton` for the
greeting panel an NPC shows when it has several quests and nothing else to say. They are
the same widget under different names, so the Lua reads both and this never learns the
difference.

    no list          -> refuse, do not click
    no line matches  -> refuse, do not click
    two lines match  -> refuse, do not click
    exactly one      -> one click, then confirm the frame moved

Refusing on ambiguity is the point. Two lines sharing a hash means two lines sharing a
title, and there is no evidence on the strip that distinguishes them — so a click would be
a guess, and a guess here opens the wrong quest and puts the playhead somewhere it did not
ask to be.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.perceive.radio_frame import ListLine, RadioReading, list_lines, name_id


class Chose(StrEnum):
    CHOSE = "chose"              # clicked, and the frame moved on
    NO_LIST = "no_list"          # nothing is drawing a list
    NO_MATCH = "no_match"        # a list, but no line is the one asked for
    AMBIGUOUS = "ambiguous"      # more than one line answers to that name
    NO_CHANGE = "no_change"      # clicked, and the frame is still showing the same list
    BLIND = "blind"              # nothing readable

    @property
    def ok(self) -> bool:
        return self is Chose.CHOSE


@dataclass
class ChooseListLine:
    hid: object
    read: Callable[[], RadioReading | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)

    clicked: tuple[int, int] | None = field(default=None, init=False)
    line: ListLine | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    def run(self, title: str, *, settle_s: float = 1.2) -> Chose:
        return self.run_id(name_id(title), settle_s=settle_s, label=title)

    def run_id(self, wanted: int, *, settle_s: float = 1.2, label: str | None = None) -> Chose:
        """The same list composition with an identity positively painted by the addon."""
        self.clicked = self.line = None
        self.detail = ""

        reading = self.read()
        if reading is None:
            return Chose.BLIND
        before = list_lines(reading)
        if not before:
            self.detail = "no gossip or greeting list is showing"
            return Chose.NO_LIST

        matches = [line for line in before if line.name_id == wanted]
        if not matches:
            # Positions as well as hashes: a hash that matches nothing is either the
            # wrong string or the wrong bits, and the position says which — a line
            # sitting where the frame actually drew one means the packing is fine.
            seen = ", ".join(f"#{ln.index} {ln.name_id} at ({ln.x:.3f},{ln.y:.3f})"
                             for ln in before)
            self.detail = (f"{len(before)} line(s), none hashing to {wanted} for "
                           f"{label!r}: {seen}")
            return Chose.NO_MATCH
        if len(matches) > 1:
            self.detail = f"{len(matches)} lines answer to {label or wanted!r}; refusing to guess"
            return Chose.AMBIGUOUS

        self.line = matches[0]
        ox, oy = self.window_origin
        w, h = self.window_size
        self.clicked = (ox + round(self.line.x * w), oy + round(self.line.y * h))
        self.hid.click(*self.clicked)
        time.sleep(settle_s)

        after_reading = self.read()
        if after_reading is None:
            return Chose.BLIND
        after = list_lines(after_reading)
        # Clicking a line always either closes the list or replaces it. Same lines showing
        # means the click missed, which is worth distinguishing from a click that worked:
        # one is a position problem and the other is not a problem at all.
        if _same(before, after):
            self.detail = "the same list is still showing; the click did not land"
            return Chose.NO_CHANGE
        return Chose.CHOSE


def _same(a: tuple[ListLine, ...], b: tuple[ListLine, ...]) -> bool:
    return tuple(line.name_id for line in a) == tuple(line.name_id for line in b)
