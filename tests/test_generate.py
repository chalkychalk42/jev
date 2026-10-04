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


def test_a_rib_stands_where_nothing_around_it_outclasses_its_band():
    """V193: the 1-3 rib's Young Wolves clustered by Goldshire's road among level 5-6 Mangy
    Wolves and Defias Cutpurses; a level 2 mage failed over to it from Northshire and a
    Cutpurse killed it twice."""
    import math

    from jev.guide.coords import load_bounds
    from jev.guide.generate import RIB_NEIGHBOUR_YARDS, RIB_OUTCLASS_LEVELS

    world = WorldDB(DB)
    elwynn = load_bounds(DB)[12]
    stronger = [(x, y) for x, y in world.con.execute(
        "select cast(c.position_x as real), cast(c.position_y as real) from world_creature c "
        "join world_creature_template t on t.Entry = c.id where c.map = 0 and t.NpcFlags = 0 "
        "and t.MaxLevel > ?", (3 + RIB_OUTCLASS_LEVELS,))]
    [(rib, _count), *_] = world.grind_clusters(elwynn, 1, 3)
    assert not any(math.hypot(rib.x - x, rib.y - y) <= RIB_NEIGHBOUR_YARDS for x, y in stronger)


def test_an_items_droppers_are_those_the_quests_level_can_fight():
    """Every Riverpaw gnoll carries Patrolling Westfall's Gnoll Paws (item 725) at 80%, and
    the pool's densest cluster was the level 17-18 Taskmasters' camp: a level 13 paladin
    was sent there for a level 14 quest and died twice (session 140)."""
    from jev.guide.coords import load_bounds

    world = WorldDB(DB)
    westfall = load_bounds(DB)[40]
    level = dict(world.con.execute("select Entry, MinLevel from world_creature_template"))
    fit = world._drops(725, (westfall,), westfall, level=14)
    assert level[fit.npc_id] <= 16, fit.name
    assert world._drops(725, (westfall,), westfall).name == "Riverpaw Taskmaster", \
        "without the quest's level every dropper counts, as before"
    # A quest whose every dropper is above it keeps them all: somewhere beats nowhere.
    assert world._drops(725, (westfall,), westfall, level=5) is not None


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
    from jev.guide.graph import window_rib

    ribs = human.ribs()
    assert len({r.level for r in ribs}) >= 4, "one rib for the whole band again"
    assert min(r.level[0] for r in ribs) == 1, "nothing for a new character to grind"
    by_id = {r.id: r for r in ribs}
    for node in human.nodes:
        for edge in node.on_fail:
            rib = by_id.get(edge.goto)
            if rib is not None:
                assert rib is window_rib(ribs, node.level[0]), (node.id, rib.id)
                assert rib.level[0] <= max(node.level[0], min(r.level[0] for r in ribs))


def test_the_rib_for_a_level_pays_the_most_a_kill_of_those_at_most_a_level_above():
    """V323: the rib's window read as its creatures' levels put Westfall's Goretusks, 14-15 in
    the 14-16 window, above a level 15 character, which was given the 12-14 window's level
    12-13 Kobold Diggers, about 54 experience a kill for about 105: the live mage ground them
    197 minutes at 15. By the creatures' levels, the best-paying kill none above one over."""
    from jev.guide.graph import Node, rib_for, rib_xp

    def rib(lo, hi, mobs, pos=None):
        return Node(id=f"r{lo}_{hi}", kind=StepKind.GRIND, zone="z", zone_id=1, level=(lo, hi),
                    mob_levels=mobs, pos=pos)

    kobolds, goretusks, murlocs, wolves = (rib(12, 14, (12, 13)), rib(14, 16, (14, 15)),
                                           rib(16, 18, (17, 18)), rib(18, 20, (19, 20)))
    ribs = (wolves, kobolds, murlocs, goretusks)
    assert rib_for(ribs, 15) is goretusks
    assert rib_xp(goretusks, 15) > 1.3 * rib_xp(kobolds, 15)
    assert rib_for(ribs, 15, near=(0.1, 0.1)) is goretusks, "wherever it stands"
    assert rib_for(ribs, 14) is goretusks, "one level above is the next level's"
    assert rib_for(ribs, 13) is kobolds, "Goretusks of 15 are two above a 13"
    assert rib_for(ribs, 17) is murlocs and rib_for(ribs, 19) is wolves
    assert rib_for(ribs, 25) is wolves, "past every rib: the best still worth a kill"
    assert rib_for((murlocs, wolves), 12) is None, "below every rib: none, the spine (V329)"
    assert rib_for(ribs, None, preferred=kobolds) is kobolds, "an unread level keeps the guide's"
    assert rib_for((), 3) is None
    # Without the creatures' levels, the window, whose top none of them is above.
    bare = (rib(1, 3, None), rib(3, 5, None), rib(5, 7, None))
    assert [rib_for(bare, level).id for level in (1, 3, 4, 6, 9)] == [
        "r1_3", "r1_3", "r3_5", "r5_7", "r5_7"]


def test_ribs_paying_about_the_same_a_kill_are_chosen_by_distance():
    """A level 5 character failed out of Echo Ridge Mine into the wolves 1,200 yards away, the
    kobolds beside the mine passed by (run ...015701-2417ae). Of the ribs paying within
    `RIB_XP_SHARE` of the best a kill, the nearest; one paying much less is not taken for
    being near (V323)."""
    from jev.guide.graph import Node, rib_for

    def rib(name, mobs, pos):
        return Node(id=name, kind=StepKind.GRIND, zone="z", zone_id=1, level=(mobs[0], mobs[0] + 2),
                    mob_levels=mobs, pos=pos)

    kobolds, far_kobolds, wolves = (rib("kobolds", (5, 6), (0.49, 0.33)),
                                    rib("far_kobolds", (5, 6), (0.42, 0.80)),
                                    rib("wolves", (3, 3), (0.48, 0.30)))
    at_the_mine = (0.48, 0.32)
    ribs = (far_kobolds, wolves, kobolds)
    assert rib_for(ribs, 5, near=at_the_mine) is kobolds
    assert rib_for(ribs, 5, near=(0.42, 0.79)) is far_kobolds
    assert rib_for(ribs, 6, near=at_the_mine) is kobolds, "level 3 wolves pay 40% at 6"
    assert rib_for((wolves,), 2, near=at_the_mine) is wolves
    assert rib_for((far_kobolds,), 2, near=at_the_mine) is None, "level 5-6 are three above a 2"
    assert rib_for((far_kobolds,), 4, near=at_the_mine) is far_kobolds, "one above: the next level's"


def _elwynn(rid, window, mobs, pos):
    """A rib on Elwynn's map, its world position from its map fraction (`coords.map_to_world`)."""
    from jev.guide.coords import load_bounds, map_to_world
    from jev.guide.graph import Node

    x, y = map_to_world(*pos, load_bounds(DB)[12])
    return Node(id=rid, kind=StepKind.GRIND, zone="Elwynn", zone_id=12, level=window,
                mob_levels=mobs, pos=pos, world=(x, y, 0.0), map_id=0)


def test_a_guides_frame_is_read_off_its_nodes():
    """A node's map fraction is its world position in the guide's frame (`generate.place`), so
    the frame's box, and yards between two fractions, come from the guide itself (V330)."""
    from jev.guide.coords import load_bounds
    from jev.guide.graph import Graph, frame_yards

    elwynn = load_bounds(DB)[12]
    across, down = frame_yards(Graph.load("content/tbc/ally_human_1_12.json").nodes)
    assert across == pytest.approx(abs(elwynn.left - elwynn.right), abs=0.5)
    assert down == pytest.approx(abs(elwynn.top - elwynn.bottom), abs=0.5)
    assert frame_yards(Graph.load("content/tbc/ally_human_12_20.json").nodes) is not None
    assert frame_yards(()) is None


def test_a_short_rib_is_the_nearest_of_the_best_paying_and_never_a_long_walk():
    """At level 11 the rib in the band was 1,550 yards from Goldshire, where the inn's steps
    failed: a five-minute wait was four minutes' walk each way (sessions 109 to 111). From
    11:00 to 15:20 on 29 Sep the hive's short ribs were a median 822 yards from where their step
    failed, and 38% over 1,000. Of the ribs paying within `RIB_XP_SHARE` of the best a kill, the
    nearest, and none further than `SHORT_RIB_YARDS`: without one, the step again (V330).
    Elwynn's ribs' creatures, 29 Sep."""
    from jev.guide.graph import SHORT_RIB_YARDS, frame_yards, rib_for

    ribs = (_elwynn("r1", (1, 3), (1, 1), (0.432, 0.6)),
            _elwynn("r5", (5, 7), (5, 6), (0.296, 0.725)),
            _elwynn("r7", (7, 9), (7, 8), (0.606, 0.655)),
            _elwynn("r9", (9, 11), (9, 10), (0.739, 0.397)),
            _elwynn("r11", (11, 12), (11, 12), (0.068, 0.965)))
    scale = frame_yards(ribs)
    goldshire = (0.43, 0.66)
    assert rib_for(ribs, 11, near=goldshire).id == "r11", "a whole rib: the best a kill"
    assert rib_for(ribs, 11, near=goldshire, short=True) is None, \
        "the gnolls are 1,440 yards off, and the wolves of 5 to 8 pay half as much"
    assert rib_for(ribs, 12, near=(0.07, 0.95), short=True).id == "r11"
    assert rib_for(ribs, 8, near=(0.59, 0.64), short=True).id == "r7", "beside it, the best"
    assert rib_for(ribs, 3, near=goldshire, short=True).id == "r1", "the wolves of 1, nearby"
    sentinel = _elwynn("r11b", (11, 12), (11, 12), (0.17, 0.79))       # 950 yards west
    assert rib_for((*ribs, sentinel), 11, near=goldshire, short=True) is None
    near_enough = _elwynn("r11c", (11, 12), (11, 12), (0.33, 0.70))
    assert (rib_for((*ribs, sentinel, near_enough), 11, near=goldshire, short=True).id
            == "r11c"), f"within {SHORT_RIB_YARDS:.0f} yards"
    assert scale is not None


def test_a_barred_rib_that_suits_the_character_comes_before_one_above_it():
    """hive-200, a level 8 dwarf hunter, went from Dun Morogh's 1-3 rib, grey to it, up the
    9-11, 11-13 and 13-15 ribs of Dun Morogh and Loch Modan to Loch Modan's 17-19 and Dun
    Morogh's 19-20, dying on most, 11:42 to 15:02 on 29 Sep: every rib that suited it was
    barred, and the lowest of the rest was taken (V329). Its ribs' creatures."""
    from jev.guide.graph import Node, rib_for

    def rib(name, window, mobs):
        return Node(id=name, kind=StepKind.GRIND, zone="z", zone_id=1, level=window,
                    mob_levels=mobs, pos=(0.5, 0.5))

    ribs = (rib("dun_morogh_1_3", (1, 3), (1, 1)), rib("dun_morogh_3_5", (3, 5), (3, 4)),
            rib("dun_morogh_7_9", (7, 9), (7, 8)), rib("dun_morogh_9_11", (9, 11), (10, 11)),
            rib("loch_modan_11_13", (11, 13), (11, 12)), rib("loch_modan_17_19", (17, 19), (17, 18)))
    assert rib_for(ribs, 8).id == "dun_morogh_7_9"
    barred = frozenset({"dun_morogh_7_9", "dun_morogh_3_5"})
    assert rib_for(ribs, 8, barred=barred).id == "dun_morogh_7_9", "barred, but it suits a level 8"
    assert rib_for(ribs, 8, near=(0.5, 0.5), barred=barred).id == "dun_morogh_7_9"
    above = tuple(r for r in ribs if r.id not in barred)
    assert rib_for(above, 8) is None, "the 1-3 is grey, the 10-11 two above: the spine"
    assert rib_for(above, 9).id == "dun_morogh_9_11", "its lowest one above: the next level's"
    assert rib_for(above, 9, barred=frozenset({"dun_morogh_9_11"})).id == "dun_morogh_9_11"
    assert rib_for((ribs[0],), 9) is None and rib_for((ribs[-1],), 9) is None


def test_a_played_guides_ribs_carry_their_creatures_levels():
    """V323: read from the world database when a guide is played, not written into it: the
    committed guide's bytes stay the generator's."""
    from jev.guide.generate import with_rib_levels
    from jev.guide.graph import Graph, rib_for

    source = Graph.load("content/tbc/ally_human_12_20.json")
    graph = with_rib_levels(source, DB)
    levels = {r.id.split("_grind_")[1]: r.mob_levels for r in graph.ribs()}
    assert levels["westfall_12_14"] == (12, 13) and levels["westfall_14_16"] == (14, 15)
    assert levels["redridge_14_16"] == (15, 16) and levels["westfall_16_18"] == (17, 18)
    assert all(r.mob_levels is None for r in source.ribs())
    westfall = graph.get("alli_human_12_20_grind_westfall_12_14").pos
    assert rib_for(graph.ribs(), 15, near=westfall).id.endswith("westfall_14_16")
    assert rib_for(graph.ribs(), 13, near=westfall).id.endswith("westfall_12_14")
    assert rib_for(source.ribs(), 13, near=westfall).id.endswith("westfall_12_14")
    assert with_rib_levels(source, None) is source


def test_a_window_has_a_rib_for_each_of_its_creatures_and_steps_fail_into_its_first(human):
    """V332: one rib a window put every character of a race at a level on one creature: up to
    38 of the hive's bots on Durotar's Scorpid Workers and 36 on Dun Morogh's Juvenile Snow
    Leopards at once (29 Sep, 03:00-09:30). A window has up to `RIB_CREATURES` ribs in a zone,
    each a creature of its own; the first is the one it always had, its id unchanged, and it is
    the one steps fail into."""
    from collections import defaultdict

    from jev.guide.generate import RIB_CREATURES, rib_id
    from jev.guide.graph import window_rib

    ribs = human.ribs()
    windows = defaultdict(list)
    for r in ribs:
        windows[(r.zone_id, r.level)].append(r)
    assert any(len(w) > 1 for w in windows.values()), "one creature a window again"
    for (zone, (lo, hi)), group in windows.items():
        assert len(group) <= RIB_CREATURES
        assert len({r.target_name for r in group}) == len(group), "each rib its own creature"
        assert group[0].id == rib_id("t", group[0].zone, lo, hi), "the first's id is the window's"
    firsts = [w[0] for w in windows.values()]
    by_id = {r.id: r for r in ribs}
    for node in human.nodes:
        for edge in node.on_fail:
            if edge.goto in by_id:
                assert by_id[edge.goto] is window_rib(firsts, node.level[0]), node.id


def test_characters_spread_over_the_ribs_as_good_for_their_level_and_each_keeps_its_own():
    """V332: of the ribs paying within `RIB_SPREAD_SHARE` of the best a kill and no more than
    `RIB_SPREAD_YARDS` further than the nearest, each character takes the one its own hash puts
    first (`spread_rank`): replayed on the hive's rib time of 29 Sep 03:00-09:30 with each
    window's three creatures, no rib held more than 14 characters at once, where 38 stood on
    Durotar's Scorpid Workers. The same character always takes the same rib, and keeps it as
    others come and go; without a character, the nearest (V323)."""
    from collections import Counter

    from jev.guide.graph import RIB_SPREAD_YARDS, rib_for

    ribs = (_elwynn("wolves", (5, 7), (5, 6), (0.30, 0.72)),
            _elwynn("boars", (5, 7), (5, 6), (0.40, 0.88)),
            _elwynn("cutpurses", (5, 7), (5, 6), (0.42, 0.53)),
            _elwynn("kobolds", (3, 5), (3, 3), (0.49, 0.35)),
            _elwynn("far", (5, 7), (5, 6), (0.95, 0.20)))
    here = (0.40, 0.66)
    assert rib_for(ribs, 6, near=here).id == "cutpurses", "no character: the nearest"
    picks = Counter(rib_for(ribs, 6, near=here, key=key).id for key in range(300))
    assert set(picks) == {"wolves", "boars", "cutpurses"}, "not the far one, nor the kobolds"
    assert min(picks.values()) > 60, picks
    for key in range(40):
        chosen = rib_for(ribs, 6, near=here, key=key)
        assert chosen is rib_for(ribs, 6, near=here, key=key), "deterministic"
        for gone in ("wolves", "boars", "cutpurses"):
            if gone != chosen.id:
                rest = tuple(r for r in ribs if r.id != gone)
                assert rib_for(rest, 6, near=here, key=key) is chosen, "kept as others go"
    assert RIB_SPREAD_YARDS < 2000


def test_a_rib_in_a_capital_or_of_a_friendly_creature_is_no_grind_where_a_guide_is_played():
    """V331: V317 leaves a capital's box and a creature friendly to the faction out of the ribs
    it makes, but the hive's `.v2` routes, made before it, held 56 ribs in the boxes of
    Orgrimmar, the Undercity, Silvermoon, the Exodar and Thunder Bluff, played by 22 characters
    (29 Sep). Marked where a guide is played (`with_rib_levels`), from what the generator knows,
    they are left out of its route, and no rib choice takes one."""
    from jev.guide.generate import with_rib_levels
    from jev.guide.graph import FailEdge, FailWhen, Graph, Node, rib_for
    from jev.guide.route import compile_route

    def rib(rid, zone_id, name, window):
        return Node(id=rid, kind=StepKind.GRIND, zone="z", zone_id=zone_id, level=window,
                    target_name=name, target_kind="creature", pos=(0.5, 0.5),
                    world=(0.0, 0.0, 0.0), map_id=0, skills=("GRIND_UNTIL",))

    wolves = rib("wolves", 12, "Mangy Wolf", (5, 7))
    city = rib("city", 1519, "Mangy Wolf", (5, 7))              # Stormwind City's box
    lumberjacks = rib("lumberjacks", 12, "Lumberjack", (5, 7))   # Stormwind's own
    accept = Node(id="accept", kind=StepKind.QUEST_ACCEPT, zone="z", zone_id=12, quest_id=1,
                  level=(5, 8), pos=(0.5, 0.5), world=(0.0, 0.0, 0.0), map_id=0,
                  target_name="Marshal", target_kind="creature",
                  skills=("TRAVEL_TO", "ACCEPT_QUEST"),
                  on_fail=(FailEdge(when=FailWhen.TIMEOUT, value=240, goto="city"),))
    source = Graph(graph_id="g", faction="alliance", entry="accept",
                   nodes=(accept, city, lumberjacks, wolves))
    graph = with_rib_levels(source, DB)
    by = graph.by_id()
    assert "capital" in by["city"].route_blocked_reason
    assert "friendly" in by["lumberjacks"].route_blocked_reason
    assert by["wolves"].route_blocked_reason is None and by["wolves"].mob_levels == (5, 6)
    assert all(r.route_blocked_reason is None for r in source.ribs()), "the guide's bytes stay"
    assert rib_for((by["city"], by["lumberjacks"]), 5) is None
    assert rib_for(graph.ribs(), 5) is by["wolves"]
    assert graph.rib_for(5) is by["wolves"]
    plan = compile_route(graph, available_skills=frozenset({"TRAVEL_TO", "ACCEPT_QUEST",
                                                            "GRIND_UNTIL"}))
    assert {n.id for n in plan.graph.ribs()} == {"wolves"}
    assert [e.goto for e in plan.graph.get("accept").on_fail] == ["wolves"], \
        "an edge into a rib left out leads to one kept"


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


def test_an_elite_quest_taker_does_not_block_a_delivery(human):
    """Gryan Stoutmantle takes Westfall's hand-ins at Sentinel Hill and is an elite; only a
    fought elite (Hogger, whose claw Wanted: "Hogger" asks for) blocks its objective."""
    from jev.guide.route import compile_route

    skills = frozenset({"TRAVEL_TO", "ACCEPT_QUEST", "TURNIN_QUEST", "GRIND_UNTIL",
                        "VENDOR_REPAIR", "COMBAT_PROFILE", "LOOT"})
    westfall = {40: "Westfall", 44: "Redridge"}
    g = generate(DB, graph_id="t", faction="alliance", zone_ids=tuple(westfall),
                 zone_names=westfall, level_min=12, level_max=20)
    reasons = [ex.reason for ex in compile_route(g, available_skills=skills).excluded]
    assert not any("Gryan Stoutmantle is an elite" in r for r in reasons)
    hogger = [ex.reason for ex in compile_route(human, available_skills=skills).excluded
              if ex.quest_id == 176]
    assert hogger and "elite" in hogger[0]


def test_no_rib_stands_in_a_capital_and_none_on_a_creature_friendly_to_the_character():
    """V317: the hive's routes had 62 ribs in the map boxes of Orgrimmar, the Undercity, Thunder
    Bluff, Silvermoon and the Exodar in 32 of 52 (29 Sep), a second rib for a window beside the
    surrounding zone's own for a leave to hop to. A capital's box has none; the zone around it
    keeps its own."""
    from jev.guide.coords import load_bounds
    from jev.guide.generate import rib_windows

    world = WorldDB(DB)
    bounds = load_bounds(DB)
    for city in (1497, 1519, 1537, 1637, 1638, 1657, 3487, 3557):
        assert world.capital(city)
        assert not any(world.grind_clusters(bounds[city], lo, hi, limit=3, faction="horde")
                       for lo, hi in rib_windows(1, 20))
    tirisfal = bounds[85]
    assert not world.capital(85)
    assert world.grind_clusters(tirisfal, 7, 9, limit=3, faction="horde")
    # The friendly filter takes nothing hostile or neutral: Elwynn's ribs are as before.
    elwynn = bounds[12]
    for lo, hi in rib_windows(1, 12):
        assert (world.grind_clusters(elwynn, lo, hi, limit=3, faction="alliance")
                == world.grind_clusters(elwynn, lo, hi, limit=3))
    friendly = world._friendly("horde")
    assert 68 in friendly and 11 not in friendly          # Undercity Guardian, Stormwind's
