"""Outcome labels use the real radio wire keys and typed quest-log contracts."""

import hashlib
import io
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev.guide.graph import Graph, Node
from jev.perceive import radio, radio_frame
from jev.perceive.fields import SCHEMA
from jev.perceive.radio_frame import name_id
from jev.play.controller import finished
from jev.play.observation import (
    SCALE_BORDER_PX,
    LiveObserver,
    capability,
    judge,
    measured_effects,
)
from jev.world.state_v1 import Objective, Quest, StepKind

WOLF = name_id("Young Wolf")
OTHER = name_id("Kobold Vermin")


def view(seq=10, *, values=None, quests=None, context=None):
    raw = {"schema": SCHEMA, "seq": seq, "vitals.dead": False, "vitals.ghost": False,
           "vitals.combat": False, "vitals.hp": 1.0, "vitals.power": 0.5,
           "vitals.power_type": 0, "target.has": True, "target.name_id": WOLF,
           "target.hp": 1.0, "target.in_melee": False, "target.attacking_me": False,
           "pos.zone_id": 12080, "pos.mx": 0.45, "pos.my": 0.4,
           "bags.free": 10, "bags.money_copper": 100, "bags.durability_min": 0.5,
           "ui.loot": False, "ui.gossip": False, "ui.vendor": False,
           "ui.quest_frame": False, "ui.modal": False, **(values or {})}
    # Do not let invented field names or enum encodings make a passing fake test.
    decoded = radio.unpack(radio.pack(raw))
    assert set(raw) <= set(decoded)
    return {"id": f"observation-{seq}", "captured_at": 100 + seq / 10,
            "values": decoded, "state": {"quests": quests}, "features": {},
            "context": {"skill": "GRIND_UNTIL", "target_name_id": WOLF,
                        "quest_id": 33, **(context or {})}, "synthetic": True}


def quest(have=0, *, complete=False, counter_index=0, need=8):
    return Quest(quest_id=33, complete=complete,
                 objectives=(Objective(text="Tough Wolf Meat", have=have, need=need,
                                       counter_index=counter_index),)).model_dump(mode="json")


def outcome(before, after, expected="target_hp_decreased", *, action=None, delivered=True):
    return judge(before, after, expected_effect=expected, delivered=delivered,
                 action=action or {"kind": "action_slot", "slot": 1})


def test_disappearance_at_low_health_is_not_a_kill():
    before = view(values={"target.hp": 0.01})
    after = view(11, values={"target.has": False, "target.hp": None, "target.name_id": None})
    result = outcome(before, after, "target_dead")
    assert result["verified"] and not result["success"]
    assert "target_cleared" in result["effects"] and "target_dead" not in result["effects"]


def test_exact_observed_zero_after_health_drop_is_death():
    before = view(values={"target.hp": 0.01})
    after = view(11, values={"target.hp": 0})
    assert outcome(before, after, "target_dead")["success"]


def test_a_fight_that_selected_its_own_kill_is_a_death():
    """Measured 23 September: a delegated COMBAT_PROFILE killed a Kobold Worker (XP 60,
    slain 1/10) with nothing selected before it, and was judged a failure."""
    nothing = {"target.has": False, "target.hp": None, "target.name_id": None}
    before = view(values={**nothing, "char.xp_pct": 0.20}, quests=[quest(0)])
    corpse = view(11, values={"target.hp": 0, "char.xp_pct": 0.25}, quests=[quest(0)])
    assert outcome(before, corpse, "target_dead")["success"]
    counted = view(12, values={**nothing, "char.xp_pct": 0.25}, quests=[quest(1)])
    assert outcome(before, counted, "target_dead")["success"]
    # Experience alone (exploration, a hand-in) with nothing selected is not a kill.
    explored = view(13, values={**nothing, "char.xp_pct": 0.25}, quests=[quest(0)])
    assert "target_dead" not in outcome(before, explored, "target_dead")["effects"]
    # A corpse that was already there, with no experience, is not a new kill either.
    stale = view(14, values={"target.hp": 0, "char.xp_pct": 0.20}, quests=[quest(0)])
    assert "target_dead" not in outcome(before, stale, "target_dead")["effects"]


@pytest.mark.parametrize("previous,current", [(0.4, 0.4), (None, 0.4), (0.4, 0.6)])
def test_initial_injury_unread_health_or_healing_is_not_new_damage(previous, current):
    result = outcome(view(values={"target.hp": previous}), view(11, values={"target.hp": current}))
    assert "target_hp_decreased" not in result["effects"] and not result["success"]


def test_new_damage_requires_same_observed_target_identity():
    before = view()
    after = view(11, values={"target.hp": 0.7})
    assert outcome(before, after)["success"]
    after["values"]["target.name_id"] = OTHER
    assert not outcome(before, after)["success"]
    assert "target_hp_decreased" not in outcome(before, after)["effects"]


def test_newly_selected_injured_target_is_not_damage_caused_by_selection():
    before = view(values={"target.has": False, "target.name_id": None, "target.hp": None})
    after = view(11, values={"target.hp": 0.4})
    assert not outcome(before, after)["success"]
    assert outcome(before, after, "selected", action={"kind": "key", "control": "target_next"})["success"]


def test_a_kill_whose_selection_moves_on_is_target_dead():
    """The client can move the selection on at the kill itself, in the paint the
    experience arrives (run 20260923T233909-8b1484)."""
    fight = {"kind": "skill", "name": "COMBAT_PROFILE"}
    before = view(values={"target.hp": 0.2, "vitals.combat": True, "char.xp_pct": 0.4})
    after = view(11, values={"target.name_id": WOLF + 1, "target.hp": 1.0,
                             "vitals.combat": True, "char.xp_pct": 0.45})
    assert "target_dead" in outcome(before, after, "target_dead", action=fight)["effects"]
    no_experience = view(11, values={"target.name_id": WOLF + 1, "target.hp": 1.0,
                                     "vitals.combat": True, "char.xp_pct": 0.4})
    assert "target_dead" not in outcome(before, no_experience, "target_dead",
                                        action=fight)["effects"]


def test_tab_from_a_corpse_to_a_living_unit_of_the_same_name_is_a_selection():
    """After a kill the corpse stays selected. Tab to the next living kobold keeps the
    name, and a name-only test never credited it (run 20260923T191946-2b79ed); a corpse
    cannot get up within one action, so dead to alive is another unit."""
    tab = {"kind": "key", "control": "target_next"}
    corpse = view(values={"target.hp": 0.0})
    assert outcome(corpse, view(11, values={"target.hp": 1.0}), "selected", action=tab)["success"]
    assert not outcome(corpse, view(11, values={"target.hp": 0.0}), "selected",
                       action=tab)["success"], "still the corpse"
    assert not outcome(view(), view(11), "selected", action=tab)["success"], \
        "the same living unit, or another just like it, proves nothing"


@pytest.mark.parametrize("action", [{"kind": "key", "control": "target_next"},
                                   {"kind": "key", "control": "target_previous"},
                                   {"kind": "click", "intent": "select"}])
def test_same_name_selection_cannot_prove_continuous_damage_identity(action):
    result = outcome(view(), view(11, values={"target.hp": 0.2}), action=action)
    assert not result["success"] and not result["verified"]


def test_same_paint_sequence_is_unknown_even_with_later_capture_time():
    before = view()
    after = view(10, values={"target.hp": 0.1})
    after["captured_at"] += 2
    result = outcome(before, after)
    assert not result["verified"] and not result["success"] and not result["effects"]


def test_sequence_wrap_can_still_observe_a_new_effect():
    before = view(254)
    after = view(0, values={"target.hp": 0.5})
    after["captured_at"] = before["captured_at"] + 0.2
    assert outcome(before, after)["success"]


def test_continuously_observed_paint_generation_survives_long_skill_sequence_wrap():
    from jev.run.client import Client

    client = Client(hwnd=0, hid=None, cap=None, origin=(0, 0), size=(1600, 900))
    client._note_seq(10)
    before = view(10)
    before["freshness"] = {"paint_generation": client._paint_generation}
    # Travel/service primitives keep using the same client's reader through a full
    # wire sequence wrap. Identical endpoint byte does not erase those observations.
    for seq in [*range(11, 255), None, *range(11)]:
        client._note_seq(seq)
    after = view(10, values={"target.hp": 0.5})
    after["captured_at"] = before["captured_at"] + 26
    after["freshness"] = {"paint_generation": client._paint_generation}
    assert outcome(before, after)["success"]


@pytest.mark.parametrize("after", [None, view(9), view(11, values={"seq": None}),
                                    view(11, values={"vitals.dead": None})])
def test_missing_old_or_incomplete_post_observation_never_becomes_success(after):
    result = outcome(view(), after, "observed")
    assert not result["verified"] and not result["success"]


def test_refused_delivery_cannot_claim_observed_damage_success():
    result = outcome(view(), view(11, values={"target.hp": 0.5}), delivered=False)
    assert not result["success"]


def test_map_zone_change_is_not_motion_and_motion_is_not_closer():
    before = view()
    after = view(11, values={"pos.zone_id": 12081, "pos.mx": 0.46})
    effects, _ = measured_effects(before, after)
    assert "moved" not in effects and "closer" not in effects
    after["values"]["pos.zone_id"] = before["values"]["pos.zone_id"]
    effects, _ = measured_effects(before, after)
    assert "moved" in effects and "closer" not in effects


def test_changed_declared_coordinate_frame_is_not_motion():
    before, after = view(), view(11, values={"pos.mx": 0.46})
    before["values"]["pos.coord_zone_id"] = 12
    after["values"]["pos.coord_zone_id"] = 9
    assert "moved" not in measured_effects(before, after)[0]


def test_arrival_requires_coordinates_in_the_declared_destination_frame():
    before = view(context={"destination": [0.5, 0.5], "arrival_radius": 0.01,
                           "coord_zone_id": 12})
    after = view(11, values={"pos.mx": 0.5, "pos.my": 0.5})
    before["values"]["pos.coord_zone_id"] = 12
    after["values"]["pos.coord_zone_id"] = 9
    assert "arrived" not in measured_effects(before, after)[0]


def test_only_an_observed_broad_range_transition_proves_closer():
    assert outcome(view(), view(11, values={"target.in_melee": True}), "closer")["success"]
    before = view(values={"target.in_melee": None})
    assert not outcome(before, view(11, values={"target.in_melee": True}), "closer")["success"]


def test_quest_counter_gain_and_positive_completion_are_independent_evidence():
    before = view(quests=[quest(0)])
    after = view(11, quests=[quest(1)])
    assert outcome(before, after, "quest_progress")["success"]
    after = view(11, quests=[quest(0, complete=True)])
    assert outcome(before, after, "quest_progress")["success"]
    after = view(11, quests=None)
    assert not outcome(before, after, "quest_progress")["success"]


def test_unknown_counter_indices_cannot_create_progress_from_unchanged_counts():
    ambiguous = Quest(quest_id=33, complete=False, objectives=(
        Objective(text="first", have=4, need=8),
        Objective(text="second", have=1, need=8))).model_dump(mode="json")
    before = view(quests=[ambiguous])
    after = view(11, quests=[deepcopy(ambiguous)])
    effects, progress = measured_effects(before, after)
    assert "quest_progress" not in effects and progress == 0


def test_mismatched_counter_requirements_do_not_join():
    result = outcome(view(quests=[quest(1)]), view(11, quests=[quest(4, need=10)]), "quest_progress")
    assert not result["success"]


def test_quest_disappearance_alone_is_not_turnin():
    before = view(quests=[quest(8, complete=True)])
    after = view(11, quests=[])
    assert not outcome(before, after, "quest_cleared")["success"]
    after["values"]["bags.money_copper"] += 10
    assert outcome(before, after, "quest_cleared")["success"]


def test_death_prevents_success_even_if_damage_was_observed():
    result = outcome(view(), view(11, values={"vitals.dead": True, "target.hp": 0.5}))
    assert result["fatal"] and not result["success"]


def test_deliberate_release_is_recovery_progress_not_an_accidental_death():
    before = view(values={"vitals.dead": True})
    after = view(11, values={"vitals.dead": True, "vitals.ghost": True})
    result = outcome(before, after, "released", action={"kind": "skill", "name": "RELEASE_SPIRIT"})
    assert result["success"] and not result["fatal"]


@pytest.mark.parametrize("values,context,want", [
    ({"target.has": False}, {}, "acquire"),
    ({"target.name_id": OTHER}, {}, "acquire"),
    ({}, {}, "approach"),
    ({"vitals.combat": True}, {}, "combat"),
    ({"target.attacking_me": True}, {}, "combat"),
    ({"target.hp": 0}, {}, "loot"),
    ({}, {"skill": "CORPSE_RUN"}, "recover"),
    ({}, {"skill": "TRAVEL_TO"}, "travel"),
    ({}, {"skill": "ACCEPT_QUEST"}, "interact"),
    ({}, {"skill": "BUY_AMMO_REAGENT_FOOD"}, "service"),
    ({}, {"skill": "EAT_DRINK"}, "rest"),
])
def test_capabilities_are_shared_state_categories(values, context, want):
    assert capability(view(values=values, context=context)) == want


def test_full_health_does_not_finish_rest_with_empty_mana():
    before = view(context={"skill": "EAT_DRINK"})
    current = view(11, context={"skill": "EAT_DRINK"},
                   values={"vitals.hp": 1.0, "vitals.power_type": 0, "vitals.power": 0.0})
    assert not finished(before, current)
    current["values"]["vitals.power"] = 0.9
    assert finished(before, current)


@pytest.mark.parametrize("cash", [54, 100, 150])
def test_closed_shop_service_measures_stable_supply_gain_without_claiming_purchase(cash):
    context = {"skill": "BUY_AMMO_REAGENT_FOOD"}
    before = view(values={"bags.drink_id": 159, "bags.drink_count": 0}, context=context)
    after = view(11, values={"bags.drink_id": 159, "bags.drink_count": 10,
                            "bags.money_copper": cash}, context=context)
    effects, _ = measured_effects(before, after)
    assert "supplies_replenished" in effects and "supplies_bought" not in effects
    assert finished(before, after)
    assert outcome(before, after, "supplies_replenished",
                   action={"kind": "skill", "name": "BUY_AMMO_REAGENT_FOOD"})["success"]


@pytest.mark.parametrize("before_id,after_id,before_count,after_count", [
    (159, 117, 0, 10), (None, 159, 0, 10), (159, None, 0, 10),
    (0, 0, 0, 10), (159, 159, None, 10), (159, 159, 0, None),
    (159, 159, 10, 10), (159, 159, 10, 5),
])
def test_supply_replenishment_requires_same_known_item_and_positive_count_delta(
        before_id, after_id, before_count, after_count):
    before = view(values={"bags.drink_id": before_id, "bags.drink_count": before_count},
                  context={"skill": "BUY_AMMO_REAGENT_FOOD"})
    after = view(11, values={"bags.drink_id": after_id, "bags.drink_count": after_count})
    assert "supplies_replenished" not in measured_effects(before, after)[0]
    assert not finished(before, after)


def test_purchase_attribution_needs_stable_item_gain_open_shop_and_observed_spend():
    before = view(values={"ui.vendor": True, "bags.food_id": 117, "bags.food_count": 0})
    after = view(11, values={"bags.food_id": 117, "bags.food_count": 5, "bags.money_copper": 75})
    effects, _ = measured_effects(before, after)
    assert "supplies_bought" in effects and "supplies_replenished" in effects
    after["values"]["bags.money_copper"] = 100
    assert "supplies_bought" not in measured_effects(before, after)[0]


def test_owned_observation_uses_one_real_frame_for_pixels_and_radio():
    import numpy as np
    from PIL import Image

    path = (Path(__file__).parent / "fixtures" / "target-localization-evaluation"
            / "000099-20260922T182233.847933Z.png")
    pixels = np.asarray(Image.open(path).convert("RGB"))
    height, width = pixels.shape[:2]
    frame = SimpleNamespace(rgb=pixels, origin=(1821, 35), size=(width, height))
    captured, sequences, retained = [], [], []

    def grab():
        captured.append(True)
        return frame

    def record_frame(label, owned, *, captured_at):
        retained.append((label, owned, captured_at))
        return {"status": "ok", "file": "frame.png"}

    def note_seq(seq):
        sequences.append(seq)
        client._paint_generation += 1

    client = SimpleNamespace(
        _capturing=threading.RLock(), cap=SimpleNamespace(grab=grab),
        _note_seq=note_seq, _paint_generation=0, frozen_for=lambda: 0.0,
        log=SimpleNamespace(observe=lambda values: None),
        state_from=lambda reading, captured_at: radio_frame.to_state(
            reading, t=captured_at, client_id="offline-fixture"),
        _navigation_values=lambda values: values, origin=frame.origin, size=frame.size)
    node = Node(id="wolves", kind=StepKind.QUEST_OBJECTIVE, zone="Northshire", zone_id=9,
                target_name="Young Wolf", quest_id=33)
    graph = Graph(graph_id="fixture", faction="alliance", nodes=(node,), entry="wolves")
    arm = SimpleNamespace(step_id="wolves", arm_id="offline", decision=SimpleNamespace(
        skill="GRIND_UNTIL", goal="collect wolf meat", params={}))
    observer = LiveObserver(client, graph, screenshots=SimpleNamespace(
        directory=path.parent, record_frame=record_frame))
    observed = observer.observe(arm)
    assert len(captured) == len(retained) == len(sequences) == 1
    assert observed.data["values"]["seq"] == sequences[0]
    assert observed.data["values"]["target.name_id"] == WOLF
    assert observed.data["screen"]["sha256"] == hashlib.sha256(observed.png).hexdigest()
    # The tutor's copy is the frame itself but for the scale along its edges.
    border = SCALE_BORDER_PX + 12
    tutor_copy = np.asarray(Image.open(io.BytesIO(observed.png)))
    assert tutor_copy.shape == pixels.shape
    assert np.array_equal(tutor_copy[border:-border, border:-border],
                          pixels[border:-border, border:-border])
    assert not np.array_equal(tutor_copy, pixels), "the scale is drawn"
    assert retained[0][1] is pixels
    assert retained[0][2] == observed.data["captured_at"]


def test_another_unit_of_the_same_name_and_health_is_a_new_selection():
    """Clicking a second Kobold Worker while one is selected was never credited: same name,
    both at full health. The strip's GUID tells them apart (schema 14)."""
    worker = {"target.has": True, "target.name_id": 7, "target.hp": 1.0}
    before = {"values": {**worker, "target.guid": 101}, "context": {"target_name_id": 7}}
    after = {"values": {**worker, "target.guid": 202}}
    assert "selected" in measured_effects(before, after)[0]
    same = {"values": {**worker, "target.guid": 101}}
    assert "selected" not in measured_effects(before, same)[0]
