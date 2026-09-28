"""A level gate on a guide's spine grinds where the guide says until its level (V297)."""

from __future__ import annotations

from jev.clients.source import ScriptedSource
from jev.coach import policy
from jev.guide.graph import Graph, Node
from jev.guide.route import compile_route
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.skills.catalog import NAMES
from jev.world.state_v1 import Bags, Char, Pos, Sense, State, StepKind, Ui, Vitals


def _graph() -> Graph:
    gate = Node(id="gate_8", kind=StepKind.DING_GATE, zone="Durotar", zone_id=14,
                level=(7, 8), pos=(0.5, 0.5), world=(100.0, 100.0, 10.0), map_id=1,
                coord_zone_id=14, r=0.06, hunt_yards=40.0, target_name="Durotar Tiger",
                target_kind="creature", skills=("TRAVEL_TO", "GRIND_UNTIL"),
                timeout_s=3600.0, next=("after",))
    after = Node(id="after", kind=StepKind.TRAVEL, zone="Durotar", zone_id=14,
                 level=(8, 9), pos=(0.6, 0.6), world=(200.0, 200.0, 10.0), map_id=1,
                 coord_zone_id=14, skills=("TRAVEL_TO",))
    return Graph(graph_id="gated", faction="horde", entry="gate_8", nodes=(gate, after),
                 coord_zone_id=14)


def _at(level: int, t: float = 0.0) -> State:
    return State(t=t, client_id="c01", char=Char(level=level, cls="warrior"),
                 pos=Pos(zone="Durotar", zone_id=14, coord_zone_id=14, mx=0.5, my=0.5),
                 vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                 bags=Bags(free=10, durability_min=1.0, money_copper=0),
                 ui=Ui(loot=False, modal=False), sense=Sense(addon_ok=True, vision_conf=1.0))


def test_a_gate_below_its_level_grinds_to_it(tmp_path):
    graph = _graph()
    runtime = ClientRuntime(client_id="c01", graph=graph, source=ScriptedSource([_at(7)] * 3),
                            recorder=Recorder(root=tmp_path))
    runtime.run(ticks=1, period_s=0)
    decision = runtime.armed.decision
    assert decision.skill == "GRIND_UNTIL" and decision.params["until_level"] == 8
    assert policy._guide(_at(7), graph.get("gate_8")).decision.skill == "GRIND_UNTIL"


def test_a_gate_already_reached_is_passed(tmp_path):
    runtime = ClientRuntime(client_id="c01", graph=_graph(), source=ScriptedSource([_at(9)] * 3),
                            recorder=Recorder(root=tmp_path))
    runtime.run(ticks=2, period_s=0)
    assert runtime.tracker.step_id == "after"


def test_a_gate_is_kept_on_the_supported_route():
    compiled = compile_route(_graph(), available_skills=NAMES).graph
    assert [n.id for n in compiled.nodes] == ["gate_8", "after"]


def _spine() -> Graph:
    def accept(qid, band, nxt):
        return Node(id=f"a{qid}", kind=StepKind.QUEST_ACCEPT, zone="Durotar", zone_id=14,
                    level=band, pos=(0.5, 0.5), world=(1.0, 1.0, 1.0), map_id=1,
                    coord_zone_id=14, quest_id=qid, title=f"q{qid}", target_name="Gornek",
                    target_kind="creature", skills=("TRAVEL_TO", "ACCEPT_QUEST"), next=nxt)
    gate = Node(id="gate", kind=StepKind.DING_GATE, zone="Durotar", zone_id=14, level=(9, 10),
                pos=(0.5, 0.5), world=(1.0, 1.0, 1.0), map_id=1, coord_zone_id=14,
                target_name="Durotar Tiger", target_kind="creature",
                skills=("TRAVEL_TO", "GRIND_UNTIL"), next=("a3",))
    nodes = (accept(1, (1, 4), ("a2",)), accept(2, (5, 8), ("gate",)), gate,
             accept(3, (10, 14), ()))
    return Graph(graph_id="spine", faction="horde", entry="a1", nodes=nodes, coord_zone_id=14)


def test_a_character_new_to_a_guide_begins_where_its_level_is():
    from jev.run.cli import level_start

    graph = _spine()
    assert level_start(graph, 1) is None            # the entry, as before
    assert level_start(graph, 7) == "a2"            # quest 1 (band to 4) is grey to it
    assert level_start(graph, 8) == "a2"            # a quest to 8 still pays at 8
    assert level_start(graph, 9) == "gate"          # the gate to 10 is still above it
    assert level_start(graph, 10) == "a3"
    assert level_start(graph, 30) is None and level_start(graph, None) is None
