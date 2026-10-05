"""Every hunt serves every held quest (V363): the quarry of the log's open kill and loot
counters round a hunt, the fight's pull for it, and the stations it adds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jev.clients.fight import Fought, Kinds, Paying, Quarry, _logged, _named, _takes
from jev.perceive.radio_frame import name_id
from jev.world import quarry
from jev.world.quarry import Kind, Want, held, open_counter
from jev.world.state_v1 import Objective, Quest

BOAR, WOLF, SPIDER, OWN = 11, 22, 33, 44


def _quest(qid, *counters, complete=False):
    return Quest(quest_id=qid, complete=complete,
                 objectives=tuple(Objective(text="", have=h, need=n, counter_index=i)
                                  for i, (h, n) in enumerate(counters)))


def _wants(table):
    return lambda qid: table.get(qid, ())


def _kind(name, low, high, *points, map_id=0):
    return Kind(entry=name, name=name, low=low, high=high,
                points=tuple((map_id, x, y, 0.0) for x, y in points))


TABLE = {
    1: (Want(1, "kill", 0, 8, (_kind(BOAR, 5, 6, (10.0, 0.0), (30.0, 0.0)),)),
        Want(1, "loot", 1, 5, (_kind(WOLF, 6, 7, (0.0, 40.0)),))),
    2: (Want(2, "kill", 0, 6, (_kind(SPIDER, 9, 10, (5.0, 5.0)),)),),
    3: (Want(3, "kill", 0, 4, (_kind(BOAR, 5, 6, (500.0, 500.0)),)),),
    4: (Want(4, "kill", 0, 4, (_kind(OWN, 5, 6, (1.0, 1.0)),)),),
}


def test_a_hunt_takes_every_held_quests_open_counters_round_it():
    """V363: the server counts a kill for every quest in the log, and quest steps earned 2,401
    experience an hour against a rib's 1,283 (4 Oct 20:30-23:00): a hunt's quarry is the union
    of the log's open kill and loot counters whose creatures spawn in or near its disk."""
    log = (_quest(1, (0, 8), (2, 5)), _quest(2, (0, 6)), _quest(3, (0, 4)), _quest(4, (0, 4)))
    found = held(log, 0, (0.0, 0.0, 0.0), 30.0, level=6, grind=False, own=OWN,
                 wanted=_wants(TABLE))
    assert found.names == {BOAR, WOLF}, "the spider is 3 above; the far boars' quest is out"
    assert found.quests == (1,) and found.high == 7
    assert set(found.points) == {(10.0, 0.0, 0.0), (30.0, 0.0, 0.0), (0.0, 40.0, 0.0)}
    full = (_quest(1, (8, 8), (5, 5)),)
    assert not held(full, 0, (0.0, 0.0, 0.0), 30.0, level=6, grind=False,
                    wanted=_wants(TABLE)), "a full counter is no quarry"
    done = (_quest(1, (0, 8), (0, 5), complete=True),)
    assert not held(done, 0, (0.0, 0.0, 0.0), 30.0, level=6, grind=False, wanted=_wants(TABLE))
    assert not held(log, 1, (0.0, 0.0, 0.0), 30.0, level=6, grind=False,
                    wanted=_wants(TABLE)), "another map"
    assert not held(None, 0, (0.0, 0.0, 0.0), 30.0, level=6, grind=False,
                    wanted=_wants(TABLE)), "the log unread"
    far = held(log, 0, (0.0, 0.0, 0.0), 30.0, level=6, grind=False, wanted=_wants(TABLE))
    assert OWN in far.names, "without an own creature the fourth quest's is wanted too"


def test_a_grinds_quarry_is_none_grey_to_the_character():
    """V363 with V344: a grind's pulls are for experience, so a held quest's creature all grey
    to the character is not added to it; a quest objective's hunt takes it, as its own."""
    log = (_quest(1, (0, 8), (0, 5)),)
    assert held(log, 0, (0.0, 0.0, 0.0), 30.0, level=13, grind=True,
                wanted=_wants(TABLE)).names == frozenset(), "boars 5-6 and wolves 6-7 grey at 13"
    assert held(log, 0, (0.0, 0.0, 0.0), 30.0, level=13, grind=False,
                wanted=_wants(TABLE)).names == {BOAR, WOLF}


def test_a_counter_with_no_identity_is_wanted_only_while_every_counter_is_short():
    """A quest mixing creatures and items has no counter identity (the objective code's rule):
    its creature is wanted only while none of its counters is full."""
    want = Want(5, "kill", None, 4, ())
    assert open_counter(_quest(5, (0, 4), (1, 3)), want)
    assert not open_counter(_quest(5, (4, 4), (1, 3)), want)
    assert not open_counter(_quest(5), want), "no counters read"
    assert not open_counter(_quest(1, (0, 9)), Want(1, "kill", 0, 8, ())), "a count that differs"


def test_the_quarry_takes_the_steps_own_as_ever_and_the_others_safely():
    """The step's own creature at any level, as a quest's always was; another held quest's not
    friendly, of normal rank and at most a level above the character (`high`)."""
    pull = Quarry(OWN, frozenset({BOAR}), high=7)

    def unit(name, level=6, reaction=4, rank=1):
        return {"target.name_id": name, "target.level": level, "target.reaction": reaction,
                "target.classification": rank, "char.level": 6}
    assert _takes(pull, unit(OWN, level=12)), "its own at any level"
    assert _takes(pull, unit(BOAR)), "a neutral boar for another quest"
    assert not _takes(pull, unit(BOAR, level=8)), "two above"
    assert not _takes(pull, unit(BOAR, reaction=5)), "friendly"
    assert not _takes(pull, unit(BOAR, rank=2)), "an elite"
    assert not _takes(pull, unit(BOAR, level=None)), "a skull"
    assert not _takes(pull, unit(WOLF))
    assert _named(pull, BOAR) and _named(pull, OWN) and not _named(pull, WOLF)
    assert pull.own == OWN and _logged(pull) == sorted({OWN, BOAR})
    grind = Paying(pull)
    assert not _takes(grind, {**unit(BOAR, level=1), "char.level": 10}), "a grind's: none grey"
    assert grind.own == OWN and _logged(grind) == sorted({OWN, BOAR})
    wider = Paying(Quarry(Kinds(own=OWN, names=frozenset({WOLF}), low=5, high=7),
                          frozenset({BOAR}), high=7))
    hostile = {**unit(WOLF), "target.reaction": 2}
    assert _takes(wider, hostile) and _takes(wider, unit(BOAR)), "a dry rib's kinds and quests'"
    assert wider.own == OWN and _logged(wider) == sorted({OWN, WOLF, BOAR})


def _hunt(walked, fights=None):
    from jev.clients.rest import Rested
    from jev.run.hunt import Hunt

    fight = SimpleNamespace(run=lambda name_id=None, **k: (fights or []).append(name_id)
                            or Fought.NO_TARGET, pressed=[], closed=0, heals_landed=0,
                            heals_ignored=0, detail="", broken=False, last_plate=None,
                            killed_name_id=None, top_up=lambda *a, **k: True, top_ups=0,
                            top_ups_landed=0)
    rest = SimpleNamespace(until=lambda *a, **k: Rested.HEALTHY, detail="")
    return Hunt(fight=fight, rest=rest, read=lambda: {}, approach=lambda p, **k:
                walked.append(tuple(p)) or True, progress=lambda: (0, 8), say=lambda s: None)


def test_the_other_quests_stations_come_after_an_objectives_own_and_before_a_grinds():
    """V363: the hunt's stations are the union of the spawn tours, the step's own objective
    first; a grind rib's quest creatures first and its own creature after. A lone spawn, a
    named one, is still waited at, nothing added."""
    from jev.run.hunt import SPAWN_LAPS

    own = ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0))
    also = ((0.0, 40.0, 0.0), (0.0, 60.0, 0.0), (21.0, 1.0, 0.0))
    walked = []
    assert _hunt(walked).run((0.0, 0.0, 0.0), 30.0, OWN, timeout_s=5, spawns=own,
                             also=also).value == "unreachable"
    lap = walked[: len(walked) // SPAWN_LAPS]
    assert lap == [*own, (0.0, 40.0, 0.0), (0.0, 60.0, 0.0)], "own first; one stance is one"
    walked = []
    _hunt(walked).run((0.0, 0.0, 0.0), 30.0, OWN, timeout_s=5, spawns=own, also=also,
                      also_first=True)
    assert walked[: len(walked) // SPAWN_LAPS] == [(0.0, 40.0, 0.0), (0.0, 60.0, 0.0), *own]
    walked = []
    hunt = _hunt(walked)
    hunt.sleep = lambda s: None
    hunt.run((0.0, 0.0, 0.0), 30.0, OWN, timeout_s=0.2, spawns=own[:1], also=also)
    assert set(walked) == {own[0]}, "a lone spawn is waited at"


def _objective_body(log, monkeypatch, table, kind="quest_objective"):
    from test_live_body import _grind_body, body

    from jev.guide.graph import Graph, Node, ObjectiveTarget
    from jev.guide.route_memory import RouteMemory
    from jev.world.state_v1 import StepKind

    monkeypatch.setattr(quarry, "wants", _wants(table))
    if kind == "grind":
        b = _grind_body(RouteMemory())
        b.hunt_spawns = {"rib": ((50.0, 50.0, 0.0), (60.0, 50.0, 0.0))}
    else:
        target = ObjectiveTarget(kind="kill", required_count=4, counter_index=0, target_id=OWN,
                                 target_name="Own", target_kind="creature",
                                 world=(50.0, 50.0, 0.0), map_id=0, hunt_yards=30.0)
        node = Node(id="quest", kind=StepKind.QUEST_OBJECTIVE, zone="zone", zone_id=1,
                    pos=(0.5, 0.5), world=(50.0, 50.0, 0.0), map_id=0, quest_id=4,
                    objective_targets=(target,))
        b = body(StepKind.QUEST_OBJECTIVE, log=log)
        b.graph = Graph(graph_id="g", faction="alliance", entry=node.id, nodes=(node,))
        b.hunt_spawns = {"quest#44": ((50.0, 50.0, 0.0), (70.0, 50.0, 0.0))}
    b.client.log.complete = log
    b._hostiles = lambda *a, **k: ()
    b._stations = lambda *a, **k: None
    b._service_needed = lambda: None
    b._read = lambda: {"vitals.hp": 1.0, "char.level": 6}
    b._approach = lambda p, **kw: True
    return b


def test_an_objective_hunt_fights_the_other_held_quests_creatures_too(monkeypatch):
    """V363: the objective dispatch hunted one target of one quest (`select_objective`), and in
    the hive's 4 Oct 20:00-01:00 replay 45% of objective hunt time had another held quest's
    open creature spawning round it, its kills of them by accident."""
    table = {**TABLE, 4: (Want(4, "kill", 0, 4, (_kind(name_id("Own"), 5, 6, (50.0, 50.0)),)),),
             1: (Want(1, "kill", 0, 8, (_kind(BOAR, 5, 6, (50.0, 90.0), (50.0, 110.0)),)),)}
    log = (_quest(4, (0, 4)), _quest(1, (2, 8)))
    b = _objective_body(log, monkeypatch, table)
    asked, runs = [], []
    import jev.run.hunt as hunt_module
    original = hunt_module.Hunt.run

    def run(self, centre, radius, name_id_=None, **kw):
        asked.append(name_id_)
        runs.append(kw)
        return original(self, centre, radius, name_id_, **kw)
    monkeypatch.setattr(hunt_module.Hunt, "run", run)
    b.fight = SimpleNamespace(run=lambda name_id=None, **k: Fought.DIED, pressed=[], closed=0,
                              heals_landed=0, heals_ignored=0, detail="", broken=False,
                              top_up=lambda *a, **k: True, top_ups=0, top_ups_landed=0)
    b._hunt(None)
    assert asked == [Quarry(name_id("Own"), frozenset({BOAR}), high=7)]
    assert runs[0]["also"] == ((50.0, 90.0, 0.0), (50.0, 110.0, 0.0))
    assert runs[0]["also_first"] is False
    alone = _objective_body((_quest(4, (0, 4)),), monkeypatch, table)
    asked.clear()
    alone.fight = b.fight
    alone._hunt(None)
    assert asked == [name_id("Own")], "no other quest held: its own creature, as before"


def test_a_grind_rib_takes_the_held_quests_creatures_first_and_for_experience(monkeypatch):
    """V363: a grind's pull is its rib's creature for experience (`Paying`, V344); the held
    quests' creatures round it are its quarry too, their stations walked first."""
    import jev.run.hunt as hunt_module

    table = {1: (Want(1, "kill", 0, 8, (_kind(BOAR, 2, 3, (50.0, 90.0)),)),)}
    b = _grind_body_with(table, monkeypatch)
    asked, kws = [], []
    original = hunt_module.Hunt.run

    def run(self, centre, radius, name_id_=None, **kw):
        asked.append(name_id_)
        kws.append(kw)
        return original(self, centre, radius, name_id_, **kw)
    monkeypatch.setattr(hunt_module.Hunt, "run", run)
    b._hunt(None)
    assert asked == [Paying(Quarry(name_id("Wolf"), frozenset({BOAR}), high=3))]
    assert kws[0]["also"] == ((50.0, 90.0, 0.0),) and kws[0]["also_first"] is True


def _grind_body_with(table, monkeypatch):
    b = _objective_body((_quest(1, (0, 8)),), monkeypatch, table, kind="grind")
    b.client.log.complete = (_quest(1, (0, 8)),)
    b._read = lambda: {"vitals.hp": 1.0, "char.level": 2}
    b.fight = SimpleNamespace(run=lambda name_id=None, **k: Fought.DIED, pressed=[], closed=0,
                              heals_landed=0, heals_ignored=0, detail="", broken=False,
                              top_up=lambda *a, **k: True, top_ups=0, top_ups_landed=0)
    return b


@pytest.mark.skipif(not quarry.WORLD_DB.is_file(), reason="needs the world snapshot")
def test_bounty_on_murlocs_is_served_by_every_murloc_dropping_the_fin():
    """V363, from the hive: hive-409, a level 13 human priest, spent 9,553 ticks on Bounty on
    Murlocs' objective (quest 46, 8 Torn Murloc Fins) on 4 Oct 20:00-01:00, the hunt taking only
    the Murloc Forager its step names, while the Murloc Lurkers spawned among them drop
    the same fin. The loot counter's creatures are every dropper of its item."""
    (fins,) = quarry.wants(46)
    assert fins.kind == "loot" and fins.counter_index == 0 and fins.need == 8
    assert {k.entry for k in fins.kinds} >= {46, 732}
    log = (Quest(quest_id=46, complete=False, objectives=(
        Objective(text="", have=0, need=8, counter_index=0),)),)
    found = held(log, 0, (-9002.4, -1204.5, 70.5), 39.2, level=13, grind=False,
                 own=name_id("Murloc Forager"))
    assert found.names == {name_id("Murloc Lurker")} and found.quests == (46,)
    assert len(found.points) >= 10
    # Wolves Across the Border: Tough Wolf Meat drops from four wolves (`WorldDB._drops`).
    (meat,) = quarry.wants(33)
    assert {k.entry for k in meat.kinds} == {69, 299, 704, 705}
    assert quarry.wants(176) == (), "Hogger is an elite: no solo fight"


def test_a_quest_finished_by_another_hunt_passes_its_objective_and_is_handed_in(tmp_path):
    """V363 needs no change to the spine, checked here: a later objective step whose quest got
    completed on the way passes at once, its hand-in next (the tracker's `_quest_complete`);
    from a grind the hand-in of a quest complete in the log behind it is gone back to
    (`_look_back`, V342); and one passed on another step's way is handed in (`DETOUR`)."""
    from test_passed_steps import runtime, state

    done = Quest(quest_id=183, complete=True)
    rt = runtime(tmp_path, [state(0, 5, done)], start_step="boar_do")
    rt.tick(choose=False)
    assert rt.tracker.step_id == "boar_in", "complete in the log: the objective passes at once"
    back = runtime(tmp_path, [state(0, 7, done)], start_step="gate")
    back.tick(choose=False)
    assert (back.tracker.step_id, back.tracker.memory.rejoin_to) == ("boar_in", "gate")
    passing = runtime(tmp_path, [state(0, 7, done), state(1, 7, done)],
                      start_step="recombobulation", completed={218})
    passing.tick(choose=False)
    passing.tick(choose=False)
    assert passing.tracker.step_id == "boar_in"
    assert passing.tracker.memory.rejoin_to == "recombobulation", "and back on its way after"
