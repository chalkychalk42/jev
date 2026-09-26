"""The trainer's list census (schema 18, V237): whole under one list, or unread."""

from __future__ import annotations

from jev.perceive.trainer import AVAILABLE, HEADER, UNAVAILABLE, TrainerCensus, list_key


def _row(index, kind, *, revision=1, total=6, short=3, name_id=100, rank=1, cost=100):
    return {"trainer.revision": revision, "trainer.total": total, "trainer.short": short,
            "trainer.index": index, "trainer.type": kind, "trainer.name_id": name_id + index,
            "trainer.rank": rank, "trainer.cost": None if kind == HEADER else cost}


def test_the_short_cycle_is_whole_only_once_every_header_and_learnable_row_is_seen():
    census = TrainerCensus()
    census.observe(_row(1, HEADER))
    census.observe(_row(2, UNAVAILABLE))
    census.observe(_row(3, AVAILABLE))
    assert census.short is None, "the second learnable row is not seen yet"
    census.observe(_row(5, AVAILABLE))
    assert [r.index for r in census.short] == [1, 3, 5]
    assert census.short[1].cost == 100 and census.short[1].name_id == 103
    assert [r.index for r in census.rows] == [1, 2, 3, 5]


def test_a_changed_list_throws_the_half_read_one_away():
    """A purchase rebuilds the list, and every row below the bought one moves up: a row
    number read before names another row after."""
    census = TrainerCensus()
    for index, kind in ((1, HEADER), (3, AVAILABLE)):
        census.observe(_row(index, kind))
    census.observe(_row(5, AVAILABLE, revision=2))
    assert census.short is None and census.key == (2, 6, 3)
    assert [r.index for r in census.rows] == [5]


def test_a_strip_without_the_list_is_no_census():
    census = TrainerCensus()
    census.observe({"ui.trainer": True, "trainer.revision": None, "trainer.total": None})
    assert census.short is None and census.key is None
    assert list_key({"schema": 17, "ui.trainer": True}) is None


def test_a_list_with_nothing_learnable_and_no_headers_is_whole_and_empty():
    census = TrainerCensus()
    census.observe(_row(1, UNAVAILABLE, total=1, short=0))
    assert census.short == ()
