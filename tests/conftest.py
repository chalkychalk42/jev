from __future__ import annotations

import pytest

from jev.world.state_v1 import Bags, GuidePos, Pos, State, StepKind, Vitals


@pytest.fixture
def state() -> State:
    """A plain mid-run state: alive, on route, nothing wrong."""
    return State(
        t=1000.0,
        client_id="c01",
        pos=Pos(zone="Elwynn", zone_id=12, mx=0.47, my=0.62, facing=0.0),
        vitals=Vitals(hp=0.9, combat=False, dead=False, ghost=False),
        bags=Bags(free=8, durability_min=0.9, money_copper=1840),
        guide=GuidePos(
            graph_id="g1", step_id="s1", kind=StepKind.QUEST_OBJECTIVE,
            age_s=30.0, on_route=True, progress=0.2,
        ),
    )
