"""The menu is the executor's own rules, and a reply is exact or it is nothing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jev.play import tutor
from jev.play.actions import action_dict
from jev.play.controller import expected_for
from jev.play.controls import build_manifest
from jev.play.observation import EFFECTS, measured_effects

ALIVE = {"ui.modal": False, "vitals.dead": False, "vitals.ghost": False}
CONTROLS = build_manifest({"char.class_id": 2, "char.race_id": 1}).to_dict()


def names(values, *, skills=(), lookup=False, context=None):
    observation = {"values": {**ALIVE, **values}, "context": context or {}}
    return [c.name for c in tutor.menu(observation, CONTROLS, skills=skills, lookup=lookup)]


def test_a_blocking_dialog_offers_only_observe_and_escape():
    assert names({"ui.modal": True}, skills=("LOOT",), lookup=True) == ["observe", "escape",
                                                                       "lookup"]
    assert names({"ui.modal": None}) == ["observe"], "unknown dialog state authorizes nothing"


def test_a_spell_waiting_for_a_target_offers_only_observe_and_escape():
    """A click now would cast the waiting spell at whatever it lands on."""
    waiting = names({"bars.targeting": True, "target.has": True, "target.hp": 1.0},
                    skills=("COMBAT_PROFILE",))
    assert waiting == ["observe", "escape"]
    assert "attack_target" in names({"bars.targeting": False, "target.has": True,
                                     "target.hp": 1.0})


def test_the_dead_may_only_release_and_the_ghost_may_walk():
    dead = names({"vitals.dead": True}, skills=("RELEASE_SPIRIT", "LOOT"))
    assert dead == ["observe", "skill:RELEASE_SPIRIT"]
    ghost = names({"vitals.ghost": True, "target.has": True, "target.hp": 1.0},
                  skills=("CORPSE_RUN",))
    assert "move_forward" in ghost and "skill:CORPSE_RUN" in ghost
    assert not any(n.startswith(("slot_", "interact", "loot", "attack")) for n in ghost)


def test_living_targets_can_be_attacked_and_corpses_only_looted():
    living = names({"target.has": True, "target.hp": 0.7})
    assert "interact_unit" in living and "attack_target" in living
    assert "loot_corpse" not in living
    corpse = names({"target.has": True, "target.hp": 0.0})
    assert "loot_corpse" in corpse
    assert "interact_unit" not in corpse and "attack_target" not in corpse
    none = names({"target.has": False})
    assert not {"interact_unit", "loot_corpse", "attack_target"} & set(none)


def test_select_needs_an_objective_unit_and_ui_needs_painted_controls():
    assert "select_unit" not in names({})
    assert "select_unit" in names({}, context={"target_name_id": 2864, "target_name": "Young Wolf"})
    assert "quest_advance" not in names({"ui.quest_frame": True})


def test_an_objects_step_offers_a_right_click_on_the_object_not_a_selection():
    crate = {"target_name_id": 777, "target_name": "Milly's Harvest", "target_kind": "gameobject"}
    offered = names({"target.has": False}, context=crate)
    assert "use_object" in offered
    assert not {"select_unit", "select_corpse"} & set(offered)
    seen = {"values": {**ALIVE, "target.has": False}, "context": crate}
    choice = tutor.TutorChoice(observation_id="o", action="use_object", x=0.5, y=0.62)
    action = tutor.to_action(choice, tutor.menu(seen, CONTROLS), seen)
    assert (action.button, action.intent, action.expected_target_id) == ("right", "object", 777)
    assert tutor.describe(action_dict(action)) == "use_object at (0.50, 0.62)"
    assert expected_for(action_dict(action), None, "acquire") == "quest_progress"


def test_tab_is_offered_where_any_enemy_will_do():
    """It verified a selection in 3 of 22 presses on objective steps."""
    worker = {"target_name_id": 2864, "target_name": "Kobold Worker"}
    assert "target_next" not in names({"target.has": False}, context=worker)
    assert "target_next" in names({"target.has": False, "vitals.combat": True}, context=worker)
    assert "target_next" in names({"target.has": False})


def test_escape_is_offered_only_when_there_is_something_to_close():
    """With nothing to close it opens the game menu (run 20260924T012829-382fd4)."""
    assert "escape" not in names({"target.has": False})
    assert "escape" not in names({"target.has": True, "target.hp": 1.0}), \
        "in a fight it raised the TimeManager's blocked-action popup instead"
    assert "escape" in names({"target.has": False, "ui.vendor": True})


def test_an_unselected_corpse_of_the_objective_can_be_selected_for_looting():
    wolf = {"target_name_id": 2864, "target_name": "Young Wolf"}
    assert "select_corpse" in names({"target.has": False}, context=wolf)
    assert "select_corpse" in names({"target.has": True, "target.name_id": 99,
                                     "target.hp": 1.0}, context=wolf)
    assert "select_corpse" not in names({"target.has": True, "target.name_id": 2864,
                                         "target.hp": 0.0}, context=wolf)
    assert "select_corpse" not in names({"target.has": False})
    seen = {"values": {**ALIVE, "target.has": False}, "context": wolf}
    choice = tutor.TutorChoice(observation_id="o", action="select_corpse", x=0.4, y=0.6)
    action = tutor.to_action(choice, tutor.menu(seen, CONTROLS), seen)
    assert action.intent == "select" and action.button == "left"
    assert action.expected_dead is True and action.expected_target_id == 2864
    assert tutor.describe(action_dict(action)) == "select_corpse at (0.40, 0.60)"
    assert "quest_advance" in names({"ui.quest_frame": True, "ui.advance_x": 0.3})
    assert "gossip_line" not in names({"ui.gossip": True})
    assert "gossip_line" in names({"ui.gossip": True, "ui.list_hash1": 777})


def test_a_reward_page_waiting_for_a_choice_offers_the_suggested_reward():
    """Measured 23 September: Complete Quest did nothing until a reward was chosen."""
    page = {"ui.quest_frame": True, "ui.advance_x": 0.1, "ui.choice_count": 2,
            "ui.choice_made": False, "ui.choice_x": 0.05, "ui.choice_y": 0.4}
    assert "quest_reward" in names(page)
    assert "quest_reward" not in names({**page, "ui.choice_made": True})
    assert "quest_reward" not in names({**page, "ui.choice_count": 0})
    assert "quest_reward" not in names({**page, "ui.choice_made": None}), "unknown is not unchosen"
    choice = tutor.TutorChoice(observation_id="o", action="quest_reward")
    action = action_dict(tutor.to_action(choice, tutor.menu(
        {"values": {**ALIVE, **page}, "context": {}}, CONTROLS), {"values": {**ALIVE, **page}}))
    assert action["ui_control"] == "quest_reward"
    assert expected_for(action, None, "interact") == "reward_chosen"
    assert "reward_chosen" in EFFECTS
    before = {"values": {**page}}
    after = {"values": {**page, "ui.choice_made": True}}
    assert "reward_chosen" in measured_effects(before, after)[0]


def test_only_bound_and_usable_slots_are_offered_with_their_abilities():
    offered = {c.name: c for c in tutor.menu({"values": {**ALIVE, "bars.usable": 0b101}},
                                             CONTROLS)}
    assert "slot_1" in offered and "slot_3" in offered and "slot_2" not in offered
    assert "Attack" in offered["slot_1"].meaning and "toggle" in offered["slot_1"].meaning


def test_missing_bindings_are_never_offered():
    unbound = tutor.menu({"values": ALIVE}, {"bindings": {}})
    assert [c.name for c in unbound][:1] == ["observe"]
    assert not any(c.name in tutor.HOLDS or c.name in tutor.TAPS for c in unbound)


@pytest.mark.parametrize("text", [
    '{"observation_id": "o", "action": "observe"}',
    '```json\n{"observation_id": "o", "action": "observe"}\n```',
    '  {"observation_id": "o", "action": "observe", "capability": "habit"}  ',
])
def test_one_json_object_is_read(text):
    assert tutor.parse(text).action == "observe"


@pytest.mark.parametrize("text", [
    'Sure! {"observation_id": "o", "action": "observe"}',
    '{"observation_id": "o", "action": "observe"} and then attack',
    '{"observation_id": "o", "action": "observe", "action": "escape"}',
    '[{"observation_id": "o", "action": "observe"}]',
    '{"observation_id": "o", "action": "observe", "seconds": NaN}',
])
def test_anything_but_one_unambiguous_object_is_rejected(text):
    with pytest.raises((tutor.ChoiceError, ValidationError)):
        tutor.parse(text)


def test_every_choice_maps_to_one_exact_executable_action():
    values = {**ALIVE, "target.has": True, "target.hp": 1.0, "target.name_id": 2864,
              "ui.quest_frame": True, "ui.advance_x": 0.4, "ui.list_hash2": 4242}
    observation = {"values": values, "context": {"target_name_id": 2864,
                                                  "target_name": "Young Wolf"}}
    choices = tutor.menu(observation, CONTROLS, skills=("COMBAT_PROFILE",), lookup=True)
    params = {"seconds": 0.4, "x": 0.5, "y": 0.6, "pixels": 30, "line": 3, "query": "wolf"}
    for choice in choices:
        reply = tutor.TutorChoice(observation_id="o", action=choice.name,
                                  **{p: params[p] for p in choice.required})
        action = tutor.to_action(reply, choices, observation)
        if choice.name == "lookup":
            assert action == "wolf"
            continue
        doc = action_dict(action)
        assert tutor.describe(doc).split(" ")[0] == choice.name.split(" ")[0]
    gossip = tutor.to_action(tutor.TutorChoice(observation_id="o", action="gossip_line", line=3),
                             choices, observation)
    assert gossip.ui_name_id == 4242, "the line's identity comes from the painted text hash"


def test_expected_effects_are_derived_and_judged_by_the_observer():
    assert expected_for({"kind": "key", "control": "turn_left", "duration_s": 0.2}, None,
                        "approach") == "faced"
    assert expected_for({"kind": "key", "control": "turn_left", "duration_s": 0.2}, None,
                        "travel") == "scene_changed"
    assert expected_for({"kind": "key", "control": "attack_target", "duration_s": 0}, None,
                        "combat") == "attacking"
    assert expected_for({"kind": "click", "button": "right", "intent": "interact", "x": 0.5,
                         "y": 0.5, "expected_target_id": 1, "expected_dead": True}, None,
                        "loot") == "loot_received"
    assert expected_for({"kind": "skill", "name": "FACE_TARGET"}, None, "approach") == "faced"
    assert {"faced", "attacking"} <= EFFECTS


def test_faced_and_attacking_are_measured_not_claimed():
    before = {"values": {"bars.attacking": False, "target.has": True, "target.name_id": 1,
                         "target.hp": 1.0},
              "detections": {"selected_plate": {"x": 0.81, "y": 0.4}}}
    after = {"values": {"bars.attacking": True, "target.has": True, "target.name_id": 1,
                        "target.hp": 1.0},
             "detections": {"selected_plate": {"x": 0.52, "y": 0.4}}}
    effects, _ = measured_effects(before, after)
    assert "faced" in effects and "attacking" in effects
    jitter = {**after, "detections": {"selected_plate": {"x": 0.80, "y": 0.4}}}
    assert "faced" not in measured_effects(before, jitter)[0]


def test_the_rendered_prompt_names_the_state_that_decides_the_next_action():
    observation = {"id": "o-1", "size": [1600, 900],
                   "values": {**ALIVE, "target.has": True, "target.hp": 0.0,
                              "target.name_id": 2864, "bars.attacking": False,
                              "ui.error_last": 3},
                   "context": {"target_name_id": 2864, "target_name": "Young Wolf",
                               "skill": "GRIND_UNTIL", "kind": "quest_objective",
                               "quest_id": 33},
                   "state": {"quests": [{"quest_id": 33, "complete": False,
                                         "objectives": [{"have": 2, "need": 8}]}]}}
    choices = tutor.menu(observation, CONTROLS)
    text = tutor.render(observation, choices=choices,
                        knowledge={"current_node": {"title": "Wolves Across the Border",
                                                    "objectives": ["Bring 8 meat."]}})
    assert "DEAD (a corpse)" in text and "Last game error: not_facing" in text
    assert "Quest log progress: 2/8" in text and "auto-attack on: no" in text
    assert "- loot_corpse x 0-1 y 0-1:" in text
    assert '{"observation_id": "o-1", "action": "<one name' in text


def test_a_failed_routine_tells_jev_why_it_stopped():
    observation = {"id": "o-2", "values": ALIVE, "context": {}, "size": [1600, 900]}
    recent = [{"action": {"kind": "skill", "name": "COMBAT_PROFILE", "params": {}},
               "outcome": {"success": False, "effects": ["observed"],
                           "reason": "did not observe target_dead"},
               "delivery": {"code": "delegated", "metadata": {"skill_result": {
                   "outcome": "aborted", "code": "not_visible",
                   "detail": "no plate proved to be the selected unit's after the search turn"}}}}]
    text = tutor.render(observation, choices=tutor.menu(observation, CONTROLS), recent=recent)
    assert ("skill:COMBAT_PROFILE -> did not work (delegated); effects: none; did not observe "
            "target_dead; the routine said: not_visible no plate proved") in text


def test_loot_onto_a_stack_is_received_and_a_meal_is_not():
    corpse = {"target.has": True, "target.name_id": 1, "target.hp": 0, "ui.loot": True,
              "bags.free": 8, "inventory.revision": 40, "bags.food_count": 5}
    before = {"values": corpse}
    stacked = {"values": {**corpse, "ui.loot": False, "inventory.revision": 41}}
    assert "loot_received" in measured_effects(before, stacked)[0]
    meal = {"values": {**corpse, "ui.loot": False, "inventory.revision": 41,
                       "bags.food_count": 4}}
    assert "loot_received" not in measured_effects(before, meal)[0]
