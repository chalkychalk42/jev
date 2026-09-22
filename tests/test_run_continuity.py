"""The real hunt/body/runtime handoff, with physical actions replaced by observations."""

from types import SimpleNamespace

from test_live_body import body
from test_runtime_records import seen

from jev.clients.fight import Fought
from jev.clients.loot import Looted
from jev.clients.repair import Repaired
from jev.learn.episode import Recorder, read
from jev.orch.runtime import ClientRuntime
from jev.run.supervisor import Supervisor
from jev.world.state_v1 import Bags, Objective, Quest, StepKind


def test_hunt_loots_repairs_and_resumes_the_same_objective(tmp_path):
    live = body(StepKind.QUEST_OBJECTIVE)
    events = []

    class World:
        t = 0
        have = 0
        durability = 1.0

        def read(self):
            quest = Quest(quest_id=1, objectives=(Objective(text="kill", have=self.have, need=2),),
                          complete=self.have == 2)
            return seen(self.t, quests=(quest,),
                        bags=Bags(free=8, durability_min=self.durability, money_copper=100))

        def values(self):
            return {"vitals.hp": 1.0, "vitals.combat": False, "bags.free": 8,
                    "bags.durability_min": self.durability}

        def quest_ids(self, **kwargs):
            live.client.log.complete = self.read().quests
            return (1,)

        def fight(self, *args, **kwargs):
            self.have += 1
            if self.have == 1:
                self.durability = 0.1
            events.append("fight")
            return Fought.KILLED

        def loot(self, **kwargs):
            events.append("loot")
            return Looted.NOTHING

        def repair(self):
            events.append("repair")
            self.durability = 1.0
            return Repaired.DONE

    world = World()
    live.client.read = world.values
    live.client.state = world.read
    live.client.quest_ids = world.quest_ids
    live.fight = SimpleNamespace(run=world.fight, top_up=lambda: True, detail="",
                                 pressed=[], closed=0, heals_landed=0, heals_ignored=0,
                                 broken=False)
    live.loot = SimpleNamespace(run=world.loot, detail="empty corpse")
    live.repair = SimpleNamespace(run=world.repair, detail="durability restored")
    recorder = Recorder(tmp_path)
    runtime = ClientRuntime("c", live.graph, world, recorder)
    supervisor = Supervisor(runtime, live, say=lambda _: None)
    try:
        for i in range(8):
            world.t = i * 0.5
            supervisor.step(i * 0.5)
            if supervisor.worker is not None:
                assert supervisor.worker.done.wait(1)
            if runtime.finished:
                break
        assert events == ["fight", "loot", "repair", "fight", "loot"]
        assert runtime.finished and not supervisor.failure
        assert runtime.tracker.step_id == "quest"
        assert runtime.tracker.memory.attempts == 0
        assert live.policy_context is runtime.policy_context
    finally:
        supervisor.close()
        recorder.close()
    results = read(recorder.dir / "skills.jsonl")
    assert [(r["skill"], r["outcome"]) for r in results] == [
        ("GRIND_UNTIL", "preempted"), ("VENDOR_REPAIR", "succeeded"),
        ("GRIND_UNTIL", "succeeded"),
    ]
    assert len({r["decision_id"] for r in results}) == 3
    assert all(r["step_id"] == "quest" for r in results)
