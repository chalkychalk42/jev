"""Unknown is not a negative fact — the property the rest of the system assumes."""

from __future__ import annotations

from jev.world.state_v1 import Flags, State, Target, is_false, is_true


def test_unobserved_flags_are_neither_true_nor_false():
    f = Flags()
    assert f.mounted is None
    assert not is_true(f.mounted), "unknown must not read as yes"
    assert not is_false(f.mounted), "unknown must not read as no"


def test_present_omits_unknowns_rather_than_denying_them():
    f = Flags(mounted=True, swimming=False)
    assert f.present() == ["MOUNTED"]
    assert "SWIMMING" not in f.present()
    assert "FALLING" in f.unknown(), "a flag nobody looked at should be reportable as such"


def test_a_target_nobody_read_is_not_an_absent_target():
    assert Target().has is None
    assert Target(has=False).has is False


def test_state_is_immutable(state: State):
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        state.vitals.hp = 0.1


def test_state_round_trips_through_json(state: State):
    assert State.model_validate_json(state.model_dump_json()) == state


def test_objective_counts_flatten_every_quest():
    from jev.world.state_v1 import Objective, Quest

    s = State(
        t=0.0, client_id="c",
        quests=(
            Quest(quest_id=1, objectives=(Objective(text="a", have=3, need=10),)),
            Quest(quest_id=2, objectives=(Objective(text="b", have=1, need=1),)),
        ),
    )
    assert s.objective_counts() == [(3, 10), (1, 1)]
