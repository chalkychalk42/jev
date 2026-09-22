"""Quest-wide progress from the assembled log, with unread kept distinct from empty."""

from dataclasses import dataclass

from jev.guide.graph import Node, ObjectiveTarget
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


@dataclass(frozen=True)
class Selection:
    target: ObjectiveTarget | None = None
    complete: bool | None = None
    reason: str | None = None


def target_progress(log: tuple[Quest, ...] | None, quest_id: int | None,
                    target: ObjectiveTarget) -> Progress:
    """The selected requirement's counter, preserving its original painted slot.

    This predicate finishes one body execution; only the overall positive completion
    flag finishes a multi-objective guide step. It never calls a fourth, unpainted
    counter complete because the first three happen to be full.
    """
    quest = next((q for q in log or () if q.quest_id == quest_id), None)
    if quest is None:
        return Progress()
    if quest.complete is True:
        return Progress(complete=True)
    if target.counter_index is None:
        return Progress(complete=quest.complete)
    objective = next((o for o in quest.objectives
                      if o.counter_index == target.counter_index), None)
    if objective is None or objective.need != target.required_count:
        return Progress()
    return Progress(have=objective.have, need=objective.need,
                    first_incomplete=None if objective.done else target.counter_index,
                    complete=objective.done)


def select_objective(node: Node, log: tuple[Quest, ...] | None) -> Selection:
    """Choose the first *observed* unfinished source requirement, or explain why not."""
    if log is None:
        return Selection(reason="quest log unread")
    quest = next((q for q in log if q.quest_id == node.quest_id), None)
    if quest is None:
        return Selection(reason="quest absent from readable log")
    if quest.complete is True:
        return Selection(complete=True)
    if not node.objective_targets:
        return Selection(complete=quest.complete, reason="guide has no structured objective targets")
    for target in node.objective_targets:
        value = target_progress(log, node.quest_id, target)
        if value.complete is True:
            continue
        if target.blocked_reason:
            return Selection(complete=quest.complete, reason=target.blocked_reason)
        if target.kind == "explore":
            return Selection(target=target, complete=False)
        if target.counter_index is None:
            return Selection(complete=quest.complete, reason="objective has no verified radio counter mapping")
        if value.have is None:
            return Selection(complete=quest.complete,
                             reason=f"objective counter {target.counter_index} unread or does not match the guide")
        if target.kind == "delivery":
            return Selection(complete=quest.complete,
                             reason=f"acceptance-supplied delivery item {target.required_id} is missing")
        return Selection(target=target, complete=False)
    return Selection(complete=quest.complete,
                     reason="painted objectives are full but quest completion is not confirmed")
