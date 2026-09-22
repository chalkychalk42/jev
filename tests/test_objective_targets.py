from jev.guide.graph import Node, ObjectiveTarget
from jev.guide.objectives import select_objective, target_progress
from jev.perceive.questlog import QuestLog
from jev.world.state_v1 import Objective, Quest, StepKind


def target(index, required=8):
    return ObjectiveTarget(kind="kill", counter_index=index, required_id=118 + index,
                           required_count=required, target_name="mob", target_kind="creature")


def node(*targets):
    return Node(id="do", kind=StepKind.QUEST_OBJECTIVE, zone="Elwynn", zone_id=12,
                quest_id=52, objective_targets=targets)


def log(*counters, complete=False):
    return (Quest(quest_id=52, complete=complete, objectives=tuple(
        Objective(text="", counter_index=index, have=have, need=need)
        for index, have, need in counters)),)


def test_second_requirement_uses_second_target_and_its_own_completion():
    first, second = target(0), target(1, 5)
    observed = log((0, 8, 8), (1, 2, 5))
    selection = select_objective(node(first, second), observed)
    assert selection.target == second and selection.complete is False
    progress = target_progress(observed, 52, second)
    assert (progress.have, progress.need, progress.complete) == (2, 5, False)
    assert target_progress(log((0, 8, 8), (1, 5, 5)), 52, second).complete is True
    assert select_objective(node(first, second), log((0, 8, 8), (1, 5, 5))).complete is False


def test_complete_flag_is_authority_even_without_counters():
    selection = select_objective(node(target(0)), log(complete=True))
    assert selection.complete is True and selection.target is None


def test_unpainted_fourth_counter_and_mismatched_count_do_not_guess():
    targets = tuple(target(i) for i in range(4))
    selection = select_objective(node(*targets), log((0, 8, 8), (1, 8, 8), (2, 8, 8)))
    assert selection.target is None and "counter 3 unread" in selection.reason
    selection = select_objective(node(target(0)), log((0, 0, 10)))
    assert selection.target is None and "does not match" in selection.reason


def test_unread_and_absent_are_separate_named_failures():
    assert select_objective(node(target(0)), None).reason == "quest log unread"
    assert select_objective(node(target(0)), ()).reason == "quest absent from readable log"


def test_sparse_radio_counters_keep_their_original_slots():
    assembly = QuestLog()
    observed = assembly.observe({"quests.count": 1, "quests.log_hash": 9,
                                 "quests.slot": 0, "quests.slot_id": 52,
                                 "quests.slot_complete": False,
                                 "quests.o1_have": 2, "quests.o1_need": 5})
    assert observed[0].objectives[0].counter_index == 1
    selection = select_objective(node(target(0), target(1, 5)), observed)
    assert selection.target is None and "counter 0 unread" in selection.reason


def test_delivery_missing_item_never_selects_a_hunt():
    delivery = ObjectiveTarget(kind="delivery", required_id=745, required_count=1, counter_index=0)
    selection = select_objective(node(delivery), log((0, 0, 1)))
    assert selection.target is None and "delivery item 745 is missing" in selection.reason
    assert select_objective(node(delivery), log((0, 1, 1), complete=True)).complete is True
