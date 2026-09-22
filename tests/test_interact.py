"""The interact skill, and the caps that are the point of having rewritten it.

The previous version grew a method per failure — preflight, approach, leave_range,
turn_to, find_on_screen, locate, a click offset and a fourteen-point sweep — each a patch
on the last one's symptom. These tests are mostly about what is *absent*.
"""

from __future__ import annotations

import inspect
import pathlib
from unittest.mock import Mock

import pytest

from jev.clients.hid import Hid
from jev.clients.interact import (
    AT_NODE_YARDS,
    MAX_CANDIDATES,
    Interact,
    Result,
)
from jev.guide.coords import ZoneBounds

ELWYNN = ZoneBounds(area_id=12, map_id=0, left=1535.4166, right=-1935.4166,
                    top=-7939.583, bottom=-10254.166)


def _interact(values=None, frame=None):
    return Interact(hid=Hid(hwnd=1), bounds=ELWYNN, read=lambda: values,
                    read_frame=lambda: frame, read_pos=lambda: (0.5, 0.5),
                    window_centre=(800, 450))


# --- the caps ---------------------------------------------------------------

def test_the_bot_never_types():
    """A bot that can talk is a bot that can say the wrong thing, and it did: with Caps
    Lock on — machine state nothing here could see — `/target Marshal McBride` left the
    client as `?target mARSHAL mCbRIDE`, out loud in Northshire. Every keystroke
    "succeeded", so there was nothing to detect. Right-clicking a nameplate does the same
    job with no keyboard at all."""
    # Everything below the module docstring: the docstring names `/target` on purpose,
    # to say why it is gone and stop it coming back.
    import jev.clients.interact as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    code = src[src.index("from __future__"):]
    for talking in ("slash(", "type_text(", "/target"):
        assert talking not in code, f"{talking}: this skill is typing again"
    assert not hasattr(Interact, "_target"), "the chat-targeting method came back"


def test_there_is_exactly_one_look():
    """Nothing visible is a failure, not a reason to spin. An earlier version turned
    twenty-two steps looking for a unit and then gave up anyway."""
    body = inspect.getsource(Interact.open_on)
    assert "find_on_screen" not in body
    assert body.count("self._candidates()") == 1


def test_one_click_per_candidate_and_no_more():
    """A bounded handful, ordered by how central they are — not a sweep. Each click is
    answered by the radio before the next one is considered."""
    assert MAX_CANDIDATES <= 3
    # Two per candidate: one to select, one to interact. They are different actions on
    # different points and neither is a guess — see `_try`.
    assert inspect.getsource(Interact._try).count("self.hid.click(") == 2
    # open_on itself presses nothing: every click belongs to a candidate that the radio
    # then has to confirm.
    assert inspect.getsource(Interact.open_on).count("self.hid.click(") == 0
    assert inspect.getsource(Interact._try_centre).count("self.hid.click(") == 1


def test_the_dead_machinery_is_gone():
    """Named individually because each one was a patch that outlived its problem."""
    for gone in ("CLICK_OFFSET", "turn_to", "_leave_range", "preflight", "OFFSETS",
                 "_recent_heading", "_walk_in", "_yaw_toward"):
        assert not hasattr(Interact, gone), f"{gone} survived the rewrite"

    # `approach` is a *field* now, not a method this skill implements — the distinction
    # is the point of the rewrite.
    import dataclasses

    assert "approach" in {f.name for f in dataclasses.fields(Interact)}


# --- what it will and will not do -------------------------------------------


def test_this_skill_does_not_walk():
    """`W` follows character facing; `find()` reports camera pixels. A turn computed from
    a pixel error does not point the body at anything, so the walk went twelve yards in
    the wrong direction and stopped — repeatedly, with every fix a better pulse for a
    problem that was not the pulse.

    The mesh knows how to stand somewhere. The locator knows where to click. Mixing them
    is why a nine-yard blind walk found a fence the planner had already routed around.
    """
    for gone in ("_walk_in", "_yaw_toward"):
        assert not hasattr(Interact, gone), f"{gone} is back"

    body = inspect.getsource(Interact.open_on)
    for walker in ("key_down", 'hold("w"', 'hold("a"', 'hold("d"'):
        assert walker not in body, f"open_on moves the character via {walker}"


def test_the_approach_is_injected_not_built():
    """The planner belongs to the guide layer. This skill has no business knowing about
    navmeshes, and a skill that builds its own is a skill that grows one."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(Interact)}
    assert "approach" in names
    src = inspect.getsource(Interact)
    assert "MmapQuery" not in src and "Travel" not in src


def test_a_failed_approach_is_not_a_locator_problem():
    """If the mesh cannot stand us on the NPC that is a recorded-route question (V5), and
    the skill says so instead of clicking at where it wishes the unit were."""
    inter = Interact(hid=Hid(hwnd=1), bounds=ELWYNN, read=lambda: {"target.has": True,
                     "target.name_id": None}, read_frame=lambda: None,
                     read_pos=lambda: (0.5, 0.5), window_centre=(800, 450),
                     approach=lambda _world: False)
    assert inter.open_on("Deputy Willem", node_world=(1.0, 2.0, 3.0)) in (
        Result.APPROACH_FAILED, Result.NO_TARGET)
    assert inter.clicked is None


def test_nothing_visible_is_a_clean_failure():
    """A guessed pixel is worse than an honest refusal."""
    result = _interact(values={"target.has": True, "target.name_id": None}, frame=None)
    assert result.open_on("Deputy Willem") in (Result.NO_TARGET, Result.NOT_VISIBLE)
    assert result.clicked is None, "it clicked without seeing anything"


def test_identity_is_confirmed_from_the_radio_after_the_click():
    """The click orders candidates; it does not identify them. Only the radio can say who
    is actually selected, and a plate that turns out to be somebody else is a closed
    window and the next candidate."""
    body = inspect.getsource(Interact._try)
    assert "target.name_id" in body
    assert "painted != wanted" in body
    # The identity check sits *between* the two clicks: nothing is interacted with until
    # the radio has said who is selected, so a wrong plate costs a selection, not an
    # action.
    select, _, interact = body.partition('painted != wanted')
    assert select.count("self.hid.click(") == 1 and interact.count("self.hid.click(") == 1


def test_every_opened_window_counts_as_opened():
    """Gossip, quest, vendor and loot are all 'it worked' — the skill is not quest-giver
    specific, and neither is the locator under it."""
    assert all(r.opened for r in (Result.GOSSIP, Result.QUEST, Result.VENDOR, Result.LOOT))
    assert not any(r.opened for r in (Result.NO_TARGET, Result.NOT_VISIBLE,
                                      Result.NO_WINDOW, Result.BLIND))


def test_being_underfoot_is_the_only_excuse_for_a_centre_click():
    """Under about four yards the player model occludes the selection ring. That is a
    click-time fact, not a walk-time one: arrive with the mesh, and if the ring is hidden
    because the unit is underfoot, one centre click is honest. Anywhere else, fail."""
    assert AT_NODE_YARDS <= 8.0
    body = inspect.getsource(Interact.open_on)
    assert "self._at(node_map)" in body, "the centre click is not gated on being there"
    # And it is identity-checked like every other click, not trusted because we arrived.
    assert "painted != wanted" in inspect.getsource(Interact._try_centre)


def test_confirmed_repair_plate_leads_to_a_body_click_on_the_live_frame(monkeypatch):
    import numpy as np

    import jev.clients.interact as module
    from jev.perceive.radio_frame import name_id
    from jev.perceive.units import Plate, RingColour

    frame = np.load(pathlib.Path(__file__).parent / "fixtures/live-dermot-targeted.npz")["frame"]
    clicks = []

    class Device:
        def click(self, x, y, right=False):
            clicks.append((x, y, right))
            return True

    def painted():
        return {"target.name_id": name_id("Dermot Johns"),
                "ui.vendor": bool(clicks and clicks[-1] == (1007, 486, True))}

    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    inter = _interact(frame=frame)
    inter.hid, inter.read = Device(), painted
    plate = Plate(cx=996, cy=422, w=145, colour=RingColour.GREEN)
    assert inter._try(plate, name_id("Dermot Johns")) is Result.VENDOR
    assert clicks == [(996, 422, False), (1007, 486, True)]


@pytest.mark.parametrize("near", [True, False])
def test_selected_but_occluded_unit_keeps_the_existing_underfoot_fallback(near):
    inter = _interact()
    inter._close_open_window = lambda: None
    inter._candidates = lambda: [object(), object()]
    inter._try = Mock(return_value=Result.NOT_VISIBLE)
    inter._try_centre = Mock(return_value=Result.VENDOR)
    point = (0.5, 0.5) if near else (0.9, 0.9)
    result = inter.open_on("Supplier", node_map=point)
    assert result is (Result.VENDOR if near else Result.NOT_VISIBLE)
    assert inter._try.call_count == 1
    assert inter._try_centre.call_count == int(near)


def test_capture_failure_after_selection_never_uses_the_underfoot_fallback(monkeypatch):
    import jev.clients.interact as module
    from jev.perceive.radio_frame import name_id
    from jev.perceive.units import Plate, RingColour

    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    inter = _interact(values={"target.name_id": name_id("Supplier")}, frame=None)
    inter.hid = Mock()
    inter._candidates = lambda: [Plate(cx=800, cy=350, w=145, colour=RingColour.GREEN)]
    inter._try_centre = Mock()
    assert inter.open_on("Supplier", node_map=(0.5, 0.5)) is Result.BLIND
    inter.hid.click.assert_called_once_with(800, 350)
    inter._try_centre.assert_not_called()
