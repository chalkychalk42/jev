"""Shared production runtime -> worker -> observed state -> recorded ticks.

Only the physical game is replaced. The fixture changes state in response to executed
skills; a replay that advances regardless of which skill ran would not test this seam.
"""
from jev.guide.graph import Graph, Node
from jev.learn.episode import Recorder, SkillOutcome, read
from jev.orch.runtime import ClientRuntime
from jev.run.supervisor import Result, Supervisor
from jev.world.state_v1 import (
    Char,
    Objective,
    Pos,
    Quest,
    Sense,
    Source,
    State,
    StepKind,
    Ui,
    Vitals,
)


class World:
    def __init__(self):
        self.t = -0.25
        self.quest = None
        self.executed = []

    def read(self):
        self.t += 0.25
        return State(t=self.t, client_id="fixture", char=Char(level=1, xp_pct=0),
                     pos=Pos(mx=0.5, my=0.5, zone="zone"),
                     vitals=Vitals(hp=1, power=1, combat=False, dead=False, ghost=False),
                     ui=Ui(modal=False), quests=() if self.quest is None else (self.quest,),
                     sense=Sense(addon_ok=True, source=Source.SYNTHETIC))

    def close(self):
        pass


class Body:
    available = frozenset({"ACCEPT_QUEST", "GRIND_UNTIL", "TURNIN_QUEST", "IDLE"})
    travelling = False

    def __init__(self, world):
        self.world = world
        self.releases = 0

    def execute(self, arm, state, checkpoint):
        checkpoint()
        skill = arm.decision.skill
        self.world.executed.append(skill)
        if skill == "ACCEPT_QUEST":
            assert self.world.quest is None
            self.world.quest = Quest(quest_id=7, objectives=(Objective(text="kill", have=0, need=10),))
        elif skill == "GRIND_UNTIL":
            assert self.world.quest is not None
            self.world.quest = Quest(quest_id=7, objectives=(Objective(text="kill", have=10, need=10),))
        elif skill == "TURNIN_QUEST":
            assert self.world.quest.objectives[0].done
            self.world.quest = None
        else:
            raise AssertionError(f"unexpected skill {skill}")
        return Result(SkillOutcome.SUCCEEDED, "observed fixture change", "done")

    def release(self):
        self.releases += 1


def test_a_recorded_slice_advances_only_by_observed_change(tmp_path):
    nodes = tuple(Node(id=step, kind=kind, zone="zone", zone_id=1, pos=(0.5, 0.5),
                       quest_id=7, skills=(skill,), next=(next_step,) if next_step else ())
                  for step, kind, skill, next_step in (
                      ("accept", StepKind.QUEST_ACCEPT, "ACCEPT_QUEST", "objective"),
                      ("objective", StepKind.QUEST_OBJECTIVE, "GRIND_UNTIL", "turnin"),
                      ("turnin", StepKind.QUEST_TURNIN, "TURNIN_QUEST", None)))
    graph = Graph(graph_id="fixture", entry="accept", nodes=nodes, faction="alliance")
    world = World()
    recorder = Recorder(tmp_path)
    runtime = ClientRuntime("fixture", graph, world, recorder)
    body = Body(world)
    supervisor = Supervisor(runtime, body, say=lambda message: None)
    try:
        for i in range(300):
            supervisor.step(i / 4)
            if supervisor.worker is not None:
                assert supervisor.worker.done.wait(1)
        assert runtime.finished and runtime.completed == {7}
        assert runtime.counters.advances == 3
        assert world.executed == ["ACCEPT_QUEST", "GRIND_UNTIL", "TURNIN_QUEST"]
        assert body.releases == 3
    finally:
        supervisor.close()
        recorder.close()
    ticks = read(recorder.dir / "ticks.jsonl")
    assert all(t["client_id"] == t["state"]["client_id"] == "fixture" for t in ticks)
    assert {t["state"]["guide"]["step_id"] for t in ticks} == {"accept", "objective", "turnin"}
    assert runtime.counters.escalated == 0
