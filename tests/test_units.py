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
    body_candidates,
    candidates,
    corpse_candidates,
    corpse_probe_points,
    find,
    find_plates,
    mask_for,
    plate_for,
    revalidate,
    selected_plates,
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


def test_an_attack_selection_ring_can_change_colour_without_changing_its_plate():
    # Live wolf frame 90: the ring flashed red/orange while its health bar stayed yellow.
    ring = Ring(cx=1302.0, cy=423.0, w=119, h=16, colour=RingColour.RED, area=768)
    plate = Plate(cx=1308.0, cy=305.0, w=145, colour=RingColour.YELLOW)
    assert plate_for(ring, [plate]) is plate


def test_a_different_colour_plate_does_not_belong_to_the_ring():
    ring = Ring(cx=800.0, cy=600.0, w=50, h=28, colour=RingColour.GREEN, area=380)
    label = Plate(cx=800.0, cy=500.0, w=140, colour=RingColour.YELLOW)
    assert plate_for(ring, [label]) is None


# --- unmeasured rules stay off ----------------------------------------------

def test_only_measured_colours_are_searched_by_default():
    """Enabling the unmeasured hostile rule immediately produced a confident false ring on
    terrain, on a frame whose only ring was friendly. An unmeasured rule is a guess with a
    type annotation."""
    assert MEASURED == (RingColour.GREEN, RingColour.YELLOW)
    assert RingColour.RED not in MEASURED, "red detection still admits measured terrain"


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


@pytest.mark.parametrize("fixture, expected", [
    ("live-dermot-targeted.npz", (1007, 486)),
    ("live-dermot-thin-ring.npz", (1002, 480)),
])
def test_dermot_is_bracketed_despite_larger_yellow_terrain(fixture, expected):
    """22 September acceptance: selection succeeded, but the largest yellow ground
    component displaced the actual green ring before any plate association occurred.
    The second frame also contains a genuine partly hidden 67x7 ring (aspect 9.57).
    Both captures retain all original pixels, including the distracting terrain.
    """
    frame = np.load(LIVE.parent / fixture)["frame"]
    for known_plate in (None, Plate(cx=996, cy=422, w=145, colour=RingColour.GREEN)):
        sighting = find(frame, plate=known_plate)
        assert sighting is not None
        assert sighting.colour is RingColour.GREEN
        assert sighting.torso == expected
        assert 990 < sighting.ring.cx < 1020
        assert 560 < sighting.ring.cy < 580
        # Selection rearranged the labels; the old y=422 must not become the head.
        assert 390 < sighting.plate.cy < 405


def test_confirmed_plate_cannot_borrow_a_ring_of_another_colour():
    frame = np.load(LIVE.parent / "live-dermot-thin-ring.npz")["frame"]
    wrong = Plate(cx=996, cy=422, w=145, colour=RingColour.YELLOW)
    assert find(frame, plate=wrong) is None


def test_two_current_plates_matching_the_confirmed_column_are_ambiguous():
    frame = np.load(LIVE.parent / "live-dermot-thin-ring.npz")["frame"].copy()
    # A second solid green bar in the same column cannot be identified by colour and
    # geometry alone. The locator must abstain rather than choose the nearer label.
    frame[450:455, 925:1070] = (72, 219, 48)
    known = Plate(cx=996, cy=422, w=145, colour=RingColour.GREEN)
    assert find(frame, plate=known) is None


def test_stock_nameplate_border_cannot_be_selected_as_a_health_bar():
    """The monitored merchant attempt selected a rabbit through the yellow border.
    Its 37x1 component at (990,391) sorts ahead of the real green bar at (1000,398)
    by distance to screen centre. Both components occur in this original live capture.
    """
    frame = np.load(LIVE.parent / "live-dermot-targeted.npz")["frame"]
    assert mask_for(frame, RingColour.YELLOW)[391, 972:1009].all()
    plates = find_plates(frame)
    assert len(plates) == 4
    assert all(plate.colour is RingColour.GREEN for plate in plates)
    selected = find_plates(frame, selected=True)
    assert len(selected) == 1
    assert abs(selected[0].cx - 1000) < 2
    assert abs(selected[0].cy - 398) < 2


def test_faded_merchants_remain_candidates_when_someone_else_is_selected():
    """Last frame of the monitored attempt: Brother Danil is selected while Dermot,
    Godric and Janos have faded bars. Reusing the bright ring mask erased all three.
    """
    frame = np.load(LIVE.parent / "live-dermot-unselected.npz")["frame"]
    plates = find_plates(frame)
    assert len(plates) == 4
    assert all(plate.colour is RingColour.GREEN for plate in plates)
    nearest = sorted(plates, key=lambda plate: abs(plate.cx - 800))[:3]
    assert any(abs(plate.cx - 997) < 2 and abs(plate.cy - 401) < 2 for plate in nearest)
    # Broadening acquisition cannot change the selected target's locator mask. Its
    # one bright bar belongs to Brother Danil, and the faded merchant cannot borrow it.
    bright = find_plates(frame, selected=True)
    assert len(bright) == 1 and abs(bright[0].cx - 774) < 2
    dermot = next(plate for plate in plates if abs(plate.cx - 997) < 2)
    assert find(frame, plate=dermot) is None


# --- untrusted proposals and fresh geometry --------------------------------

def _scene(*, ring_colour=(211, 173, 8), ring_left=770, ring_top=540,
           bar_left=728, bar_top=345):
    """Simple independent surfaces; no fixture's desired point is used to draw these."""
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    frame[bar_top:bar_top + 5, bar_left:bar_left + 145] = (130, 117, 3)
    _outline(frame, ring_left, ring_top, 60, 20, ring_colour)
    return frame


def _outline(frame, left, top, width, height, colour):
    frame[top, left:left + width] = colour
    frame[top + height - 1, left:left + width] = colour
    frame[top:top + height, left] = colour
    frame[top:top + height, left + width - 1] = colour


def test_component_bounds_preserve_extrema_instead_of_centring_on_colour_mass():
    from jev.perceive.radio_frame import _blobs

    mask = np.zeros((12, 20), dtype=bool)
    mask[2:9, 3] = True
    mask[8, 3:16] = True
    [blob] = _blobs(mask)
    assert blob.bounds == (3, 2, 16, 9)
    assert (blob.w, blob.h) == (13, 7)
    assert blob.cx != (3 + 15) / 2
    assert blob.cy != (2 + 8) / 2


def test_proposals_preserve_measured_ring_and_bar_bounds():
    [sighting] = candidates(_scene())
    assert sighting.ring.bounds == (770, 540, 830, 560)
    assert sighting.plate.bounds == (728, 345, 873, 350)
    assert sighting.plate.h == 5
    assert sighting.torso == (800, 445)
    assert sighting.admits(sighting.torso)
    assert not sighting.admits((800, 349))
    assert not sighting.admits((800, 540))
    assert not sighting.admits((873, 445))


def test_a_plate_the_view_moved_off_its_anchor_is_found_again_when_it_is_the_only_one():
    """At Marshal McBride the selection click's frame and the next, 0.4 s apart, put his
    plate 76 px apart, past half its width: with no plate left the hand-in failed "no
    eligible target geometry" (session 136). The only plate of its colour is still his to
    propose; a hover proves identity before any click."""
    frame = _scene()
    [plate] = [s.plate for s in candidates(frame)]
    moved = Plate(cx=plate.cx - 76, cy=plate.cy - 65, w=plate.w, colour=plate.colour)
    [sighting] = candidates(frame, plate=moved)
    assert sighting.torso == (800, 445)
    assert body_candidates(frame, plate=moved)
    # Two plates of that colour and neither near the anchor: not the view's to choose.
    frame[200:205, 100:245] = (130, 117, 3)
    assert candidates(frame, plate=moved) == ()
    assert body_candidates(frame, plate=moved) == ()


def test_multiple_proposals_do_not_promote_the_largest_component_to_identity():
    frame = _scene()
    _outline(frame, 755, 650, 90, 40, (211, 173, 8))
    proposals = candidates(frame)
    assert len(proposals) == 2
    assert proposals[0].ring.area < proposals[1].ring.area
    assert proposals[0].ring.bounds == (770, 540, 830, 560)
    assert candidates(frame, limit=1) == proposals[:1]


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_proposal_budget_requires_a_positive_integer(limit):
    frame = _scene()
    with pytest.raises(ValueError, match="positive integer"):
        candidates(frame, limit=limit)
    with pytest.raises(ValueError, match="positive integer"):
        corpse_candidates(frame, limit=limit)


def test_ring_badge_overlapping_bar_is_rejected_by_actual_bounds():
    # A red component remains separate from the yellow bar, even where they overlap
    # vertically. Comparing centroids alone would allow it to pose as feet below a bar.
    frame = _scene(ring_colour=(220, 20, 20), ring_top=346)
    assert candidates(frame) == ()


def test_living_point_must_stay_inside_paired_bar_horizontal_span():
    frame = _scene(ring_left=910)
    assert find(frame) is not None  # legacy loose 140-pixel association
    assert candidates(frame) == ()


def test_another_nameplate_covering_the_point_excludes_it():
    frame = _scene()
    frame[443:448, 740:885] = (72, 219, 48)
    assert candidates(frame) == ()
    assert revalidate(frame, (800, 445)) is None


def test_red_is_an_explicit_untrusted_ring_proposal_with_a_yellow_bar():
    frame = _scene(ring_colour=(220, 20, 20))
    assert find(frame) is None
    [sighting] = candidates(frame)
    assert sighting.ring.colour is RingColour.RED
    assert sighting.plate.colour is RingColour.YELLOW
    assert MEASURED == (RingColour.GREEN, RingColour.YELLOW)


def test_revalidation_checks_the_requested_point_in_current_pixels():
    before = _scene()
    [sighting] = candidates(before)
    assert revalidate(before, sighting.torso) is not None
    # The target moves sideways while the pointer approaches. The earlier midpoint
    # cannot be reused merely because the earlier bracket still exists in memory.
    after = _scene(ring_left=1010, bar_left=968)
    assert candidates(after)
    assert revalidate(after, sighting.torso) is None
    assert revalidate(before, (800, 348)) is None  # own nameplate surface


def test_revalidation_does_not_require_the_requested_point_to_be_a_new_midpoint():
    frame = _scene()
    checked = (790, 430)
    current = revalidate(frame, checked)
    assert current is not None
    assert current.torso != checked
    assert current.admits(checked)


def test_legacy_manual_components_cannot_invent_measured_brackets():
    from jev.perceive.units import Sighting

    ring = Ring(800, 550, 60, 20, RingColour.YELLOW, 150)
    plate = Plate(800, 347, 145, RingColour.YELLOW)
    assert ring.bounds is None and plate.bounds is None
    assert not Sighting(ring, plate, (800, 445)).admits((800, 445))


def test_corpse_probes_search_under_the_last_living_plate_before_ring_proposals():
    frame = _scene()
    [sighting] = corpse_candidates(frame)
    assert sighting.point == (800, 544)
    assert sighting.ring.bounds == (770, 540, 830, 560)
    # A ring off the centre-line grid is still proposed, but only after both grids:
    # measured 23 September, grass rings ordered first spent the whole search.
    aside = _scene(ring_left=1010, bar_left=968)
    [ring] = corpse_candidates(aside)
    points = corpse_probe_points(aside, limit=60)
    assert ring.point in points
    assert all(abs(x - 800) <= 85 or abs(abs(x - 800) - 230) <= 1
               for x, _ in points[:points.index(ring.point)])
    anchor = Plate(500.0, 300.0, 147, RingColour.YELLOW)
    anchored = corpse_probe_points(np.zeros((900, 1600, 3), dtype=np.uint8), anchor)
    assert anchored[0] == (500, 400), "the column under the last living plate comes first"
    assert all(abs(x - 500) <= 85 for x, _ in anchored[:8])
    assert all(abs(x - 800) <= 85 for x, _ in anchored[8:16]), "then the centre line"
    assert (1030, 630) in anchored[16:22], "then beside the character's own model"
    assert len(anchored) == 24, "the search is bounded"
    no_anchor = corpse_probe_points(np.zeros((900, 1600, 3), dtype=np.uint8))
    assert no_anchor[0] == (800, round(900 * 0.46) + 100), "centre line without a plate"


def test_corpse_proposal_rejects_plate_surfaces_and_interface():
    frame = _scene()
    frame[542:547, 728:873] = (72, 219, 48)
    assert all(not (542 <= s.point[1] < 547) for s in corpse_candidates(frame))
    assert all(not (542 <= y < 547 and 728 <= x < 873) for x, y in corpse_probe_points(frame))
    # The component's centroid clears the minimap, while the prone-pose point lies
    # inside it. The shared point check still excludes that interface surface.
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    _outline(frame, 1400, 200, 80, 80, (211, 173, 8))
    assert _find_ring(frame) is not None
    assert corpse_candidates(frame) == ()
    bars = Plate(800.0, 760.0, 147, RingColour.YELLOW)
    assert all(y < 0.88 * 900 for _x, y in corpse_probe_points(frame, bars)), "probed the action bars"


def test_the_selected_plate_is_the_bright_one_in_a_reaction_colour():
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    frame[380:387, 1227:1374] = (230, 200, 10)         # bright yellow: the selected wolf
    frame[500:507, 100:247] = (110, 95, 5)              # faded yellow: another wolf
    [plate] = selected_plates(frame, reaction=4)
    assert abs(plate.cx - 1300) < 2
    assert selected_plates(frame, reaction=5) == [], "a friendly unit has a green plate"


@pytest.mark.parametrize("fixture", [
    "live-dermot-targeted.npz", "live-dermot-thin-ring.npz",
    "live-dermot-unselected.npz",
])
def test_measured_merchant_brackets_survive_proposal_and_revalidation(fixture):
    frame = np.load(LIVE.parent / fixture)["frame"]
    proposals = candidates(frame)
    assert len(proposals) == 1
    current = proposals[0]
    assert current.ring.bounds is not None and current.plate.bounds is not None
    assert revalidate(frame, current.torso) == current


def test_name_only_target_cannot_borrow_unrelated_visible_bars():
    assert candidates(hostile_frame()) == ()


def test_a_new_selection_is_located_by_the_colour_it_adds_between_two_frames():
    """Measured 23 September: a Tab-picked Young Wolf in plain view, beyond nameplate
    distance, with its ring broken by its body and grass and its name above it."""
    from jev.perceive.units import Mark, selection_marks

    rng = np.random.default_rng(7)
    before = np.full((906, 1611, 3), (70, 90, 30), dtype=np.uint8)       # grass
    for _ in range(40):                                                    # flowers
        x, y = int(rng.integers(0, 1600)), int(rng.integers(200, 780))
        before[y:y + 4, x:x + 5] = (215, 180, 10)
    after = before.copy()
    after[:, 1:] = before[:, :-1]                   # grass sways a pixel: not new colour
    for x0, x1 in ((318, 331), (341, 349), (355, 363)):                   # a broken ring
        after[211:219, x0:x1] = (250, 255, 30)
    after[170:174, 327:352] = (245, 255, 5)                                 # its name
    after[20:60, 300:420] = (230, 200, 10)                                  # target frame
    [mark] = selection_marks(before, after)
    assert isinstance(mark, Mark)
    assert 320 <= mark.cx <= 360 and 170 <= mark.cy <= 219
    assert selection_marks(before, before) == []


def test_selection_marks_refuse_frames_of_different_sizes():
    from jev.perceive.units import selection_marks

    with pytest.raises(ValueError):
        selection_marks(np.zeros((10, 10, 3), np.uint8), np.zeros((10, 11, 3), np.uint8))


def test_a_hostile_units_plate_is_a_facing_candidate():
    """Every hostile bar measured 5 px under the red rule, and a 6 px floor made facing
    turn in circles past a Defias Cutpurse's plate in plain view (lossless live frame)."""
    from jev.perceive.units import plate_candidates, plate_colours

    frame = np.load(LIVE.parent / "live-hostile-plate.npz")["frame"]
    hostile = plate_candidates(frame, plate_colours(2))
    assert [(round(p.cx), round(p.cy), p.colour) for p in hostile] == [(663, 335, RingColour.RED)]
    # The floor stays for yellow, where flower blobs are 4-5 px.
    yellow = np.zeros_like(frame)
    yellow[400:405, 700:845] = (230, 200, 10)
    assert plate_candidates(yellow, (RingColour.YELLOW,)) == []
