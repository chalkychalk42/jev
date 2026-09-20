"""A graph that loads is a graph whose every edge points somewhere."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jev.guide.graph import FailEdge, FailWhen, Graph, Node, stats
from jev.world.state_v1 import StepKind


def _n(nid: str, **kw) -> Node:
    base = dict(kind=StepKind.TRAVEL, zone="Elwynn", zone_id=12, pos=(0.5, 0.5))
    return Node(id=nid, **{**base, **kw})


def test_a_dangling_edge_is_refused_at_load():
    """A dangling edge is a character standing in a field with nothing to do and no
    error that explains it. Refuse at load, not at 3am in a live run."""
    with pytest.raises(ValidationError, match="dangling"):
        Graph(graph_id="g", faction="alliance", entry="a",
              nodes=(_n("a", next=("nowhere",)),))


def test_a_dangling_fail_edge_is_refused_too():
    with pytest.raises(ValidationError, match="dangling"):
        Graph(graph_id="g", faction="alliance", entry="a",
              nodes=(_n("a", on_fail=(FailEdge(when=FailWhen.QUEST_MISSING, goto="gone"),)),))


def test_duplicate_node_ids_are_refused():
    with pytest.raises(ValidationError, match="duplicate"):
        Graph(graph_id="g", faction="alliance", entry="a", nodes=(_n("a"), _n("a")))


def test_the_entry_must_be_a_node():
    with pytest.raises(ValidationError, match="entry"):
        Graph(graph_id="g", faction="alliance", entry="b", nodes=(_n("a"),))


def test_a_fail_edge_that_needs_a_threshold_must_have_one():
    """`timeout` with no value never fires, and never firing looks exactly like a step
    that simply has not timed out yet."""
    with pytest.raises(ValidationError, match="threshold"):
        FailEdge(when=FailWhen.TIMEOUT, goto="x")


def test_quest_missing_needs_no_threshold():
    assert FailEdge(when=FailWhen.QUEST_MISSING, goto="x").value is None


def test_a_graph_round_trips_through_disk(tmp_path):
    g = Graph(graph_id="g", faction="alliance", entry="a",
              nodes=(_n("a", next=("b",)), _n("b")))
    p = tmp_path / "g.json"
    g.save(p)
    assert Graph.load(p) == g


def test_stats_reports_what_is_actually_there():
    """A graph that quietly produced eleven nodes for a whole zone looks exactly like
    success until the character runs out of things to do."""
    g = Graph(graph_id="g", faction="alliance", entry="a",
              nodes=(_n("a", next=("b",)), _n("b", kind=StepKind.GRIND, pos=None)))
    s = stats(g)
    assert s.nodes == 2
    assert s.with_position == 1
    assert s.by_kind["grind"] == 1
