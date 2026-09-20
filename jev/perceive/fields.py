"""The JevRadio field table — the single definition both sides are generated from.

`addons/JevRadio/Fields.lua` is generated from this module by `tools/gen_addon_fields.py`.
Hand-editing the generated Lua is a build error: anything two components must agree on is
generated from one definition, because agreement by convention decays silently and
agreement by construction cannot (`DECISIONS.md` V13).

Adding a field is one entry here. The entry must say how the value is *measured*, in Lua,
so a field cannot exist without a way to observe it.

Wire format
-----------
Values are packed MSB-first into one bitstream, then cut into **12-bit cells**. Each cell
is one screen square painted as an RGB colour, **4 bits per channel** — sixteen levels, not
256. Sixteen levels tolerate roughly +/-8 levels of per-channel error, which absorbs gamma,
capture colour transforms and compression. Throughput is not the constraint here; a bad
decode is (`ARCHITECTURE.md` §7).

Row 0 of the grid is the **calibration row** and carries no payload:

    [ MARKER_L | k0 k1 ... k9 | MARKER_R ]

The markers bracket the row so the decoder can *locate and scale the grid* rather than
trust a fixed offset — the strip survives the window being moved or resized. The ten
swatches between them are known values, so the decoder solves the observed colour
transform and inverts it before reading any payload.

**The markers are not unique.** Magenta quantises to nibbles (15, 0, 15) and cyan to
(0, 15, 15); both are ordinary payload cells and a grid of them will produce matching
pairs by chance. An earlier version of this comment claimed otherwise and the decoder
duly locked onto payload-derived pairs. `radio_frame.locate` therefore scores a candidate
pair on the **calibration row between them** — ten known colours are a far stronger
signature than two — and only then accepts it.

Unknown is not a negative fact
------------------------------
Booleans are **two bits**, not one: 0 unknown, 1 false, 2 true. Numerics reserve their
maximum code as a not-available sentinel. A field the addon could not evaluate says so
rather than reporting a plausible zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# --------------------------------------------------------------------------- wire

BITS_PER_CHANNEL = 4
BITS_PER_CELL = BITS_PER_CHANNEL * 3          # 12
LEVELS = 1 << BITS_PER_CHANNEL                # 16
GRID_COLS = 12
CALIBRATION_ROWS = 1
SCHEMA = 4                                    # bump when the field table changes shape
"""2: the quest log arrives one entry per paint (`quests.slot`), replacing a watched-
quest field that was unknown on every live client because nothing sets a watch.
3: the advance button's screen position, so a stock frame is clicked where it actually is
rather than swept for.
4: gossip and quest-greeting lines, each with its own text hash, because a list has no
"whichever is showing" and two quest hand-ins look identical to a camera."""

# Quantisation step: nibble n renders as n * STEP, so 15 -> 255 exactly.
STEP = 255 // (LEVELS - 1)                    # 17

MARKER_L = (255, 0, 255)   # magenta
MARKER_R = (0, 255, 255)   # cyan

# Ten known swatches between the markers. Chosen to span each channel independently and
# to include both extremes, so the solved transform is constrained rather than fitted.
CALIBRATION_SWATCHES: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),
    (255, 255, 255),
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (3 * STEP, 3 * STEP, 3 * STEP),
    (6 * STEP, 6 * STEP, 6 * STEP),
    (9 * STEP, 9 * STEP, 9 * STEP),
    (12 * STEP, 12 * STEP, 12 * STEP),
    (15 * STEP, 0, 8 * STEP),
)


class Kind(StrEnum):
    UINT = "uint"    # raw unsigned integer; max code = not available
    FRAC = "frac"    # 0.0..1.0 spread over 0..(2^bits - 2); max code = not available
    ANGLE = "angle"  # 0..2*pi spread over 0..(2^bits - 2); max code = not available
    TRI = "tri"      # 0 unknown, 1 false, 2 true
    ENUM = "enum"    # small dense integer; max code = not available


@dataclass(frozen=True)
class Field:
    """One value on the wire.

    `lua` is the body of a function returning either the encoded integer, or `nil` for
    not-available. Helpers available to it: `frac(v)`, `tri(v)`, `clamp(v, hi)`,
    `nameid(s)`. It runs inside the addon with no upvalues beyond those helpers.
    """

    name: str
    bits: int
    kind: Kind
    lua: str
    note: str = ""

    @property
    def na(self) -> int:
        """The code reserved for not-available. TRI uses 0 (unknown) instead."""
        return 0 if self.kind is Kind.TRI else (1 << self.bits) - 1

    @property
    def span(self) -> int:
        """Number of codes available to real values, excluding the NA sentinel."""
        return (1 << self.bits) - 1


def _tri(name: str, lua: str, note: str = "") -> Field:
    return Field(name, 2, Kind.TRI, lua, note)


# --------------------------------------------------------------------------- the table
#
# Order is the wire order. Appending is cheap; reordering is a schema break.

FIELDS: tuple[Field, ...] = (
    # -- header ------------------------------------------------------------------
    Field("schema", 4, Kind.UINT, f"return {SCHEMA}",
          "must match the decoder's SCHEMA or the strip is from another build"),
    Field("seq", 8, Kind.UINT, "return SEQ % 256",
          "increments every paint; a frozen seq is a hung addon, not a misread"),

    # -- character ---------------------------------------------------------------
    Field("char.class_id", 5, Kind.ENUM, "return CLASS_ID[select(2, UnitClass('player'))]"),
    Field("char.race_id", 5, Kind.ENUM, "return RACE_ID[select(2, UnitRace('player'))]"),
    Field("char.level", 7, Kind.UINT, "return clamp(UnitLevel('player'), 126)"),
    Field("char.xp_pct", 8, Kind.FRAC,
          "local m = UnitXPMax('player'); if not m or m == 0 then return nil end\n"
          "return frac(UnitXP('player') / m)"),

    # -- position ----------------------------------------------------------------
    Field("pos.zone_id", 14, Kind.UINT, "return ZONE_ID()",
          "WorldMapArea row; mx/my are meaningless without it"),
    Field("pos.mx", 14, Kind.FRAC,
          "local x = select(1, GetPlayerMapPosition('player'))\n"
          "if not x or x == 0 then return nil end\n"
          "return frac(x)",
          "GetPlayerMapPosition returns 0,0 when the map is not set to the current zone"),
    Field("pos.my", 14, Kind.FRAC,
          "local _, y = GetPlayerMapPosition('player')\n"
          "if not y or y == 0 then return nil end\n"
          "return frac(y)"),
    Field("pos.facing", 10, Kind.ANGLE, "return angle(GetPlayerFacing())",
          "radians, 0 = +X increasing toward +Y; do not negate dy"),
    _tri("pos.indoors", "return tri(IsIndoors())"),

    # -- vitals ------------------------------------------------------------------
    Field("vitals.hp", 10, Kind.FRAC,
          "local m = UnitHealthMax('player'); if m == 0 then return nil end\n"
          "return frac(UnitHealth('player') / m)"),
    Field("vitals.hp_max", 15, Kind.UINT, "return clamp(UnitHealthMax('player'), 32766)"),
    Field("vitals.power", 10, Kind.FRAC,
          "local m = UnitManaMax('player'); if m == 0 then return nil end\n"
          "return frac(UnitMana('player') / m)"),
    Field("vitals.power_max", 15, Kind.UINT, "return clamp(UnitManaMax('player'), 32766)"),
    Field("vitals.power_type", 3, Kind.ENUM, "return UnitPowerType('player')"),
    _tri("vitals.combat", "return tri(UnitAffectingCombat('player'))"),
    _tri("vitals.dead", "return tri(UnitIsDead('player'))"),
    _tri("vitals.ghost", "return tri(UnitIsGhost('player'))"),

    # -- flags -------------------------------------------------------------------
    _tri("flags.mounted", "return tri(IsMounted())"),
    _tri("flags.swimming", "return tri(IsSwimming())"),
    _tri("flags.falling", "return tri(IsFalling())"),
    _tri("flags.on_taxi", "return tri(UnitOnTaxi('player'))"),
    _tri("flags.resting", "return tri(IsResting())"),
    _tri("flags.stealthed", "return tri(IsStealthed())"),
    _tri("flags.afk", "return tri(UnitIsAFK('player'))"),

    # -- target ------------------------------------------------------------------
    _tri("target.has", "return tri(UnitExists('target'))"),
    Field("target.name_id", 16, Kind.UINT, "return nameid(UnitName('target'))",
          "row in the generated name table; names travel as numbers, never as letters"),
    Field("target.hp", 10, Kind.FRAC,
          "if not UnitExists('target') then return nil end\n"
          "local m = UnitHealthMax('target'); if m == 0 then return nil end\n"
          "return frac(UnitHealth('target') / m)"),
    Field("target.level", 7, Kind.UINT,
          "if not UnitExists('target') then return nil end\n"
          "local l = UnitLevel('target'); if l < 0 then return nil end\n"
          "return clamp(l, 126)",
          "UnitLevel returns -1 for a skull; that is unknown, not level 0"),
    Field("target.reaction", 3, Kind.ENUM,
          "if not UnitExists('target') then return nil end\n"
          "return clamp(UnitReaction('player', 'target') or 0, 6)"),
    Field("target.classification", 3, Kind.ENUM,
          "if not UnitExists('target') then return nil end\n"
          "return CLASSIFICATION_ID[UnitClassification('target')]"),
    _tri("target.attacking_me",
         "if not UnitExists('target') then return 0 end\n"
         "return tri(UnitIsUnit('targettarget', 'player'))"),
    _tri("target.in_melee",
         "if not UnitExists('target') then return 0 end\n"
         "return tri(CheckInteractDistance('target', 3))",
         "CheckInteractDistance index 3 is DUEL range, about 11 yards -- NOT the ~5 yards "
         "gossip needs. True here means 'near', not 'close enough to talk to'"),

    # -- bags and money ----------------------------------------------------------
    Field("bags.free", 6, Kind.UINT, "return clamp(BAG_FREE(), 62)"),
    Field("bags.durability_min", 8, Kind.FRAC, "return DURABILITY_MIN()"),
    Field("bags.money_silver", 21, Kind.UINT,
          "return clamp(math.floor(GetMoney() / 100), 2097150)",
          "silver, not copper: 21 bits covers ~20,900g, past any 1-70 need"),

    # -- UI windows --------------------------------------------------------------
    _tri("ui.loot", "return tri(LootFrame and LootFrame:IsVisible())"),
    _tri("ui.gossip", "return tri(GossipFrame and GossipFrame:IsVisible())"),
    _tri("ui.vendor", "return tri(MerchantFrame and MerchantFrame:IsVisible())"),
    _tri("ui.quest_frame", "return tri(QuestFrame and QuestFrame:IsVisible())"),
    _tri("ui.trainer", "return tri(ClassTrainerFrame and ClassTrainerFrame:IsVisible())"),
    _tri("ui.mail", "return tri(MailFrame and MailFrame:IsVisible())"),
    _tri("ui.modal", "return tri(MODAL_UP())",
         "any StaticPopup is up; the world is obstructed and travel must not move"),
    Field("ui.error_id", 6, Kind.ENUM, "return LAST_ERROR()",
          "index into the shared UI-error enum, cleared after it is painted once"),

    # Where to click to move a stock frame forward.
    #
    # The quest frame is the client's own UI at a position the client itself knows, so
    # asking it is exact, works at any resolution and UI scale, and needs no per-install
    # binding. The alternative was sweeping the panel for yellow buttons, which is the
    # same flail that cost a session at the NPC.
    #
    # One pair of fields with one meaning — *the button that advances this frame* —
    # because Accept, Complete and Continue are the same intent at different moments and
    # a caller that had to know which is a caller that has to grow a state machine.
    #
    # **Fractions of the interface, not pixels.**
    #
    # Pixels were tried and were wrong by exactly a hundred on the vertical: converting a
    # button's UI coordinates to screen pixels mixes the button's effective scale with
    # UIParent's, and UIParent's pixel height turned out not to be the client height at
    # all. The painted point landed at (62, 533) for a button at (66, 632) — the
    # horizontal fine, the vertical a constant offset nobody could name.
    #
    # A ratio within one coordinate system needs no scale and no screen height. The
    # decoder multiplies by the frame it captured, which is by definition the right size,
    # and the whole class of confusion goes away. Eleven bits is about one pixel at 1600
    # wide. Origin top-left, flipped in the addon, because every reader here works that way.
    Field("ui.advance_x", 11, Kind.FRAC, "return ADVANCE_BUTTON('x')",
          "fraction across the interface of Accept/Complete/Continue, whichever is showing"),
    Field("ui.advance_y", 11, Kind.FRAC, "return ADVANCE_BUTTON('y')"),

    # Which line of a list to click, and how to know it is the right one.
    #
    # A gossip is not a button. `ADVANCE_BUTTON` works because Accept, Continue and
    # Complete Quest are the same intent wearing different names, so "whichever is
    # showing" is an answer. A list has no "whichever": an NPC with two quests to hand in
    # offers two lines that are identical in every respect a pixel can see, and clicking
    # the first one is a coin toss that opens the wrong quest half the time.
    #
    # So each line carries its **identity** as well as its position — `fnv1a16` of the
    # button's text, the same hash and the same function as `target.name_id`. The bot
    # knows the title it is looking for because the guide carries it, and matching is a
    # comparison rather than a guess. Nothing here knows what a quest is.
    #
    # One x for all of them: the buttons share an anchor and a width, so the column is a
    # property of the frame, not of the line.
    #
    # Five lines. Not a budget — it is what the fifth grid row holds, and a sixth row is
    # available the day a live frame needs it. Beyond five the bot fails honestly, which
    # is the failure worth having: a list it cannot fully see is one it must not click in.
    Field("ui.list_x", 11, Kind.FRAC, "return LIST_LINE(1, 'x')",
          "fraction across the interface of the gossip/greeting line column"),
    Field("ui.list_y0", 11, Kind.FRAC, "return LIST_LINE(1, 'y')",
          "fraction down the interface of list line 1, if it is showing"),
    Field("ui.list_hash0", 16, Kind.UINT, "return LIST_LINE(1, 'hash')",
          "fnv1a16 of line 1's text, matched against a title the guide already knows"),
    Field("ui.list_y1", 11, Kind.FRAC, "return LIST_LINE(2, 'y')",
          "fraction down the interface of list line 2, if it is showing"),
    Field("ui.list_hash1", 16, Kind.UINT, "return LIST_LINE(2, 'hash')",
          "fnv1a16 of line 2's text, matched against a title the guide already knows"),
    Field("ui.list_y2", 11, Kind.FRAC, "return LIST_LINE(3, 'y')",
          "fraction down the interface of list line 3, if it is showing"),
    Field("ui.list_hash2", 16, Kind.UINT, "return LIST_LINE(3, 'hash')",
          "fnv1a16 of line 3's text, matched against a title the guide already knows"),
    Field("ui.list_y3", 11, Kind.FRAC, "return LIST_LINE(4, 'y')",
          "fraction down the interface of list line 4, if it is showing"),
    Field("ui.list_hash3", 16, Kind.UINT, "return LIST_LINE(4, 'hash')",
          "fnv1a16 of line 4's text, matched against a title the guide already knows"),
    Field("ui.list_y4", 11, Kind.FRAC, "return LIST_LINE(5, 'y')",
          "fraction down the interface of list line 5, if it is showing"),
    Field("ui.list_hash4", 16, Kind.UINT, "return LIST_LINE(5, 'hash')",
          "fnv1a16 of line 5's text, matched against a title the guide already knows"),

    # -- quests ------------------------------------------------------------------
    Field("quests.log_hash", 16, Kind.UINT, "return QUEST_HASH()",
          "cheap change detector over the whole log; a changed hash means re-read"),

    # The log arrives one entry per paint, not all at once.
    #
    # There is no room on the wire for twenty quests and no "current quest" in 2.4.3
    # beyond the watch list — and nothing sets a watch, so the previous design reported
    # every quest field as unknown on a live client with quests in the log. The tracker
    # then could not answer "did I accept this?", which is the predicate the whole
    # questing half of the guide rests on.
    #
    # So the addon cycles: each paint describes one log entry, and `slot`/`count` let the
    # decoder assemble the whole log over about two seconds at 10 Hz. `log_hash` says when
    # to throw that assembly away. The addon stays stateless and paint-only; nothing has
    # to tell it which quest matters.
    Field("quests.count", 5, Kind.UINT, "return QUEST_COUNT()",
          "entries in the log, headers excluded"),
    Field("quests.slot", 5, Kind.UINT, "return QUEST_SLOT()",
          "which entry the fields below describe; advances once per paint"),
    Field("quests.slot_id", 16, Kind.UINT, "return QUEST_SLOT_ID()",
          "quest id at `slot`, from the hyperlink; unknown rather than a title hash, "
          "which the decoder could not tell apart"),
    _tri("quests.slot_complete", "return tri(QUEST_SLOT_COMPLETE())"),
    Field("quests.o0_have", 7, Kind.UINT, "return OBJ(1, 'have')"),
    Field("quests.o0_need", 7, Kind.UINT, "return OBJ(1, 'need')"),
    Field("quests.o1_have", 7, Kind.UINT, "return OBJ(2, 'have')"),
    Field("quests.o1_need", 7, Kind.UINT, "return OBJ(2, 'need')"),
    Field("quests.o2_have", 7, Kind.UINT, "return OBJ(3, 'have')"),
    Field("quests.o2_need", 7, Kind.UINT, "return OBJ(3, 'need')"),

    # -- action bars -------------------------------------------------------------
    # Thirteen bits, not twelve. These are bitmasks over twelve slots, so all 4,096
    # patterns are meaningful — and at twelve bits the all-ones pattern collides with the
    # not-available code. For `bars.ready` "all twelve off cooldown" is the *ordinary*
    # out-of-combat state, so the common case would have reported as unknown.
    Field("bars.usable", 13, Kind.UINT, "return BAR_BITS('usable')",
          "slots 1-12, bit 0 = slot 1; 13 bits so the all-ones mask is not the NA code"),
    Field("bars.ready", 13, Kind.UINT, "return BAR_BITS('ready')",
          "as above; all twelve ready is the normal out-of-combat state"),
    Field("bars.gcd", 8, Kind.FRAC, "return GCD_FRAC()"),
    _tri("bars.casting", "return tri(CASTING())"),
)

# --------------------------------------------------------------------------- layout

PAYLOAD_BITS = sum(f.bits for f in FIELDS)
CHECKSUM_BITS = 16
TOTAL_BITS = PAYLOAD_BITS + CHECKSUM_BITS
PAYLOAD_CELLS = -(-TOTAL_BITS // BITS_PER_CELL)          # ceil
PAYLOAD_ROWS = -(-PAYLOAD_CELLS // GRID_COLS)            # ceil
GRID_ROWS = PAYLOAD_ROWS + CALIBRATION_ROWS


def layout() -> dict:
    """Everything the addon and the decoder must agree on, in one dict."""
    return {
        "schema": SCHEMA,
        "bits_per_channel": BITS_PER_CHANNEL,
        "bits_per_cell": BITS_PER_CELL,
        "levels": LEVELS,
        "step": STEP,
        "cols": GRID_COLS,
        "rows": GRID_ROWS,
        "calibration_rows": CALIBRATION_ROWS,
        "payload_bits": PAYLOAD_BITS,
        "checksum_bits": CHECKSUM_BITS,
        "payload_cells": PAYLOAD_CELLS,
        "spare_bits": PAYLOAD_CELLS * BITS_PER_CELL - TOTAL_BITS,
        "field_count": len(FIELDS),
    }


def checksum(bits: str) -> int:
    """Fletcher-16 over the payload, byte-aligned with zero padding.

    Chosen over a parity or sum because it catches transposed cells, which is the failure
    a grid of coloured squares actually has — a misaligned read shifts cells rather than
    corrupting them individually.
    """
    padded = bits + "0" * (-len(bits) % 8)
    a = b = 0
    for i in range(0, len(padded), 8):
        a = (a + int(padded[i:i + 8], 2)) % 255
        b = (b + a) % 255
    return (b << 8) | a


def by_name() -> dict[str, Field]:
    return {f.name: f for f in FIELDS}
