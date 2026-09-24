"""Taking what the corpse is holding, and knowing whether anything came off it."""

from __future__ import annotations

import pytest
from test_fight import _Targeting

from jev.clients.loot import Loot, Looted
from jev.clients.targeting import ClickCode, ClickResult

A_FRAME = object()


class _Hid:
    def __init__(self):
        self.clicks = []
        self.taps = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))
        return True

    def tap(self, key):
        self.taps.append(key)
        return True


def _loot(readings, hid=None, code=ClickCode.CLICKED):
    seq = list(readings)
    state = {"i": 0}

    def read():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    hid = hid or _Hid()
    action = ClickResult(code, (710, 533) if code is ClickCode.CLICKED else None,
                         "observed targeting result", 1)
    skill = Loot(hid=hid, read=read, read_frame=lambda: A_FRAME,
                 window_origin=(10, 38), targeting=_Targeting(read, hid, action=action))
    return skill


HAVE = {"bags.free": 8, "ui.loot": False, "bags.money_silver": 3,
        "target.has": True, "target.hp": 0.0, "target.name_id": 1161}


def test_full_bags_close_a_persisting_loot_frame_before_service(monkeypatch):
    monkeypatch.setattr("jev.clients.loot.time.sleep", lambda _: None)
    hid = _Hid()
    opened = {**HAVE, "bags.free": 0, "ui.loot": True}
    skill = _loot([opened, opened, {**opened, "ui.loot": False}], hid=hid)
    assert skill.run() is Looted.BAGS_FULL
    assert not hid.clicks
    assert hid.taps == ["esc"]


def test_a_full_bag_is_not_clicked_at():
    """Looting into a full bag silently takes nothing, and the fix is a vendor rather
    than another click."""
    hid = _Hid()
    skill = _loot([{**HAVE, "bags.free": 0}], hid=hid)
    assert skill.run() is Looted.BAGS_FULL
    assert hid.clicks == []


def test_nothing_selected_is_not_a_corpse():
    hid = _Hid()
    skill = _loot([{**HAVE, "target.has": False}], hid=hid, code=ClickCode.NO_TARGET)
    assert skill.run() is Looted.NO_CORPSE
    assert hid.clicks == []


def test_loot_delegates_the_selected_corpse_pose_to_shared_targeting():
    hid = _Hid()
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert skill.targeting.requests == [{"expected_name_id": 1161, "anchor": None,
                                        "past_selection": False, "max_probes": 24}]
    assert hid.clicks == [(710, 533, True)]
    assert skill.clicked == (710, 533)


def test_bags_falling_is_what_counts_as_having_looted():
    """Not "we clicked"."""
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert skill.took == 1


def test_a_loot_frame_is_not_a_take():
    """A frame means a corpse was opened, not that anything came out of it. Only auto
    loot makes the two coincide, and that is a client setting this code cannot see: if it
    were ever off, every empty wolf in the zone would report a take."""
    opened = {**HAVE, "ui.loot": True}
    skill = _loot([HAVE, opened, opened, HAVE])
    assert skill.run(settle_s=0) is Looted.NOTHING
    assert skill.took == 0


def test_the_objective_counter_is_believed_before_the_bags():
    """The server's own tally, and the only signal that answers the question actually
    being asked. It is also the one that survives stacking."""
    counts = iter([3, 4])
    have = [3]

    def progress():
        have[0] = next(counts, have[0])
        return (have[0], 8)

    # Bags never move: meat 2 through 8 land on the stack meat 1 made.
    skill = _loot([HAVE, HAVE, HAVE])
    outcome = skill.run(settle_s=1.0, progress=progress)
    assert outcome is Looted.TOOK, "a full stack landed and the bags said nothing"
    assert "objective" in skill.detail


def test_the_last_one_completing_the_objective_is_a_take():
    """Measured 23 September: the eighth Tough Wolf Meat read 7/8 -> 1/1 (the complete
    flag, once the counter stops painting), landed on the existing stack, and was
    reported as "nothing" while the quest log said 8/8."""
    readings = iter([(7, 8), (1, 1)])
    last = [(7, 8)]

    def progress():
        last[0] = next(readings, last[0])
        return last[0]

    skill = _loot([HAVE, HAVE, HAVE])
    assert skill.run(settle_s=1.0, progress=progress) is Looted.TOOK
    assert "complete" in skill.detail


def test_an_objective_that_was_already_complete_is_not_a_take():
    skill = _loot([HAVE, HAVE, HAVE])
    assert skill.run(settle_s=0.3, progress=lambda: (1, 1)) is Looted.NOTHING


def test_money_counts_when_the_bags_cannot_see_it():
    """A copper or two comes off almost everything, and it fills no slot."""
    skill = _loot([HAVE, {**HAVE, "bags.money_silver": 5}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert "2 silver" in skill.detail


def test_stacked_loot_is_not_reported_as_an_empty_corpse():
    """`bags.free` alone is why a collect quest reads as a dry camp."""
    skill = _loot([HAVE, HAVE, HAVE])
    assert skill.run(settle_s=0.8) is Looted.NOTHING, "nothing moved, so nothing took"


def test_an_empty_corpse_is_not_a_failure():
    """A skill that treats an empty wolf as an error retires itself on a good camp."""
    skill = _loot([HAVE])
    outcome = skill.run(settle_s=0.8)
    assert outcome is Looted.NOTHING
    assert outcome.ok, "an empty corpse must not count against the skill"


def test_a_loot_window_that_stays_open_is_closed():
    """Auto-loot usually closes itself; when it does not, a left-open window swallows the
    next click. Escape is pressed against a screen the radio has measured."""
    hid = _Hid()
    open_frame = {**HAVE, "ui.loot": True}
    took = {**open_frame, "bags.free": 7}
    skill = _loot([HAVE, took, took, {**took, "ui.loot": False}], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert hid.taps == ["esc", "esc"], "the window shut, then the looted corpse released"


def test_an_empty_corpse_still_gets_its_window_shut():
    """A left-open window swallows the next click whether or not anything came out."""
    hid = _Hid()
    open_frame = {**HAVE, "ui.loot": True}
    skill = _loot([HAVE, open_frame, open_frame, HAVE], hid=hid)
    assert skill.run(settle_s=0) is Looted.NOTHING
    assert hid.taps == ["esc", "esc"], "the window shut, then the emptied corpse released"


@pytest.mark.parametrize("code, expected", [
    (ClickCode.REFUSED, Looted.REFUSED), (ClickCode.BLIND, Looted.BLIND),
    (ClickCode.INTERRUPTED, Looted.INTERRUPTED),
    (ClickCode.NOT_VISIBLE, Looted.NO_CORPSE), (ClickCode.STALE, Looted.NO_CORPSE),
])
def test_unverified_corpse_input_never_becomes_an_empty_corpse(code, expected):
    skill = _loot([HAVE], code=code)
    assert skill.run(settle_s=0) is expected
    assert not expected.ok
    assert skill.hid.clicks == []
    assert skill.took == 0


def test_lost_radio_after_delivered_input_leaves_outcome_unobserved():
    skill = _loot([HAVE, None])
    assert skill.run(settle_s=1) is Looted.BLIND
    assert skill.hid.clicks == [(710, 533, True)]
    assert skill.took == 0


def test_missing_final_observation_does_not_claim_no_loot_change():
    skill = _loot([HAVE, None])
    assert skill.run(settle_s=0) is Looted.BLIND


def test_a_single_copper_counts_without_rounded_silver_or_bag_change():
    before = {**HAVE, "bags.money_copper": 303}
    after = {**before, "bags.money_copper": 304}
    skill = _loot([before, after])
    assert skill.run(settle_s=1) is Looted.TOOK
    assert skill.detail == "1 copper"


def test_exact_copper_takes_precedence_over_a_rounded_silver_change():
    before = {**HAVE, "bags.money_copper": 399}
    after = {**before, "bags.money_copper": 400, "bags.money_silver": 4}
    skill = _loot([before, after])
    assert skill.run(settle_s=1) is Looted.TOOK
    assert skill.detail == "1 copper"


def test_camera_refusal_prevents_a_corpse_click():
    skill = _loot([HAVE])
    skill.level = lambda: False
    assert skill.run() is Looted.REFUSED
    assert skill.targeting.requests == []


def test_objective_reader_cancellation_propagates_before_input():
    from jev.run.supervisor import Cancelled

    skill = _loot([HAVE])
    def cancelled():
        raise Cancelled("stop requested")
    with pytest.raises(Cancelled, match="stop requested"):
        skill.run(progress=cancelled)
    assert skill.hid.clicks == []


def test_targeting_cancellation_propagates():
    from jev.run.supervisor import Cancelled

    skill = _loot([HAVE])
    def cancelled(**_):
        raise Cancelled("stop requested")
    skill.targeting.click_corpse = cancelled
    with pytest.raises(Cancelled, match="stop requested"):
        skill.run()


def test_final_observation_can_confirm_a_take_after_the_settle_deadline():
    after = {**HAVE, "bags.free": 7}
    skill = _loot([HAVE, after])
    assert skill.run(settle_s=0) is Looted.TOOK
    assert skill.took == 1
    assert skill.detail == "1 bag slot"


@pytest.mark.parametrize("failure", ["refused", "blind", "open"])
def test_observed_take_survives_in_evidence_but_failed_closure_is_propagated(failure):
    taken = {**HAVE, "bags.free": 7, "ui.loot": True}
    skill = _loot([HAVE, taken, taken, None if failure == "blind" else taken])
    if failure == "refused":
        skill.hid.tap = lambda _: False
    expected = {"refused": Looted.REFUSED, "blind": Looted.BLIND, "open": Looted.WINDOW_OPEN}
    assert skill.run(settle_s=0) is expected[failure]
    assert skill.took == 1
    assert "1 bag slot" in skill.detail
    assert not expected[failure].ok


def test_no_change_with_an_unclosed_window_is_not_a_completed_empty_corpse():
    opened = {**HAVE, "ui.loot": True}
    skill = _loot([HAVE, opened])
    assert skill.run(settle_s=0) is Looted.WINDOW_OPEN
    assert skill.took == 0


def test_full_bags_propagate_refused_window_closure_before_service():
    skill = _loot([{**HAVE, "bags.free": 0, "ui.loot": True}])
    skill.hid.tap = lambda _: False
    assert skill.run() is Looted.REFUSED
    assert skill.hid.clicks == []
    assert "bags are full" in skill.detail


def test_the_corpse_search_starts_under_the_last_living_plate():
    from jev.perceive.units import Plate, RingColour

    plate = Plate(640.0, 430.0, 147, RingColour.YELLOW)
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}])
    assert skill.run(settle_s=1.0, anchor=plate) is Looted.TOOK
    assert skill.targeting.requests == [{"expected_name_id": 1161, "anchor": plate,
                                        "past_selection": False, "max_probes": 24}]


def test_an_item_onto_a_stack_is_a_take():
    """Stringy Wolf Meat onto its own stack took no slot and read "nothing" (13 of 24
    loots, runs 20260924T043637 to ...050644); the strip's revision moved."""
    skill = _loot([{**HAVE, "inventory.revision": 40}, {**HAVE, "inventory.revision": 41}])
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert skill.detail == "an item onto a stack"


def test_a_meal_moving_the_revision_is_not_a_take():
    before = {**HAVE, "inventory.revision": 40, "bags.food_count": 5}
    skill = _loot([before, {**before, "inventory.revision": 41, "bags.food_count": 4}])
    assert skill.run(settle_s=0.5) is Looted.NOTHING


def test_a_looted_corpse_is_released_from_the_selection():
    """Left selected, it put the tutor in its loot situation after every kill: 43 tutor
    loot attempts on emptied corpses, none taking anything (sessions 30-50)."""
    hid = _Hid()
    skill = _loot([HAVE, {**HAVE, "bags.free": 7}], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert hid.taps == ["esc"]


def test_nothing_selected_is_not_escaped_into_the_game_menu():
    hid = _Hid()
    gone = {**HAVE, "target.has": False, "target.hp": None, "target.name_id": None}
    skill = _loot([HAVE, {**gone, "bags.free": 7}], hid=hid)
    assert skill.run(settle_s=1.0) is Looted.TOOK
    assert hid.taps == []


def test_a_selection_moved_on_to_a_living_packmate_does_not_hide_the_corpse(monkeypatch):
    """Run 20260924T140621-fc3531: at a kobold's kill the client selected the next Kobold
    Tunneler, and the search refused it as not dead - three corpses in one session."""
    monkeypatch.setattr("jev.clients.loot.time.sleep", lambda _: None)
    moved_on = {**HAVE, "target.hp": 1.0, "target.name_id": 32830}
    skill = _loot([moved_on, {**moved_on, "bags.money_silver": 4}])
    assert skill.run(settle_s=1.0, name_id=32830) is Looted.TOOK
    assert skill.targeting.requests == [{"expected_name_id": 32830, "anchor": None,
                                         "past_selection": True, "max_probes": 24}]


def test_in_combat_the_corpse_search_is_short_and_waits_below_half_health(monkeypatch):
    monkeypatch.setattr("jev.clients.loot.time.sleep", lambda _: None)
    fighting = {**HAVE, "target.hp": 1.0, "vitals.combat": True, "vitals.hp": 0.8}
    skill = _loot([fighting, {**fighting, "bags.money_silver": 4}])
    assert skill.run(settle_s=1.0, name_id=1161) is Looted.TOOK
    assert skill.targeting.requests[0]["max_probes"] == 6
    hurt = {**fighting, "vitals.hp": 0.3}
    skill = _loot([hurt])
    assert skill.run(settle_s=1.0, name_id=1161) is Looted.NO_CORPSE
    assert skill.targeting.requests == [] and "not now" in skill.detail
