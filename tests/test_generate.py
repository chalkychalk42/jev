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


def test_an_item_that_only_drops_from_crates_still_places_its_objective(human):
    """Milly's Harvest (3904) wants item 11119, which has **no creature dropper at all**:
    it lives in forty chests. A creature-only search returned nothing and the objective
    fell back to the quest giver, so the node sat on Milly Osworth with a fifteen-yard
    disk and the bot would have hunted the woman who wanted the apples.

    Same failure shape as quest 33 hunting Eagan Peltskinner, one table over."""
    node = next(n for n in human.nodes
                if n.quest_id == 3904 and n.kind is StepKind.QUEST_OBJECTIVE)
    giver = next(n for n in human.nodes
                 if n.quest_id == 3904 and n.kind is StepKind.QUEST_ACCEPT)
    assert node.world is not None
    assert node.world != giver.world, "the objective is standing on the quest giver"
    assert "Harvest" in node.notes, node.notes
    assert node.target_kind == "gameobject"
    assert "Harvest" in node.target_name
    assert giver.target_kind == "creature"
    assert giver.target_name == "Milly Osworth"


def test_hunt_targets_have_structured_names_separate_from_guide_prose(human):
    wolves = next(n for n in human.nodes
                  if n.quest_id == 33 and n.kind is StepKind.QUEST_OBJECTIVE)
    assert wolves.target_kind == "creature"
    assert wolves.target_name == "Young Wolf"
    assert wolves.skills == ("TRAVEL_TO", "GRIND_UNTIL")
    ribs = [n for n in human.nodes if n.kind is StepKind.GRIND]
    assert ribs and all(n.target_name and n.target_kind == "creature" for n in ribs)


def test_a_camp_is_allowed_to_be_bigger_than_fifty_yards(human):
    """Measured, against the belief that "no camp in the game is a hundred yards across".
    The Tough Wolf Meat population is 27 spawns strung along the Northshire border with a
    median of 100 yards; clamped to 50 the searched disk held three of them, and the bot
    correctly reported an empty camp twenty times over."""
    wolves = next(n for n in human.nodes
                  if n.quest_id == 33 and n.kind is StepKind.QUEST_OBJECTIVE)
    assert wolves.hunt_yards is not None and wolves.hunt_yards > 50.0, wolves.hunt_yards


def test_a_cluster_is_pulled_by_drop_chance_not_by_headcount():
    """A pool of droppers is not a pool of equals. Ten spawns of a 1% dropper should not
    outvote four of an 80% one: the node belongs where the bag actually fills."""
    from jev.guide.generate import _cluster

    def rows(n, x, chance):
        return [{"px": x + i, "py": 0.0, "pz": 0.0, "weight": chance} for i in range(n)]

    many_but_stingy = rows(10, 1000.0, 1.0)
    few_but_generous = rows(4, 0.0, 80.0)
    spawn = _cluster(1, "mixed", 0, many_but_stingy + few_but_generous)
    assert abs(spawn.x) < 100.0, f"clustered on the stingy pack at {spawn.x}"

    # With no weights at all it is a plain headcount, exactly as a kill objective needs.
    plain = _cluster(1, "mixed", 0,
                     [{"px": r["px"], "py": 0.0, "pz": 0.0} for r in
                      many_but_stingy + few_but_generous])
    assert plain.x > 500.0


def test_every_protect_frontier_counter_has_its_own_required_creature(human):
    node = next(n for n in human.nodes
                if n.quest_id == 52 and n.kind is StepKind.QUEST_OBJECTIVE)
    first, second = node.objective_targets
    assert (first.kind, first.required_id, first.required_count, first.counter_index) == ("kill", 118, 8, 0)
    assert (second.kind, second.required_id, second.required_count, second.counter_index) == ("kill", 822, 5, 1)
    assert first.target_name == "Prowler"
    assert second.target_name == "Young Forest Bear"
    assert first.world != second.world
    assert first.world == node.world  # Existing first-camp geometry stays the same.


@pytest.mark.parametrize("quest_id", [54, 3905, 61, 84, 106, 107, 114, 59, 71])
def test_acceptance_supplied_delivery_items_do_not_generate_a_hunt(human, quest_id):
    node = next(n for n in human.nodes
                if n.quest_id == quest_id and n.kind is StepKind.QUEST_OBJECTIVE)
    taker = next(n for n in human.nodes
                 if n.quest_id == quest_id and n.kind is StepKind.QUEST_TURNIN)
    assert len(node.objective_targets) == 1
    target = node.objective_targets[0]
    assert target.kind == "delivery"
    assert target.world == taker.world == node.world
    assert target.blocked_reason is None


@pytest.mark.parametrize(("quest_id", "trigger_id"), [(62, 88), (76, 87)])
def test_exploration_quests_expose_the_actual_server_trigger(human, quest_id, trigger_id):
    from jev.guide.coords import _as_float

    node = next(n for n in human.nodes
                if n.quest_id == quest_id and n.kind is StepKind.QUEST_OBJECTIVE)
    target, = node.objective_targets
    db = WorldDB(DB)
    try:
        row = db.con.execute("select * from dbc_AreaTrigger where id = ?", (trigger_id,)).fetchone()
    finally:
        db.close()
    assert target.kind == "explore" and target.required_id == trigger_id
    assert target.world == tuple(_as_float(row[f"c{i}"]) for i in (2, 3, 4))
    assert target.counter_index is None and target.target_name is None


@pytest.mark.parametrize(("quest_id", "item_id"), [(11, 782), (46, 780)])
def test_random_spawn_entry_loot_sources_are_not_reported_missing(human, quest_id, item_id):
    node = next(n for n in human.nodes
                if n.quest_id == quest_id and n.kind is StepKind.QUEST_OBJECTIVE)
    target, = node.objective_targets
    db = WorldDB(DB)
    try:
        assert not db.con.execute("select 1 from world_creature where id = ? limit 1",
                                  (target.target_id,)).fetchone()
        assert db.con.execute("select 1 from world_creature_spawn_entry where entry = ? limit 1",
                              (target.target_id,)).fetchone()
        loot = db.con.execute(
            "select 1 from world_creature_template t join world_creature_loot_template l "
            "on l.entry = t.LootId where t.Entry = ? and l.item = ?",
            (target.target_id, item_id),
        ).fetchone()
    finally:
        db.close()
    assert loot
    assert target.kind == "loot" and target.required_id == item_id
    assert target.world is not None and target.blocked_reason is None


def test_prerequisite_metadata_keeps_milly_chain_connected(human):
    manifest = next(n for n in human.nodes if n.quest_id == 3905)
    assert manifest.quest_prerequisites == ((3904,),)


def test_ribs_cover_the_band_in_level_windows_and_steps_fail_into_their_own(human):
    """The one densest cluster for levels 1-12 was level 5-6 boars, and every Northshire
    step sent a level 3 character there to die (run 20260923T174132-d01302)."""
    ribs = human.ribs()
    assert len({r.level for r in ribs}) >= 4, "one rib for the whole band again"
    assert min(r.level[0] for r in ribs) == 1, "nothing for a new character to grind"
    by_id = {r.id: r for r in ribs}
    for node in human.nodes:
        for edge in node.on_fail:
            rib = by_id.get(edge.goto)
            if rib is not None:
                assert rib is human.rib_for(node.level[0]), (node.id, rib.id)
                assert rib.level[0] <= max(node.level[0], min(r.level[0] for r in ribs))


def test_the_rib_for_a_level_is_the_highest_window_it_has_reached():
    from jev.guide.graph import Node, rib_for

    def rib(lo, hi):
        return Node(id=f"r{lo}", kind=StepKind.GRIND, zone="z", zone_id=1, level=(lo, hi))

    low, mid, high = rib(1, 3), rib(3, 5), rib(5, 7)
    ribs = (high, low, mid)
    assert rib_for(ribs, 1) is low and rib_for(ribs, 3) is mid and rib_for(ribs, 4) is mid
    assert rib_for(ribs, 9) is high, "past every window: the highest"
    assert rib_for((mid, high), 1) is mid, "below every window: the lowest"
    assert rib_for(ribs, None, preferred=high) is high, "an unread level keeps the guide's"
    assert rib_for((), 3) is None


def test_a_character_fails_into_the_nearest_rib_that_still_suits_it():
    """A level 5 character failed out of Echo Ridge Mine into the level 5-7 wolves 1,200
    yards away, the level 3-5 kobolds beside the mine passed by (run ...015701-2417ae)."""
    from jev.guide.graph import Node, rib_for

    def rib(lo, hi, pos):
        return Node(id=f"r{lo}", kind=StepKind.GRIND, zone="z", zone_id=1, level=(lo, hi),
                    pos=pos)

    wolves, kobolds, boars = rib(1, 3, (0.48, 0.30)), rib(3, 5, (0.49, 0.33)), rib(5, 7, (0.42, 0.80))
    ribs = (wolves, kobolds, boars)
    at_the_mine = (0.48, 0.32)
    assert rib_for(ribs, 5) is boars
    assert rib_for(ribs, 5, near=at_the_mine) is kobolds
    assert rib_for(ribs, 5, near=(0.42, 0.79)) is boars
    assert rib_for(ribs, 6, near=at_the_mine) is boars, "never more than two levels below"
    assert rib_for(ribs, 2, near=(0.42, 0.79)) is wolves, "never a window above the level"


def test_every_hunt_knows_where_its_target_spawns_without_touching_the_guide():
    """Rings round a cluster's centre stood where Northshire's wolves were not: they spawn
    24 to 170 yards from it, and the rings looked 38 times and found nothing (run
    ...233909). The points go in a side table - the guide's bytes are the tutor's
    knowledge fingerprint, and a changed one starts the motor learner's corpus again."""
    import math

    table = {}
    g = generate(DB, graph_id="t", faction="alliance", zone_ids=tuple(HUMAN_ZONES),
                 zone_names=HUMAN_ZONES, level_min=1, level_max=12, spawns=table)
    hunts = [n for n in g.nodes if n.kind is StepKind.GRIND
             or any(t.kind in ("kill", "loot") and t.target_kind == "creature"
                    for t in n.objective_targets)]
    assert hunts and all(n.id in table for n in hunts), "a hunt with nowhere to stand"
    wolves = next(n for n in g.nodes if n.id == "t_33_wolves_across_the_border_do")
    points = table[wolves.id]
    assert 8 <= len(points) <= 16
    distances = [math.dist(wolves.world[:2], p[:2]) for p in points]
    assert distances == sorted(distances), "nearest the centre first"
    plain = generate(DB, graph_id="t", faction="alliance", zone_ids=tuple(HUMAN_ZONES),
                     zone_names=HUMAN_ZONES, level_min=1, level_max=12)
    assert plain.model_dump() == g.model_dump(), "the guide itself changed"


def test_a_grind_rib_stands_among_the_creature_it_is_named_for(human):
    """Pooling every creature in a band put the "Kobold Worker" rib in Northshire's Defias
    Thug camp, 230 yards from any kobold, and a failed step's detour died there hunting
    kobolds among thugs (run 20260924T002817-cee9c2)."""
    import math
    import sqlite3

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for rib in (n for n in human.nodes if n.kind is StepKind.GRIND):
        entry = con.execute("select Entry from world_creature_template where Name = ?",
                            (rib.target_name,)).fetchone()[0]
        spawns = [(float(x), float(y)) for x, y in con.execute(
            "select position_x, position_y from world_creature where id = ? and map = ?",
            (entry, rib.map_id))]
        nearest = min(math.dist(rib.world[:2], p) for p in spawns)
        assert nearest < 40, f"{rib.id} is {nearest:.0f} yards from any {rib.target_name}"
    three_five = next(n for n in human.nodes if n.id.endswith("grind_elwynn_3_5"))
    assert three_five.target_name != "Defias Thug", "a packed humanoid camp as a safety net"
