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

from dataclasses import dataclass, replace
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
    InventorySlot,
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


def fnv1a32(s: str) -> int:
    """FNV-1a over the UTF-8 bytes. Lua strings are bytes, so UTF-8 is what the addon
    hashes for any locale whose names are not plain ASCII."""
    h = _FNV_OFFSET
    for byte in s.encode("utf-8"):
        h = ((h ^ byte) * _FNV_PRIME) & _UINT32
    return h


def fnv1a16(s: str) -> int:
    """FNV-1a folded from 32 bits to 16 by xor.

    Folded rather than truncated because that is FNV's own recommendation: truncation
    discards the mixing the high half performed.
    """
    h = fnv1a32(s)
    return ((h >> 16) ^ h) & 0xFFFF


def character_key(name: str, realm: str) -> int:
    """The code the addon paints for the player, `char.key`: its name and realm, 31 bits.

    A character's own place in the guide is saved under it, so two characters never share
    one. 31 bits leave the all-ones code free for not-available.
    """
    return fnv1a32(f"{name}-{realm}") % 0x7FFFFFFF


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

# The game's own ids, which is what `world_playercreateinfo_action` is keyed by. An
# earlier compact 1-N table of our own made human 1 and paladin 2 by coincidence and
# would have handed a warlock a druid's action bar.
CLASS_BY_ID: dict[int, str] = {
    1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest",
    7: "shaman", 8: "mage", 9: "warlock", 11: "druid",
}

RACE_BY_ID: dict[int, str] = {
    1: "human", 2: "orc", 3: "dwarf", 4: "nightelf", 5: "scourge",
    6: "tauren", 7: "gnome", 8: "troll", 10: "bloodelf", 11: "draenei",
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
MARKER_BALANCE = 0.35
"""How closely a marker's two **high** channels must match, as a fraction of the larger.

Cyan means green and blue are equal; blue means blue is much larger. The low-channel
ratio below does not say that, so a saturated blue Stormwind banner — `(30, 90, 200)` —
read as cyan and put **492,101 pixels** in the mask while the character stood in
Northshire Abbey. Measured on that frame: the banner's imbalance is 0.55 and the marker's
is 0. A ratio again, so a dimmed capture keeps its shape.

Magenta is the same test on red and blue."""

MARKER_LOW_RATIO = 0.5
"""How dark a marker's remaining channel must be, **as a fraction of its high ones**.

A ratio rather than a threshold, because this file already promises to decode a capture
at a gain of 0.25 — where a marker painted (0, 255, 255) arrives as (0, 64, 64) and any
absolute floor rejects it. Gain scales every channel together, so the *shape* of a colour
survives it and its brightness does not.

Measured on the frame that needed this: the marker is (0, 255, 255), and the desaturated
world a ghost sees is cyan and bright but not saturated — (125, 173, 188), a low channel
at two thirds of its high ones. Half is a clean gap on both sides."""

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
    # Preserve actual component extrema. A colour centroid need not be the centre of
    # its bounding rectangle (an occluded selection arc is a common example).
    bounds: tuple[int, int, int, int] | None = None


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
                bounds=(int(xmin), int(ymin), int(xmax) + 1, int(ymax) + 1),
            )
        )
    out.sort(key=lambda b: -b.area)
    return out


def _marker_masks(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pixels that could be a marker: the right *hue*, and saturated with it.

    The brightness floor is not decoration. The margins alone are relative, and a
    character that dies gets a full-screen desaturation shader that turns the entire world
    blue-green — Elwynn ground reads (100, 140, 153), which clears `g - r` and `b - r`
    comfortably. On a live ghost frame that put **293,555 pixels** in the cyan mask, the
    candidate search drowned, and `locate` returned `None` while both markers sat on
    screen pixel-exact. The bot went blind precisely when it was a ghost and needed to
    find its corpse.

    Two tests, and both earned their place on a live frame. The low channel must be dark
    *relative to the high ones*, which is what a desaturated ghost world fails. The two
    high channels must also **match each other**, which is what a saturated blue banner
    fails — cyan means green equals blue, and blue does not.

    Both are ratios rather than thresholds, because this file already promises to decode a
    capture at a gain of 0.25 — where a marker arrives at (0, 64, 64) and any absolute
    floor throws it away.
    """
    d = _rgb(frame).astype(np.int16)
    r, g, b = d[:, :, 0], d[:, :, 1], d[:, :, 2]
    left = ((r - g >= MARKER_MARGIN) & (b - g >= MARKER_MARGIN)      # magenta
            & (g <= MARKER_LOW_RATIO * np.minimum(r, b))
            & (np.abs(r - b) <= MARKER_BALANCE * np.maximum(r, b)))
    right = ((g - r >= MARKER_MARGIN) & (b - r >= MARKER_MARGIN)     # cyan
             & (r <= MARKER_LOW_RATIO * np.minimum(g, b))
             & (np.abs(g - b) <= MARKER_BALANCE * np.maximum(g, b)))
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
MAX_CELL_PX = 64.0      # past this the 'strip' spans half the screen


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
                # A run can also merge with the *scene* on its outer side, and then only
                # its inner edge means anything. Inside Echo Ridge Mine the teal cave,
                # (41, 101, 121), passes the cyan test, and the right marker's run went
                # on fifty pixels past the strip: every bracket above was wrong and the
                # strip read as absent (run 20260923T190938-3b2dd7). In the calibration
                # row the inner edges are exact - ten swatches lie between them, none a
                # marker colour.
                inner = (_rx0 - _lx1 - 1) / (GRID_COLS - 2)
                if MIN_CELL_PX <= inner <= MAX_CELL_PX and (lx0, rx1, -int(inner)) not in seen:
                    seen.add((lx0, rx1, -int(inner)))
                    icy = y + inner / 2.0 - 0.5
                    out.append((
                        _Blob(area=int(inner * inner), cx=_lx1 - inner / 2.0 + 0.5, cy=icy,
                              w=inner, h=inner),
                        _Blob(area=int(inner * inner), cx=_rx0 + inner / 2.0 - 0.5, cy=icy,
                              w=inner, h=inner),
                    ))
                if len(out) >= limit:
                    return out
    return out


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


# The grid of this process's last whole read, shared by every reader of the window: the
# client's and the targeting's own views of a fight (V190). The targeting decoded its frames
# without the client's remembered grid, and the misled locator of session 145 went on
# failing its checksum there: "blind target observation: checksum" (session 151).
_last_grid: Grid | None = None


def forget_grid() -> None:
    """Drop the shared grid (tests; a window that has moved is found again anyway)."""
    global _last_grid
    _last_grid = None


def _this_layout(grid: Grid) -> Grid:
    """A grid kept from a strip of another shape, with this layout's rows and columns.

    Where the strip is and how big its cells are outlast a schema that adds a row; the
    row count does not. `var/radio-grid.json` holds schema 17's eleven rows, and a
    twelve-row strip sampled on them is nine cells short: every read would fall back to
    the locator, the half of the reader the scenery can mislead (session 145). An older,
    shorter strip read on more rows is whole all the same, its schema's own layout
    deciding how much of the grid is payload (`radio.unpack_bits`)."""
    if (grid.rows, grid.cols) == (GRID_ROWS, GRID_COLS):
        return grid
    return replace(grid, rows=GRID_ROWS, cols=GRID_COLS)


def read(frame: np.ndarray, *, prev_seq: int | None = None,
         grid: Grid | None = None) -> RadioReading:
    """The whole pipeline: locate, sample, solve the transform, invert, unpack.

    `grid`, where the strip was last read whole, is tried first: the strip does not move
    while the window does not, and the locator can be misled where the grid cannot. Behind
    the strip's top-right corner a patch of Westfall's scenery passed the cyan marker's
    mask, the marker's blob came out 40 px wide for a 14 px cell, and the grid it implied
    was 4.5% too large: every read failed its checksum, a fight was cut off by blindness
    every other second until the character died, and the session sat blind beside the body
    (session 145). On the remembered grid those frames read whole.
    """
    global _last_grid
    for hint in dict.fromkeys(_this_layout(g) for g in (grid, _last_grid) if g is not None):
        hinted = _read_on(frame, hint, prev_seq)
        if hinted.ok or hinted.fault is SenseFault.STALE:
            _last_grid = hint
            return hinted
    located = locate(frame)
    if located is None:
        return RadioReading(None, False, SenseFault.NOT_FOUND, detail="no marker pair")
    reading = _read_on(frame, located, prev_seq)
    if reading.ok:
        _last_grid = located
    return reading


def _read_on(frame: np.ndarray, grid: Grid, prev_seq: int | None) -> RadioReading:
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


LIST_LINES = 5
"""How many gossip / greeting lines the strip carries. `fields.py` owns the reason."""


@dataclass(frozen=True)
class ListLine:
    """One clickable line of a gossip or quest-greeting frame."""

    index: int                    # 0-based, in the order the frame draws them
    name_id: int                  # fnv1a16 of the line's text, as `name_id` computes it
    x: float                      # fraction across the interface
    y: float                      # fraction down it


def list_lines(reading: RadioReading) -> tuple[ListLine, ...]:
    """Every list line the strip is painting, in frame order.

    Empty when no list is up, which is not the same as a list whose lines could not be
    read: a line with a position and no hash is dropped, because a line that cannot be
    identified must not be clicked. That is the whole reason the hash is painted — an NPC
    with two quests to hand in draws two lines a camera cannot tell apart.
    """
    if not reading.ok or not reading.values:
        return ()
    v = reading.values
    x = v.get("ui.list_x")
    if x is None:
        return ()
    out = []
    for i in range(LIST_LINES):
        y, h = v.get(f"ui.list_y{i}"), v.get(f"ui.list_hash{i}")
        if y is None or h is None:
            continue
        out.append(ListLine(index=i, name_id=h, x=x, y=y))
    return tuple(out)


def to_state(reading: RadioReading, *, t: float, client_id: str,
             quests: tuple[Quest, ...] | None = None) -> State:
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
        key=v.get("char.key"),
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

    # Schema 7 carries exact copper for merchant transaction verification. Historical
    # readings without it retain their original silver precision; the vendor executor
    # never uses that fallback as evidence for a transaction.
    silver = v["bags.money_silver"]
    copper = v.get("bags.money_copper")
    slot = None
    if v.get("inventory.bag") is not None and v.get("inventory.slot") is not None:
        slot = InventorySlot(bag=v["inventory.bag"], slot=v["inventory.slot"],
                             item_id=v.get("inventory.item_id"),
                             count=v.get("inventory.count"), quality=v.get("inventory.quality"),
                             locked=v.get("inventory.locked"))
    bags = Bags(
        free=v["bags.free"],
        durability_min=v["bags.durability_min"],
        money_copper=copper if copper is not None else silver * 100 if silver is not None else None,
        food_id=v.get("bags.food_id"), food_count=v.get("bags.food_count"),
        drink_id=v.get("bags.drink_id"), drink_count=v.get("bags.drink_count"),
        inventory_revision=v.get("inventory.revision"), inventory_total=v.get("inventory.total"),
        slot=slot,
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

    # Objectives are assembled in `jev.perceive.questlog`, not here, for the same reason
    # the log is: one frame carries one slot's counters, and a `Quest` built from them
    # would claim to describe a log this function has not seen. `quests.log_hash` stays in
    # `reading.values` — it is a change detector for whoever is reading, not state.
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
    # An assembled log from `QuestLog` beats this frame, and is the only way the result
    # has a log with anything in it. Passing it in rather than importing the accumulator
    # keeps the decode a pure function of one frame.
    if quests is None:
        count = v["quests.count"]
        quests = () if count == 0 else None

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
