"""The interact skill, and the caps that are the point of having rewritten it.

The previous version grew a method per failure — preflight, approach, leave_range,
turn_to, find_on_screen, locate, a click offset and a fourteen-point sweep — each a patch
on the last one's symptom. These tests are mostly about what is *absent*.
"""

from __future__ import annotations

import inspect

from jev.clients.hid import Hid
from jev.clients.interact import CENTRED_PX, Interact, Result
from jev.guide.coords import ZoneBounds

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)


def _interact(values=None, frame=None):
    return Interact(hid=Hid(hwnd=1), bounds=ELWYNN, read=lambda: values,
                    read_frame=lambda: frame, read_pos=lambda: (0.5, 0.5),
                    window_centre=(800, 450))


# --- the caps ---------------------------------------------------------------

def test_the_command_is_typed_once():
    """Retyping hid a focus problem behind a wall of identical chat lines, and spammed
    the player's own screen doing it."""
    body = inspect.getsource(Interact._target)
    assert body.count("slash(") == 1
    assert "for " not in body and "while " not in body


def test_there_is_no_search_only_one_look():
    """Nothing visible is a failure, not a reason to spin. An earlier version turned
    twenty-two steps looking for a unit and then gave up anyway."""
    body = inspect.getsource(Interact.open_on)
    assert "find_on_screen" not in body
    assert body.count("self._look()") <= 3, "more than a look, a centre and a recheck"


def test_exactly_one_yaw_and_it_always_happens():
    """The locator reports a unit's position relative to the **camera**; walking is
    relative to the **character**, and in this client those are different headings. A
    keyboard turn brings the camera round behind the character, so the yaw is what makes
    "centred on screen" mean "in front of me". Measured: a unit dead centre, requiring no
    correction, walked at for twelve yards and never reached."""
    body = inspect.getsource(Interact._yaw_toward)
    assert "for " not in body and "while " not in body, "the yaw is a loop again"

    caller = inspect.getsource(Interact.open_on)
    assert caller.count("self._yaw_toward(") == 1
    assert "CENTRED_PX" not in caller, "the yaw is conditional again"


def test_exactly_one_click():
    body = inspect.getsource(Interact.open_on)
    assert body.count("self.hid.click(") == 1


def test_the_dead_machinery_is_gone():
    """Named individually because each one was a patch that outlived its problem."""
    for gone in ("CLICK_OFFSET", "turn_to", "_leave_range", "preflight", "OFFSETS",
                 "_recent_heading", "approach"):
        assert not hasattr(Interact, gone), f"{gone} survived the rewrite"


# --- what it will and will not do -------------------------------------------

def test_the_duel_range_flag_gates_nothing():
    """`in_melee` is CheckInteractDistance index 3 — about eleven yards — and an NPC
    talks at roughly five. Believing it is how a character stood well back and
    right-clicked a model it could see and could not reach."""
    src = inspect.getsource(Interact)
    body = src.split('"""')[-1]
    assert "in_melee" not in body, "the skill still keys off duel range"


def test_arrival_is_the_clients_collision():
    body = inspect.getsource(Interact._walk_in)
    assert "distance_yards" in body and "still_since" in body
    assert "in_melee" not in body


def test_the_walk_does_not_consult_the_locator():
    """The locator is a snapshot, not a homing missile. The walk changes pitch, distance
    and which pixels the ring occupies, so requiring a fresh sighting every frame fails on
    the first one that blinks — and the miss tolerances that follow are the beginning of
    the sprawl this module was rewritten to remove. Aim once, then walk."""
    # The code, not the prose — the docstring explains why it does not re-find.
    code = inspect.getsource(Interact._walk_in).split('"""')[-1]
    assert "_look" not in code and "find(" not in code


def test_the_locator_is_used_exactly_at_the_ends():
    """Once to aim, once after the yaw, once on arrival. Not in between."""
    src = inspect.getsource(Interact)
    assert src.count("self._look()") == 3


def test_nothing_visible_is_a_clean_failure():
    """A guessed pixel is worse than an honest refusal."""
    result = _interact(values={"target.has": True, "target.name_id": None}, frame=None)
    assert result.open_on("Deputy Willem") in (Result.NO_TARGET, Result.NOT_VISIBLE)
    assert result.clicked is None, "it clicked without seeing anything"


def test_an_unfocused_window_is_reported_not_retried():
    inter = _interact(values=None)
    assert inter.open_on("Deputy Willem") is Result.NO_TARGET
    assert "focus" in inter.detail


def test_every_opened_window_counts_as_opened():
    """Gossip, quest, vendor and loot are all 'it worked' — the skill is not quest-giver
    specific, and neither is the locator under it."""
    assert all(r.opened for r in (Result.GOSSIP, Result.QUEST, Result.VENDOR, Result.LOOT))
    assert not any(r.opened for r in (Result.NO_TARGET, Result.NOT_VISIBLE,
                                      Result.NO_WINDOW, Result.BLIND))


def test_centring_is_about_the_screen_edge_not_precision():
    """The click point comes from the locator, so centring only stops a click landing on
    a model clipped by the edge of the screen."""
    assert CENTRED_PX > 200


def test_stopping_against_a_fence_is_not_a_reason_to_click():
    """`in_melee` cannot answer "can I gossip" — it is duel range, about eleven yards —
    but it is reliable as a **negative**: not within eleven yards means we certainly did
    not walk into the target. A live run stopped against a wooden fence in Northshire with
    Willem back in the courtyard, and clicked the screen centre hopefully."""
    assert Result.APPROACH_FAILED in set(Result)
    assert not Result.APPROACH_FAILED.opened

    body = inspect.getsource(Interact.open_on)
    assert "APPROACH_FAILED" in body
    assert body.index("in_melee") < body.index("used_centre"), "it clicks before checking"


def test_the_walk_is_bounded_by_distance_not_only_time():
    """A unit six yards away when it was aimed at is not twenty seconds of walking away.
    A live run walked the full timeout and ended a hundred yards from where it started —
    the aim was wrong, and every further step made it worse."""
    sig = inspect.signature(Interact._walk_in)
    assert "max_yards" in sig.parameters
    body = inspect.getsource(Interact._walk_in).split('"""')[-1]
    assert "max_yards" in body and "walked_too_far" in body
