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
