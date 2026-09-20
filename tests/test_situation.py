"""Two states share a key exactly when the same answer is correct for both."""

from __future__ import annotations

from jev.coach.situation import SITUATION_VERSION, digest, situation_key, with_key
from jev.world.state_v1 import Bags, GuidePos, State, Vitals


def test_irrelevant_detail_does_not_split_the_bucket(state: State):
    """Position drift and a few percent of health are the same situation."""
    moved = state.model_copy(update={
        "t": state.t + 5,
        "pos": state.pos.model_copy(update={"mx": 0.48, "my": 0.63}),
        "vitals": state.vitals.model_copy(update={"hp": 0.86}),
    })
    assert situation_key(moved) == situation_key(state)


def test_a_changed_answer_changes_the_bucket(state: State):
    stuck = state.model_copy(update={
        "guide": state.guide.model_copy(update={"age_s": 500.0, "deaths_on_step": 3}),
    })
    assert situation_key(stuck) != situation_key(state)


def test_being_blind_is_its_own_situation(state: State):
    """A decision made without the radio is not the same decision made with it."""
    blind = state.model_copy(update={"sense": state.sense.model_copy(update={"addon_ok": False})})
    seeing = state.model_copy(update={"sense": state.sense.model_copy(update={"addon_ok": True})})
    assert situation_key(blind) != situation_key(seeing)


def test_health_bands_not_numbers(state: State):
    def at(hp):
        return situation_key(state.model_copy(update={"vitals": Vitals(hp=hp, combat=False)}))

    assert at(0.95) == at(0.70), "both are simply fine"
    assert at(0.95) != at(0.30), "hurt is a different situation"
    assert at(0.30) != at(0.10), "critical is a different situation again"


def test_unknown_bags_are_not_full_bags(state: State):
    unknown = state.model_copy(update={"bags": Bags(free=None)})
    full = state.model_copy(update={"bags": Bags(free=0)})
    assert situation_key(unknown) != situation_key(full)


def test_the_version_travels_inside_the_key(state: State):
    assert situation_key(state).startswith(f"v{SITUATION_VERSION}|")


def test_with_key_attaches_the_key_to_the_state(state: State):
    assert state.situation_key is None
    assert with_key(state).situation_key == situation_key(state)


def test_digest_is_stable_and_fixed_width(state: State):
    k = situation_key(state)
    assert digest(k) == digest(k)
    assert len(digest(k)) == 12


def test_a_missing_playhead_still_produces_a_key():
    s = State(t=0.0, client_id="c", guide=GuidePos())
    assert situation_key(s), "a state with no step must still bucket, not raise"
