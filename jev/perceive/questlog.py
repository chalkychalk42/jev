"""Assemble the quest log from a strip that paints one entry per frame.

The radio cycles: each paint describes one log slot (`fields.py`, `quests.slot`), so a
single frame carries one quest out of however many `count` says there are. Turning that
frame into a one-quest log is wrong in a way that looks fine — the tracker reads it, sees
its step's quest absent, and `QUEST_MISSING` skips a live step. Twice a second.

So a partial cycle is **unread**, not short. `to_state` says `None` whenever `count > 0`,
and this is the only thing allowed to say otherwise: it accumulates slots across frames
and produces a log exactly once, when every slot has been seen under one unchanged
`log_hash`.

`log_hash` is what makes that safe. It covers titles, levels, completion and objective
text, so anything that could change an entry mid-cycle changes the hash, and a changed
hash throws the half-built assembly away rather than mixing two logs together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jev.world.state_v1 import Objective, Quest


@dataclass
class QuestLog:
    """Accumulates cycling slots into a whole log."""

    hash_seen: int | None = field(default=None, init=False)
    count: int | None = field(default=None, init=False)
    slots: dict[int, Quest] = field(default_factory=dict, init=False)
    _complete: tuple[Quest, ...] | None = field(default=None, init=False)
    cycles: int = field(default=0, init=False)

    def observe(self, values: dict[str, Any]) -> tuple[Quest, ...] | None:
        """Take one frame. Returns the whole log, or `None` while it is still partial.

        The last complete assembly is held and returned on subsequent frames, so a caller
        ticking at 20 Hz does not flicker between a log and no log while the strip cycles.
        It is dropped the moment `log_hash` moves.
        """
        count = values.get("quests.count")
        log_hash = values.get("quests.log_hash")

        if count is None:
            return self._complete

        if log_hash != self.hash_seen:
            # A different log, or the same log changed. Anything half-built describes
            # something that no longer exists.
            self.hash_seen = log_hash
            self.slots.clear()
            self._complete = None

        self.count = count
        if count == 0:
            # Positively empty, and knowable from one frame — there are no slots to wait
            # for. This is what tells a fresh character it is on the entry step.
            self._complete = ()
            return self._complete

        slot = values.get("quests.slot")
        quest_id = values.get("quests.slot_id")
        if slot is None:
            return self._complete

        objectives = tuple(
            Objective(text="", have=values[f"quests.o{i}_have"],
                      need=values[f"quests.o{i}_need"], counter_index=i)
            for i in range(3)
            if values.get(f"quests.o{i}_have") is not None
            and values.get(f"quests.o{i}_need") is not None
        )
        self.slots[slot] = Quest(quest_id=quest_id, objectives=objectives,
                                 complete=values.get("quests.slot_complete"))

        if len(self.slots) >= count:
            self._complete = tuple(self.slots[k] for k in sorted(self.slots))
            self.cycles += 1
        return self._complete

    @property
    def complete(self) -> tuple[Quest, ...] | None:
        """The whole log, or `None` if a cycle has never finished.

        Public because every caller that wants a log wants *this* one, and reaching for
        `_complete` from outside was the alternative. `None` is not an empty log: it means
        nobody has read one yet, and the difference is a skipped step.
        """
        return self._complete

    @property
    def progress(self) -> tuple[int, int | None]:
        """Slots seen against slots expected. For a dashboard, and for a postmortem that
        needs to say whether the log was ever actually read."""
        return (len(self.slots), self.count)

    def reset(self) -> None:
        self.hash_seen = None
        self.count = None
        self.slots.clear()
        self._complete = None
