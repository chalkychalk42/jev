"""V495: a repair the policy asks for while sick after the Spirit Healer is made at the line it was
asked at. V379 asks for one under 90% durability through the sickness (the get-up's 25%), and the
repair skill mended only under 60% (`REPAIR_ASKED_BELOW`): from 60% to 90% it answered "not
needed", the policy asked again at once, and the hive's characters looped every 0.6 s through
their sickness - 23,245 repairs "not needed" in the 100 minutes to 16:00 on 8 Oct, 8,029 of the
hour's repair reasons "resurrection sickness: repairing meanwhile" (hive-727 2,752 times), the
training the sickness was to be spent on never reached."""

from __future__ import annotations

from types import SimpleNamespace

from test_hunter import _hunter_body, _s

from jev.clients.repair import REPAIR_ASKED_BELOW, Repair, Repaired
from jev.coach.policy import SICK_REPAIR_BELOW


def _repair(durability: float, asked_below: float = REPAIR_ASKED_BELOW) -> Repair:
    values = {"bags.durability_min": durability}
    return Repair(hid=None, read=lambda: values, visit=lambda: False, asked_below=asked_below)


def test_a_repair_asked_under_ninety_while_sick_is_needed_at_seventy_five():
    # A get-up from whole gear leaves 75%: the line V379 asks at, and the one the skill had.
    assert _repair(0.75, SICK_REPAIR_BELOW).needed()
    assert not _repair(0.75).needed(), "not sick: a repair at 75% is not worth the walk"
    assert not _repair(0.95, SICK_REPAIR_BELOW).needed()
    assert _repair(0.5).needed()


def test_the_body_asks_the_repair_at_the_policys_line_sick_or_not(tmp_path):
    b = _hunter_body(tmp_path)
    b.repair = SimpleNamespace(run=lambda: Repaired.NOT_NEEDED, detail="", first_slots=(),
                               asked_below=None)
    b._repairer_yards = lambda: None
    state = _s(durability_min=0.75)
    b.policy_context.sick_until = state.t + 300.0
    b._repair(state)
    assert b.repair.asked_below == SICK_REPAIR_BELOW
    b.policy_context.sick_until = state.t - 1.0           # the sickness over
    b._repair(state)
    assert b.repair.asked_below == REPAIR_ASKED_BELOW
