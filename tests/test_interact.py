"""The interact skill, and the caps that are the point of having rewritten it.

The previous version grew a method per failure — preflight, approach, leave_range,
turn_to, find_on_screen, locate, a click offset and a fourteen-point sweep — each a patch
on the last one's symptom. These tests are mostly about what is *absent*.
"""

from __future__ import annotations

import inspect

from jev.clients.hid import Hid
from jev.clients.interact import AT_NODE_YARDS, Interact, Result
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


def test_there_is_exactly_one_look():
    """Nothing visible is a failure, not a reason to spin. An earlier version turned
    twenty-two steps looking for a unit and then gave up anyway."""
    body = inspect.getsource(Interact.open_on)
    assert "find_on_screen" not in body
    assert body.count("self._look()") == 1



def test_exactly_one_click():
    body = inspect.getsource(Interact.open_on)
    assert body.count("self.hid.click(") == 1


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


def test_being_underfoot_is_the_only_excuse_for_a_centre_click():
    """Under about four yards the player model occludes the selection ring. That is a
    click-time fact, not a walk-time one: arrive with the mesh, and if the ring is hidden
    because the unit is underfoot, one centre click is honest. Anywhere else, fail."""
    assert AT_NODE_YARDS <= 8.0
    body = inspect.getsource(Interact.open_on)
    assert "self._at(node_map)" in body, "the centre click is not gated on being there"
