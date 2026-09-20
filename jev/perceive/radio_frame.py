"""JevRadio decoder — a captured frame to `state_v1`.

`radio.py` owns the wire format and knows nothing about images. This module owns the
optics and depends on that one, never the reverse, so the whole codec stays testable with
no capture and no numpy while everything here is exercised against synthesised frames.

numpy is the only dependency. Finding two saturated markers and taking a median over a
cell interior is array arithmetic; pulling in OpenCV for it would add a wheel the size of
the rest of the project to do less than a hundred lines.

The pipeline, and what each stage can fail with
-----------------------------------------------
    locate   markers not found            -> SenseFault.NOT_FOUND
    sample   (cannot fail on its own)
    solve    calibration row unreadable   -> SenseFault.CALIBRATION
    unpack   checksum or schema refused   -> SenseFault.CHECKSUM
    seq      unchanged since last read    -> SenseFault.STALE

The last one is the point of the sequence counter. A strip that decodes perfectly and
says exactly what it said 100 ms ago is a live strip that stopped updating — a hung addon
or a frozen client — and that is a different postmortem from a bad read, even though both
set `addon_ok=False` (ARCHITECTURE.md §7). A caller that does not supply `prev_seq` is not
asking the question, and gets no answer to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from jev.perceive import radio
from jev.perceive.fields import (
    CALIBRATION_ROWS,
    CALIBRATION_SWATCHES,
    GRID_COLS,
    GRID_ROWS,
    PAYLOAD_CELLS,
)
from jev.world.state_v1 import (
    Bags,
    Char,
    Classification,
    Flags,
    Objective,
    Pos,
    PowerType,
    Quest,
    Reaction,
    Sense,
    SenseFault,
    Source,
    State,
    Target,
    Ui,
    Vitals,
)

# --------------------------------------------------------------------------- hashing
#
# The addon paints names as numbers. `addons/JevRadio/Helpers.lua` computes the same hash
# in Lua 5.1 with no bitwise operators, and `tests/test_radio_frame.py` proves the two
# agree by tracing the Lua arithmetic rather than by trusting this comment.

_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619
_UINT32 = 0xFFFFFFFF


def fnv1a16(s: str) -> int:
    """FNV-1a over the UTF-8 bytes, folded from 32 bits to 16 by xor.

    Folded rather than truncated because that is FNV's own recommendation: truncation
    discards the mixing the high half performed. Lua strings are bytes, so UTF-8 is what
    the addon hashes for any locale whose names are not plain ASCII.
    """
    h = _FNV_OFFSET
    for byte in s.encode("utf-8"):
        h = ((h ^ byte) * _FNV_PRIME) & _UINT32
    return ((h >> 16) ^ h) & 0xFFFF


def name_id(name: str) -> int:
    """The code the addon paints for a unit name.

    16 bits, so 65535 is the not-available sentinel and a name landing on it is moved off
    by one. Two names in 65536 then share a code, which is the deal: a phantom "no target"
    costs a decision, a shared code costs a lookup that returns two candidates.
    """
    h = fnv1a16(name)
    return 65534 if h == 0xFFFF else h


def zone_id(map_file: str) -> int:
    """The code the addon paints for `GetMapInfo()`, e.g. `"Elwynn"`.

    14 bits, so the hash is taken modulo the sentinel. The 68 TBC zone map files are
    collision-free under this, which the tests assert against the real table rather than
    assume.
    """
    return fnv1a16(map_file) % 16383


# --------------------------------------------------------------------------- enums
#
# Inverses of the tables in `Helpers.lua`. These are agreement by convention, which decays
# silently (DECISIONS.md V13), so the tests parse the Lua and compare. They should be
# generated; see the report note.

CLASS_BY_ID: dict[int, str] = {
    1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest",
    6: "shaman", 7: "mage", 8: "warlock", 9: "druid",
}

RACE_BY_ID: dict[int, str] = {
    1: "human", 2: "dwarf", 3: "nightelf", 4: "gnome", 5: "draenei",
    6: "orc", 7: "scourge", 8: "tauren", 9: "troll", 10: "bloodelf",
}

CLASSIFICATION_BY_ID: dict[int, Classification] = {
    1: Classification.NORMAL,
    2: Classification.ELITE,
    3: Classification.RARE,
    4: Classification.RARE_ELITE,
    5: Classification.BOSS,
}

# Index 0 is "no error pending"; index 1 is an error the table does not model, which is
# still a positive observation that something failed. Order is the wire contract.
UI_ERROR_KEYS: tuple[str, ...] = (
    "", "other", "out_of_range", "not_facing", "no_line_of_sight", "bad_target",
    "no_target", "target_dead", "no_power", "not_ready", "already_casting", "moving",
    "immune", "too_close", "player_dead", "bags_full", "quest_log_full",
    "not_enough_money", "cannot_do_that",
)

# Faction follows from race with no further observation, so deriving it is not a guess.
FACTION_BY_RACE: dict[str, str] = {
    "human": "alliance", "dwarf": "alliance", "nightelf": "alliance",
    "gnome": "alliance", "draenei": "alliance",
    "orc": "horde", "scourge": "horde", "tauren": "horde",
    "troll": "horde", "bloodelf": "horde",
}

# UnitPowerType's indices. 4 is happiness, which no player has; an index outside the four
# the game gives a player is unmodelled, and unmodelled is not PowerType.NONE.
POWER_BY_ID: dict[int, PowerType] = {
    0: PowerType.MANA, 1: PowerType.RAGE, 2: PowerType.FOCUS, 3: PowerType.ENERGY,
}


# --------------------------------------------------------------------------- geometry

# A channel gap that survives the worst transform `solve_transform` will still accept.
# The markers differ by a full 255 in two channels; at the 0.25 gain floor that lands at
# 64, so 40 leaves headroom without admitting ordinary UI purple.
MARKER_MARGIN = 40

# How far an inverted calibration swatch may sit from the colour it is supposed to be,
# per channel, before a candidate grid is not the strip. A real strip lands within a few
# levels; the codec itself tolerates half a quantisation step, which is 8, so 24 refuses
# anything the decode could not have survived in any case.
MAX_SWATCH_RESIDUAL = 24.0

# A calibration row spans black to white in every channel, so a candidate row that spans
# less than this is not carrying the swatches: it is either washed out or it is payload
# that happens to be flat. Same reasoning as MARKER_MARGIN — the 0.25 gain floor still
# leaves 64 levels between the extremes.
FLAT_ROW_SPREAD = 40.0

# Below this a cell has no interior left to take a median over, so a "grid" this small is
# a pair of coincidental blobs rather than the strip.
MIN_CELL_PX = 3.0

# Fraction of a cell sampled. The outer 40% is where rescaling and chroma subsampling put
# their damage, and it is also where a half-pixel layout error shows up first.
INNER = 0.6


@dataclass(frozen=True)
class Grid:
    """Where the strip is, in frame pixels.

    `dx` is signed so a mirrored capture still reads: column 0 is always the magenta
    marker, whichever side of the frame it landed on. There is no equivalent for the
    vertical axis — two markers on one row cannot tell you which way is down — so rows are
    assumed to descend, and a vertically flipped capture is a capture bug, not a strip
    state.
    """

    x0: float          # centre of row 0, column 0 (the magenta marker)
    y0: float
    dx: float          # signed step per column
    dy: float          # step per row, positive downward
    cell_w: float
    cell_h: float
    rows: int = GRID_ROWS
    cols: int = GRID_COLS

    def centre(self, row: int, col: int) -> tuple[float, float]:
        return self.x0 + col * self.dx, self.y0 + row * self.dy


@dataclass(frozen=True)
class _Blob:
    area: int
    cx: float
    cy: float
    w: float
    h: float


def _rgb(frame: np.ndarray) -> np.ndarray:
    a = np.asarray(frame)
    if a.ndim != 3 or a.shape[2] < 3:
        raise ValueError(f"expected an HxWx3 RGB frame, got {a.shape}")
    return a[:, :, :3]


def _blobs(mask: np.ndarray) -> list[_Blob]:
    """4-connected components of a boolean mask, as run-length unions.

    Row runs plus union-find rather than a labelling pass over every pixel: the masks here
    are two small saturated squares in an otherwise empty frame, so the work is
    proportional to the handful of runs instead of to the megapixels.
    """
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    edges = np.diff(padded, axis=1)
    starts = np.argwhere(edges == 1)
    ends = np.argwhere(edges == -1)
    if len(starts) == 0:
        return []

    rows = starts[:, 0]
    x0 = starts[:, 1]
    x1 = ends[:, 1]  # exclusive; argwhere keeps row-major order so these pair up

    parent = list(range(len(rows)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Runs are already ordered by (row, x0), so the previous row's slice is a contiguous
    # window and the overlap test is a merge, not a search.
    prev_start = prev_end = 0
    i = 0
    n = len(rows)
    while i < n:
        j = i
        while j < n and rows[j] == rows[i]:
            j += 1
        if i > 0 and rows[i - 1] == rows[i] - 1:
            k = prev_start
            for cur in range(i, j):
                while k < prev_end and x1[k] <= x0[cur]:
                    k += 1
                m = k
                while m < prev_end and x0[m] < x1[cur]:
                    union(m, cur)
                    m += 1
        prev_start, prev_end = i, j
        i = j

    acc: dict[int, list[float]] = {}
    for idx in range(n):
        root = find(idx)
        length = int(x1[idx] - x0[idx])
        r = int(rows[idx])
        a = acc.get(root)
        centre_sum = (x0[idx] + x1[idx] - 1) / 2.0 * length
        if a is None:
            acc[root] = [length, centre_sum, r * length, x0[idx], x1[idx] - 1, r, r]
        else:
            a[0] += length
            a[1] += centre_sum
            a[2] += r * length
            a[3] = min(a[3], x0[idx])
            a[4] = max(a[4], x1[idx] - 1)
            a[5] = min(a[5], r)
            a[6] = max(a[6], r)

    out = []
    for area, sx, sy, xmin, xmax, ymin, ymax in acc.values():
        out.append(
            _Blob(
                area=int(area),
                cx=sx / area,
                cy=sy / area,
                w=xmax - xmin + 1,
                h=ymax - ymin + 1,
            )
        )
    out.sort(key=lambda b: -b.area)
    return out


def _marker_masks(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = _rgb(frame).astype(np.int16)
    r, g, b = d[:, :, 0], d[:, :, 1], d[:, :, 2]
    left = (r - g >= MARKER_MARGIN) & (b - g >= MARKER_MARGIN)    # magenta
    right = (g - r >= MARKER_MARGIN) & (b - r >= MARKER_MARGIN)   # cyan
    return left, right


def _sample_cell(
    img: np.ndarray, grid: Grid, row: int, col: int, half_w: float, half_h: float
) -> tuple[float, float, float]:
    height, width = img.shape[0], img.shape[1]
    cx, cy = grid.centre(row, col)
    x0 = max(0, min(width - 1, round(cx - half_w)))
    x1 = max(x0 + 1, min(width, round(cx + half_w) + 1))
    y0 = max(0, min(height - 1, round(cy - half_h)))
    y1 = max(y0 + 1, min(height, round(cy + half_h) + 1))
    med = np.median(img[y0:y1, x0:x1].reshape(-1, 3).astype(np.float64), axis=0)
    return float(med[0]), float(med[1]), float(med[2])


def _score_row0(img: np.ndarray, grid: Grid) -> tuple[float | None, float]:
    """Judge a candidate grid by the ten cells between its markers.

    Returns (residual, spread). `residual` is how far the swatches sit from the colours
    they claim to be once the solved transform is undone, or `None` if the row will not
    solve at all. `spread` is the widest per-channel range across the ten samples, and it
    is what tells a washed-out calibration row — flat, the strip is there and unreadable
    — from a row of payload that merely happens to sit between two coloured cells. The
    two answers are different faults, so they are measured separately.
    """
    half_w = max(0.5, grid.cell_w * INNER / 2.0)
    half_h = max(0.5, grid.cell_h * INNER / 2.0)
    observed = [
        _sample_cell(img, grid, 0, col, half_w, half_h)
        for col in range(1, 1 + len(CALIBRATION_SWATCHES))
    ]
    spread = max(
        max(o[ch] for o in observed) - min(o[ch] for o in observed) for ch in range(3)
    )
    try:
        transform = radio.solve_transform(observed)
    except radio.DecodeError:
        return None, spread
    worst = 0.0
    for obs, true in zip(observed, CALIBRATION_SWATCHES, strict=True):
        back = radio.apply_inverse(obs, transform)
        worst = max(worst, max(abs(a - b) for a, b in zip(back, true, strict=True)))
    return worst, spread


MAX_CANDIDATES = 24
MIN_FILL = 0.7          # a marker cell is solid, not an outline
MAX_CELL_PX = 64.0      # past this the 'strip' spans half the screen
ASPECT_TOLERANCE = 1.8  # generous: a scaled capture is not exactly square


def _runs(row: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True spans in one mask row, as (start, end) inclusive."""
    xs = np.flatnonzero(row)
    if xs.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(xs) > 1)
    starts = np.concatenate(([xs[0]], xs[breaks + 1]))
    ends = np.concatenate((xs[breaks], [xs[-1]]))
    return list(zip(starts.tolist(), ends.tolist(), strict=True))


def _strip_candidates(left: np.ndarray, right: np.ndarray,
                      limit: int = MAX_CANDIDATES) -> list[tuple[_Blob, _Blob]]:
    """Propose (left marker, right marker) pairs from **row runs**, not from blobs.

    Connected-component detection is the wrong primitive for this image, and two live
    runs proved it in opposite directions. A marker sits flush against payload cells that
    can carry its exact colour — magenta quantises to nibbles (15,0,15) and cyan to
    (0,15,15), both of which payload reaches — so the component containing a marker is
    whatever happens to touch it that frame. Once it was five slivers of interface ranked
    above it by area. Once it was a 46x48 sprawl at 0.56 fill that a solidity filter then
    discarded, while the marker sat in plain sight at the centre of it.

    A row run is immune to all of that. The strip occupies twelve cells on one row, its
    first cell is magenta and its last is cyan, so **the left edge of a magenta run and
    the right edge of a cyan run bracket the strip** however much either run has merged
    with its neighbours. Cell width follows from the span, and the caller then scores the
    ten known swatches between them — which is the signature that actually identifies the
    strip.
    """
    rows = np.flatnonzero(left.any(axis=1) & right.any(axis=1))
    out: list[tuple[_Blob, _Blob]] = []
    seen: set[tuple[int, int, int]] = set()

    for y in rows.tolist():
        for lx0, _lx1 in _runs(left[y]):
            for _rx0, rx1 in _runs(right[y]):
                span = rx1 - lx0 + 1
                if span <= 0:
                    continue
                cell = span / GRID_COLS
                if cell < MIN_CELL_PX or cell > MAX_CELL_PX:
                    continue
                key = (lx0, rx1, int(cell))
                if key in seen:
                    continue
                seen.add(key)
                half = cell / 2.0

                # Two hypotheses per side, because neither is right in both cases.
                # A run that is exactly one cell gives its centre directly, which
                # survives the edge erosion that blur and rescaling cause. A run that
                # has merged with a same-coloured neighbour has a meaningless centre,
                # and only its outer edge locates the strip. Offering both costs a
                # handful of candidates and the calibration score decides between them —
                # which is what the score is for.
                lefts = {lx0 + half, (lx0 + _lx1) / 2.0}
                rights = {rx1 - half, (_rx0 + rx1) / 2.0}
                cy = y + half - 0.5
                for lcx in lefts:
                    for rcx in rights:
                        if rcx - lcx < MIN_CELL_PX * (GRID_COLS - 1):
                            continue
                        out.append((
                            _Blob(area=int(cell * cell), cx=lcx, cy=cy, w=cell, h=cell),
                            _Blob(area=int(cell * cell), cx=rcx, cy=cy, w=cell, h=cell),
                        ))
                if len(out) >= limit:
                    return out
    return out


def _marker_candidates(mask: np.ndarray, limit: int = MAX_CANDIDATES) -> list[_Blob]:
    """Blobs that could be a marker cell: square, solid, big enough — or *inside* one.

    Filtering on **shape before size** is what makes this work on a real screen. The
    markers are square filled cells; the rest of the magenta and cyan on a WoW screen is
    text, bar fill, spell glow and icon edging, none of which is square. Measured live:
    the cyan mask held 3,798 pixels against the marker's 196, and ranking by area put five
    57x7 slivers of interface ahead of the real marker.

    But a square-only filter is brittle in the other direction, and that cost a live run
    too. A payload cell next to a marker can carry the *same* colour — magenta is nibbles
    (15,0,15), cyan is (0,15,15), and payload reaches both — so the two merge into one
    blob twice as wide as it is tall, and the marker disappears from the candidate list
    entirely while sitting in plain sight. So a wide blob is not discarded: it is split at
    its ends, because whichever cell in it is the real marker, the marker is flush with
    one edge of the run.
    """
    out: list[_Blob] = []
    for b in _blobs(mask):
        if b.w < MIN_CELL_PX or b.h < MIN_CELL_PX:
            continue
        if b.area < MIN_FILL * b.w * b.h:
            continue

        aspect = b.w / max(1.0, b.h)
        if (1 / ASPECT_TOLERANCE) <= aspect <= ASPECT_TOLERANCE:
            out.append(b)
            continue

        # Too wide to be one cell: offer the cell at each end of the run. A marker is
        # always at an edge of the merge, because it is at an edge of the strip.
        if aspect > ASPECT_TOLERANCE:
            half = b.h / 2.0
            left_edge = b.cx - b.w / 2.0
            right_edge = b.cx + b.w / 2.0
            for cx in (left_edge + half, right_edge - half):
                out.append(_Blob(area=int(b.h * b.h), cx=cx, cy=b.cy,
                                 w=b.h, h=b.h))

    out.sort(key=lambda blob: -blob.area)
    return out[:limit]


def locate(frame: np.ndarray) -> Grid | None:
    """Find the strip from its two markers. `None` is `SenseFault.NOT_FOUND`.

    The markers, not a fixed screen offset, are what make the strip survive the window
    being moved, resized or rescaled (ARCHITECTURE.md §7).

    Two markers alone are not enough to identify it, though. `fields.py` says the markers
    are colours no payload cell can produce, and that is not so: magenta is nibbles
    (15, 0, 15) and cyan is (0, 15, 15), both of which a payload cell reaches whenever the
    bits land that way. So candidate pairs are scored on the *calibration row* between
    them — ten known colours are a far stronger signature than two — and the pair whose
    swatches invert closest to the truth wins.

    A pair whose row 0 is flat rather than merely wrong is kept as a fallback, so a
    washed-out calibration row still reaches the caller as CALIBRATION and not as a
    missing strip. A pair with a perfectly lively row of payload between it is neither,
    which is how half a bracket fails to be a grid at all.
    """
    img = _rgb(frame)
    left_mask, right_mask = _marker_masks(frame)
    pairs = _strip_candidates(left_mask, right_mask)
    if not pairs:
        return None

    best: tuple[float, Grid] | None = None
    fallback: tuple[float, Grid] | None = None

    for bl, br in pairs:
        if True:
            dx = (br.cx - bl.cx) / (GRID_COLS - 1)
            cell_w = abs(dx)
            if cell_w < MIN_CELL_PX:
                continue
            # The row pitch comes from the markers' aspect, not from their height. A blur
            # or a rescale bleeds a blob outward by a pixel or two in every direction, so
            # its absolute height is inflated and its shape is not; taking the height
            # directly puts the bottom row half a cell out on a four-row grid. The ratio
            # still tracks a genuinely anamorphic capture, which a hardcoded square would
            # not.
            blob_w = (bl.w + br.w) / 2.0
            blob_h = (bl.h + br.h) / 2.0
            if blob_w <= 0 or blob_h <= 0:
                continue
            cell_h = cell_w * (blob_h / blob_w)
            if cell_h < MIN_CELL_PX:
                continue
            if abs(br.cy - bl.cy) > cell_h:
                continue  # the markers bracket one row; these are not on the same one
            # Each marker is exactly one cell, so its own bounding box has to agree with
            # the spacing the pair implies.
            shape = max(blob_w / cell_w, cell_w / blob_w, blob_h / cell_h, cell_h / blob_h)
            if shape > 2.0:
                continue

            grid = Grid(
                x0=bl.cx, y0=bl.cy, dx=dx, dy=cell_h, cell_w=cell_w, cell_h=cell_h
            )
            residual, spread = _score_row0(img, grid)
            if spread < FLAT_ROW_SPREAD and (fallback is None or shape < fallback[0]):
                fallback = (shape, grid)
            if residual is None or residual > MAX_SWATCH_RESIDUAL:
                continue
            if best is None or residual < best[0]:
                best = (residual, grid)

    if best is not None:
        return best[1]
    return fallback[1] if fallback is not None else None


def sample(frame: np.ndarray, grid: Grid) -> list[list[tuple[float, float, float]]]:
    """One observed colour per cell, row-major, calibration row included.

    The median of the cell's inner ~60%, not the centre pixel. Edge bleed from the
    capture's rescale and chroma handling lives at the borders, and a single pixel is one
    dropout or one half-pixel layout error away from reading a different nibble; a median
    over an interior needs most of the cell to be wrong before it moves.
    """
    img = _rgb(frame)
    half_w = max(0.5, grid.cell_w * INNER / 2.0)
    half_h = max(0.5, grid.cell_h * INNER / 2.0)
    return [
        [_sample_cell(img, grid, row, col, half_w, half_h) for col in range(grid.cols)]
        for row in range(grid.rows)
    ]


# --------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class RadioReading:
    """One attempt at the strip, successful or not, with its postmortem attached.

    `values` survives a `STALE` verdict on purpose: a hung addon's last painted state is
    evidence about where it hung, and discarding it would make the two failure modes
    indistinguishable in a log, which is the thing the sequence counter exists to prevent.
    """

    values: dict[str, Any] | None
    ok: bool
    fault: SenseFault
    seq: int | None = None
    detail: str = ""
    grid: Grid | None = None
    transform: tuple[tuple[float, float], ...] | None = None


def read(frame: np.ndarray, *, prev_seq: int | None = None) -> RadioReading:
    """The whole pipeline: locate, sample, solve the transform, invert, unpack."""
    grid = locate(frame)
    if grid is None:
        return RadioReading(None, False, SenseFault.NOT_FOUND, detail="no marker pair")

    observed = sample(frame, grid)

    # Row 0 is [marker, ten swatches, marker]; only the swatches constrain the transform.
    swatches = observed[0][1 : 1 + len(CALIBRATION_SWATCHES)]
    try:
        transform = radio.solve_transform(swatches)
    except radio.DecodeError as exc:
        return RadioReading(
            None, False, SenseFault.CALIBRATION, detail=str(exc), grid=grid
        )

    flat = [cell for line in observed[CALIBRATION_ROWS:] for cell in line]
    cells = [radio.apply_inverse(c, transform) for c in flat[:PAYLOAD_CELLS]]

    try:
        values = radio.unpack(cells)
    except radio.DecodeError as exc:
        # `short` means the grid we found is not the shape fields.py describes, which is a
        # geometry failure; `checksum` and `schema` mean we read a strip and could not
        # believe it. SenseFault has no member for a build mismatch, so schema lands on
        # CHECKSUM and `detail` carries which of the two it actually was.
        fault = SenseFault.NOT_FOUND if exc.reason == "short" else SenseFault.CHECKSUM
        return RadioReading(
            None, False, fault, detail=str(exc), grid=grid, transform=transform
        )

    seq = values["seq"]
    if prev_seq is not None and seq == prev_seq:
        return RadioReading(
            values, False, SenseFault.STALE, seq=seq,
            detail=f"sequence held at {seq}", grid=grid, transform=transform,
        )

    return RadioReading(
        values, True, SenseFault.NONE, seq=seq, grid=grid, transform=transform
    )


# --------------------------------------------------------------------------- to state


def _level(value: Any) -> int | None:
    """A level outside 1..70 on a TBC server is a misread, not a character."""
    if value is None or not 1 <= value <= 70:
        return None
    return int(value)


def _reaction(code: int | None) -> Reaction | None:
    # The getter clamps `UnitReaction(...) or 0`, so 0 is the client having declined to
    # answer. UnitReaction's own scale is 1-2 hostile, 3 unfriendly, 4 neutral, 5+ friendly.
    if not code:
        return None
    if code <= 3:
        return Reaction.HOSTILE
    if code == 4:
        return Reaction.NEUTRAL
    return Reaction.FRIENDLY


def to_state(reading: RadioReading, *, t: float, client_id: str) -> State:
    """A decoded strip as `state_v1`.

    Field names in `fields.py` are the dotted paths of this model, so this is a
    transcription rather than an interpretation, with three exceptions that are each
    commented where they happen.

    Names are left unresolved. `target.name_id` is a 16-bit hash and roughly one in two
    hundred of the 1,003 known TBC creature names shares a code with another, so resolving
    it honestly yields a candidate set against the world DB, not a name — that belongs to
    whoever owns the name table. `name_id()` and `zone_id()` above are exported so such a
    resolver can build its inverse from the same hash both sides already agree on.
    """
    sense = Sense(
        addon_ok=reading.ok,
        fault=reading.fault,
        seq=reading.seq,
        source=Source.RADIO,
    )
    if reading.values is None:
        # A read that failed is still a tick, and it has to say so in the same shape as
        # one that worked. Absence of an answer is not an answer.
        return State(t=t, client_id=client_id, sense=sense)

    v = reading.values

    race = RACE_BY_ID.get(v["char.race_id"])
    char = Char(
        cls=CLASS_BY_ID.get(v["char.class_id"]),
        race=race,
        faction=FACTION_BY_RACE.get(race) if race else None,
        level=_level(v["char.level"]),
        xp_pct=v["char.xp_pct"],
    )

    # mx/my are a percentage of a zone map and mean nothing without the zone they are a
    # percentage of (state_v1 module docstring). The addon already withholds all three
    # together; dropping them here as well costs nothing and closes the case where it did not.
    zone = v["pos.zone_id"]
    pos = Pos(
        zone_id=zone,
        mx=v["pos.mx"] if zone is not None else None,
        my=v["pos.my"] if zone is not None else None,
        facing=v["pos.facing"],
        indoors=v["pos.indoors"],
    )

    vitals = Vitals(
        hp=v["vitals.hp"],
        hp_max=v["vitals.hp_max"],
        power=v["vitals.power"],
        power_max=v["vitals.power_max"],
        power_type=POWER_BY_ID.get(v["vitals.power_type"])
        if v["vitals.power_type"] is not None
        else None,
        combat=v["vitals.combat"],
        dead=v["vitals.dead"],
        ghost=v["vitals.ghost"],
    )

    flags = Flags(
        mounted=v["flags.mounted"],
        swimming=v["flags.swimming"],
        falling=v["flags.falling"],
        on_taxi=v["flags.on_taxi"],
        resting=v["flags.resting"],
        stealthed=v["flags.stealthed"],
        afk=v["flags.afk"],
    )

    target_level = v["target.level"]
    target = Target(
        has=v["target.has"],
        name=None,
        level=target_level if target_level else None,   # a unit at level 0 does not exist
        hp=v["target.hp"],
        reaction=_reaction(v["target.reaction"]),
        classification=CLASSIFICATION_BY_ID.get(v["target.classification"]),
        attacking_me=v["target.attacking_me"],
        in_melee=v["target.in_melee"],
    )

    # The wire carries silver: 21 bits of copper would not reach level 40's mount, and
    # nothing above System 1 has ever needed the last two digits. The copper the model
    # wants is therefore exact to a silver and no finer, which is worth knowing before
    # anyone writes a test that expects a vendor price to reconcile.
    silver = v["bags.money_silver"]
    bags = Bags(
        free=v["bags.free"],
        durability_min=v["bags.durability_min"],
        money_copper=silver * 100 if silver is not None else None,
    )

    error_id = v["ui.error_id"]
    ui = Ui(
        loot=v["ui.loot"],
        gossip=v["ui.gossip"],
        vendor=v["ui.vendor"],
        quest_frame=v["ui.quest_frame"],
        trainer=v["ui.trainer"],
        mail=v["ui.mail"],
        modal=v["ui.modal"],
        error=UI_ERROR_KEYS[error_id]
        if error_id and error_id < len(UI_ERROR_KEYS)
        else None,
    )

    # The strip carries objective counters, not objective text. An empty `text` is the
    # absence of a label, not a label that is empty; the tracker's predicate is
    # `objective_counts()` and joins on numbers. `quests.log_hash` has no home in the
    # model at all — it is a change detector for whoever is reading, not state — so it
    # stays in `reading.values` for the caller that wants it.
    objectives = tuple(
        Objective(text="", have=v[f"quests.o{i}_have"], need=v[f"quests.o{i}_need"])
        for i in range(3)
        if v[f"quests.o{i}_have"] is not None and v[f"quests.o{i}_need"] is not None
    )
    # One frame is never a log.
    #
    # The strip cycles one entry per paint, so a frame carries one quest out of however
    # many `count` says there are. Returning that as a one-quest log is wrong in a way
    # that looks fine: the tracker reads it, finds its step's quest absent, and
    # `QUEST_MISSING` skips a live step — twice a second, for as long as the log has more
    # than one entry in it.
    #
    # So `count > 0` is **unread** here. `jev.perceive.questlog.QuestLog` accumulates
    # across frames and is the only thing allowed to produce a log. The one case a single
    # frame does settle is an empty one: there are no slots to wait for.
    count = v["quests.count"]
    quests: tuple[Quest, ...] | None = () if count == 0 else None

    return State(
        t=t,
        client_id=client_id,
        char=char,
        pos=pos,
        vitals=vitals,
        flags=flags,
        target=target,
        bags=bags,
        ui=ui,
        quests=quests,
        sense=sense,
    )
