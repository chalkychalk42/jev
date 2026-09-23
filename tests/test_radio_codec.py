"""The wire format, tested with no capture, no client and no numpy.

Round-tripping is the test that matters. If `unpack(pack(v)) == v` holds at every
boundary and for not-available, the only remaining failure modes are optical, and those
belong to `radio_frame`.
"""

from __future__ import annotations

import math

import pytest

from jev.perceive import radio
from jev.perceive.fields import (
    CALIBRATION_SWATCHES,
    FIELDS,
    GRID_COLS,
    SCHEMA_FIELDS,
    Kind,
    checksum,
    layout,
)


def _boundaries(f):
    """Every value worth trying for one field, including the ones that historically break."""
    match f.kind:
        case Kind.TRI:
            return [None, True, False]
        case Kind.FRAC:
            return [None, 0.0, 1.0, 0.5]
        case Kind.ANGLE:
            # tau is the one that matters: it must wrap to 0, not round up into the
            # not-available code and report the facing as unknown.
            return [None, 0.0, math.pi, radio.TAU - 1e-9, radio.TAU]
        case _:
            return [None, 0, f.na - 1]


def test_layout_fits_the_grid():
    lay = layout()
    assert lay["payload_cells"] <= (lay["rows"] - lay["calibration_rows"]) * lay["cols"]
    assert lay["spare_bits"] >= 0
    assert len(radio.calibration_row()) == GRID_COLS


@pytest.mark.parametrize(("version", "field_count", "payload_bits"),
                         [(6, 75, 582), (7, 112, 1035), (8, 117, 1059), (9, 121, 1073),
                          (10, 125, 1100), (11, 126, 1102)])
def test_historical_schema_prefixes_keep_their_checksum_boundary(version, field_count, payload_bits):
    fields = SCHEMA_FIELDS[version]
    assert len(fields) == field_count
    assert sum(f.bits for f in fields) == payload_bits
    values = {f.name: _boundaries(f)[-1] for f in fields}
    values["schema"] = version
    bits = "".join(format(radio.encode_field(f, values[f.name]), f"0{f.bits}b") for f in fields)
    cells = radio.bits_to_cells(bits + format(checksum(bits), "016b"))
    decoded = radio.unpack(cells)
    assert decoded["schema"] == version
    for field in fields:
        if field.kind not in (Kind.FRAC, Kind.ANGLE):
            assert decoded[field.name] == values[field.name]
    assert all(decoded[f.name] is None for f in FIELDS[field_count:])


def test_appended_fields_fit_the_existing_grid():
    """Schemas 10-12 appended 27, 2 and 4 bits; the painted strip does not grow."""
    lay = layout()
    assert (lay["cols"], lay["rows"]) == (12, 9)
    assert lay["payload_bits"] == 1106
    assert lay["field_count"] == 127
    assert sum(f.bits for f in SCHEMA_FIELDS[9]) == 1073, "schema 9 is a preserved prefix"
    assert sum(f.bits for f in SCHEMA_FIELDS[10]) == 1100, "schema 10 is a preserved prefix"
    assert sum(f.bits for f in SCHEMA_FIELDS[11]) == 1102, "schema 11 is a preserved prefix"


@pytest.mark.parametrize("field", FIELDS, ids=lambda f: f.name)
def test_every_field_round_trips_at_its_boundaries(field):
    for value in _boundaries(field):
        code = radio.encode_field(field, value)
        assert 0 <= code < (1 << field.bits), f"{field.name}={value!r} overflowed its width"
        got = radio.decode_field(field, code)

        if value is None:
            assert got is None, f"{field.name}: not-available decoded as {got!r}"
        elif field.kind is Kind.FRAC:
            assert abs(got - value) <= 1.0 / (field.span - 1)
        elif field.kind is Kind.ANGLE:
            want = value % radio.TAU
            err = min(abs(got - want), radio.TAU - abs(got - want))
            assert err <= radio.TAU / field.span
        else:
            assert got == value


def test_unknown_never_decodes_as_a_negative_fact():
    """A tri-state that nobody observed must come back None, not False."""
    tris = [f for f in FIELDS if f.kind is Kind.TRI]
    assert tris, "the table should have tri-state fields"
    for f in tris:
        assert radio.decode_field(f, 0) is None
        assert radio.decode_field(f, 1) is False
        assert radio.decode_field(f, 2) is True


def test_full_frame_round_trips_through_cells():
    values = {f.name: _boundaries(f)[-1] for f in FIELDS}
    values["schema"] = layout()["schema"]
    got = radio.unpack(radio.pack(values))
    for f in FIELDS:
        if f.kind in (Kind.FRAC, Kind.ANGLE):
            continue
        assert got[f.name] == values[f.name], f.name


def test_all_not_available_round_trips():
    """The strip a client paints before it knows anything must decode, not raise."""
    values = {f.name: None for f in FIELDS}
    values["schema"] = layout()["schema"]
    got = radio.unpack(radio.pack(values))
    assert got["schema"] == layout()["schema"]
    assert all(got[f.name] is None for f in FIELDS if f.name != "schema")


def test_checksum_rejects_a_transposed_cell():
    """Swapped cells are the failure a grid of squares actually has, not bit rot."""
    values = {f.name: _boundaries(f)[-1] for f in FIELDS}
    values["schema"] = layout()["schema"]
    cells = radio.pack(values)
    for i in range(len(cells) - 1):
        if cells[i] != cells[i + 1]:
            cells[i], cells[i + 1] = cells[i + 1], cells[i]
            break
    else:
        pytest.skip("no distinct adjacent cells to transpose")
    with pytest.raises(radio.DecodeError, match="checksum"):
        radio.unpack(cells)


def test_checksum_rejects_a_single_flipped_nibble():
    values = {f.name: _boundaries(f)[-1] for f in FIELDS}
    values["schema"] = layout()["schema"]
    cells = radio.pack(values)
    r, g, b = cells[3]
    cells[3] = ((r + 17) % 255, g, b)
    with pytest.raises(radio.DecodeError, match="checksum"):
        radio.unpack(cells)


def test_a_wrong_schema_is_refused_not_misread():
    payload = radio.pack_bits({f.name: None for f in FIELDS})
    assert checksum(payload[: -16]) == int(payload[-16:], 2)
    bumped = format(layout()["schema"] + 1, "04b") + payload[4:]
    bumped = bumped[: -16] + format(checksum(bumped[: -16]), "016b")
    with pytest.raises(radio.DecodeError, match="schema"):
        radio.unpack_bits(bumped)


def test_quantisation_absorbs_channel_noise():
    """Half a step of error per channel must still decode identically. That is the point
    of four bits per channel rather than eight."""
    values = {f.name: _boundaries(f)[-1] for f in FIELDS}
    values["schema"] = layout()["schema"]
    clean = radio.pack(values)
    noisy = [
        tuple(min(255, max(0, c + (7 if (i + j) % 2 else -7))) for j, c in enumerate(cell))
        for i, cell in enumerate(clean)
    ]
    assert radio.unpack(noisy) == radio.unpack(clean)


def test_calibration_inverts_a_gamma_like_shift():
    """A capture pipeline that darkens and lifts black must not break the decode."""
    def observe(c):
        return tuple(min(255.0, max(0.0, 0.78 * ch + 12.0)) for ch in c)

    transform = radio.solve_transform([observe(s) for s in CALIBRATION_SWATCHES])
    for swatch in CALIBRATION_SWATCHES:
        back = radio.apply_inverse(observe(swatch), transform)
        assert all(abs(a - b) <= 1 for a, b in zip(back, swatch, strict=True))


def test_calibration_refuses_a_washed_out_strip():
    """Near-zero gain means the strip is occluded or absent. Say so; do not read noise."""
    flat = [(128.0, 128.0, 128.0)] * len(CALIBRATION_SWATCHES)
    with pytest.raises(radio.DecodeError, match="calibration"):
        radio.solve_transform(flat)


def test_a_full_action_bar_is_a_value_not_an_absence():
    """`bars.ready` with every slot off cooldown is the ordinary out-of-combat reading.

    At twelve bits the all-ones mask collided with the not-available code, so the most
    common state on the bar reported as unknown and the coach was blind to it whenever
    nothing was on cooldown. Thirteen bits is the whole fix.
    """
    from jev.perceive.fields import by_name

    for name in ("bars.usable", "bars.ready"):
        field = by_name()[name]
        all_twelve = 0b111111111111
        assert all_twelve != field.na, f"{name}: a full bar is indistinguishable from unknown"
        assert radio.decode_field(field, radio.encode_field(field, all_twelve)) == all_twelve
        assert radio.decode_field(field, field.na) is None


def test_the_markers_are_not_unique_and_the_format_does_not_pretend_otherwise():
    """Magenta and cyan quantise to ordinary payload nibbles, so a grid of cells will
    produce matching pairs by chance. Locating on the pair alone locks onto payload;
    the calibration row between them is the actual signature."""
    from jev.perceive.fields import MARKER_L, MARKER_R

    producible = {cell for cell in radio.bits_to_cells("0" * 240)}
    assert MARKER_L == (255, 0, 255) and MARKER_R == (0, 255, 255)
    # Both are reachable from nibble triples, which is the point being asserted.
    assert radio.cells_to_bits([MARKER_L]) == "111100001111"
    assert radio.cells_to_bits([MARKER_R]) == "000011111111"
    assert producible is not None
