"""The graph is generated from this server's own database, so these assert against it."""

from __future__ import annotations

import pytest

from jev.guide.generate import ALLIANCE_MASK, HORDE_MASK, WorldDB, generate
from jev.guide.graph import stats
from jev.world.state_v1 import StepKind

DB = "data/knowledge/tbc-243.sqlite"
HUMAN_ZONES = {9: "Northshire", 12: "Elwynn"}


@pytest.fixture(scope="module")
def human():
    return generate(DB, graph_id="t", faction="alliance", zone_ids=tuple(HUMAN_ZONES),
                    zone_names=HUMAN_ZONES, level_min=1, level_max=12)


def test_the_factions_do_not_overlap():
    """A shared bit would put Horde quests in an Alliance spine, which fails silently:
    the character simply never finds the giver."""
    assert ALLIANCE_MASK & HORDE_MASK == 0


def test_a_whole_starting_spine_comes_out(human):
    s = stats(human)
    assert s.quests >= 30, f"only {s.quests} quests — the zone filter is too tight"
    assert s.nodes >= 90
    assert s.by_kind.get("quest_accept", 0) == s.by_kind.get("quest_turnin", 0)


def test_the_spine_starts_in_the_starting_zone(human):
    """Level order alone starts a fresh character halfway across Elwynn, at a level-1
    quest it cannot walk to, instead of the one ten yards from where it logged in."""
    assert human.get(human.entry).zone == "Northshire"


def test_prerequisites_come_before_the_quests_they_unlock(human):
    """Otherwise the character stands at an NPC with nothing to offer, which looks
    exactly like a broken reader."""
    order = {n.id: i for i, n in enumerate(human.nodes)}
    by_quest: dict[int, int] = {}
    for n in human.nodes:
        if n.kind is StepKind.QUEST_ACCEPT and n.quest_id:
            by_quest[n.quest_id] = order[n.id]

    db = WorldDB(DB)
    try:
        rows = db.con.execute(
            "select entry, PrevQuestId from world_quest_template where PrevQuestId > 0"
        ).fetchall()
    finally:
        db.close()

    for r in rows:
        a, b = by_quest.get(r["entry"]), by_quest.get(r["PrevQuestId"])
        if a is not None and b is not None:
            assert b < a, f"quest {r['entry']} placed before its prerequisite"


def test_most_nodes_know_where_they_are(human):
    """Northshire has thirteen starting quests and no WorldMapArea row of its own —
    2.4.3 draws it on Elwynn's map. Without the containing-zone fallback every one of
    those nodes comes out positionless while converting perfectly one level up."""
    s = stats(human)
    assert s.with_position / s.nodes > 0.85


def test_northshire_nodes_resolve_against_elwynns_map(human):
    northshire = [n for n in human.nodes if n.zone == "Northshire" and n.pos]
    assert len(northshire) >= 10, "the containing-zone fallback is not firing"


def test_the_first_human_quest_is_where_the_client_puts_it(human):
    """Marshal McBride stands in Northshire Abbey, near (49, 42) on Elwynn's map."""
    node = next(n for n in human.nodes
                if n.quest_id == 7 and n.kind is StepKind.QUEST_ACCEPT)
    assert node.pos == pytest.approx((0.49, 0.42), abs=0.03)


def test_grind_ribs_are_real_places_with_real_mobs(human):
    ribs = human.ribs()
    assert ribs, "no grind rib means nothing to fail to"
    for rib in ribs:
        assert rib.pos is not None
        assert rib.r > 0.03, "a rib is a loop you walk, not a pin you stand on"
        assert "spawns of" in rib.notes


def test_objective_steps_can_always_fail_somewhere(human):
    """A step with no escape is a run that ends standing at an NPC with nothing to say."""
    objectives = [n for n in human.nodes if n.kind is StepKind.QUEST_OBJECTIVE]
    assert objectives
    assert all(n.on_fail for n in objectives)


def test_a_node_with_no_position_says_why(human):
    """Inventing a position would be worse than admitting to not having one — but an
    unexplained blank is nearly as bad. "No spawn at all" and "a spawn we could not map"
    need different fixes, so the note has to tell them apart."""
    for n in (x for x in human.nodes if x.pos is None):
        assert n.notes, f"{n.id} has no position and no explanation"
        assert ("no " in n.notes and "spawn found" in n.notes) or "off every zone map" in n.notes


def test_a_quest_given_by_an_object_is_still_placed(human):
    """Not every quest giver is a person. "Wanted: Hogger" comes off a wanted poster, and
    a creature-only lookup leaves that whole class positionless."""
    hogger = next((n for n in human.nodes if n.quest_id == 176
                   and n.kind is StepKind.QUEST_ACCEPT), None)
    assert hogger is not None, "Wanted: Hogger is not in the spine"
    assert hogger.pos is not None, "the gameobject giver lookup is not firing"


@pytest.mark.parametrize(
    ("faction", "zones"),
    [("alliance", {132: "Coldridge", 1: "DunMorogh"}),
     ("horde", {14: "Durotar"}),
     ("horde", {215: "Mulgore"})],
)
def test_other_starting_spines_also_generate(faction, zones):
    g = generate(DB, graph_id="t", faction=faction, zone_ids=tuple(zones),
                 zone_names=zones, level_min=1, level_max=12)
    assert stats(g).quests >= 15
