"""The unit locator: where anything is on screen, from what the client draws.

One solver for every NPC and every mob. Nothing here knows what a kobold looks like — it
reads the selection ring and the nameplate, which the client draws identically for a
level-1 rabbit and a raid boss.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from jev.perceive.units import (
    _RULES,
    MEASURED,
    PLATE_PAIR_MAX_DX,
    Plate,
    Ring,
    RingColour,
    _find_ring,
    find,
    find_plates,
    mask_for,
    plate_for,
)

LIVE = pathlib.Path(__file__).parent / "fixtures" / "live-willem-targeted.npz"


@pytest.fixture(scope="module")
def frame() -> np.ndarray:
    if not LIVE.exists():
        pytest.skip("no live fixture")
    return np.load(LIVE)["frame"]


# --- the live frame ---------------------------------------------------------

def test_it_finds_a_real_unit_on_a_real_frame(frame):
    """A 1600x900 capture of Deputy Willem targeted in Northshire. Verified by eye against
    an annotated render: the ring marker sat exactly on his selection circle."""
    sighting = find(frame)
    assert sighting is not None, "the ring and plate are both in this frame"
    assert sighting.colour is RingColour.GREEN

    # Feet below head, torso between them. The geometry is the whole method.
    assert sighting.plate.cy < sighting.ring.cy
    assert sighting.plate.cy < sighting.torso[1] < sighting.ring.cy
    assert sighting.torso[0] == round(sighting.ring.cx)


def test_the_ring_is_hollow_and_the_plate_is_not(frame):
    """Fill ratio is the whole discrimination: measured, a ring came out at 0.27 fill and
    a nameplate bar at 0.97, from the same colour mask in the same pass."""
    sighting = find(frame)
    ring_fill = sighting.ring.area / (sighting.ring.w * sighting.ring.h)
    assert ring_fill < 0.55, "a solid blob is a bar or an icon, not a ring"
    assert sighting.plate.w > sighting.ring.w, "a plate is wider than the ring below it"


def test_interface_bars_are_not_mistaken_for_nameplates(frame):
    """The player and target frames are the same colour and shape as a nameplate on
    purpose. They are excluded by where they sit, not by what they look like."""
    for plate in find_plates(frame):
        assert not (plate.cy < 120 and plate.cx < 560), "that is the interface"


# --- refusing to guess ------------------------------------------------------

def test_an_empty_frame_is_nothing_not_a_guess():
    """A guessed pixel is worse than an honest None: a caller can act on "cannot see it"
    and cannot act on a plausible wrong answer."""
    assert find(np.zeros((900, 1600, 3), dtype=np.uint8)) is None


def test_a_ring_with_no_plate_is_not_a_sighting():
    """An earlier version guessed the torso from a fraction of the ring's width. That
    fraction measured 1.27 on one live frame and 2.05 on another — not a constant, two
    numbers — and a click at it landed on Willem's feet and did nothing."""
    ring = Ring(cx=800.0, cy=600.0, w=50, h=28, colour=RingColour.GREEN, area=380)
    assert plate_for(ring, []) is None


def test_a_plate_below_the_ring_does_not_belong_to_it():
    """A plate floats over a unit's head; the ring is under its feet."""
    ring = Ring(cx=800.0, cy=600.0, w=50, h=28, colour=RingColour.GREEN, area=380)
    below = Plate(cx=800.0, cy=700.0, w=140, colour=RingColour.GREEN)
    assert plate_for(ring, [below]) is None


def test_a_plate_far_to_the_side_does_not_belong_to_it():
    ring = Ring(cx=800.0, cy=600.0, w=50, h=28, colour=RingColour.GREEN, area=380)
    far = Plate(cx=1400.0, cy=500.0, w=140, colour=RingColour.GREEN)
    assert plate_for(ring, [far]) is None


# --- unmeasured rules stay off ----------------------------------------------

def test_only_measured_colours_are_searched_by_default():
    """Enabling the unmeasured hostile rule immediately produced a confident false ring on
    terrain, on a frame whose only ring was friendly. An unmeasured rule is a guess with a
    type annotation."""
    assert MEASURED == (RingColour.GREEN, RingColour.YELLOW)
    assert RingColour.RED not in MEASURED, "red has not met a frame yet"


def test_the_hostile_rule_still_exists_ready_to_be_measured(frame):
    """Off by default is not absent. It is written, and it wants a live hostile target."""
    assert mask_for(frame, RingColour.RED) is not None


def test_grass_alone_does_not_make_a_sighting(frame):
    """Colour is not enough — bright grass hits the same range, which is why shape
    decides. On this frame the mask holds thousands of pixels and one sighting."""
    assert int(mask_for(frame, RingColour.GREEN).sum()) > 1000
    assert find(frame) is not None


# -- a live hostile: measured, not guessed ------------------------------------------

HOSTILE = pathlib.Path(__file__).parent / "fixtures" / "live-hostile-targeted.npz"


def hostile_frame():
    return np.load(HOSTILE)["frame"]


def test_a_hostile_target_is_drawn_yellow_not_red():
    """The correction that renamed this module's rules. The radio reported
    `target.reaction` **hostile** for this Kobold Vermin while the client drew a bright
    yellow ring, because WoW colours unfriendly and neutral identically and a camera
    cannot tell them apart. Colour says where a unit is; the radio says who it is."""
    ring = _find_ring(hostile_frame())
    assert ring is not None, "the ring measured at (961,319) is not being found"
    assert ring.colour is RingColour.YELLOW
    assert abs(ring.cx - 961) < 12 and abs(ring.cy - 319) < 12


def test_yellow_needs_two_bright_channels_which_the_old_rule_could_not_say():
    """The old rule named a single `hi` channel, so yellow — R and G both high — was
    unrepresentable rather than mis-thresholded, and the `NEUTRAL` rule written by
    symmetry demanded G >> R and would never have matched anything."""
    rule = _RULES[RingColour.YELLOW]
    assert len(rule.high) == 2, "yellow is two bright channels, not one"
    ring = np.full((4, 4, 3), (211, 173, 8), dtype=np.uint8)     # measured
    dirt = np.full((4, 4, 3), (104, 87, 20), dtype=np.uint8)     # Northshire, measured
    plate = np.full((4, 4, 3), (130, 117, 3), dtype=np.uint8)    # measured
    assert rule.mask(ring).all(), "the measured ring colour does not pass its own rule"
    assert rule.mask(plate).all(), "the nameplate is darker than the ring and must pass"
    assert not rule.mask(dirt).any(), "Northshire dirt passes as a unit"


def test_nameplates_are_found_on_the_hostile_frame():
    plates = find_plates(hostile_frame())
    assert len(plates) >= 2, "two Kobold Vermin plates are in this frame"
    assert all(p.colour is RingColour.YELLOW for p in plates)
    assert all(p.w >= 100 for p in plates)


def test_the_minimap_is_not_a_unit():
    """The sun icon is a small yellow disc that passes every shape test a ring has, and it
    outranked a real selection ring by area on a live frame. Excluded by geometry, because
    no threshold on colour or shape can tell it from a ring."""
    frame = hostile_frame()
    sun = frame[40:64, 1562:1588]
    assert _RULES[RingColour.YELLOW].mask(sun).any(), "the sun is yellow; this test is moot"
    ring = _find_ring(frame)
    assert ring is not None and ring.cx < 1400, "the minimap came back as a unit"


def test_a_target_with_no_nameplate_is_not_a_sighting():
    """Honest `None`, not a bug. The targeted kobold in this frame is far enough away that
    the client draws its name but no health bar, and without the bar there is nothing to
    bracket the model against — the ring alone gives feet, and the torso would be a
    guessed fraction of the ring's width, which measured 1.27 on one frame and 2.05 on
    another. The nearer plates belong to other kobolds and are correctly not borrowed."""
    frame = hostile_frame()
    ring = _find_ring(frame)
    plates = find_plates(frame)
    assert ring is not None and plates
    assert all(abs(p.cx - ring.cx) > PLATE_PAIR_MAX_DX for p in plates)
    assert find(frame) is None
