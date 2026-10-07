"""A quest's own item used on its creature (V387), and a quest whose reward teaches a spell
kept on the route (V388): Taming the Beast and Training the Beast, from this server's world
database, as a hunter's route holds them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_live_body import body
from test_runtime_records import seen

from jev.clients.fight import Fought
from jev.clients.use import UseOn
from jev.guide.coords import world_to_map
from jev.guide.generate import WorldDB
from jev.guide.graph import Graph, Node, ObjectiveTarget
from jev.guide.objectives import select_objective
from jev.guide.route import compile_route
from jev.perceive.radio_frame import UI_ERROR_KEYS, name_id
from jev.run.body import LiveBody
from jev.run.cli import worthless_quests
from jev.world.state_v1 import Objective, Quest, StepKind

DB = "data/knowledge/tbc-243.sqlite"
DUROTAR = 14
# Thotar's three in Razor Hill, then Ormak Grimshot's in Orgrimmar: an orc or troll hunter's.
CHAIN = (6062, 6083, 6082, 6081)


def _targets(db, quest_id: int, bounds) -> tuple[ObjectiveTarget, ...]:
    """The objective targets the generator writes, placed in Durotar's frame."""
    out = []
    for r in db.requirements(quest_id):
        spawn = r.spawn
        pos = world_to_map(spawn.x, spawn.y, bounds) if spawn is not None else None
        out.append(ObjectiveTarget(
            kind=r.kind, required_id=r.required_id, required_count=r.required_count,
            source_slot=r.source_slot, counter_index=r.counter_index,
            target_id=spawn.npc_id if spawn else None,
            target_name=spawn.name if spawn and r.kind != "explore" else None,
            target_kind=spawn.kind if spawn and r.kind != "explore" else None,
            pos=pos, world=(spawn.x, spawn.y, spawn.z) if spawn else None,
            map_id=spawn.map_id if spawn else None, blocked_reason=r.blocked_reason))
    return tuple(out)


def taming_route() -> Graph:
    """Thotar's chain as a route of accepts, objectives and hand-ins, the converter's shape."""
    db = WorldDB(DB)
    try:
        bounds = db.bounds[DUROTAR]
        nodes: list[Node] = []
        for q in CHAIN:
            row = db.con.execute("select Title from world_quest_template where entry = ?",
                                 (q,)).fetchone()
            prerequisites, _ = db.prerequisites(q)
            ends = {"accept": db.giver(q), "turnin": db.taker(q)}
            targets = _targets(db, q, bounds)
            meta = {"quest_id": q, "title": row["Title"], "zone": "Durotar", "zone_id": DUROTAR,
                    "level": (10, 13), "quest_prerequisites": prerequisites}
            for part in ("accept", "do", "turnin"):
                if part == "do" and not targets:
                    continue
                spawn = ends.get(part) if part != "do" else None
                world = (spawn.x, spawn.y, spawn.z) if spawn else targets[0].world
                nodes.append(Node(
                    id=f"{q}_{part}", **meta,
                    kind={"accept": StepKind.QUEST_ACCEPT, "do": StepKind.QUEST_OBJECTIVE,
                          "turnin": StepKind.QUEST_TURNIN}[part],
                    pos=world_to_map(world[0], world[1], bounds), world=world, map_id=1,
                    npc_id=spawn.npc_id if spawn else None,
                    target_name=spawn.name if spawn else targets[0].target_name,
                    target_kind="creature", objective_targets=targets if part == "do" else (),
                    skills=("TRAVEL_TO", "GRIND_UNTIL" if part == "do" else
                            "ACCEPT_QUEST" if part == "accept" else "TURNIN_QUEST")))
    finally:
        db.close()
    wired = [n.model_copy(update={"next": (nodes[i + 1].id,) if i + 1 < len(nodes) else ()})
             for i, n in enumerate(nodes)]
    return Graph(graph_id="taming", faction="horde", nodes=tuple(wired), entry=wired[0].id)


@pytest.mark.parametrize(("quest_id", "rod", "creature", "name"), [
    (6062, 15917, 3099, "Dire Mottled Boar"), (6083, 15919, 3107, "Surf Crawler"),
    (6082, 15920, 3126, "Armored Scorpid"), (6061, 15914, 2956, "Adult Plainstrider"),
    (9591, 23896, 17217, "Barbed Crawler")])
def test_a_taming_rod_is_used_on_the_beast_its_item_names(quest_id, rod, creature, name):
    """The rod's own `item_required_target` names the beast; the rod handed back is the item the
    accept supplied, so nothing is counted and nothing needs a painted counter."""
    db = WorldDB(DB)
    try:
        use, delivery = db.requirements(quest_id)
    finally:
        db.close()
    assert (use.kind, use.required_id, use.spawn.npc_id, use.spawn.name) == ("event", rod,
                                                                           creature, name)
    assert use.blocked_reason is None and use.counter_index is None
    assert use.spawn.points, "a hunt stands at the beast's own spawns"
    assert (delivery.kind, delivery.required_id, delivery.blocked_reason) == ("delivery", rod, None)


def test_an_event_no_item_names_a_creature_for_stays_off_the_route():
    """Winterhoof Cleansing's totem is used at a well, not on a creature (754); Matis the Cruel
    (9711) is a flare and a fight. Both events stay unworkable, as before."""
    db = WorldDB(DB)
    try:
        for quest_id in (754, 9711):
            requirement, = db.requirements(quest_id)
            assert requirement.kind == "event" and requirement.required_id is None
            assert requirement.blocked_reason == ("quest event requires a measured interaction "
                                                  "or route")
    finally:
        db.close()


def test_the_taming_chain_and_its_training_are_kept_on_a_session_route():
    """Compiled as a session compiles it: the live body's skills and the world snapshot's
    worthless quests. To V388 Training the Beast paid nothing and was left out, and the three
    tamings before it were left out for their event."""
    graph = taming_route()
    plan = compile_route(graph, available_skills=LiveBody.available,
                         worthless=worthless_quests(DB, graph))
    assert {n.quest_id for n in plan.graph.nodes} == set(CHAIN)
    assert not plan.excluded
    order = [plan.graph.entry]
    while plan.graph.get(order[-1]).next:
        order.append(plan.graph.get(order[-1]).next[0])
    assert order[:3] == ["6062_accept", "6062_do", "6062_turnin"]
    assert order[-2:] == ["6081_accept", "6081_turnin"], "Ormak Grimshot: a talk, no objective"


def test_an_event_naming_no_item_has_no_executor():
    graph = taming_route()
    nodes = [n.model_copy(update={"objective_targets": tuple(
        t.model_copy(update={"required_id": None}) if t.kind == "event" else t
        for t in n.objective_targets)}) if n.quest_id == 6062 else n for n in graph.nodes]
    # A grind at the spine's end, so something of it remains.
    last = nodes[-1]
    rib = Node(id="rib", kind=StepKind.GRIND, zone="Durotar", zone_id=DUROTAR, level=(10, 12),
               pos=last.pos, world=last.world, map_id=1, target_name="Dire Mottled Boar",
               target_kind="creature", skills=("GRIND_UNTIL",))
    nodes[-1] = last.model_copy(update={"next": ("rib",)})
    plan = compile_route(graph.model_copy(update={"nodes": (*nodes, rib)}),
                         available_skills=LiveBody.available)
    reasons = {e.quest_id: e.reason for e in plan.excluded}
    assert reasons[6062] == "objective kind event has no verified executor"
    assert 6081 in reasons, "the chain behind it goes with it"


def test_the_use_is_selected_before_the_rod_it_hands_back():
    graph = taming_route()
    node = graph.get("6062_do")
    rod = (Quest(quest_id=6062, complete=False, objectives=(
        Objective(text="Taming Rod", have=1, need=1, counter_index=0),)),)
    selection = select_objective(node, rod)
    assert selection.target.uses_item and selection.target.target_name == "Dire Mottled Boar"
    done = (rod[0].model_copy(update={"complete": True}),)
    assert select_objective(node, done).complete is True


def test_an_item_use_objective_is_hunted_with_the_item(monkeypatch):
    """The hunt is the objective's own (stations, rests, services); its pulls are the rod's
    uses, and no other quest's creatures are fought beside it."""
    graph = taming_route()
    quest = Quest(quest_id=6062, complete=False, objectives=(
        Objective(text="Taming Rod", have=1, need=1, counter_index=0),))
    b = body(StepKind.QUEST_OBJECTIVE, log=(quest,))
    node = graph.get("6062_do").model_copy(update={"id": "quest", "quest_id": 1, "map_id": 0})
    use_target = node.objective_targets[0].model_copy(update={"map_id": 0})
    node = node.model_copy(update={"objective_targets": (use_target,
                                                        *node.objective_targets[1:])})
    b.client.log.complete = (quest.model_copy(update={"quest_id": 1}),)
    b.graph = b.graph.model_copy(update={"nodes": (node,)})
    monkeypatch.setattr(b, "_held", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("a use fights no other quest's creatures")))
    made = {}

    class MeasuredHunt:
        detail = "fixture"

        def __init__(self, **kw):
            made.update(kw)

        def run(self, destination, radius, target_hash, **kw):
            made.update(destination=destination, target=target_hash, also=kw.get("also"))
            from jev.run.hunt import Hunted
            return Hunted.DONE

    monkeypatch.setattr("jev.run.body.Hunt", MeasuredHunt)
    result = b._hunt(seen(quests=b.client.log.complete))
    assert result.code == "done"
    use = made["fight"]
    assert isinstance(use, UseOn) and use.fight is b.fight and use.item_id == 15917
    assert made["destination"] == use_target.world
    assert made["target"] == name_id("Dire Mottled Boar") and made["also"] == ()
    assert use.charms == frozenset({name_id("Dire Mottled Boar")})


# -- the engagement --------------------------------------------------------------------------


class _Fight:
    """A fight that selects on `acquire` and records what it was asked to fight."""

    def __init__(self, acquired=None):
        self.acquired, self.fought, self.detail = acquired, [], ""
        self.pressed = 0
        self.targeting = SimpleNamespace(cancel_pending_spell=lambda values=None: False,
                                         face_selected=lambda **kw: None)

    def _targeting(self):
        return self.targeting

    def acquire(self, name_id_, *, defend=False):
        return self.acquired

    def run(self, name_id_=None, *, timeout_s=45.0):
        self.fought.append(name_id_)
        self.detail = "fought"
        return Fought.KILLED


class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _engagement(readings, *, complete_after=None, fight=None, charmed=False):
    """`UseOn` over scripted readings; the quest completes after `complete_after` reads."""
    clock, reads, clicks = _Clock(), [0], []
    rows = list(readings)

    def read():
        reads[0] += 1
        return rows[min(reads[0] - 1, len(rows) - 1)]

    def complete():
        return complete_after is not None and reads[0] >= complete_after

    hid = SimpleNamespace(click=lambda x, y, right=False: clicks.append((x, y, right)) or True,
                          hold=lambda key, s, **kw: clicks.append((key, s)) or True)
    use = UseOn(fight=fight or _Fight(), read=read, item_id=15917, complete=complete, hid=hid,
                sleep=clock.sleep, monotonic=clock.monotonic)
    if charmed:
        use.charmed = lambda: True
    return use, clicks


ROD = {"inventory.item_id": 15917, "inventory.x": 0.5, "inventory.y": 0.5,
       "vitals.hp": 1.0, "vitals.combat": False}


def test_the_rod_is_used_on_the_selection_and_stood_under_until_the_quest_completes():
    channel = {**ROD, "bars.casting": True, "vitals.combat": True, "vitals.hp": 0.7}
    use, clicks = _engagement([ROD, ROD, ROD, channel, channel, channel], complete_after=6)
    assert use.run(name_id("Dire Mottled Boar")) is Fought.USED
    assert clicks == [(800, 450, True)], "right-clicked in the bags, and no step taken"
    assert (use.uses, use.lost) == (1, 0) and use.fight.fought == []


def test_a_channel_ending_without_the_credit_is_a_try_lost():
    channel = {**ROD, "bars.casting": True}
    over = {**ROD, "bars.casting": False}
    use, _ = _engagement([ROD, ROD, ROD, channel, channel, over])
    assert use.run(1) is Fought.LOST
    assert (use.uses, use.lost) == (1, 1) and "not complete" in use.detail


def test_a_use_that_never_channels_from_beyond_reach_steps_closer_and_uses_again():
    far = {**ROD, "ui.error_count": 1, "ui.error_last": UI_ERROR_KEYS.index("out_of_range")}
    channel = {**ROD, "bars.casting": True, "ui.error_count": 1}
    rows = [ROD, ROD, ROD] + [far] * 20 + [channel] * 3
    use, clicks = _engagement(rows, complete_after=len(rows))
    assert use.run(1) is Fought.USED
    assert ("w", 1.0) in clicks and use.uses == 2


def test_attacked_or_beside_its_own_charm_the_character_fights_instead():
    attacked = {**ROD, "vitals.combat": True}
    use, clicks = _engagement([attacked])
    assert use.run(7) is Fought.KILLED and use.fight.fought == [7] and not clicks
    use, clicks = _engagement([ROD], charmed=True)
    assert use.run(7) is Fought.KILLED and "charm" in use.detail and not clicks


def test_hurt_in_the_channel_it_steps_out_and_fights_the_beast():
    channel = {**ROD, "bars.casting": True, "vitals.hp": 0.15, "vitals.combat": True}
    use, clicks = _engagement([ROD, ROD, ROD, channel])
    assert use.run(7) is Fought.KILLED
    assert ("s", 0.3) in clicks and use.fight.fought == [7] and use.lost == 1


def test_no_rod_in_the_bags_stops_the_hunt_and_nothing_to_use_it_on_is_a_dry_look():
    use, clicks = _engagement([{"vitals.hp": 1.0, "vitals.combat": False}])
    assert use.run(1) is Fought.REFUSED and "bag census" in use.detail and not clicks
    use, _ = _engagement([ROD], fight=_Fight(acquired=Fought.NO_TARGET))
    assert use.run(1) is Fought.NO_TARGET and use.uses == 0


def test_the_hunt_reads_the_fights_own_lines_through_the_engagement():
    fight = _Fight()
    fight.heals_landed, fight.top_up = 3, lambda: True
    use, _ = _engagement([ROD], fight=fight)
    assert use.heals_landed == 3 and use.top_up() is True and use.pressed == 0
