"""Quest-wide progress from the assembled log, with unread kept distinct from empty."""

from dataclasses import dataclass

from jev.world.state_v1 import Quest


@dataclass(frozen=True)
class Progress:
    have: int | None = None
    need: int | None = None
    first_incomplete: int | None = None
    complete: bool | None = None

    @property
    def fraction(self) -> float | None:
        if self.complete is True:
            return 1.0
        if self.have is None or self.need is None or self.need <= 0:
            return None
        return self.have / self.need


def progress(log: tuple[Quest, ...] | None, quest_id: int | None) -> Progress:
    if log is None or quest_id is None:
        return Progress()
    quest = next((q for q in log if q.quest_id == quest_id), None)
    if quest is None:
        return Progress()
    if not quest.objectives:
        return Progress(complete=quest.complete)
    pending = next((i for i, o in enumerate(quest.objectives) if not o.done), None)
    return Progress(have=sum(min(o.have, o.need) for o in quest.objectives),
                    need=sum(o.need for o in quest.objectives), first_incomplete=pending,
                    complete=quest.complete if quest.complete is not None else pending is None)
