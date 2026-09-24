"""JevRadio codec — values <-> cells. Pure, no image dependency.

The addon packs; this unpacks. Both sides are driven by `fields.FIELDS`, so a field cannot
exist on one side only. Image work (finding the strip in a frame, sampling cell centres)
lives in `radio_frame.py` and depends on this, not the other way round — which means the
whole wire format can be tested with no capture, no client and no numpy.

Round-tripping is the test that matters: `unpack(pack(v)) == v` for every field, at every
boundary value, including not-available.
"""

from __future__ import annotations

import math
from typing import Any

from jev.perceive.fields import (
    BITS_PER_CELL,
    BITS_PER_CHANNEL,
    CALIBRATION_SWATCHES,
    CHECKSUM_BITS,
    EXTENDED,
    FIELDS,
    GRID_COLS,
    LAST_HEADER_SCHEMA,
    LEVELS,
    MARKER_L,
    MARKER_R,
    SCHEMA,
    SCHEMA_FIELDS,
    STEP,
    Field,
    Kind,
    checksum,
)

TAU = 2.0 * math.pi
NA = None  # what an unobservable field encodes to, and decodes back into


class DecodeError(Exception):
    """The strip was found but could not be believed. Carries which check failed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- per-field


def encode_field(f: Field, value: Any) -> int:
    """Value -> integer code. `None` becomes the not-available sentinel."""
    if value is None:
        return f.na
    match f.kind:
        case Kind.TRI:
            return 2 if value else 1
        case Kind.FRAC:
            if not (0.0 <= value <= 1.0):
                value = min(1.0, max(0.0, float(value)))
            return round(value * (f.span - 1))
        case Kind.ANGLE:
            # floor, not round: rounding lets a value just under tau land on `span`,
            # which is the NA code. A facing of 359.99 degrees is not "unknown".
            return int((float(value) % TAU) / TAU * f.span) % f.span
        case _:  # UINT, ENUM
            v = int(value)
            if v < 0 or v >= f.na:
                raise ValueError(f"{f.name}={v} does not fit {f.bits} bits (na={f.na})")
            return v


def decode_field(f: Field, code: int) -> Any:
    """Integer code -> value. The sentinel becomes `None`, never a plausible zero."""
    if f.kind is Kind.TRI:
        return None if code == 0 else (code == 2)
    if code == f.na:
        return None
    match f.kind:
        case Kind.FRAC:
            return code / (f.span - 1)
        case Kind.ANGLE:
            return code / f.span * TAU
        case _:
            return code


# --------------------------------------------------------------------------- bitstream


def pack_bits(values: dict[str, Any]) -> str:
    """Field values -> payload bitstring, MSB-first, with the checksum appended.

    `schema` is the schema number, as `unpack_bits` returns it; the header's code for it
    (EXTENDED, with the number in `schema_rev`) is this function's business.
    """
    values = {**values, "schema": EXTENDED, "schema_rev": values.get("schema", SCHEMA)}
    out: list[str] = []
    for f in FIELDS:
        code = encode_field(f, values.get(f.name))
        out.append(format(code, f"0{f.bits}b"))
    payload = "".join(out)
    return payload + format(checksum(payload), f"0{CHECKSUM_BITS}b")


def unpack_bits(bits: str) -> dict[str, Any]:
    """Payload bitstring -> field values. Verifies the checksum before believing any of it."""
    if len(bits) < FIELDS[0].bits:
        raise DecodeError("short", "missing schema header")
    width = FIELDS[0].bits
    header = int(bits[:width], 2)
    extended = header == EXTENDED
    version = int(bits[width:width + FIELDS[1].bits] or "0", 2) if extended else header
    # The header names schemas up to 14 itself and the revision byte names the rest; a
    # number arriving the other way (a header of 15 is its not-available code) is no
    # layout at all.
    known = version in SCHEMA_FIELDS and extended == (version > LAST_HEADER_SCHEMA)
    # An unknown header may itself be corrupt. Check the current layout's integrity
    # before classifying it as a build mismatch, preserving the checksum/schema split.
    fields = SCHEMA_FIELDS[version] if known else FIELDS
    need = sum(f.bits for f in fields) + CHECKSUM_BITS
    if len(bits) < need:
        raise DecodeError("short", f"{len(bits)} bits, need {need}")

    payload, tail = bits[: need - CHECKSUM_BITS], bits[need - CHECKSUM_BITS : need]
    want, got = checksum(payload), int(tail, 2)
    if want != got:
        raise DecodeError("checksum", f"computed {want:#06x}, read {got:#06x}")
    if not known:
        raise DecodeError("schema", f"strip says {version}, decoder is {SCHEMA}")

    values: dict[str, Any] = {f.name: None for f in FIELDS}
    i = 0
    for f in fields:
        values[f.name] = decode_field(f, int(payload[i : i + f.bits], 2))
        i += f.bits
    # The schema number, whichever way the header carried it.
    values["schema"] = version

    return values


# --------------------------------------------------------------------------- cells


def bits_to_cells(bits: str) -> list[tuple[int, int, int]]:
    """Bitstring -> RGB cells, 4 bits per channel. Trailing bits are zero-padded."""
    padded = bits + "0" * (-len(bits) % BITS_PER_CELL)
    cells: list[tuple[int, int, int]] = []
    for i in range(0, len(padded), BITS_PER_CELL):
        chunk = padded[i : i + BITS_PER_CELL]
        nibbles = [int(chunk[j : j + BITS_PER_CHANNEL], 2) for j in (0, 4, 8)]
        cells.append(tuple(n * STEP for n in nibbles))  # type: ignore[arg-type]
    return cells


def cells_to_bits(cells: list[tuple[int, int, int]]) -> str:
    """RGB cells -> bitstring. Each channel is quantised to its nearest of 16 levels.

    Quantising here is what buys the tolerance: a channel has to be wrong by more than
    half a step (~8 of 255) before it decodes as a different nibble.
    """
    out: list[str] = []
    for cell in cells:
        for chan in cell:
            n = min(LEVELS - 1, max(0, round(chan / STEP)))
            out.append(format(n, f"0{BITS_PER_CHANNEL}b"))
    return "".join(out)


def pack(values: dict[str, Any]) -> list[tuple[int, int, int]]:
    """Field values -> the payload cells the addon paints (calibration row excluded)."""
    return bits_to_cells(pack_bits(values))


def unpack(cells: list[tuple[int, int, int]]) -> dict[str, Any]:
    """Payload cells -> field values."""
    return unpack_bits(cells_to_bits(cells))


def calibration_row() -> list[tuple[int, int, int]]:
    """Row 0: locate-me markers bracketing the known swatches.

    The markers are saturated colours no quantised payload cell reproduces, so finding
    them locates and scales the grid without trusting a fixed screen offset.
    """
    return [MARKER_L, *CALIBRATION_SWATCHES, MARKER_R]


assert len(calibration_row()) == GRID_COLS, "calibration row must fill exactly one row"


# --------------------------------------------------------------------------- photometry


def solve_transform(
    observed: list[tuple[float, float, float]],
) -> tuple[tuple[float, float], ...]:
    """Fit per-channel gain and offset mapping *true* -> *observed*, from the swatches.

    Least squares on ten points per channel. A per-channel affine is deliberately the
    whole model: it absorbs gamma-ish shifts, exposure and the capture pipeline's colour
    handling, and it cannot silently "correct" a genuine misread the way a richer fit
    could. Returns ((gain, offset), ...) for R, G, B.
    """
    if len(observed) != len(CALIBRATION_SWATCHES):
        raise DecodeError("calibration", f"{len(observed)} swatches, need {len(CALIBRATION_SWATCHES)}")

    out: list[tuple[float, float]] = []
    n = len(observed)
    for ch in range(3):
        xs = [float(s[ch]) for s in CALIBRATION_SWATCHES]
        ys = [float(o[ch]) for o in observed]
        mx, my = sum(xs) / n, sum(ys) / n
        var = sum((x - mx) ** 2 for x in xs)
        if var < 1e-6:
            raise DecodeError("calibration", f"channel {ch} swatches are degenerate")
        gain = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
        if gain < 0.25:
            # The strip is washed out, occluded or not actually on screen.
            raise DecodeError("calibration", f"channel {ch} gain {gain:.3f} too low")
        out.append((gain, my - gain * mx))
    return tuple(out)


def apply_inverse(
    cell: tuple[float, float, float],
    transform: tuple[tuple[float, float], ...],
) -> tuple[int, int, int]:
    """Undo the solved transform on one observed cell, back to true colour space."""
    vals = []
    for ch in range(3):
        gain, offset = transform[ch]
        vals.append(min(255, max(0, round((cell[ch] - offset) / gain))))
    return tuple(vals)  # type: ignore[return-value]
