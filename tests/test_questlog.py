"""Assembling a log from a strip that paints one entry at a time.

The failure this prevents is quiet and fast: a partial cycle presented as a short log has
the tracker find its step's quest absent, fire `QUEST_MISSING`, and skip a live step —
twice a second, for as long as the log has more than one entry.
"""

from __future__ import annotations

from jev.perceive.questlog import QuestLog


def frame(count, slot=None, quest_id=None, log_hash=7385, have=None, need=None,
          complete=None):
    v = {
        "quests.count": count,
        "quests.slot": slot,
        "quests.slot_id": quest_id,
        "quests.slot_complete": complete,
        "quests.log_hash": log_hash,
    }
    for i in range(3):
        v[f"quests.o{i}_have"] = have if i == 0 else None
        v[f"quests.o{i}_need"] = need if i == 0 else None
    return v


def test_an_empty_log_is_knowable_from_one_frame():
    """There are no slots to wait for, and this is what puts a fresh character on the
    graph entry rather than on whoever is standing nearby."""
    assert QuestLog().observe(frame(count=0)) == ()


def test_a_partial_cycle_is_unread_not_short():
    """The whole reason this class exists."""
    log = QuestLog()
    assert log.observe(frame(3, slot=0, quest_id=783)) is None
    assert log.observe(frame(3, slot=1, quest_id=7)) is None
    assert log.progress == (2, 3)


def test_a_full_cycle_assembles_every_entry():
    log = QuestLog()
    log.observe(frame(3, slot=0, quest_id=783))
    log.observe(frame(3, slot=1, quest_id=7))
    result = log.observe(frame(3, slot=2, quest_id=5261))
    assert result is not None
    assert [q.quest_id for q in result] == [783, 7, 5261]


def test_the_tracker_never_sees_an_intermediate_log():
    """Three quests painted one per frame: the caller sees None, None, then all three —
    and never a one- or two-quest log it could act on."""
    log = QuestLog()
    seen = [log.observe(frame(3, slot=i, quest_id=q))
            for i, q in enumerate((783, 7, 5261))]
    assert seen[0] is None and seen[1] is None
    assert seen[2] is not None and len(seen[2]) == 3
    assert not any(r is not None and len(r) < 3 for r in seen)


def test_a_complete_log_does_not_flicker_while_the_strip_keeps_cycling():
    """A caller ticking at 20 Hz must not alternate between a log and no log."""
    log = QuestLog()
    for i, q in enumerate((783, 7)):
        log.observe(frame(2, slot=i, quest_id=q))
    assert len(log.observe(frame(2, slot=0, quest_id=783))) == 2
    assert len(log.observe(frame(2, slot=1, quest_id=7))) == 2


def test_a_changed_log_hash_discards_the_half_built_assembly():
    """`log_hash` covers titles, completion and objective text, so anything that could
    change an entry mid-cycle changes it. Mixing two logs would be worse than waiting."""
    log = QuestLog()
    log.observe(frame(3, slot=0, quest_id=783))
    log.observe(frame(3, slot=1, quest_id=7))
    assert log.observe(frame(3, slot=2, quest_id=5261, log_hash=9999)) is None
    assert log.progress == (1, 3)


def test_accepting_a_quest_is_visible_once_the_new_cycle_completes():
    """The live case: empty log, accept 783, and the log becomes exactly one quest."""
    log = QuestLog()
    assert log.observe(frame(0)) == ()
    assert log.observe(frame(1, slot=0, quest_id=783, log_hash=1234)) == (
        log.observe(frame(1, slot=0, quest_id=783, log_hash=1234))
    )
    result = log.observe(frame(1, slot=0, quest_id=783, log_hash=1234))
    assert [q.quest_id for q in result] == [783]


def test_objective_counts_travel_with_their_slot():
    log = QuestLog()
    result = log.observe(frame(1, slot=0, quest_id=7, have=4, need=10))
    assert result[0].objectives[0].have == 4
    assert result[0].objectives[0].need == 10


def test_a_frame_that_could_not_be_read_changes_nothing():
    """An unreadable frame is not evidence that the log emptied."""
    log = QuestLog()
    log.observe(frame(1, slot=0, quest_id=783))
    assert log.observe({"quests.count": None}) is not None


def _full(values):
    """`to_state` reads the whole strip, so fill the keys this module does not care about
    with the `None` that means unreadable."""
    from jev.perceive.fields import FIELDS

    return {f.name: values.get(f.name) for f in FIELDS}


def test_the_assembled_log_is_public_and_none_until_a_cycle_finishes():
    """`None` is not an empty log. A caller that cannot tell them apart concludes its
    step's quest is missing and skips a live step."""
    log = QuestLog()
    assert log.complete is None, "a log before any frame is unread, not empty"
    for slot, qid in enumerate((783, 7)):
        log.observe(frame(2, slot=slot, quest_id=qid))
    assert log.complete is not None
    assert tuple(q.quest_id for q in log.complete) == (783, 7)
    assert log.complete is log._complete, "a second copy of the assembly"


def test_an_assembled_log_beats_the_single_frame_answer_in_to_state():
    """`to_state` refuses to call one frame a log, correctly — so the accumulated one has
    to reach it some other way, or the tracker sees an empty log for every quest held."""
    from jev.perceive import radio_frame

    last = frame(1, slot=0, quest_id=783)
    log = QuestLog()
    log.observe(last)

    reading = radio_frame.RadioReading(values=_full(last), ok=True,
                                   fault=radio_frame.SenseFault.NONE)
    alone = radio_frame.to_state(reading, t=0.0, client_id="c")
    assert alone.quests is None, "one frame of a non-empty log settled anything"

    with_log = radio_frame.to_state(reading, t=0.0, client_id="c", quests=log.complete)
    assert tuple(q.quest_id for q in with_log.quests) == (783,)


# -- V409: the last whole log patched slot by slot ----------------------------------------

GOLDEN = 0.6180339887498949


def _whole(log, quests, log_hash=7385, have=None):
    for slot, quest_id in enumerate(quests):
        log.observe(frame(len(quests), slot=slot, quest_id=quest_id, log_hash=log_hash,
                          have=have, need=10 if have is not None else None))
    assert log.complete is not None
    return log


def test_a_ticked_counter_patches_the_last_whole_log_and_never_unreads_it():
    """A kill ticks a counter, which changes the hash: the whole log stays read, and the slot
    painted under the new hash is patched in, where the log used to go unread for a cycle."""
    log = _whole(QuestLog(), (783, 7, 5261), have=2)
    ticked = log.observe(frame(3, slot=1, quest_id=7, log_hash=1111, have=3, need=10))
    assert [q.quest_id for q in ticked] == [783, 7, 5261]
    assert ticked[1].objectives[0].have == 3, "the slot painted is patched in"
    assert ticked[0].objectives[0].have == 2, "the others as last read"
    assert log.patched == 1
    # Another tick before the cycle is whole: the patches made so far are kept.
    again = log.observe(frame(3, slot=0, quest_id=783, log_hash=2222, have=5, need=10))
    assert [(q.quest_id, q.objectives[0].have) for q in again] == [(783, 5), (7, 3), (5261, 2)]


def test_another_quest_in_a_slot_is_another_log_read_whole_first():
    """A hand-in and an accept between two reads leave as many entries, the slots holding other
    quests: the first slot that holds another quest drops the patched log."""
    log = _whole(QuestLog(), (783, 7, 5261))
    assert log.observe(frame(3, slot=2, quest_id=5261, log_hash=4444)) is not None
    assert log.observe(frame(3, slot=1, quest_id=33, log_hash=4444)) is None
    assert log.observe(frame(3, slot=0, quest_id=783, log_hash=4444)) is not None, \
        "whole again under the new hash"
    assert [q.quest_id for q in log.complete] == [783, 33, 5261]


def test_another_number_of_entries_is_another_log():
    log = _whole(QuestLog(), (783, 7))
    assert log.observe(frame(3, slot=0, quest_id=783, log_hash=5555)) is None
    log.reset()
    assert log.observe(frame(2, slot=0, quest_id=783, log_hash=7385)) is None, \
        "a reset log patches nothing"


def test_count_change_invalidates_even_when_the_short_hash_collides():
    log = _whole(QuestLog(), (783, 7))
    assert log.observe(frame(3, slot=0, quest_id=783)) is None
    assert log.progress == (1, 3)


def test_a_changed_membership_cannot_revive_the_disproved_whole_log():
    log = _whole(QuestLog(), (783, 7, 5261))
    assert log.observe(frame(3, slot=1, quest_id=33, log_hash=4444)) is None
    # A counter changes before the new membership has assembled. The old quest 7 must
    # not reappear just because this paint describes an unchanged slot.
    assert log.observe(frame(3, slot=2, quest_id=5261, log_hash=5555)) is None
    assert log.observe(frame(3, slot=0, quest_id=783, log_hash=5555)) is None
    result = log.observe(frame(3, slot=1, quest_id=33, log_hash=5555))
    assert [q.quest_id for q in result] == [783, 33, 5261]


def test_an_unknown_slot_identity_cannot_complete_a_log():
    log = QuestLog()
    assert log.observe(frame(1, slot=1)) is None
    assert log.progress == (0, 1)


def _golden_slot(tick: int, count: int) -> int:
    """The slot the addon paints at its `tick`th paint (`advanceQuestSlot`)."""
    return min(count - 1, int(((tick * GOLDEN) % 1) * count))


def test_a_steady_two_hertz_reader_reads_a_log_of_ten_on_the_golden_turn():
    """Ten paints a second and a read every fifth: a cursor stepping one slot a paint showed
    two of ten slots for ever (the hive's parity audit, 7 Oct); the golden ratio's turn shows
    every one within a few seconds, at any steady rate."""
    quests = tuple(100 + i for i in range(10))
    for step in (1, 2, 3, 4, 5, 7, 10, 20):
        for start in range(step):
            log = QuestLog()
            reads = 0
            for tick in range(1 + start, 4000, step):
                slot = _golden_slot(tick, len(quests))
                log.observe(frame(10, slot=slot, quest_id=quests[slot]))
                reads += 1
                if log.complete is not None:
                    break
            assert log.complete is not None, (step, start)
            assert reads <= 60, (step, start, reads)
    stepping = QuestLog()
    for k in range(200):                        # the old cursor at a 2 Hz reader
        slot = (5 * k) % 10
        stepping.observe(frame(10, slot=slot, quest_id=quests[slot]))
    assert stepping.complete is None and len(stepping.slots) == 2
