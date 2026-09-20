"""Where units are on screen — one solver, every NPC and every mob.

This exists because there is no way to ask 2.4.3 where a unit is. `GetPlayerFacing`,
`INTERACTTARGET` and `InteractUnit` all arrived in 3.0; `/follow` refuses NPCs; and aiming
the character at a node's coordinates fails because a node is a *spawn point* and units
wander — at five yards, a few yards of drift is forty degrees of error.

So the unit's position is read off the screen. Not by recognising models, which would need
a class per creature and would still lose to armour, mounts and camera angle, but from the
two things the client draws around **every** unit in the game, identically:

  * the **selection ring** — the coloured ellipse under whatever is targeted. Exactly one
    exists, it sits at the unit's feet, and its colour is the unit's reaction.
  * the **nameplate health bar** — a solid horizontal bar above any unit with a plate.

Both come out of a single colour-mask and connected-components pass, and **fill ratio
separates them**: a ring is hollow and a bar is not. Measured on live frames —

    ring        59x33   aspect 1.79   fill 0.27
    nameplate  145x5    aspect 29     fill 0.99
    interface  131x4    aspect 33     fill 0.90   (fixed y, excluded by region)

Nothing here knows what a kobold looks like. It knows what the *client* draws, which is
the same for a level-1 rabbit and a raid boss.

Failure is a fact, not a fallback
---------------------------------
No ring means the target is not on screen — which is real information, and the caller
fails the skill rather than clicking hopefully. That is the same rule as
`Interact.preflight`: the reason a click is refused is worth more than the click.
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

    So this answers one question — *where is the unit on screen* — and identity comes from
    `target.name_id` after the click, which is the only source that cannot be wrong about
    it. A ring matched by the "wrong" colour costs nothing, because nothing downstream
    believes the colour.
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

MEASURED: tuple[RingColour, ...] = (RingColour.GREEN, RingColour.YELLOW)
"""Which rules have met a real frame, and **the default search set**.

Red is written above by symmetry and is not searched, because no frame has proved it yet.
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
# The ceiling is 8 rather than 12 because 12 let a **nameplate plus its name text** in at
# 11.4 — a bar and a line of writing stacked, which is not an ellipse at any pitch. The
# bounds only exclude shapes no ellipse can be; they do not try to identify one.
RING_FILL_MAX = 0.55
RING_ASPECT = (0.8, 8.0)
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

# How far a plate may sit from a ring, horizontally, and still belong to the same unit.
# Generous, because the ring's centroid is pulled sideways by grass occluding one arc.
PLATE_PAIR_MAX_DX = 140


@dataclass(frozen=True)
class Ring:
    """The selection ring under the current target: its feet, on screen."""

    cx: float
    cy: float
    w: int
    h: int
    colour: RingColour
    area: int


@dataclass(frozen=True)
class Plate:
    """A nameplate health bar. One per visible unit that has a plate enabled."""

    cx: float
    cy: float
    w: int
    colour: RingColour

    def unit_below(self, drop: float = 0.55) -> tuple[int, int]:
        """Roughly where the unit is, below its plate. Bars are wider than rings for the
        same unit, so the ratio differs."""
        return (round(self.cx), round(self.cy + self.w * drop))


@dataclass(frozen=True)
class Sighting:
    """A unit found on screen: its feet, its head, and where to click it."""

    ring: Ring
    plate: Plate
    torso: tuple[int, int]

    @property
    def colour(self) -> RingColour:
        """What the client drew, not who the unit is. See `RingColour`."""
        return self.ring.colour


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
         colours: tuple[RingColour, ...] = MEASURED) -> Sighting | None:
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
    ring = _find_ring(frame, colours)
    if ring is None:
        return None
    plate = plate_for(ring, find_plates(frame, colours))
    if plate is None:
        return None
    return Sighting(ring=ring, plate=plate,
                    torso=(round(ring.cx), round((ring.cy + plate.cy) / 2)))


def _find_ring(frame: np.ndarray,
               colours: tuple[RingColour, ...] = MEASURED) -> Ring | None:
    """The selection ring. Exactly one exists, under whatever is targeted."""
    best: Ring | None = None
    for colour in colours:
        for blob in _blobs(mask_for(frame, colour)):
            if blob.area < RING_MIN_AREA or not _outside_interface(blob, frame.shape[:2]):
                continue
            aspect = blob.w / max(1.0, blob.h)
            fill = blob.area / max(1.0, blob.w * blob.h)
            if not (RING_ASPECT[0] <= aspect <= RING_ASPECT[1]):
                continue
            if fill > RING_FILL_MAX:
                continue        # solid: a bar, or an icon, not a ring
            if best is None or blob.area > best.area:
                best = Ring(cx=blob.cx, cy=blob.cy, w=int(blob.w), h=int(blob.h),
                            colour=colour, area=blob.area)
    return best


def plate_for(ring: Ring, plates: list[Plate]) -> Plate | None:
    """The nameplate belonging to a ring: nearest above it, horizontally overlapping.

    Above, because a plate floats over a unit's head and the ring is under its feet. The
    horizontal tolerance is loose because grass hides part of the ring and drags its
    centroid sideways.
    """
    candidates = [
        p for p in plates
        if p.cy < ring.cy and abs(p.cx - ring.cx) <= PLATE_PAIR_MAX_DX
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: ring.cy - p.cy)


def find_plates(frame: np.ndarray,
                colours: tuple[RingColour, ...] = MEASURED) -> list[Plate]:
    """Every nameplate health bar on screen, biggest first.

    Not needed to click the current target — the ring is better for that — but it is how
    a unit is found *before* it is targeted, which is what picking the next kobold out of
    a camp requires.
    """
    out: list[Plate] = []
    for colour in colours:
        for blob in _blobs(mask_for(frame, colour)):
            if blob.w < BAR_MIN_W or not _outside_interface(blob, frame.shape[:2]):
                continue
            aspect = blob.w / max(1.0, blob.h)
            fill = blob.area / max(1.0, blob.w * blob.h)
            if aspect >= BAR_ASPECT_MIN and fill >= BAR_FILL_MIN:
                out.append(Plate(cx=blob.cx, cy=blob.cy, w=int(blob.w), colour=colour))
    out.sort(key=lambda p: -p.w)
    return out
