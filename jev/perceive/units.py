"""Shared world-geometry proposals for living units and corpses.

The client paints selection rings and nameplate health bars. Colour components with
measured shape and fill can suggest those anchors, while preserving their actual pixel
bounds. Bright grass also satisfies these tests: neither a component nor a complete
ring/bar pair establishes the target's location.

``candidates`` returns bounded hypotheses instead of assigning identity to the largest
component. ``revalidate`` checks an explicit point against freshly observed geometry.
Living proposals require a separated bar/ring bracket; corpse proposals retain the
measured point on the ring for a prone model. Missing anchors remain missing evidence.

Both contracts require exact fresh hover ownership and an observed action outcome in
their caller. Hover can match a nameplate, and geometry cannot prove a body hitbox.
The legacy ``find`` and ``_find_ring`` remain for compatibility with historical replay;
new action callers must use the untrusted proposal contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from jev.perceive.radio_frame import _Blob, _blobs


class RingColour(StrEnum):
    """The colour the client draws a unit's ring and nameplate in.

    Named by **colour, not by reaction**, and that is the correction. The rules here used
    to be called `HOSTILE`, `NEUTRAL` and `FRIENDLY`, which quietly claimed that finding a
    ring told you what the unit was. It does not, and the measurement that proved it is
    Kobold Vermin: the radio reported `target.reaction` hostile while the client drew a
    bright **yellow** ring, because WoW colours unfriendly and neutral the same way a
    camera cannot tell apart.

    This supplies colour hypotheses. The radio supplies target identity, and exact
    mouseover-to-target equality supplies pointer ownership; a shared name alone cannot
    distinguish two units. None of those observations establishes the actual hitbox or
    proves an action worked.
    """

    GREEN = "green"      # friendly
    YELLOW = "yellow"    # neutral, and unfriendly, which look identical
    RED = "red"          # hostile


@dataclass(frozen=True)
class ColourRule:
    """Channels that must be bright, channels that must be dark, and by how much.

    The shape matters and the old one was wrong. It named a single `hi` channel, which
    cannot express yellow — R and G both high — so the yellow ring was unrepresentable
    rather than merely mis-thresholded, and the `NEUTRAL` rule written by symmetry
    demanded G >> R and would never have matched anything.
    """

    high: tuple[int, ...]
    low: tuple[int, ...]
    high_min: int
    gap: int

    def mask(self, frame: np.ndarray) -> np.ndarray:
        a = frame.astype(np.int16)
        hi = np.minimum.reduce([a[:, :, c] for c in self.high])
        lo = np.maximum.reduce([a[:, :, c] for c in self.low])
        return (hi > self.high_min) & (hi - lo > self.gap)


# Measured on live 1600x900 frames, and the numbers are the point:
#
#   friendly ring   Deputy Willem      ( 95, 200,   5)   min(G) - max(R,B) = 105
#   friendly plate  Deputy Willem      ( 72, 219,  48)                     = 147
#   yellow   ring   Kobold Vermin      (211, 173,   8)   min(R,G) - B      = 165
#   yellow   plate  Kobold Vermin      (130, 117,   3)                     = 114
#
# and the two things that must **not** pass:
#
#   Northshire grass                   (104,  87,   9)   as green: G=87  < 150
#   Northshire dirt                    (104,  87,  20)   as yellow: min=87 < 150
#
# Dirt is why `high_min` carries the weight here rather than the gap. Dirt is low-blue and
# R-ish-equals-G, which is the *shape* of yellow — it is separated by being dark, not by
# being a different hue, and a rule that leaned on the gap alone would paint a ring on the
# ground. The earlier guessed hostile rule did exactly that.
_RULES: dict[RingColour, ColourRule] = {
    RingColour.GREEN: ColourRule(high=(1,), low=(0, 2), high_min=150, gap=90),
    # `high_min` is lower for yellow than for green, and the gap does the work instead.
    # The yellow **plate** is much darker than the yellow ring — min(R,G) of 117 against
    # 173 — so a brightness threshold set by the ring silently excluded every nameplate.
    # Dirt is not separated by brightness either (min 87, close to the plate's 117); it is
    # separated by the gap, 67 against 114.
    RingColour.YELLOW: ColourRule(high=(0, 1), low=(2,), high_min=105, gap=100),
    RingColour.RED: ColourRule(high=(0,), low=(1, 2), high_min=150, gap=90),
}

# Selecting one unit fades the other stock nameplates. With Brother Danil selected,
# Dermot's visible bar measured median G=107 and median G-max(R,B)=64, so the selected
# ring's green mask erased it. These measured acquisition thresholds recover all four
# real merchant bars; the existing geometry still rejects terrain and borders. They
# never locate a ring or the selected target's plate, whose brighter rules stay above.
_PLATE_RULES = {**_RULES,
                RingColour.GREEN: ColourRule(high=(1,), low=(0, 2), high_min=90, gap=45)}

MEASURED: tuple[RingColour, ...] = (RingColour.GREEN, RingColour.YELLOW)
"""Which rules have met a real frame, and **the default search set**.

Red is not searched by default: live wolf frames now show a red/orange attack-selection
ring, but that mask also admits roof and terrain components. Detection is not validated.
That is not caution for its own sake: the previous guessed rule, switched on untested,
produced a confident ring on a patch of Northshire terrain.

An unmeasured rule is a guess with a type annotation. Add a colour here when a frame has
proved it, not before.
"""

# A ring is hollow; a bar is not. **Fill is the whole discrimination**, and aspect is
# deliberately loose.
#
# Aspect looked like a good second test and is not one: a selection ring is a circle on
# the ground seen in perspective, so how flat it looks is a function of camera pitch —
# which nothing here controls. Measured at 1.79 on one frame and 4.29 on another, both
# unambiguously rings, and the tight range rejected the second while the unit stood
# centred and in plain sight. A third, on a distant kobold, measured 5.56.
#
# Dermot's ring, partly hidden by his stall, measured 67x7 (9.57) on 22 September.
# Eight rejected that real ellipse. Ten includes it while retaining the exclusion of a
# measured **nameplate plus its name text** at 11.4. Shape only supplies candidates;
# a living unit still needs its matching plate before any torso can be returned.
RING_FILL_MAX = 0.55
RING_ASPECT = (0.8, 10.0)
RING_MIN_AREA = 60

# 0.65, not 0.8. A friendly nameplate measured 0.99 because it is a flat colour, but a
# kobold's is a gradient with a highlight down the middle and only 0.72 of it clears the
# mask. Rings top out at 0.55, and `BAR_ASPECT_MIN` separates the two anyway, so the
# looser fill costs nothing.
BAR_FILL_MIN = 0.65
BAR_ASPECT_MIN = 12.0

# Where the stock 2.4.3 interface is, as fractions of the frame, so a resolution change
# does not silently move the exclusions off the thing they exclude.
#
# Geometry rather than colour, because the interface is the same colour as the thing it
# would be confused with **on purpose**: the player and target health bars are drawn
# exactly like a nameplate because they mean the same thing.
#
# Every entry here is a measured false positive, not a precaution:
#
#   frames    player and target bars, top-left, identical in shape to a nameplate
#   minimap   the sun/clock icon is a small yellow disc that scores as a ring, and it
#             outranked a real selection ring by area on a live frame
#   strip     our own radio, top-centre, which paints every colour there is by design
#   bars      the action bars, bottom, full of coloured square icons
_UI_ZONES: tuple[tuple[float, float, float, float], ...] = (
    (0.00, 0.00, 0.35, 0.14),    # frames
    (0.84, 0.00, 1.00, 0.26),    # minimap
    (0.38, 0.00, 0.62, 0.13),    # strip
    (0.00, 0.88, 1.00, 1.00),    # bars
)

# A nameplate is a wide bar. Thirteen pixels of something solid is not one, and one such
# blob presented itself as a plate on a live frame.
BAR_MIN_W = 30

# A health bar has a surface, not just a horizontal edge. The yellow stock nameplate
# border produced a solid 37x1 component at (990,391); clicking it selected a rabbit
# behind the label. Every measured green/yellow bar is 5-8 pixels high. On 23 September
# a yellow chat line under the system messages measured 40x3 at (118,643) and was taken
# for a plate; four pixels keeps every measured bar and rejects both strokes.
BAR_MIN_H = 4

# A nameplate has one width at a given interface size; only its coloured *fill* shrinks
# with the unit's health, anchored at the bar's left edge. Measured full bars: 146-149 px
# on a 1611 px client, 145 px on 1600 px. Below about 57% health the fill fails the bar
# aspect test entirely (a 7 px bar needs 84 px of fill), so a damaged unit's plate is
# recognised separately and centred from its left edge.
PLATE_FULL_W_FRAC = 0.0912
# Partial bars: measured nameplate fills are 7-8 px tall here; most yellow flower blobs
# are 4-5. A fill below 12 px is under 8% health. Candidates still need proof of identity.
PARTIAL_BAR_MIN_H = 6
# Except red. The red rule keeps only the bar's saturated core, and every hostile bar
# measured 5 px under it - a Defias Cutpurse's lossless, a Mangy Wolf's and two Forest
# Spiders' - so a 6 px floor hid every hostile plate from facing. A fight with a hostile
# unit then turned in circles "searching" with its plate in plain view, and the
# character died without a swing (runs 20260923T172056-54f4d5, ...175710-b5044f).
PARTIAL_BAR_MIN_H_BY_COLOUR = {RingColour.RED: 5}
PARTIAL_BAR_MAX_H = 12       # taller solid colour is not a health bar
PARTIAL_BAR_MIN_W = 12

# How far a plate may sit from a ring, horizontally, and still belong to the same unit.
# Generous, because the ring's centroid is pulled sideways by grass occluding one arc.
PLATE_PAIR_MAX_DX = 140

# Red has measured ring proposals, including a wolf whose bar remained yellow. It is
# included only in the explicitly untrusted proposal API; acquisition and the legacy
# locator retain their existing measured defaults.
PROPOSAL_COLOURS = (RingColour.GREEN, RingColour.YELLOW, RingColour.RED)
PROPOSAL_LIMIT = 24
Bounds = tuple[int, int, int, int]
Point = tuple[int, int]


@dataclass(frozen=True)
class Ring:
    """A ring-shaped colour component; neither shape nor area establishes identity."""

    cx: float
    cy: float
    w: int
    h: int
    colour: RingColour
    area: int
    bounds: Bounds | None = None


@dataclass(frozen=True)
class Plate:
    """A nameplate health bar. One per visible unit that has a plate enabled."""

    cx: float
    cy: float
    w: int
    colour: RingColour
    h: int = 1
    bounds: Bounds | None = None

    def unit_below(self, drop: float = 0.55) -> tuple[int, int]:
        """Roughly where the unit is, below its plate. Bars are wider than rings for the
        same unit, so the ratio differs."""
        return (round(self.cx), round(self.cy + self.w * drop))


@dataclass(frozen=True)
class Sighting:
    """A possible living bracket. Geometry cannot prove ownership or a body hitbox."""

    ring: Ring
    plate: Plate
    torso: tuple[int, int]

    @property
    def colour(self) -> RingColour:
        """What the client drew, not who the unit is. See `RingColour`."""
        return self.ring.colour

    def admits(self, point: Point) -> bool:
        """Whether a point is inside this measured bar-to-ring gap.

        Old hand-authored fixtures remain constructible, but absent component bounds
        cannot authorize this check. Excluding other bars and the interface additionally
        requires the complete current frame; see ``revalidate``.
        """
        if self.plate.bounds is None or self.ring.bounds is None:
            return False
        left, _, right, bottom = self.plate.bounds
        return left <= point[0] < right and bottom <= point[1] < self.ring.bounds[1]


@dataclass(frozen=True)
class CorpseSighting:
    """A possible point on a ring in the measured prone pose, without identity."""

    ring: Ring
    point: Point

    def admits(self, point: Point) -> bool:
        return self.ring.bounds is not None and _contains(self.ring.bounds, point)


def mask_for(frame: np.ndarray, colour: RingColour) -> np.ndarray:
    """Pixels that could belong to a ring or bar drawn in this colour."""
    return _RULES[colour].mask(frame)


def _outside_interface(blob: _Blob, shape: tuple[int, int] | None = None) -> bool:
    """Is this blob clear of the stock interface?

    `shape` is `(height, width)` of the frame. It is optional only so the existing
    shape-level tests can call this with a blob alone; a caller with a frame should pass
    it, because the zones are fractions and a default guesses the resolution.
    """
    h, w = shape if shape is not None else (900, 1600)
    fx, fy = blob.cx / max(1, w), blob.cy / max(1, h)
    return not any(x0 <= fx <= x1 and y0 <= fy <= y1 for x0, y0, x1, y1 in _UI_ZONES)


def find(frame: np.ndarray,
         colours: tuple[RingColour, ...] = MEASURED, *,
         plate: Plate | None = None) -> Sighting | None:
    """Where the selected unit is, or `None`.

    The whole public surface. A sighting needs **both** a ring and its nameplate, because
    together they bracket the model — feet and head — and the midpoint is the torso at any
    distance, for any unit, without knowing the distance or the unit.

    `None` when either is missing. There is deliberately no fallback: an earlier version
    guessed the torso from a fraction of the ring's width when no plate was found, and
    that fraction measured 1.27 on one live frame and 2.05 on another. It is not a
    constant, it is two numbers, and a click at it landed on Deputy Willem's feet and did
    nothing. A guessed pixel is worse than an honest `None` — the caller can act on
    "cannot see it" and cannot act on a plausible wrong answer.
    """
    plates = find_plates(frame, colours, selected=True)
    if plate is not None:
        # The caller selected this plate and confirmed identity on the radio. Selection
        # can rearrange overlapping labels vertically: Dermot moved 24 pixels without
        # moving himself. Re-read the bar rather than using that stale head position.
        # Each bar's centre must still fall inside the other's horizontal span. Two
        # possible matches are ambiguous; proximity alone cannot identify the target.
        plates = [p for p in plates if p.colour == plate.colour
                  and abs(p.cx - plate.cx) <= min(p.w, plate.w) / 2]
        if len(plates) != 1:
            return None
        colours = (plate.colour,)

    # A bright piece of terrain can pass the hollow-shape test and be larger than the
    # real ring. Choosing the largest blob *before* pairing let yellow ground at x=279
    # hide Dermot's green ring at x=1007. Rank only complete ring/plate sightings.
    pairs = [(ring, matched) for ring in _rings(frame, colours)
             if (matched := plate_for(ring, plates)) is not None]
    if not pairs:
        return None
    ring, matched = max(pairs, key=lambda pair: pair[0].area)
    return Sighting(ring=ring, plate=matched,
                    torso=(round(ring.cx), round((ring.cy + matched.cy) / 2)))


def _find_ring(frame: np.ndarray,
               colours: tuple[RingColour, ...] = MEASURED) -> Ring | None:
    """The selection ring. Exactly one exists, under whatever is targeted."""
    return max(_rings(frame, colours), key=lambda ring: ring.area, default=None)


def _rings(frame: np.ndarray, colours: tuple[RingColour, ...]) -> list[Ring]:
    """Hollow colour components; only association with a plate makes a sighting."""
    rings = []
    for colour in colours:
        for blob in _blobs(mask_for(frame, colour)):
            if (ring := _ring_component(blob, colour, frame.shape[:2])) is not None:
                rings.append(ring)
    return rings


def _ring_component(blob: _Blob, colour: RingColour,
                    shape: tuple[int, int]) -> Ring | None:
    if blob.area < RING_MIN_AREA or not _outside_interface(blob, shape):
        return None
    aspect = blob.w / max(1.0, blob.h)
    fill = blob.area / max(1.0, blob.w * blob.h)
    if not (RING_ASPECT[0] <= aspect <= RING_ASPECT[1]) or fill > RING_FILL_MAX:
        return None
    return Ring(cx=blob.cx, cy=blob.cy, w=int(blob.w), h=int(blob.h),
                colour=colour, area=blob.area, bounds=blob.bounds)


def _plate_component(blob: _Blob, colour: RingColour,
                     shape: tuple[int, int]) -> Plate | None:
    if blob.w < BAR_MIN_W or blob.h < BAR_MIN_H or not _outside_interface(blob, shape):
        return None
    aspect = blob.w / max(1.0, blob.h)
    fill = blob.area / max(1.0, blob.w * blob.h)
    if aspect < BAR_ASPECT_MIN or fill < BAR_FILL_MIN:
        return None
    return Plate(cx=blob.cx, cy=blob.cy, w=int(blob.w), colour=colour,
                 h=int(blob.h), bounds=blob.bounds)


def plate_for(ring: Ring, plates: list[Plate]) -> Plate | None:
    """The nameplate belonging to a ring: nearest above it, horizontally overlapping.

    Above, because a plate floats over a unit's head and the ring is under its feet. The
    horizontal tolerance is loose because grass hides part of the ring and drags its
    centroid sideways.
    """
    candidates = [
        p for p in plates
        # An attack-selection ring can flash red while its health bar stays yellow.
        # Colour is a measured visual signal, not a universal identity equality.
        if (p.colour == ring.colour
            or (ring.colour is RingColour.RED and p.colour is RingColour.YELLOW))
        and p.cy < ring.cy and abs(p.cx - ring.cx) <= PLATE_PAIR_MAX_DX
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: ring.cy - p.cy)


def find_plates(frame: np.ndarray,
                colours: tuple[RingColour, ...] = MEASURED, *,
                selected: bool = False) -> list[Plate]:
    """Every nameplate health bar on screen, biggest first.

    Not needed to click the current target — the ring is better for that — but it is how
    a unit is found *before* it is targeted, which is what picking the next kobold out of
    a camp requires. Acquisition includes measured faded green bars. ``selected=True``
    retains the original bright mask for the unit whose identity was just confirmed.
    """
    out: list[Plate] = []
    rules = _RULES if selected else _PLATE_RULES
    for colour in colours:
        for blob in _blobs(rules[colour].mask(frame)):
            if (plate := _plate_component(blob, colour, frame.shape[:2])) is not None:
                out.append(plate)
    out.sort(key=lambda p: -p.w)
    return out


def _partial_plate(blob: _Blob, colour: RingColour, shape: tuple[int, int]) -> Plate | None:
    """A health bar at any fill: bar-shaped, solid, no wider than a full plate."""
    full = PLATE_FULL_W_FRAC * shape[1]
    min_h = PARTIAL_BAR_MIN_H_BY_COLOUR.get(colour, PARTIAL_BAR_MIN_H)
    if (blob.h < min_h or blob.h > PARTIAL_BAR_MAX_H or blob.w < PARTIAL_BAR_MIN_W
            or blob.w > 1.25 * full or not _outside_interface(blob, shape)):
        return None
    if blob.area / max(1.0, blob.w * blob.h) < BAR_FILL_MIN or blob.w < blob.h:
        return None
    left = blob.bounds[0] if blob.bounds else blob.cx - blob.w / 2
    # The fill grows rightward from the plate's left edge; the plate's centre is where
    # the unit is, whatever its health.
    cx = blob.cx if blob.w >= 0.9 * full else left + full / 2
    return Plate(cx=cx, cy=blob.cy, w=int(blob.w), colour=colour, h=int(blob.h),
                 bounds=blob.bounds)


def plate_candidates(frame: np.ndarray, colours: tuple[RingColour, ...] = PROPOSAL_COLOURS
                     ) -> list[Plate]:
    """Every health-bar-shaped component in these colours, at any health, nearest centre first.

    Candidates only: flowers and interface strokes can pass a shape test, so a caller must
    establish which (if any) belongs to the selected unit - by exact hover ownership, or by
    tracking a plate it has already proved. Colour follows reaction and combat: a neutral
    wolf's plate turned red once it fought back.
    """
    shape = frame.shape[:2]
    out = []
    for colour in colours:
        for blob in _blobs(_PLATE_RULES[colour].mask(frame)):
            if (plate := _partial_plate(blob, colour, shape)) is not None:
                out.append(plate)
    out.sort(key=lambda p: abs(p.cx - shape[1] / 2))
    return out


def plate_colours(reaction: int | None) -> tuple[RingColour, ...]:
    """Plate colours a unit with this radio reaction can be drawn in.

    Measured, not reasoned: hostile Kobold Vermin drew a *yellow* plate, so hostility
    admits yellow as well as red. Friendly (5 and above) is green. Unknown admits all.
    """
    if reaction is None:
        return PROPOSAL_COLOURS
    if reaction >= 5:
        return (RingColour.GREEN,)
    return (RingColour.YELLOW, RingColour.RED)


# Selecting a unit draws its ring at the feet and its name above it, in the reaction
# colour, the moment the selection is made - at any distance, while nameplates stop at
# about twenty yards (measured 23 September: a Tab-picked Young Wolf in plain view with
# its ring and name and no plate). Between frames either side of the key the camera does
# not move, so what took a selection colour is where the pick stands. Grass sways; a
# colour pixel that was within `MARK_JITTER_PX` of the same colour before is not new.
MARK_JITTER_PX = 2
# A far ring measured about 33x12 px, broken by the body and grass into pieces of 8-32 px;
# its name label about 25x4. Pieces this far apart sideways belong to one pick.
MARK_MIN_AREA = 12
MARK_GROUP_DX = 60
MARK_GROUP_DY = 140


@dataclass(frozen=True)
class Mark:
    """Where selection colour appeared. A direction to look in, not an identity."""

    cx: float
    cy: float
    area: int
    bounds: Bounds


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    out = mask.copy()
    h, w = mask.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx or dy:
                out[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)] |= \
                    mask[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
    return out


def selection_marks(before: np.ndarray, after: np.ndarray,
                    colours: tuple[RingColour, ...] = PROPOSAL_COLOURS) -> list[Mark]:
    """Groups of selection-coloured pixels new in `after`, largest first.

    Both frames must come from an unmoving camera either side of one selection change.
    Interface zones are excluded: the target frame redraws on every selection.
    """
    if before.shape != after.shape:
        raise ValueError("selection marks need two frames of one size")
    shape = after.shape[:2]
    appeared = np.zeros(shape, dtype=bool)
    for colour in colours:
        appeared |= _RULES[colour].mask(after) & ~_dilate(_RULES[colour].mask(before),
                                                          MARK_JITTER_PX)
    pieces = sorted((b for b in _blobs(appeared) if _outside_interface(b, shape)),
                    key=lambda b: -b.area)
    groups: list[list[_Blob]] = []
    for piece in pieces:
        for group in groups:
            if (abs(piece.cx - group[0].cx) <= MARK_GROUP_DX
                    and abs(piece.cy - group[0].cy) <= MARK_GROUP_DY):
                group.append(piece)
                break
        else:
            groups.append([piece])
    marks = []
    for group in groups:
        area = sum(b.area for b in group)
        if area < MARK_MIN_AREA:
            continue
        bounds = [b.bounds or (round(b.cx), round(b.cy), round(b.cx) + 1, round(b.cy) + 1)
                  for b in group]
        marks.append(Mark(sum(b.cx * b.area for b in group) / area,
                          sum(b.cy * b.area for b in group) / area, area,
                          (min(b[0] for b in bounds), min(b[1] for b in bounds),
                           max(b[2] for b in bounds), max(b[3] for b in bounds))))
    marks.sort(key=lambda m: -m.area)
    return marks


def selected_plates(frame: np.ndarray, reaction: int | None = None) -> list[Plate]:
    """Whole nameplates in the colours the selected unit's reaction can take.

    A candidate set, not an identity. Selection does not reliably fade other plates: on
    23 September a rabbit's plate measured as bright (median 129,121,8) as a selected
    wolf's. Which plate is the target's needs exact hover ownership (see
    `Targeting.face_selected`). Plates never appear on corpses, nor beyond the client's
    nameplate distance.
    """
    return find_plates(frame, plate_colours(reaction), selected=True)


def _contains(bounds: Bounds, point: Point) -> bool:
    left, top, right, bottom = bounds
    return left <= point[0] < right and top <= point[1] < bottom


def _point_clear(point: Point, frame: np.ndarray, plates: list[Plate]) -> bool:
    """Exclude measured bar surfaces and the stock interface from a proposed point."""
    x, y = point
    height, width = frame.shape[:2]
    if not (0 <= x < width and 0 <= y < height):
        return False
    if any(x0 <= x / width <= x1 and y0 <= y / height <= y1
           for x0, y0, x1, y1 in _UI_ZONES):
        return False
    return not any(p.bounds is not None and _contains(p.bounds, point) for p in plates)


def _compatible(ring: Ring, plate: Plate) -> bool:
    return (ring.colour == plate.colour
            or (ring.colour is RingColour.RED and plate.colour is RingColour.YELLOW))


def _observations(frame: np.ndarray, colours: tuple[RingColour, ...]
                  ) -> tuple[list[Ring], list[Plate], list[Plate]]:
    """Share component passes for a single frame, with no stale cross-frame cache."""
    rings, selected, excluded = [], [], []
    shape = frame.shape[:2]
    for colour in dict.fromkeys((*PROPOSAL_COLOURS, *colours)):
        for blob in _blobs(mask_for(frame, colour)):
            if (colour in colours
                    and (ring := _ring_component(blob, colour, shape)) is not None):
                rings.append(ring)
            if (plate := _plate_component(blob, colour, shape)) is not None:
                if colour in colours:
                    selected.append(plate)
                if colour is not RingColour.GREEN:
                    excluded.append(plate)
    # The only separate pass is the measured faded-green acquisition mask. Yellow
    # and red use exactly the same components for both bar exclusion and proposals.
    for blob in _blobs(_PLATE_RULES[RingColour.GREEN].mask(frame)):
        if (plate := _plate_component(blob, RingColour.GREEN, shape)) is not None:
            excluded.append(plate)
    return rings, selected, excluded


def _living_brackets(frame: np.ndarray, colours: tuple[RingColour, ...],
                     plate: Plate | None) -> tuple[list[Sighting], list[Plate]]:
    # Faded neighbouring bars still cover the world. They exclude a point even though
    # the selected target's bar uses the stricter mask when proposing its bracket.
    rings, plates, excluded = _observations(frame, colours)
    if plate is not None:
        plates = [p for p in plates if p.colour == plate.colour
                  and abs(p.cx - plate.cx) <= min(p.w, plate.w) / 2]
        if len(plates) != 1:
            return [], excluded

    out = []
    for ring in rings:
        if ring.bounds is None:
            continue
        for current in plates:
            if current.bounds is None or not _compatible(ring, current):
                continue
            # Full extrema matter: the centroid of a hollow badge beside a bar can
            # sit just below its centroid while the components still overlap.
            ring_top = ring.bounds[1]
            left, _, right, bar_bottom = current.bounds
            if ring_top <= bar_bottom or not left <= ring.cx < right:
                continue
            point = (round(ring.cx), (bar_bottom + ring_top) // 2)
            sighting = Sighting(ring, current, point)
            if sighting.admits(point):
                out.append(sighting)

    # Compact brackets get the first hover measurement. This orders hypotheses; it
    # neither promotes the largest component to identity nor claims the first is real.
    out.sort(key=lambda s: (s.ring.bounds[1] - s.plate.bounds[3],
                            s.plate.cy, s.ring.cx, s.colour))
    return out, excluded


def candidates(frame: np.ndarray,
               colours: tuple[RingColour, ...] = PROPOSAL_COLOURS, *,
               plate: Plate | None = None, limit: int = PROPOSAL_LIMIT
               ) -> tuple[Sighting, ...]:
    """Bounded possible living locations, requiring fresh ownership before input.

    A proposal needs separated component rectangles and a point below its paired bar,
    above the ring, within the bar's horizontal span, and outside every other detected
    bar and stock interface region. Bright terrain can still satisfy all of these.
    Neither a proposal nor its rank authorizes a click. Bar bounds describe the colour
    surface, not every native nameplate ornament or the unit's model.

    The limit bounds downstream measurement work. A truncated set, an empty set or
    rejection of all proposals does not establish that the target is absent.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("candidate limit must be a positive integer")
    brackets, plates = _living_brackets(frame, colours, plate)
    return tuple(s for s in brackets if _point_clear(s.torso, frame, plates))[:limit]


def revalidate(frame: np.ndarray, point: Point, *, plate: Plate | None = None,
               colours: tuple[RingColour, ...] = PROPOSAL_COLOURS) -> Sighting | None:
    """Find a current bracket containing the explicit point, without identity claims.

    Recompute from the newly observed pixels. An earlier sighting or an earlier hover
    match is not retained as permission when the model, camera or labels have moved.
    The returned ``torso`` remains the fresh bracket's proposal; ``point`` is the point
    this function checked, which need not equal that midpoint.
    """
    brackets, plates = _living_brackets(frame, colours, plate)
    if not _point_clear(point, frame, plates):
        return None
    return next((s for s in brackets if s.admits(point)), None)


# A living unit's body lies under its plate. The ring bracket above needs the selection
# ring, and an NPC standing just beyond the character has it hidden by the character's own
# model: Eagan Peltskinner on 23 September, plate clear at 148 px, no ring component at
# all, and the turn-in failed with "no eligible target geometry". A point this far below
# the bar's bottom edge is not the nameplate, so a fresh hover owning the target there is
# its body. Drops cover his torso, 30-80 px below the bar, before the character's head.
BODY_MARGIN_PX = 12
BODY_DROPS = (30, 55, 80, 110)


@dataclass(frozen=True)
class BodyProposal:
    """A point under a plate; the plate is the only geometry, identity needs a hover."""

    plate: Plate
    torso: Point

    def admits(self, point: Point) -> bool:
        if self.plate.bounds is None:
            return False
        left, _, right, bottom = self.plate.bounds
        return left <= point[0] < right and point[1] >= bottom + BODY_MARGIN_PX


def _anchored_plates(frame: np.ndarray, colours: tuple[RingColour, ...],
                     plate: Plate | None) -> tuple[list[Plate], list[Plate]]:
    _, plates, excluded = _observations(frame, colours)
    if plate is not None:
        plates = [p for p in plates if p.colour == plate.colour
                  and abs(p.cx - plate.cx) <= min(p.w, plate.w) / 2]
    plates.sort(key=lambda p: abs(p.cx - frame.shape[1] / 2))
    return plates, excluded


def body_candidates(frame: np.ndarray,
                    colours: tuple[RingColour, ...] = PROPOSAL_COLOURS, *,
                    plate: Plate | None = None, limit: int = PROPOSAL_LIMIT
                    ) -> tuple[BodyProposal, ...]:
    """Points under whole plates, nearest the centre first; never an identity.

    For when the ring bracket is missing. Excludes other bars and the interface, as
    `candidates` does, and needs the same fresh hover ownership before any input.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("candidate limit must be a positive integer")
    plates, excluded = _anchored_plates(frame, colours, plate)
    out = []
    for current in plates:
        if current.bounds is None:
            continue
        for drop in BODY_DROPS:
            point = (round(current.cx), current.bounds[3] + drop)
            if _point_clear(point, frame, excluded):
                out.append(BodyProposal(current, point))
    return tuple(out[:limit])


def revalidate_body(frame: np.ndarray, point: Point, *, plate: Plate | None = None,
                    colours: tuple[RingColour, ...] = PROPOSAL_COLOURS) -> BodyProposal | None:
    """A current plate the point lies under, recomputed from these pixels, or `None`."""
    plates, excluded = _anchored_plates(frame, colours, plate)
    if not _point_clear(point, frame, excluded):
        return None
    return next((BodyProposal(p, point) for p in plates
                 if BodyProposal(p, point).admits(point)), None)


def corpse_candidates(frame: np.ndarray,
                      colours: tuple[RingColour, ...] = PROPOSAL_COLOURS, *,
                      limit: int = PROPOSAL_LIMIT) -> tuple[CorpseSighting, ...]:
    """Bounded prone-pose hypotheses; death and exact ownership come from telemetry.

    Retain the existing measured corpse aim on the ring, a quarter of its height above
    its colour centroid. This is a candidate within observed bounds, never a standing
    body estimate or permission to loot a colour component.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("candidate limit must be a positive integer")
    rings, _, plates = _observations(frame, colours)
    out = []
    for ring in rings:
        point = (round(ring.cx), round(ring.cy - round(ring.h * 0.25)))
        sighting = CorpseSighting(ring, point)
        if sighting.admits(point) and _point_clear(point, frame, plates):
            out.append(sighting)
    # Screen order is deterministic and does not assign identity by component area.
    out.sort(key=lambda s: (s.point[1], s.point[0], s.ring.colour))
    return tuple(out[:limit])


# Where to look for a corpse, relative to the last plate seen while the unit lived. A
# corpse has no plate, and a prone model lies around where the standing one was drawn:
# in the measured wolf frames the body sat 50-100 px below its bar at melee range. The
# grid is ordered nearest-first around that band; the side columns reach past the
# character's own model, which covers a corpse lying straight ahead of it.
CORPSE_DROPS = (100, 70, 130, 45, 160)
CORPSE_COLUMNS = (0, -45, 45, -85, 85)
# Without a plate the kill still left the corpse in front of the character, which the
# camera draws on the centre line just above the character's head.
CORPSE_CENTRE_Y = 0.46
CORPSE_MERGE_PX = 12
# The nearest points of each grid come before the rest of either: every corpse found in
# the runs of 24 September was within seven probes of its anchor, and 14 searches that
# had a plate spent all sixteen probes round it, the centre line never tried.
CORPSE_FIRST = 8
# Beside the character's own model, at its hips and knees: in Fargodeep Mine, the camera
# pulled in close by the walls, a kobold lay against the character's right hip where
# neither grid reaches (run 20260924T140621-fc3531).
CORPSE_BESIDE = tuple((sx * 230, fy) for fy in (0.70, 0.62, 0.78) for sx in (1, -1))


def corpse_probe_points(frame: np.ndarray, anchor: Plate | None = None, *,
                        limit: int = 24) -> list[Point]:
    """Ordered points worth hovering to find a corpse; none is a body claim.

    The grid under the last living plate first - measured 23 September, the corpse lay
    about a hundred pixels under the plate the wolf had when it died - then the same grid
    on the centre line, each nearest-first and `CORPSE_FIRST` deep, then the band beside
    the character's own model, then the rest of both grids, and ring proposals last: in
    grass they are mostly terrain, and ordered first they spent a whole sixteen-probe
    search on it. Interface zones and plate surfaces are excluded; points closer than a
    few pixels to an earlier one are merged.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("probe limit must be a positive integer")
    height, width = frame.shape[:2]
    _, _, plates = _observations(frame, PROPOSAL_COLOURS)
    anchors = [(anchor.cx, anchor.cy)] if anchor is not None else []
    anchors.append((width / 2, height * CORPSE_CENTRE_Y))
    grid = sorted(((dx, dy) for dy in CORPSE_DROPS for dx in CORPSE_COLUMNS),
                  key=lambda d: abs(d[0]) + 0.7 * abs(d[1] - CORPSE_DROPS[0]))
    grids = [[(round(cx + dx), round(cy + dy)) for dx, dy in grid] for cx, cy in anchors]
    ordered = [point for points in grids for point in points[:CORPSE_FIRST]]
    ordered.extend((round(width / 2 + dx), round(height * fy)) for dx, fy in CORPSE_BESIDE)
    ordered.extend(point for points in grids for point in points[CORPSE_FIRST:])
    ordered.extend(c.point for c in corpse_candidates(frame))
    out: list[Point] = []
    for point in ordered:
        if not _point_clear(point, frame, plates):
            continue
        if any(abs(point[0] - q[0]) + abs(point[1] - q[1]) < CORPSE_MERGE_PX for q in out):
            continue
        out.append(point)
        if len(out) >= limit:
            break
    return out
