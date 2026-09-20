"""A skill that cannot be graded cannot be promoted, retired, or trusted."""

from __future__ import annotations

from jev.skills.catalog import BY_NAME, NAMES, Stage, retrievable
from jev.world.state_v1 import State, Target, Ui, Vitals


def test_every_skill_declares_a_timeout():
    """Without one, a stall is indistinguishable from a slow success, forever."""
    for skill in BY_NAME.values():
        assert skill.timeout_s > 0, f"{skill.name} can run forever"


def test_every_skill_has_a_success_predicate_that_runs():
    empty = State(t=0.0, client_id="c")
    for skill in BY_NAME.values():
        assert isinstance(skill.success(empty), bool), f"{skill.name} success is not a predicate"
        assert isinstance(skill.pre(empty), bool), f"{skill.name} pre is not a predicate"


def test_a_retired_skill_is_not_retrievable():
    """A skill that failed sixty percent of the time is worse than no skill: the coach
    keeps choosing it."""
    assert Stage.RETIRED not in {BY_NAME[n].stage for n in retrievable()}
    assert Stage.RETIRED not in {BY_NAME[n].stage for n in retrievable((Stage.STABLE,))}


def test_melee_reach_comes_from_the_client():
    """Inferring reach from damage having landed means the character must hit a mob to
    learn that it can reach one."""
    approach = BY_NAME["APPROACH_TARGET"]
    s = State(t=0.0, client_id="c", target=Target(has=True, in_melee=None))
    assert approach.success(s) is False, "unknown reach is not arrival"
    reached = State(t=0.0, client_id="c", target=Target(has=True, in_melee=True))
    assert approach.success(reached) is True


def test_a_loot_skill_ends_when_the_window_is_gone():
    loot = BY_NAME["LOOT"]
    assert loot.pre(State(t=0.0, client_id="c", ui=Ui(loot=True)))
    assert loot.success(State(t=0.0, client_id="c", ui=Ui(loot=False)))


def test_corpse_recovery_only_applies_to_a_ghost():
    run = BY_NAME["CORPSE_RUN"]
    assert not run.pre(State(t=0.0, client_id="c", vitals=Vitals(ghost=False)))
    assert run.pre(State(t=0.0, client_id="c", vitals=Vitals(ghost=True)))


def test_the_catalog_and_the_verifier_agree():
    from jev.coach.verifier import SOCIAL, TRAVELLING

    for name in (*SOCIAL, *TRAVELLING):
        assert name in NAMES, f"the verifier knows {name} but the catalog does not"
