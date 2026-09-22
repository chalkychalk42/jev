"""Run the brain with no game.

    python -m jev.clients.sim --ticks 200

Synthesises a character walking the generated spine: arriving at steps, accepting quests,
killing things, occasionally dying, occasionally going blind. The tracker, coach,
verifier, recorder and counters are all the real ones — only perception is fake.

This exists because the alternative is developing the brain only at the machine with the
client open, and because a bug that first appears forty minutes into a live run is
unfixable if the only way to reach it is forty minutes of live running.
"""

from __future__ import annotations

import argparse
import random

from jev.clients.source import blind
from jev.guide.graph import Graph, Node
from jev.learn.episode import Recorder
from jev.orch.runtime import ClientRuntime
from jev.world.state_v1 import (
    Bags,
    Char,
    Objective,
    Pos,
    Quest,
    Reaction,
    Sense,
    SenseFault,
    State,
    Target,
    Ui,
    Vitals,
)
from jev.world.state_v1 import (
    Source as StateSource,
)


class Pretend:
    """A character that mostly does the right thing, and sometimes does not.

    The failures are the point. A simulator where nothing ever goes wrong exercises only
    the happy path, which is the one part of this system that was never in doubt.
    """

    def __init__(self, graph: Graph, seed: int = 0, trouble: float = 0.08) -> None:
        self.graph = graph
        self.rng = random.Random(seed)
        self.trouble = trouble
        self.t = 0.0
        self.level = 1
        self.xp = 0.0
        self.hp = 1.0
        self.dead_for = 0
        self.progress: dict[int, int] = {}
        self.accepted: set[int] = set()
        self.node: Node = graph.get(graph.entry)

    def follow(self, step_id: str | None) -> None:
        node = self.graph.get(step_id) if step_id else None
        if node is not None:
            self.node = node

    def read(self) -> State:
        self.t += 0.5
        r = self.rng.random()

        # Perception outages happen, and the coach has rules for them.
        if r < self.trouble / 3:
            return blind(self.t, "sim", SenseFault.CHECKSUM, StateSource.SYNTHETIC)

        if self.dead_for > 0:
            self.dead_for -= 1
            return self._state(dead=True)

        if r < self.trouble:
            self.dead_for = self.rng.randint(4, 12)
            self.hp = 1.0
            return self._state(dead=True)

        node = self.node
        qid = node.quest_id
        combat = False

        if qid is not None:
            if node.kind.value == "quest_accept":
                self.accepted.add(qid)
            elif node.kind.value == "quest_objective":
                # Grind the counter up, taking damage on the way.
                self.progress[qid] = min(10, self.progress.get(qid, 0) + self.rng.randint(0, 2))
                combat = self.progress[qid] < 10
                self.hp = max(0.25, self.hp - (0.05 if combat else -0.15))
                self.xp += 0.02
            elif node.kind.value == "quest_turnin" and self.progress.get(qid, 0) >= 10:
                self.accepted.discard(qid)
                self.xp += 0.25

        if node.kind.value == "grind":
            # Grinding earns experience. Omitting this made the first simulated run look
            # like a rib bug — the character sat on a boar for four hundred ticks — when
            # the rib was working and the simulated world simply never paid it.
            combat = True
            self.xp += 0.06
            self.hp = max(0.3, self.hp - 0.04)

        if self.xp >= 1.0:
            self.level += 1
            self.xp -= 1.0
        self.hp = min(1.0, self.hp + 0.05)
        return self._state(combat=combat)

    def _state(self, *, dead: bool = False, combat: bool = False) -> State:
        n = self.node
        quests = tuple(
            Quest(quest_id=q, title=f"q{q}", complete=self.progress.get(q, 0) >= 10,
                  objectives=(Objective(text="do it", have=self.progress.get(q, 0),
                                        need=10, counter_index=0),))
            for q in sorted(self.accepted)
        )
        return State(
            t=self.t, client_id="sim",
            char=Char(name="Sim", cls="mage", faction="alliance",
                      level=self.level, xp_pct=min(0.99, self.xp)),
            pos=Pos(zone=n.zone, zone_id=n.zone_id, coord_zone_id=n.coord_zone_id,
                    mx=n.pos[0] if n.pos else 0.5, my=n.pos[1] if n.pos else 0.5,
                    facing=0.0, indoors=False),
            vitals=Vitals(hp=0.0 if dead else self.hp, power=0.8,
                          dead=dead, ghost=False, combat=combat),
            bags=Bags(free=8, durability_min=0.9, money_copper=self.level * 400),
            ui=Ui(loot=False, modal=False, gossip=False, vendor=False),
            quests=quests,
            # A character in combat has something selected. Omitting this made every
            # combat tick look like an unresolvable one.
            target=Target(has=True, name="Kobold Vermin", level=self.level,
                          hp=max(0.0, 1.0 - self.rng.random()),
                          reaction=Reaction.HOSTILE, attacking_me=True,
                          in_melee=True) if combat else Target(has=False),
            sense=Sense(addon_ok=True, vision_conf=1.0, source=StateSource.SYNTHETIC),
        )

    def close(self) -> None:
        return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph", default="content/tbc/ally_human_1_12.json")
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--trouble", type=float, default=0.08,
                    help="probability per tick of something going wrong")
    ap.add_argument("--runs", default="runs")
    args = ap.parse_args()

    graph = Graph.load(args.graph)
    pretend = Pretend(graph, seed=args.seed, trouble=args.trouble)
    recorder = Recorder(root=args.runs)

    rt = ClientRuntime(client_id="sim", graph=graph, source=pretend, recorder=recorder)

    print(f"{graph.graph_id}: {len(graph.nodes)} nodes, entry {graph.entry}")
    for _ in range(args.ticks):
        rt.tick()
        pretend.follow(rt.tracker.step_id)

    c = rt.counters
    print(f"\n{c.ticks} ticks, level {pretend.level}, step {rt.tracker.step_id}")
    print(f"  advances        {c.advances}")
    print(f"  fails           {c.fails}")
    print(f"  deaths          {c.deaths}")
    print(f"  off route       {c.off_route}")
    print(f"  blind ticks     {c.blind_ticks}")
    print(f"  rejected plans  {c.rejected}")
    print(f"  unresolved      {c.unresolved}   <- the number that must fall")
    print(f"  escalated       {c.escalated}   (no teacher attached)")
    print(f"\nrecorded to {recorder.dir}")
    recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
