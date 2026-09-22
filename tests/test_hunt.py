"""Working an objective: the radius, not the pin, and the server's counter, not ours."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jev.clients.fight import Fought
from jev.clients.loot import Looted
from jev.clients.rest import Rested
from jev.run.hunt import DRY_LOOKS, STATIONS, Hunt, Hunted, stations
from jev.world.state_v1 import StepKind


class _Fight:
    def __init__(self, outcomes, tops_up=False):
        self.tops_up = tops_up
        self.outcomes = list(outcomes)
        self.calls = 0
        self.pressed: list[int] = []
        self.closed = 0
        self.heals_landed = 0
        self.heals_ignored = 0
        self.top_ups = 0
        self.top_ups_landed = 0
        self.topped_up = 0
        self.detail = ""
        self.broken = False

    def top_up(self, *_a, **_k):
        self.topped_up += 1
        return self.tops_up

    def run(self, name_id=None, **_):
        out = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return out


class _Rest:
    def __init__(self, outcome=Rested.HEALTHY):
        self.outcome = outcome
        self.detail = ""
        self.calls = 0

    def until(self, *_a, **_k):
        self.calls += 1
        return self.outcome


def _hunt(outcomes, counts, rest=None, approach=None):
    seq = list(counts)
    state = {"i": 0}

    def progress():
        c = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return c

    walked = []
    return Hunt(fight=_Fight(outcomes), rest=rest or _Rest(), read=lambda: {},
                approach=approach or (lambda p: walked.append(p) or True),
                progress=progress, say=lambda _s: None), walked


def test_stations_work_outward_from_the_node_and_stay_on_the_disk():
    """A node's `r` is a map fraction, and 0.06 of Northshire is two hundred yards — so a
    single ring at two-thirds of it sends the character a hundred and forty yards from a
    camp it was standing next to. Near before far."""
    posts = stations((100.0, 200.0, 50.0), 30.0)
    assert posts[0] == (100.0, 200.0, 50.0)
    assert len(posts) == STATIONS

    def reach(p):
        return ((p[0] - 100.0) ** 2 + (p[1] - 200.0) ** 2) ** 0.5

    assert all(p[2] == 50.0 for p in posts)
    assert all(reach(p) <= 30.0 + 1e-6 for p in posts), "a station left the disk"
    rings = [round(reach(p), 3) for p in posts]
    assert rings == sorted(rings), "it does not search outward"
    assert reach(posts[1]) < 10.0, "the first move is a long way from the node"


def test_full_bags_yield_before_another_pull_without_walking_the_camp():
    hunt, walked = _hunt([Fought.KILLED], [(0, 8)])
    hunt.read = lambda: {"bags.free": 0, "vitals.combat": False}
    assert hunt.run((0, 0, 0), 30) is Hunted.BAGS_FULL
    assert hunt.fight.calls == 0 and walked == []


def test_full_bags_do_not_interrupt_an_existing_fight_or_hide_completion():
    hunt, _ = _hunt([Fought.KILLED], [(0, 1), (1, 1)])
    hunt.read = lambda: {"bags.free": 0, "vitals.combat": True}
    assert hunt.run((0, 0, 0), 30) is Hunted.DONE
    assert hunt.fight.calls == 1
    hunt.read = lambda: {"bags.free": 0, "vitals.combat": False}
    assert hunt.run((0, 0, 0), 30) is Hunted.DONE
    assert hunt.fight.calls == 1


def test_service_handoff_happens_after_looting_the_kill():
    from types import SimpleNamespace

    from jev.clients.loot import Looted

    hunt, _ = _hunt([Fought.KILLED], [(0, 8)])
    events = []
    hunt.loot = SimpleNamespace(run=lambda **kw: events.append("loot") or Looted.NOTHING,
                                detail="empty corpse")
    hunt.service_needed = lambda: "repair needed" if events else None
    assert hunt.run((0, 0, 0), 30) is Hunted.SERVICE_NEEDED
    assert hunt.fight.calls == 1 and events == ["loot"]


def test_the_counter_ends_it_and_nothing_counts_its_own_kills():
    """`quests.o0_have` is the server's tally. A swing that missed, a mob somebody else
    tagged and a kill for a different quest are indistinguishable from the inside."""
    h, _ = _hunt([Fought.KILLED], [(10, 10)])
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DONE
    assert h.fight.calls == 0, "it fought after the objective was already complete"


def test_nothing_to_fight_moves_to_another_station():
    """Standing on the pin and looking harder is how a live run reached 1/10 and reported
    'not visible' twenty times while the camp wandered fifteen yards away."""
    h, walked = _hunt([Fought.NO_TARGET], [(1, 10)])
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=2)
    assert h.moves > 1, "it never left the first station"
    assert len(walked) > 1
    assert h.fight.calls >= DRY_LOOKS


def test_a_station_it_cannot_stand_on_is_not_a_dead_end():
    h, _ = _hunt([Fought.NO_TARGET], [(1, 10)], approach=lambda p: False)
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.UNREACHABLE
    assert h.moves == STATIONS, "it gave up before trying the whole disk"


def test_dying_stops_the_hunt_rather_than_looping():
    h, _ = _hunt([Fought.DIED], [(1, 10)])
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DIED


def test_being_hurt_rests_and_running_out_of_food_stops():
    """Both happen *before* the pull now: heal, then eat, then refuse."""
    h, _ = _hunt([Fought.TOO_HURT], [(1, 10)], rest=_Rest(Rested.HEALTHY))
    h.read = lambda: {"vitals.combat": False, "vitals.hp": 0.3}
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=1)
    assert h.rest.calls >= 1

    h2, _ = _hunt([Fought.TOO_HURT], [(1, 10)], rest=_Rest(Rested.NO_FOOD))
    h2.read = lambda: {"vitals.combat": False, "vitals.hp": 0.3}
    assert h2.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.NO_FOOD


def test_an_interrupted_rest_is_not_a_failure():
    """Interrupted means something is already hitting us; the next pass fights it."""
    h, _ = _hunt([Fought.TOO_HURT, Fought.KILLED], [(1, 10), (2, 10), (10, 10)],
                 rest=_Rest(Rested.INTERRUPTED))
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DONE


# -- the disk is its own field ------------------------------------------------------

def test_the_default_is_not_the_nodes_arrival_radius():
    """`r` is the tracker's arrival slop in *map fractions*. A hunt that borrowed it
    walked a two-hundred-yard disk containing Northshire Abbey, stood in the Main Hall
    facing a wall, and correctly reported that it could not see any kobolds."""
    import pathlib

    from jev.run.hunt import DEFAULT_HUNT_YARDS

    assert 15.0 <= DEFAULT_HUNT_YARDS <= 50.0
    # Neither the engine nor the thing that calls it may reach for the arrival radius.
    import inspect

    from jev.run.body import LiveBody

    # The shared body also validates travel parameters, where node.r is legitimate.
    # Guard the hunt executor itself and the whole hunting engine.
    for code in (pathlib.Path("jev/run/hunt.py").read_text(encoding="utf-8"),
                 inspect.getsource(LiveBody._hunt)):
        assert "node.r" not in code, "hunting is reaching for the arrival radius again"
    assert "hunt_yards" in pathlib.Path("jev/run/body.py").read_text(encoding="utf-8")


def test_the_generated_objective_carries_a_camp_sized_disk():
    from jev.guide.generate import HUNT_MAX_YARDS, HUNT_MIN_YARDS
    from jev.guide.graph import Graph

    g = Graph.load("content/tbc/ally_human_1_12.json")
    node = g.get("alli_human_1_12_7_kobold_camp_cleanup_do")
    assert node.hunt_yards is not None
    assert HUNT_MIN_YARDS <= node.hunt_yards <= HUNT_MAX_YARDS
    assert node.r == 0.06, "r was shrunk to fix the hunt; that breaks arrival instead"

    # Every node that has one is camp-sized, and no giver node grew one.
    for n in g.nodes:
        if n.hunt_yards is not None:
            assert HUNT_MIN_YARDS <= n.hunt_yards <= HUNT_MAX_YARDS, n.id
        if n.kind in (StepKind.QUEST_ACCEPT, StepKind.QUEST_TURNIN):
            assert n.hunt_yards is None, f"{n.id} is a point you stand at"


def test_a_station_indoors_is_skipped_when_the_camp_is_not():
    """`pos.indoors` is a bit the strip already paints. This is not knowledge about the
    Abbey; it is one flag saying a station cannot be the camp."""
    h, _ = _hunt([Fought.NO_TARGET], [(1, 10)])
    h.read = lambda: {"pos.indoors": False}
    assert h._wrong_side_of_a_door() is False, "the first station sets which kind it is"
    h.read = lambda: {"pos.indoors": True}
    assert h._wrong_side_of_a_door() is True


def test_an_indoors_camp_does_not_skip_its_own_stations():
    """Some objectives are in a mine. The first station decides."""
    h, _ = _hunt([Fought.NO_TARGET], [(1, 10)])
    h.read = lambda: {"pos.indoors": True}
    assert h._wrong_side_of_a_door() is False
    assert h._wrong_side_of_a_door() is False


def test_printed_lines_stay_ascii():
    """The probe's log is read with grep, and one cp1252 em-dash makes the whole file
    look like a binary to it — every `until grep -q` monitor silently matched nothing."""
    import pathlib

    for path in (pathlib.Path("jev/run/hunt.py"), pathlib.Path("tools/probe_slice.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "print(" in line or 'f"  ' in line:
                assert line.isascii(), f"{path}:{i} prints non-ascii"


def test_healing_is_tried_before_food_and_food_only_when_it_is_not_enough():
    """A heal costs mana and a few seconds; food costs twenty. Order matters, and so
    does not eating when a heal already got us there."""
    h, _ = _hunt([Fought.KILLED], [(1, 10), (10, 10)], rest=_Rest(Rested.HEALTHY))
    h.read = lambda: {"vitals.combat": False, "vitals.hp": 0.3}
    h.fight.tops_up = True
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5)
    assert h.fight.topped_up >= 1
    assert h.rest.calls == 0, "ate when a heal had already got us there"

    h2, _ = _hunt([Fought.KILLED], [(1, 10), (10, 10)], rest=_Rest(Rested.HEALTHY))
    h2.read = lambda: {"vitals.combat": False, "vitals.hp": 0.3}
    h2.fight.tops_up = False
    h2.run((0.0, 0.0, 0.0), 30.0, timeout_s=5)
    assert h2.rest.calls >= 1, "healing failed and it never reached for food"


def test_health_is_restored_before_the_pull_not_after_selected_outcomes():
    """Topping up only after a kill or a break-off meant a `timeout` fell through the
    dry-look branch and the next mob was pulled at whatever health the last fight left.
    A live run reported `top up 0` and died without killing anything."""
    h, _ = _hunt([Fought.TIMEOUT, Fought.KILLED], [(1, 10), (1, 10), (10, 10)])
    h.read = lambda: {"vitals.combat": False, "vitals.hp": 0.3}
    h.fight.tops_up = True
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5)
    assert h.fight.calls >= 1
    assert h.fight.topped_up >= h.fight.calls, "pulled without topping up first"


def test_a_pull_is_refused_when_nothing_can_restore_health():
    """HP below the line, no heal and no food is not a pull. It is a corpse run."""
    h, _ = _hunt([Fought.KILLED], [(1, 10)], rest=_Rest(Rested.NO_FOOD))
    h.read = lambda: {"vitals.combat": False, "vitals.hp": 0.2}
    h.fight.tops_up = False
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.NO_FOOD
    assert h.fight.calls == 0, "pulled anyway"


def test_being_in_combat_is_not_a_moment_to_heal_up_first():
    h, _ = _hunt([Fought.KILLED], [(1, 10), (10, 10)])
    h.read = lambda: {"vitals.combat": True, "vitals.hp": 0.2}
    h.fight.tops_up = False
    h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5)
    assert h.fight.calls >= 1, "stood there healing while something was hitting us"


def test_an_item_objective_is_placed_where_the_drop_lives():
    """"Bring me eight of these" is still work that happens somewhere, and the somewhere
    is wherever the thing that drops it lives. Quest 33 wants Tough Wolf Meat and its
    node was placed on Eagan Peltskinner with a fifteen yard disk, because nothing looked
    past ReqCreatureOrGOId1 - the bot hunted the man who wanted the wolves."""
    from jev.guide.graph import Graph

    g = Graph.load("content/tbc/ally_human_1_12.json")
    node = g.get("alli_human_1_12_33_wolves_across_the_border_do")
    assert node is not None and node.pos is not None, "the objective has no position"
    assert "wolf" in (node.notes or "").lower(), f"placed on {node.notes!r}"

    giver = g.get("alli_human_1_12_33_wolves_across_the_border_accept")
    assert node.pos != giver.pos, "the objective is still on the quest giver"


def test_a_drop_source_outside_the_zones_in_scope_is_not_used():
    """`Ragged Young Wolf` lives in several zones, and the globally densest pack is
    nowhere near the quest - the first version put the node at (-6326, 380), off every
    map in scope, while the giver stood outside Northshire Abbey."""
    from jev.guide.coords import on_map
    from jev.guide.graph import Graph

    g = Graph.load("content/tbc/ally_human_1_12.json")
    for n in g.nodes:
        if n.kind is StepKind.QUEST_OBJECTIVE and n.pos is not None:
            assert on_map(*n.pos, slack=0.0), f"{n.id} is off the map at {n.pos}"


def test_a_ghost_stops_the_hunt_instead_of_reporting_a_camp_problem():
    """A ghost cannot fight, heal or eat, and every skill below reports something that
    sounds like a camp problem instead. A live run died to the wolves and then spent the
    rest of its window saying `no_target` ten times over, because the death check lived
    inside the fight loop - which a ghost never reaches, since acquiring fails first."""
    h, _ = _hunt([Fought.KILLED], [(1, 10)])
    h.read = lambda: {"vitals.ghost": True, "vitals.combat": False, "vitals.hp": 0.01}
    assert h.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DIED
    assert h.fight.calls == 0, "sent a ghost to fight something"

    h2, _ = _hunt([Fought.KILLED], [(1, 10)])
    h2.read = lambda: {"vitals.dead": True, "vitals.combat": False, "vitals.hp": 0.0}
    assert h2.run((0.0, 0.0, 0.0), 30.0, timeout_s=5) is Hunted.DIED


@pytest.mark.parametrize("outcome, expected", [
    (Fought.REFUSED, Hunted.REFUSED), (Fought.BLIND, Hunted.BLIND),
    (Fought.INTERRUPTED, Hunted.INTERRUPTED),
])
def test_terminal_fight_evidence_stops_the_hunt_before_another_station(outcome, expected):
    hunt, walked = _hunt([outcome], [(0, 8)])
    hunt.fight.detail = "shared body verification stopped"
    hunt.loot = SimpleNamespace(run=lambda **_: pytest.fail("loot after failed fight"))
    assert hunt.run((0, 0, 0), 30) is expected
    assert hunt.fight.calls == 1
    assert len(walked) == 1
    assert hunt.detail == "shared body verification stopped"


@pytest.mark.parametrize("outcome, expected", [
    (Looted.REFUSED, Hunted.REFUSED), (Looted.BLIND, Hunted.BLIND),
    (Looted.INTERRUPTED, Hunted.INTERRUPTED), (Looted.BAGS_FULL, Hunted.BAGS_FULL),
    (Looted.WINDOW_OPEN, Hunted.WINDOW_OPEN),
])
def test_terminal_loot_result_survives_a_successful_kill(outcome, expected):
    hunt, walked = _hunt([Fought.KILLED], [(0, 1), (1, 1)])
    attempts = []
    hunt.loot = SimpleNamespace(run=lambda **_: attempts.append(1) or outcome,
                                detail="corpse outcome requires attention")
    assert hunt.run((0, 0, 0), 30) is expected
    assert hunt.kills == hunt.fight.calls == len(attempts) == len(walked) == 1
    assert hunt.detail == "loot: corpse outcome requires attention"


@pytest.mark.parametrize("outcome", [Looted.TOOK, Looted.NOTHING])
def test_observed_loot_results_allow_the_next_objective_check(outcome):
    hunt, _ = _hunt([Fought.KILLED], [(0, 1), (1, 1)])
    hunt.loot = SimpleNamespace(run=lambda **_: outcome, detail="observed")
    assert hunt.run((0, 0, 0), 30) is Hunted.DONE
    assert hunt.fight.calls == 1
