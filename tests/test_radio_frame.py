"""The optics, proved without a client.

The load-bearing test here paints a frame the way `JevRadio.lua` paints the screen, puts
it through a capture pipeline's worth of abuse — a per-channel gain and lift, a blur that
bleeds every cell edge into its neighbour, and shot noise — and then asserts `read()`
recovers exactly what was packed. That is an end-to-end proof of the whole wire format
with no game running, and it is the only place the addon's format and the decoder's format
are ever compared against each other rather than each against itself.

The rest are the failures that must stay told apart: no strip, a strip that stopped
updating, and a strip whose calibration row cannot be solved.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
from pathlib import Path

import numpy as np
import pytest

from jev.perceive import radio, radio_frame
from jev.perceive.fields import (
    BITS_PER_CELL,
    CALIBRATION_SWATCHES,
    FIELDS,
    GRID_COLS,
    GRID_ROWS,
    MARKER_L,
    MARKER_R,
    PAYLOAD_CELLS,
    SCHEMA,
    STEP,
    Kind,
    layout,
)
from jev.world.state_v1 import Classification, PowerType, Reaction, SenseFault

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "addons" / "JevRadio"
HELPERS_LUA = (ADDON / "Helpers.lua").read_text(encoding="utf-8")
PAINTER_LUA = (ADDON / "JevRadio.lua").read_text(encoding="utf-8")
FIELDS_LUA = (ADDON / "Fields.lua").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- painting


def _addon_cell_px() -> int:
    """The addon's own cell size, read from its source rather than copied.

    A test that hard-codes 12 keeps passing after somebody changes the addon to 8 and the
    decoder's inner-sample stops having an interior to sample.
    """
    m = re.search(r"^local CELL_PX = (\d+)$", PAINTER_LUA, re.MULTILINE)
    assert m, "JevRadio.lua no longer declares CELL_PX"
    return int(m.group(1))


CELL_PX = _addon_cell_px()


# `_lua_*` below is `JevRadio.lua` and `Helpers.lua` transcribed statement for statement:
# the same normalising helpers, the same encode, the same bit push, the same Fletcher-16
# over the same zero padding, the same nibble-to-channel cut. The frames these tests paint
# are built from it rather than from `radio.pack`, so the end-to-end proof runs the addon's
# arithmetic against the decoder's rather than running the decoder against itself. There is
# no Lua interpreter in this environment, and this is the closest honest substitute.


def _lua_encode(field, value) -> int:
    na = 2**field.bits - 1
    if field.kind is Kind.TRI:
        # Helpers.lua's tri(): nil is the getter having declined to answer, and only the
        # getter can decline. The client answering "no" is a 1.
        return 0 if value is None else (2 if value else 1)
    if value is None:
        return na
    if field.kind is Kind.FRAC:
        v = min(1.0, max(0.0, float(value)))                  # frac()
        return math.floor(v * (na - 1) + 0.5)
    if field.kind is Kind.ANGLE:
        v = float(value) % radio.TAU                          # angle()
        return math.floor(v / radio.TAU * na) % na
    v = math.floor(value)
    return na if v < 0 or v >= na else v


def _lua_pack(values: dict) -> list[tuple[int, int, int]]:
    bits: list[int] = []

    def push(v: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((v // 2**i) % 2)

    for field in FIELDS:
        push(_lua_encode(field, values.get(field.name)), field.bits)

    a = b = 0
    payload = len(bits)
    i = 0
    while i < payload:
        byte = 0
        for k in range(8):
            byte = byte * 2 + (bits[i + k] if i + k < payload else 0)
        a = (a + byte) % 255
        b = (b + a) % 255
        i += 8
    push(b * 256 + a, 16)

    bits += [0] * (PAYLOAD_CELLS * BITS_PER_CELL - len(bits))
    cells = []
    for c in range(PAYLOAD_CELLS):
        i = c * BITS_PER_CELL
        chan = []
        for n in range(3):
            j = i + n * 4
            chan.append((bits[j] * 8 + bits[j + 1] * 4 + bits[j + 2] * 2 + bits[j + 3]) * STEP)
        cells.append((chan[0], chan[1], chan[2]))
    return cells


def _grid_colours(values: dict) -> list[list[tuple[int, int, int]]]:
    """Exactly what the addon paints: calibration row, then payload cells, then black."""
    rows = [radio.calibration_row()]
    cells = _lua_pack(values)
    spare = (GRID_ROWS - 1) * GRID_COLS - len(cells)
    cells = cells + [(0, 0, 0)] * spare
    for r in range(GRID_ROWS - 1):
        rows.append(cells[r * GRID_COLS : (r + 1) * GRID_COLS])
    return rows


def _box_blur(img: np.ndarray) -> np.ndarray:
    """A 3x3 mean. Stands in for every rescale and chroma step that bleeds cell edges."""
    padded = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge").astype(np.float64)
    out = np.zeros(img.shape, dtype=np.float64)
    for dy in range(3):
        for dx in range(3):
            out += padded[dy : dy + img.shape[0], dx : dx + img.shape[1]]
    return out / 9.0


def _background(height: int, width: int, seed: int) -> np.ndarray:
    """A dull, slightly textured backdrop with nothing magenta or cyan in it."""
    rng = np.random.default_rng(seed)
    base = np.zeros((height, width, 3), dtype=np.float64)
    ramp = np.linspace(20.0, 70.0, width)[None, :]
    base[:, :, 0] = ramp * 0.9
    base[:, :, 1] = ramp
    base[:, :, 2] = ramp * 0.6
    return base + rng.normal(0.0, 4.0, base.shape)


def _paint(
    values: dict,
    *,
    origin: tuple[int, int] = (61, 17),
    cell: int = CELL_PX,
    gain: tuple[float, float, float] = (0.82, 0.74, 0.88),
    lift: tuple[float, float, float] = (11.0, 15.0, 7.0),
    noise: float = 3.0,
    blur: bool = True,
    size: tuple[int, int] = (240, 420),
    seed: int = 7,
    swatches: list[tuple[int, int, int]] | None = None,
) -> np.ndarray:
    """Render the strip into a frame, then put it through a capture pipeline.

    Order matters and mirrors reality: the client renders true colours, the capture stack
    applies its own transfer curve, compression bleeds edges, and the sensor path adds
    noise. Doing the transform after the blur would let the calibration row absorb damage
    it would never see in the field.
    """
    height, width = size
    frame = _background(height, width, seed)

    rows = _grid_colours(values)
    if swatches is not None:
        rows[0] = [MARKER_L, *swatches, MARKER_R]

    x0, y0 = origin
    for r, line in enumerate(rows):
        for c, colour in enumerate(line):
            top, left = y0 + r * cell, x0 + c * cell
            frame[top : top + cell, left : left + cell] = colour

    for ch in range(3):
        frame[:, :, ch] = frame[:, :, ch] * gain[ch] + lift[ch]
    if blur:
        frame = _box_blur(frame)
    rng = np.random.default_rng(seed + 1)
    frame = frame + rng.normal(0.0, noise, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _values() -> dict:
    """A plausible mid-run strip. Fields left out are not-available on purpose."""
    return {
        "schema": layout()["schema"],
        "seq": 42,
        # The game's own ids: 3 is hunter, 4 is night elf. An earlier compact table of
        # our own made 3 a night elf, which agreed with the game only for human paladins.
        "char.class_id": 3,
        "char.race_id": 4,
        "char.level": 23,
        "char.xp_pct": 0.5,
        "pos.zone_id": radio_frame.zone_id("Elwynn"),
        "pos.mx": 0.4711,
        "pos.my": 0.6219,
        "pos.facing": 1.2345,
        "pos.indoors": False,
        "vitals.hp": 0.87,
        "vitals.hp_max": 1420,
        "vitals.power": 0.63,
        "vitals.power_max": 900,
        "vitals.power_type": 0,
        "vitals.combat": True,
        "vitals.dead": False,
        "vitals.ghost": False,
        "flags.mounted": False,
        "flags.swimming": False,
        "flags.falling": False,
        "flags.on_taxi": False,
        "flags.resting": True,
        "flags.stealthed": False,
        "flags.afk": False,
        "target.has": True,
        "target.name_id": radio_frame.name_id("Kobold Vermin"),
        "target.hp": 0.25,
        "target.level": 21,
        "target.reaction": 2,
        "target.classification": 2,
        "target.attacking_me": True,
        "target.in_melee": True,
        "bags.free": 11,
        "bags.durability_min": 0.72,
        "bags.money_silver": 1834,
        "ui.loot": False,
        "ui.gossip": False,
        "ui.vendor": False,
        "ui.quest_frame": False,
        "ui.trainer": False,
        "ui.mail": False,
        "ui.modal": False,
        "ui.error_id": 2,
        "quests.log_hash": 4242,
        "quests.count": 3,
        "quests.slot": 1,
        "quests.slot_id": 62,
        "quests.slot_complete": False,
        "quests.o0_have": 3,
        "quests.o0_need": 8,
        "bars.usable": 273,
        "bars.ready": 4095,   # all twelve ready: now a value, not the NA code
        "bars.gcd": 0.0,
        "bars.casting": False,
    }


# --------------------------------------------------------------------------- end to end


def test_the_addon_packer_and_the_codec_packer_produce_the_same_cells():
    """The wire format is implemented twice, in two languages, and only one of them has a
    test suite. This is the seam: bit order, nibble-to-channel order, where the checksum
    goes and what it is computed over. Every one of those is invisible in a round-trip
    against the same implementation and fatal across the pair."""
    for values in (_values(), {"schema": layout()["schema"]}, _boundary_values()):
        assert _lua_pack(values) == radio.pack(values), values.get("seq")


def test_a_rounding_tie_costs_at_most_one_code():
    """Lua has no round, so the addon uses floor(x + 0.5) where Python rounds half to
    even. They part company only on an exact tie, and then by one code — inside the
    tolerance the FRAC round-trip already allows. Written down so nobody 'fixes' it into
    a real disagreement."""
    fracs = [f for f in FIELDS if f.kind is Kind.FRAC]
    assert fracs
    for field in fracs:
        for numerator in range(0, 2 * (field.span - 1) + 1):
            value = numerator / 2.0 / (field.span - 1)
            lua = _lua_encode(field, value)
            codec = radio.encode_field(field, value)
            assert abs(lua - codec) <= 1, f"{field.name} at {value}"


def _boundary_values() -> dict:
    """Every field at the far end of its range, which is where an off-by-one lives."""
    values: dict = {}
    for field in FIELDS:
        match field.kind:
            case Kind.TRI:
                values[field.name] = True
            case Kind.FRAC:
                values[field.name] = 1.0
            case Kind.ANGLE:
                values[field.name] = radio.TAU - 1e-9
            case _:
                values[field.name] = field.na - 1
    values["schema"] = layout()["schema"]
    return values


def test_a_synthesised_frame_decodes_to_exactly_what_was_painted():
    """The whole wire format, proved without a client.

    Prevents the class of failure where the addon and the decoder each round-trip
    perfectly against themselves and disagree with each other: cell order, nibble order,
    checksum placement, calibration row position, marker identity. None of those show up
    in `test_radio_codec.py`, and all of them show up here.
    """
    values = _values()
    reading = radio_frame.read(_paint(values))

    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.fault is SenseFault.NONE
    # The codec's own round-trip is the reference: quantisation is the codec's business,
    # and the claim under test is that the optical path adds nothing on top of it.
    assert reading.values == radio.unpack(radio.pack(values))


def test_a_strip_of_maximum_values_decodes_end_to_end():
    """Every field at the top of its range at once. This is the frame where a one-bit
    misalignment stops being absorbed by a field that happened to be mostly zeroes."""
    values = _boundary_values()
    reading = radio_frame.read(_paint(values, seed=19))
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values == radio.unpack(radio.pack(values))


def test_a_strip_of_nothing_but_unknowns_decodes_end_to_end():
    """The strip a client paints before it knows anything is mostly ones, which is the
    opposite bit pattern and the other half of the same claim."""
    values = {"schema": layout()["schema"]}
    reading = radio_frame.read(_paint(values, seed=23))
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values == radio.unpack(radio.pack(values))


def test_the_optical_path_adds_no_error_to_a_clean_strip():
    """With no capture abuse the decode must be bit-identical, or the geometry is off by
    a pixel and only noise is hiding it."""
    values = _values()
    frame = _paint(values, gain=(1.0, 1.0, 1.0), lift=(0.0, 0.0, 0.0), noise=0.0, blur=False)
    reading = radio_frame.read(frame)
    assert reading.ok
    assert reading.values == radio.unpack(radio.pack(values))


def test_a_capture_near_the_gain_floor_still_decodes():
    """The calibration row exists so the strip reads under any capture pipeline rather
    than only the one it was authored against (ARCHITECTURE.md §7). A transform this flat
    leaves roughly 90 levels between black and white, and the decode must still be exact
    — otherwise the row is decoration and the format is only as good as the monitor."""
    values = _values()
    frame = _paint(values, gain=(0.35, 0.30, 0.40), lift=(62.0, 71.0, 55.0), noise=1.5)
    reading = radio_frame.read(frame)
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values == radio.unpack(radio.pack(values))


@pytest.mark.parametrize("origin", [(0, 0), (61, 17), (207, 93)])
def test_the_strip_is_found_wherever_the_window_put_it(origin):
    """The markers locate the grid so the strip survives the window moving. A fixed offset
    would pass at one origin and silently fail at the others."""
    values = _values()
    reading = radio_frame.read(_paint(values, origin=origin))
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values["seq"] == 42


@pytest.mark.parametrize("cell", [8, 12, 20])
def test_the_strip_is_found_at_any_cell_size(cell):
    """Cell size comes from the markers, not from a constant, so a different UI scale must
    decode without the decoder being told."""
    values = _values()
    reading = radio_frame.read(_paint(values, cell=cell, size=(260, 480)))
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values == radio.unpack(radio.pack(values))


def test_locate_reports_the_cell_size_it_was_painted_with():
    grid = radio_frame.locate(_paint(_values(), origin=(61, 17), cell=CELL_PX))
    assert grid is not None
    assert abs(grid.cell_w - CELL_PX) < 1.0
    assert abs(grid.cell_h - CELL_PX) < 1.5
    assert abs(grid.x0 - (61 + CELL_PX / 2 - 0.5)) < 1.0


# --------------------------------------------------------------------------- the faults


def test_a_frame_with_no_strip_is_not_found_rather_than_a_bad_read():
    """A frame with no addon in it must not produce a decode attempt at all. Reporting a
    checksum failure here would send a postmortem hunting a corrupt strip that was never
    on screen."""
    frame = np.clip(_background(240, 420, seed=3), 0, 255).astype(np.uint8)
    reading = radio_frame.read(frame)
    assert not reading.ok
    assert reading.fault is SenseFault.NOT_FOUND
    assert reading.values is None


def test_one_marker_alone_is_not_a_grid():
    """Half a bracket locates nothing. Guessing the other end from a fixed width would
    decode a column of background as payload."""
    values = _values()
    frame = _paint(values)
    grid = radio_frame.locate(frame)
    assert grid is not None
    # Paint over the cyan marker with background, leaving the magenta one.
    x = round(grid.x0 + (GRID_COLS - 1) * grid.dx)
    y = round(grid.y0)
    half = int(CELL_PX)
    frame[y - half : y + half, x - half : x + half] = (40, 45, 25)
    assert radio_frame.locate(frame) is None


def test_a_sequence_that_has_not_advanced_is_stale_not_a_misread():
    """A live strip repeating itself is a hung addon or a frozen client, and that is a
    different postmortem from a corrupt read even though both stop the state being
    believed. Conflating them is how a crashed addon gets diagnosed as a capture problem.
    """
    values = _values()
    frame = _paint(values)

    first = radio_frame.read(frame)
    assert first.ok and first.seq == 42

    again = radio_frame.read(frame, prev_seq=first.seq)
    assert not again.ok
    assert again.fault is SenseFault.STALE
    assert again.seq == 42
    # The last painted state is evidence about where it hung, so it survives the verdict.
    assert again.values is not None


def test_an_advancing_sequence_is_not_stale():
    values = _values()
    moved = dict(values, seq=43)
    reading = radio_frame.read(_paint(moved), prev_seq=42)
    assert reading.ok
    assert reading.fault is SenseFault.NONE
    assert reading.seq == 43


def test_a_caller_that_does_not_ask_about_staleness_is_not_told():
    """Absence of a `prev_seq` is a question nobody asked, not an answer of `fresh`."""
    frame = _paint(_values())
    assert radio_frame.read(frame).fault is SenseFault.NONE
    assert radio_frame.read(frame, prev_seq=None).fault is SenseFault.NONE


def test_a_washed_out_calibration_row_refuses_rather_than_reading_noise():
    """When the swatches carry no contrast the transform is unsolvable, and inverting an
    unsolvable transform turns payload into plausible-looking garbage that the checksum
    then has to catch by luck."""
    flat = [(128, 128, 128)] * len(CALIBRATION_SWATCHES)
    reading = radio_frame.read(_paint(_values(), swatches=flat))
    assert not reading.ok
    assert reading.fault is SenseFault.CALIBRATION
    assert reading.values is None
    assert "gain" in reading.detail or "degenerate" in reading.detail


def test_a_corrupted_payload_cell_is_a_checksum_fault():
    """A misread must be named as one. It is the only fault of the five that says the
    strip is there and moving and still cannot be trusted."""
    values = _values()
    frame = _paint(values)
    grid = radio_frame.locate(frame)
    assert grid is not None
    x, y = grid.centre(2, 5)
    half = int(CELL_PX * 0.3)
    frame[int(y) - half : int(y) + half, int(x) - half : int(x) + half] = (255, 255, 0)
    reading = radio_frame.read(frame)
    assert not reading.ok
    assert reading.fault is SenseFault.CHECKSUM


def test_the_five_faults_are_five_different_answers():
    """`SenseFault` exists so a postmortem can tell these apart; a decoder that collapses
    any two of them makes the enum a lie."""
    values = _values()
    seen = {
        radio_frame.read(_paint(values)).fault,
        radio_frame.read(_paint(values), prev_seq=values["seq"]).fault,
        radio_frame.read(np.clip(_background(200, 300, 5), 0, 255).astype(np.uint8)).fault,
        radio_frame.read(
            _paint(values, swatches=[(128, 128, 128)] * len(CALIBRATION_SWATCHES))
        ).fault,
    }
    assert seen == {
        SenseFault.NONE,
        SenseFault.STALE,
        SenseFault.NOT_FOUND,
        SenseFault.CALIBRATION,
    }


# --------------------------------------------------------------------------- the hash
#
# `_lua_*` below is the Lua in `Helpers.lua` transcribed statement for statement: the
# same split multiply, the same bit-at-a-time xor, the same fold. It exists so the
# production hash is checked against the arithmetic the addon actually performs, not
# against a second copy of the idea.


def _lua_xor8(a: int, b: int) -> int:
    r, place = 0, 1
    for _ in range(8):
        x, y = a % 2, b % 2
        if x != y:
            r = r + place
        a, b, place = (a - x) // 2, (b - y) // 2, place * 2
    return r


def _lua_fnv1a16(s: str) -> int:
    h = 2166136261
    for byte in s.encode("utf-8"):
        low = h % 256
        h = h - low + _lua_xor8(low, byte)
        hi = h // 65536
        lo = h % 65536
        h = (lo * 16777619 + ((hi * 16777619) % 65536) * 65536) % 4294967296
    hi = h // 65536
    lo = h % 65536
    return _lua_xor8(hi % 256, lo % 256) + _lua_xor8(hi // 256, lo // 256) * 256


# Derived by tracing the Lua above by hand. Written down so a change to either
# implementation has to be an argument, not an edit.
KNOWN_HASHES = {
    "": 7385,
    "a": 52512,
    "Elwynn": 28463,
    "Hogger": 34319,
    "Kobold Vermin": 1161,
    "Marshal McBride": 57507,
    "Defias Thug": 5098,
    "Bloodscalp Witch Doctor": 25502,
}


@pytest.mark.parametrize("text,want", sorted(KNOWN_HASHES.items()))
def test_the_lua_hash_and_the_python_hash_agree(text, want):
    """Names travel as numbers and are resolved back on the Python side, so a hash that
    disagrees by one bit turns every creature into a different creature — silently, with
    a perfectly valid checksum, forever."""
    assert _lua_fnv1a16(text) == want
    assert radio_frame.fnv1a16(text) == want


def test_the_lua_multiply_survives_the_fifty_three_bit_float():
    """Lua 5.1 has only doubles, so `h * 16777619` loses low bits above 2^53 and the
    addon splits the multiply to avoid it. This checks the split against the exact
    integer product over inputs chosen to be above the danger line."""
    for text in ("Bloodscalp Witch Doctor", "The Defias Traitor", "x" * 64):
        assert _lua_fnv1a16(text) == radio_frame.fnv1a16(text)


def test_the_lua_hash_uses_the_constants_python_uses():
    """A changed prime or offset basis still produces a random-looking hash, which is
    exactly why nothing else would notice."""
    for constant in (2166136261, 16777619, 4294967296, 65536):
        assert str(constant) in HELPERS_LUA, f"Helpers.lua no longer mentions {constant}"


def test_a_name_never_hashes_to_the_not_available_code():
    """65535 is "no target". A real unit whose name landed there would paint an empty
    target frame, and the coach would stop attacking something that is hitting it."""
    assert radio_frame.name_id("x" * 3) != 0xFFFF
    probes = [f"probe {i}" for i in range(20000)]
    assert all(radio_frame.name_id(p) != 0xFFFF for p in probes)
    collapsed = [p for p in probes if radio_frame.fnv1a16(p) == 0xFFFF]
    for p in collapsed:
        assert radio_frame.name_id(p) == 65534


def test_the_tbc_zone_map_files_do_not_collide_in_fourteen_bits():
    """`pos.zone_id` is a hash in 14 bits, and mx/my are meaningless attached to the wrong
    zone. Two zones sharing a code would put the bot on the right coordinates of the wrong
    map, which reads as a navigation bug rather than a decode one."""
    zones = json.loads((ROOT / "data" / "zones-tbc-243.json").read_text())["zones"]
    codes = {}
    for zone in zones:
        code = radio_frame.zone_id(zone["name"])
        assert code not in codes, f"{zone['name']} collides with {codes.get(code)}"
        assert code != 16383, f"{zone['name']} hashes to the not-available code"
        codes[code] = zone["name"]
    assert len(codes) == len(zones)


# --------------------------------------------------------------------------- the tables
#
# These three enum tables are not generated, so they are agreement by convention on both
# sides of a wire format. Parsing the Lua is what makes them agreement by construction
# until a generator exists.


def _lua_table(name: str) -> dict[str, int]:
    m = re.search(rf"^local {name} = \{{(.*?)^\}}", HELPERS_LUA, re.MULTILINE | re.DOTALL)
    assert m, f"Helpers.lua no longer declares {name}"
    return {k: int(v) for k, v in re.findall(r"(\w+)\s*=\s*(\d+)", m.group(1))}


def test_the_lua_class_table_matches_the_python_inverse():
    """A class id that means hunter in Lua and rogue in Python is a mislabelled training
    row that nothing downstream can detect."""
    lua = _lua_table("CLASS_ID")
    assert {v: k.lower() for k, v in lua.items()} == radio_frame.CLASS_BY_ID


def test_the_lua_race_table_matches_the_python_inverse():
    lua = _lua_table("RACE_ID")
    assert {v: k.lower() for k, v in lua.items()} == radio_frame.RACE_BY_ID


def test_the_lua_classification_table_matches_the_python_inverse():
    lua = _lua_table("CLASSIFICATION_ID")
    want = {v: k for k, v in radio_frame.CLASSIFICATION_BY_ID.items()}
    assert lua == {
        "normal": want[Classification.NORMAL],
        "elite": want[Classification.ELITE],
        "rare": want[Classification.RARE],
        "rareelite": want[Classification.RARE_ELITE],
        "worldboss": want[Classification.BOSS],
    }


def test_the_ui_error_enum_has_the_same_order_on_both_sides():
    """The wire carries an index. Inserting a key in the middle on one side renames every
    error after it without changing a single decoded number."""
    block = re.search(
        r"^local UI_ERRORS = \{(.*?)^\}", HELPERS_LUA, re.MULTILINE | re.DOTALL
    )
    assert block, "Helpers.lua no longer declares UI_ERRORS"
    keys = re.findall(r'key\s*=\s*"([a-z_]+)"', block.group(1))
    assert tuple(["", *keys]) == radio_frame.UI_ERROR_KEYS


def test_every_race_has_a_faction():
    """`faction` is derived rather than painted, so a race added without one would report
    no faction on a character that plainly has one."""
    assert set(radio_frame.RACE_BY_ID.values()) == set(radio_frame.FACTION_BY_RACE)


# --------------------------------------------------------------------------- the addon
#
# Structural checks on Lua that cannot be executed here. They are cheap and they catch the
# two failures that would otherwise only surface in-game: a helper the generated table
# calls and nobody wrote, and the addon growing a way to play the game.

LUA_KEYWORDS = frozenset({
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if",
    "in", "local", "nil", "not", "or", "repeat", "return", "then", "true", "until",
    "while",
})

# Stock 2.4.3 entry points the field table is allowed to call. Adding a field that needs a
# new one means declaring it here, which is also the list of APIs this addon depends on.
CLIENT_API = frozenset({
    "select", "math", "string", "table", "tonumber", "tostring", "type", "ipairs", "pairs",
    "UnitClass", "UnitRace", "UnitLevel", "UnitXP", "UnitXPMax", "UnitHealth",
    "UnitHealthMax", "UnitMana", "UnitManaMax", "UnitPowerType", "UnitAffectingCombat",
    "UnitIsDead", "UnitIsGhost", "UnitExists", "UnitName", "UnitReaction",
    "UnitClassification", "UnitIsUnit", "UnitOnTaxi", "UnitIsAFK",
    "IsMounted", "IsSwimming", "IsFalling", "IsResting", "IsStealthed", "IsIndoors",
    "GetPlayerFacing", "GetPlayerMapPosition", "GetMoney", "CheckInteractDistance",
    "LootFrame", "GossipFrame", "MerchantFrame", "QuestFrame", "ClassTrainerFrame",
    "MailFrame",
})

FORBIDDEN = (
    "UseAction", "CastSpell", "CastSpellByName", "SetCVar", "MoveForwardStart",
    "MoveBackwardStart", "TurnLeftStart", "TurnRightStart", "StrafeLeftStart",
    "StrafeRightStart", "JumpOrAscendStart", "InteractUnit", "TargetUnit",
    "RunMacro", "RunMacroText", "PickupAction", "UseContainerItem", "UseInventoryItem",
)


def _code_only(lua: str) -> str:
    """Lua with its comments removed. The file headers name the calls they refuse to make,
    and a scanner that cannot tell prose from code would read that as a violation."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in lua.splitlines())


@pytest.mark.parametrize("call", FORBIDDEN)
def test_the_addon_paints_and_never_actuates(call):
    """PLAN section 2.2 is what makes the addon legal at all, so it is a test and not a
    promise in a header comment."""
    for name, source in (("Helpers.lua", HELPERS_LUA), ("JevRadio.lua", PAINTER_LUA)):
        assert call not in _code_only(source), f"{name} calls {call}"


def test_every_helper_the_generated_getters_call_exists():
    """Fields.lua is generated from fields.py and calls helpers by name. A field added
    with a helper nobody wrote fails at paint time, on a client, as a frozen strip — which
    reads as a hung addon and sends the postmortem to entirely the wrong place."""
    exported = set(
        re.findall(r"^\s{4}(\w+) = ", 
                   re.search(r"^JevRadioHelpers = \{(.*?)^\}", HELPERS_LUA,
                             re.MULTILINE | re.DOTALL).group(1),
                   re.MULTILINE)
    )
    assert exported, "Helpers.lua no longer exports a helper table"

    bodies = re.findall(r"get = function\(\)(.*?)\n        end,", FIELDS_LUA, re.DOTALL)
    assert len(bodies) == len(FIELDS)

    unknown: set[str] = set()
    for body in bodies:
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", "''", body)
        locals_ = set(re.findall(r"\blocal\s+([\w,\s]+?)\s*=", stripped))
        declared = {n.strip() for group in locals_ for n in group.split(",")}
        for name in re.findall(r"(?<![\w.:])([A-Za-z_]\w*)", stripped):
            if name in LUA_KEYWORDS or name in declared or name in CLIENT_API:
                continue
            if name not in exported:
                unknown.add(name)
    assert not unknown, f"Fields.lua calls names nothing provides: {sorted(unknown)}"


def test_the_toc_loads_helpers_before_the_painter():
    """The painter builds each getter's environment out of the helper table at load time,
    so a TOC that loads it first hands out an empty environment and every field goes
    unknown on a strip that otherwise looks healthy."""
    toc = (ADDON / "JevRadio.toc").read_text(encoding="utf-8").splitlines()
    files = [line.strip() for line in toc if line.strip().endswith(".lua")]
    assert files == ["Helpers.lua", "Fields.lua", "JevRadio.lua"]
    assert "## Interface: 20400" in "\n".join(toc)


def test_the_addon_sets_the_map_only_on_demand():
    """GetPlayerMapPosition answers 0,0 unless the world map is on the current zone, but
    calling SetMapToCurrentZone every frame makes the map unusable for the human watching
    the run. It must sit behind the zone-change flag."""
    assert PAINTER_LUA.count("SetMapToCurrentZone") == 0
    assert HELPERS_LUA.count("SetMapToCurrentZone()") == 1
    guard = re.search(r"local function syncMap\(\)(.*?)\nend", HELPERS_LUA, re.DOTALL)
    assert guard and "mapDirty" in guard.group(1)
    assert guard and "WorldMapFrame" in guard.group(1)


# --------------------------------------------------------------------------- to state


def test_a_decoded_strip_becomes_a_state():
    """`fields.py` names are the model's dotted paths, so this is a transcription. It gets
    a test because a capability nothing calls does not exist, and nothing else calls it
    yet."""
    reading = radio_frame.read(_paint(_values()))
    state = radio_frame.to_state(reading, t=1234.5, client_id="c01")

    assert state.char.cls == "hunter"
    assert state.char.race == "nightelf"
    assert state.char.faction == "alliance"
    assert state.char.level == 23
    assert state.vitals.power_type is PowerType.MANA
    assert state.vitals.hp_max == 1420
    assert state.vitals.combat is True
    assert state.flags.resting is True
    assert state.target.has is True
    assert state.target.level == 21
    assert state.target.reaction is Reaction.HOSTILE
    assert state.target.classification is Classification.ELITE
    assert state.bags.free == 11
    assert state.bags.money_copper == 183400
    assert state.ui.error == "out_of_range"
    # One frame is never a log. Three quests in the log and one slot painted is *unread*,
    # not a one-quest log — the difference is a tracker that skips two live steps every
    # other second.
    assert state.quests is None, "a partial cycle must not present as a short log"
    # And the counts follow the log: unread means no counts, not zero counts. The slot's
    # objectives reach the tracker through `QuestLog`, which is tested in its own file.
    assert state.objective_counts() is None
    assert state.sense.addon_ok is True
    assert state.sense.fault is SenseFault.NONE
    assert state.sense.seq == 42


def test_unknown_reaches_the_state_as_none_and_never_as_false():
    """The whole point of two-bit booleans. A strip painted before the client knows
    anything must produce a state full of `None`, not a state claiming the player is not
    in combat, not mounted and not dead."""
    blank = {"schema": layout()["schema"], "seq": 1}
    reading = radio_frame.read(_paint(blank))
    assert reading.ok
    state = radio_frame.to_state(reading, t=1.0, client_id="c01")

    assert state.vitals.combat is None
    assert state.vitals.dead is None
    assert state.flags.mounted is None
    assert state.target.has is None
    assert state.pos.indoors is None
    assert state.char.cls is None
    assert state.bags.free is None
    assert state.flags.present() == []
    assert len(state.flags.unknown()) == 7


def test_map_position_is_dropped_when_the_zone_is_not_known():
    """A percentage across a map means nothing without the map it is a percentage of, and
    0.47 of the wrong zone is a coordinate the navigator will happily walk to."""
    values = dict(_values())
    values["pos.zone_id"] = None
    reading = radio_frame.read(_paint(values))
    state = radio_frame.to_state(reading, t=1.0, client_id="c01")
    assert state.pos.zone_id is None
    assert state.pos.mx is None
    assert state.pos.my is None


def test_a_failed_read_still_produces_a_state_that_says_why():
    """A tick where the radio failed is still a tick. Returning nothing would leave the
    episode store with a hole exactly where the interesting thing happened."""
    frame = np.clip(_background(200, 300, seed=9), 0, 255).astype(np.uint8)
    state = radio_frame.to_state(radio_frame.read(frame), t=7.0, client_id="c01")
    assert state.sense.addon_ok is False
    assert state.sense.fault is SenseFault.NOT_FOUND
    assert state.sense.seq is None
    assert state.vitals.hp is None
    assert state.t == 7.0


def test_a_stale_state_is_not_believed_but_is_still_reported():
    """Both halves matter: `addon_ok` false so nothing acts on it, and the values kept so
    a postmortem can see what the addon was stuck on."""
    frame = _paint(_values())
    reading = radio_frame.read(frame, prev_seq=42)
    state = radio_frame.to_state(reading, t=3.0, client_id="c01")
    assert state.sense.addon_ok is False
    assert state.sense.fault is SenseFault.STALE
    assert state.vitals.hp is not None


def test_an_impossible_level_is_refused_rather_than_carried():
    """126 fits the wire and no TBC character fits 126. Passing it through would trip the
    model's own bounds at the far end of the pipeline, in whichever process happened to
    deserialise it."""
    values = dict(_values())
    values["char.level"] = 120
    reading = radio_frame.read(_paint(values))
    state = radio_frame.to_state(reading, t=1.0, client_id="c01")
    assert state.char.level is None


# --------------------------------------------------------------------------- live frame

LIVE = pathlib.Path(__file__).parent / "fixtures" / "live-northshire-1600x900.npy"


@pytest.mark.skipif(not LIVE.exists(), reason="no live capture fixture")
def test_the_strip_reads_from_a_real_client_frame():
    """A 1600x900 capture of the running client, character in Northshire.

    Every other test in this file paints its own frame, which proves the format against
    itself. This one is the only thing that proves the format against a screen — and it
    caught what a fixture never could: on a real client the cyan mask held 3,798 pixels
    of interface against the marker's 196, and ranking candidates by area put five slivers
    of UI ahead of the real thing. The strip was on screen, painting correctly, and
    reported as absent.
    """
    frame = np.load(LIVE)
    assert frame.shape == (900, 1600, 3)

    grid = radio_frame.locate(frame)
    assert grid is not None, "the strip is in this frame; locate must find it"
    assert grid.cols == GRID_COLS and grid.rows == GRID_ROWS
    assert 10 <= grid.cell_w <= 20, f"cell width {grid.cell_w} is not a plausible size"

    reading = radio_frame.read(frame)
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    v = reading.values
    assert v["schema"] == SCHEMA, "the fixture and the field table must agree"
    assert 1 <= v["char.level"] <= 70
    assert 0.0 <= v["pos.mx"] <= 1.0 and 0.0 <= v["pos.my"] <= 1.0
    assert v["vitals.hp_max"] > 0

    # The log this character actually has. `count` is a positive observation either way —
    # zero means read-and-empty, which is what puts a fresh character on the graph entry
    # rather than at whichever NPC happens to be nearby.
    assert v["quests.count"] is not None
    if v["quests.count"] == 0:
        assert radio_frame.to_state(reading, t=0.0, client_id="c").quests == ()


@pytest.mark.skipif(not LIVE.exists(), reason="no live capture fixture")
def test_a_real_screen_is_full_of_the_marker_colours():
    """The premise behind detecting the strip from row runs, asserted not remembered.

    Live, the cyan mask held thousands of pixels of interface against the marker's 196,
    and on one frame the marker's connected component was a 46x48 sprawl at 0.56 fill —
    square, but far too sparse to pass a solidity filter, with the real marker sitting in
    the middle of it. Connectivity is not a usable primitive here; a row run is.
    """
    frame = np.load(LIVE)
    left, right = radio_frame._marker_masks(frame)
    assert right.sum() > 1000, "this frame should have plenty of non-marker cyan in it"

    pairs = radio_frame._strip_candidates(left, right)
    assert pairs, "no candidate pair from a frame that definitely contains the strip"
    assert len(pairs) <= radio_frame.MAX_CANDIDATES


def test_a_marker_merged_with_its_neighbour_is_still_found():
    """A payload cell next to a marker can carry the marker's exact colour, so the two
    merge into one run. The strip is still bracketed by the run's outer edge."""
    values = _values()
    frame = _paint(values, origin=(40, 40), cell=CELL_PX)
    # Paint a cyan block flush against the right marker, as a neighbouring cell would be.
    x = 40 + GRID_COLS * CELL_PX
    frame[40:40 + CELL_PX, x:x + CELL_PX] = np.array(MARKER_R, dtype=np.uint8)

    reading = radio_frame.read(frame)
    assert reading.ok, f"{reading.fault}: {reading.detail}"
    assert reading.values["char.level"] == values["char.level"]
