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


class Reaction(StrEnum):
    FRIENDLY = "friendly"
    HOSTILE = "hostile"
    NEUTRAL = "neutral"


# Measured on live 1600x900 frames of a friendly target in Northshire: the ring runs
# RGB (89, 191, 3) to (102, 211, 7) — saturated yellow-green with **near-zero blue**,
# which is what separates it from grass. Northshire grass averages (104, 87, 9): brighter
# in red than green, where the ring is the reverse.
#
# Hostile and neutral rules are the same shape with the channels permuted. They are
# written down here rather than left for later so the engine is whole, and they carry a
# note that they have not yet met a live hostile target — see `MEASURED`.
_RULES: dict[Reaction, dict[str, int]] = {
    Reaction.FRIENDLY: {"hi": 1, "lo": 2, "other": 0, "hi_min": 150, "gap": 140, "lo_max": 40},
    Reaction.HOSTILE: {"hi": 0, "lo": 2, "other": 1, "hi_min": 150, "gap": 120, "lo_max": 60},
    Reaction.NEUTRAL: {"hi": 1, "lo": 2, "other": 0, "hi_min": 170, "gap": 130, "lo_max": 60},
}

MEASURED: tuple[Reaction, ...] = (Reaction.FRIENDLY,)
"""Which rules have met a real frame, and **the default search set**.

Hostile and neutral are written above by symmetry with the measured friendly rule, and
they are not trusted until a live hostile target has confirmed them. That is not caution
for its own sake: enabling the unmeasured hostile rule immediately produced a confident
false ring at (588, 574) on a frame whose only ring was friendly at (722, 355) — terrain,
matched by a threshold nobody had checked.

An unmeasured rule is a guess with a type annotation. Add a reaction here when a frame
has proved it, not before.
"""

# A ring is hollow; a bar is not. This is the whole discrimination.
RING_FILL_MAX = 0.55
RING_ASPECT = (1.1, 3.2)
RING_MIN_AREA = 60

BAR_FILL_MIN = 0.8
BAR_ASPECT_MIN = 12.0

# The player and target frames live in the top-left corner and are the same shape as a
# nameplate bar. Excluded by geometry rather than by colour, because they are the same
# colour on purpose.
INTERFACE_MARGIN_Y = 120
INTERFACE_MARGIN_X = 560

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
    reaction: Reaction
    area: int


@dataclass(frozen=True)
class Plate:
    """A nameplate health bar. One per visible unit that has a plate enabled."""

    cx: float
    cy: float
    w: int
    reaction: Reaction

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
    def reaction(self) -> Reaction:
        return self.ring.reaction


def mask_for(frame: np.ndarray, reaction: Reaction) -> np.ndarray:
    """Pixels that could belong to this reaction's ring or bar."""
    rule = _RULES[reaction]
    a = frame.astype(np.int16)
    hi = a[:, :, rule["hi"]]
    lo = a[:, :, rule["lo"]]
    other = a[:, :, rule["other"]]
    return (
        (hi > rule["hi_min"])
        & (hi - lo > rule["gap"])
        & (lo < rule["lo_max"])
        & (hi - other > 60)
    )


def _outside_interface(blob: _Blob) -> bool:
    """Interface bars sit in the top-left and are otherwise indistinguishable."""
    return not (blob.cy < INTERFACE_MARGIN_Y and blob.cx < INTERFACE_MARGIN_X)


def find(frame: np.ndarray,
         reactions: tuple[Reaction, ...] = MEASURED) -> Sighting | None:
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
    ring = _find_ring(frame, reactions)
    if ring is None:
        return None
    plate = plate_for(ring, find_plates(frame, reactions))
    if plate is None:
        return None
    return Sighting(ring=ring, plate=plate,
                    torso=(round(ring.cx), round((ring.cy + plate.cy) / 2)))


def _find_ring(frame: np.ndarray,
               reactions: tuple[Reaction, ...] = MEASURED) -> Ring | None:
    """The selection ring. Exactly one exists, under whatever is targeted."""
    best: Ring | None = None
    for reaction in reactions:
        for blob in _blobs(mask_for(frame, reaction)):
            if blob.area < RING_MIN_AREA or not _outside_interface(blob):
                continue
            aspect = blob.w / max(1.0, blob.h)
            fill = blob.area / max(1.0, blob.w * blob.h)
            if not (RING_ASPECT[0] <= aspect <= RING_ASPECT[1]):
                continue
            if fill > RING_FILL_MAX:
                continue        # solid: a bar, or an icon, not a ring
            if best is None or blob.area > best.area:
                best = Ring(cx=blob.cx, cy=blob.cy, w=int(blob.w), h=int(blob.h),
                            reaction=reaction, area=blob.area)
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
                reactions: tuple[Reaction, ...] = MEASURED) -> list[Plate]:
    """Every nameplate health bar on screen, biggest first.

    Not needed to click the current target — the ring is better for that — but it is how
    a unit is found *before* it is targeted, which is what picking the next kobold out of
    a camp requires.
    """
    out: list[Plate] = []
    for reaction in reactions:
        for blob in _blobs(mask_for(frame, reaction)):
            if not _outside_interface(blob):
                continue
            aspect = blob.w / max(1.0, blob.h)
            fill = blob.area / max(1.0, blob.w * blob.h)
            if aspect >= BAR_ASPECT_MIN and fill >= BAR_FILL_MIN:
                out.append(Plate(cx=blob.cx, cy=blob.cy, w=int(blob.w), reaction=reaction))
    out.sort(key=lambda p: -p.w)
    return out
