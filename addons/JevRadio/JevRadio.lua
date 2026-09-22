-- JevRadio -- JevRadio.lua
--
-- PAINT ONLY. This addon reads stock 2.4.3 Lua and renders the answers to textures. It
-- calls no UseAction, no movement function, no SetCVar, and nothing else that plays the
-- game. That is a hard constraint from PLAN section 2.2 and the whole reason the addon is
-- allowed to exist; anything actuating belongs on the virtual HID, outside the client.
--
-- The strip is a 12-wide grid. Row 0 is the calibration row -- a magenta marker, the ten
-- known swatches, a cyan marker -- and every following row carries payload cells in wire
-- order, four bits per channel, twelve bits per cell. jev/perceive/fields.py defines the
-- format; Fields.lua is generated from it and this file only obeys it.

local L         = JevRadioLayout
local FIELDS    = JevRadioFields
local MARKERS   = JevRadioMarkers
local SWATCHES  = JevRadioSwatches

local COLS      = L.cols
local ROWS      = L.rows
local CAL_ROWS  = L.calibrationRows
local CELLS     = L.payloadCells
local STEP      = L.step
local CELL_BITS = L.bitsPerChannel * 3

local TAU = 2 * math.pi

-- Twelve device pixels a side. The decoder samples the middle ~60% of each cell and takes
-- a median, so at 12 px it still has a 7 px interior after a capture rescale has smeared
-- the borders; at 8 px that interior degenerates to three pixels and a single bad row of
-- resampling can swing the median. Larger only costs screen area, and the strip already
-- covers part of the UI.
local CELL_PX = 12

-- 10 Hz. The tick stream is 2 Hz (ARCHITECTURE section 4) and System 1 runs at 20-30 Hz;
-- painting faster than the capture reads is wasted, painting slower makes every reader
-- wait on us.
local PERIOD = 0.1

local POW = {}
for i = 0, 31 do POW[i] = 2 ^ i end

-- --------------------------------------------------------------------- getter sandbox
--
-- The generated getters call bare names -- frac, tri, ZONE_ID, SEQ -- and Lua resolves
-- those from the function's environment at call time, not at load time. Left in _G they
-- would be one generically-named global away from being replaced by any other addon that
-- loads after us, and the strip would silently start painting somebody else's `frac`.
-- setfenv puts our own table in front of each getter instead, with _G behind it so the
-- client API still resolves.

local ENV = {}
for k, v in pairs(JevRadioHelpers) do ENV[k] = v end
setmetatable(ENV, { __index = _G })

for i = 1, #FIELDS do
    setfenv(FIELDS[i].get, ENV)
end

-- --------------------------------------------------------------------- encoding
--
-- The width-dependent half of the wire format. Helpers hand back normalised values
-- because they cannot know which field called them; `bits` and `kind` live here.

local function encode(field, v)
    local na = POW[field.bits] - 1

    if field.kind == "tri" then
        -- tri codes are already 0 unknown / 1 false / 2 true, and 0 is the sentinel.
        if v == nil then return 0 end
        return v
    end

    if v == nil then return na end

    if field.kind == "frac" then
        -- fields.py: round(v * (span - 1)). Lua has no round, and floor(x + 0.5) differs
        -- from Python's banker's rounding only on exact .5 ties, which land one code
        -- apart -- inside the tolerance the FRAC round-trip test already allows.
        return math.floor(v * (na - 1) + 0.5)
    end

    if field.kind == "angle" then
        -- floor, not round: rounding lets a facing just under tau land on `na` and report
        -- the direction as unknown. Matches encode_field in radio.py exactly.
        return math.floor(v / TAU * na) % na
    end

    v = math.floor(v)
    -- Every uint and enum in the table is already bounded by clamp() at its call site, so
    -- reaching either of these means the table changed under us. Saying unknown is the
    -- honest answer; clamping would paint a number nobody measured.
    if v < 0 then return na end
    if v >= na then return na end
    return v
end

-- --------------------------------------------------------------------- bitstream
--
-- One table, reused across paints. At ~400 entries and 10 Hz a fresh table per paint
-- would hand the collector 4,000 dead slots a second for no gain.

local bits = {}
local nbits = 0

local function push(v, width)
    for i = width - 1, 0, -1 do
        nbits = nbits + 1
        bits[nbits] = math.floor(v / POW[i]) % 2
    end
end

local function fletcher16(n)
    -- Byte-aligned with zero padding, mod 255, (b << 8) | a -- fields.checksum() to the
    -- letter. There is no << in 5.1, and b is below 255, so b * 256 cannot carry into a.
    local a, b = 0, 0
    local i = 1
    while i <= n do
        local byte = 0
        for k = 0, 7 do
            local bit = 0
            if i + k <= n then bit = bits[i + k] end
            byte = byte * 2 + bit
        end
        a = (a + byte) % 255
        b = (b + a) % 255
        i = i + 8
    end
    return b * 256 + a
end

-- --------------------------------------------------------------------- the grid

local frame = CreateFrame("Frame", "JevRadioFrame", UIParent)
frame:SetFrameStrata("TOOLTIP")   -- above DIALOG on purpose: a StaticPopup must not cover
frame:SetToplevel(true)           -- the strip, because that is exactly when ui.modal matters

local cells = {}
for i = 1, ROWS * COLS do
    cells[i] = frame:CreateTexture(nil, "OVERLAY")
end

local function setColour(tex, r, g, b)
    tex:SetTexture(r / 255, g / 255, b / 255)
end

local function paintCalibration()
    -- Constant, so it is painted when the grid is laid out rather than ten times a second;
    -- repainting it per frame would cost twelve SetTexture calls carrying no information.
    setColour(cells[1], MARKERS.left[1], MARKERS.left[2], MARKERS.left[3])
    for i = 1, #SWATCHES do
        local s = SWATCHES[i]
        setColour(cells[1 + i], s[1], s[2], s[3])
    end
    setColour(cells[COLS], MARKERS.right[1], MARKERS.right[2], MARKERS.right[3])
end

local function layoutGrid()
    -- ARCHITECTURE section 7: the ROI must be a pure function of window size and UI scale,
    -- and cells must land on exact pixel boundaries. A frame's size is in UI units, and
    -- units * effective scale is device pixels, so the size that renders as exactly
    -- CELL_PX pixels is CELL_PX / scale. Anything else leaves a cell straddling a pixel,
    -- the resampler blends two nibbles into one, and the decode fails in a way that looks
    -- like noise rather than like a layout bug.
    local eff = UIParent:GetEffectiveScale()
    if not eff or eff <= 0 then return end
    local cell = CELL_PX / eff

    -- Top centre. The default TBC UI leaves it empty, and more importantly the player and
    -- target frames must stay uncovered: ARCHITECTURE section 5 logs (vision_estimate,
    -- radio_truth) for every field both can produce, and covering the unit frames would
    -- destroy the vision half of every pair the radio exists to label.
    local screenW = math.floor(UIParent:GetWidth() * eff + 0.5)
    local ox = math.floor((screenW - COLS * CELL_PX) / 2)
    if ox < 0 then ox = 0 end

    frame:ClearAllPoints()
    frame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", ox / eff, 0)
    frame:SetWidth(COLS * cell)
    frame:SetHeight(ROWS * cell)

    for r = 0, ROWS - 1 do
        for c = 0, COLS - 1 do
            local tex = cells[r * COLS + c + 1]
            tex:ClearAllPoints()
            tex:SetPoint("TOPLEFT", frame, "TOPLEFT", c * cell, -(r * cell))
            tex:SetWidth(cell)
            tex:SetHeight(cell)
        end
    end

    paintCalibration()
    -- Cells past the payload exist because the grid is rectangular and the payload is not
    -- a multiple of twelve. Black is a defined value; left unpainted they would sample
    -- whatever the world is doing behind them.
    for i = CAL_ROWS * COLS + CELLS + 1, ROWS * COLS do
        setColour(cells[i], 0, 0, 0)
    end
    frame:Show()
end

-- --------------------------------------------------------------------- the paint

local TOTAL_BITS = CELLS * CELL_BITS

local function paint()
    JevRadioHelpers.syncMap()
    -- Once per frame, before any getter runs: every quest field in this paint has
    -- to describe the same log entry, or the decoder assembles a chimera.
    JevRadioHelpers.advanceQuestSlot()
    -- One inventory/merchant snapshot per paint keeps every field on the same row.
    -- Unsupported stock APIs yield unknown telemetry without freezing the radio.
    pcall(JevRadioHelpers.snapshotInventory)

    ENV.SEQ = (ENV.SEQ + 1) % 256

    nbits = 0
    for i = 1, #FIELDS do
        local field = FIELDS[i]
        -- A getter that threw has not observed "false"; it has observed nothing. pcall
        -- keeps one bad API call from freezing the whole strip, which the decoder would
        -- read as a hung addon rather than as one unknown field.
        local ok, v = pcall(field.get)
        if not ok then v = nil end
        push(encode(field, v), field.bits)
    end

    push(fletcher16(nbits), L.checksumBits)
    for i = nbits + 1, TOTAL_BITS do bits[i] = 0 end

    local base = CAL_ROWS * COLS
    for c = 0, CELLS - 1 do
        local i = c * CELL_BITS
        local r = (bits[i + 1] * 8 + bits[i + 2] * 4 + bits[i + 3] * 2 + bits[i + 4]) * STEP
        local g = (bits[i + 5] * 8 + bits[i + 6] * 4 + bits[i + 7] * 2 + bits[i + 8]) * STEP
        local b = (bits[i + 9] * 8 + bits[i + 10] * 4 + bits[i + 11] * 2 + bits[i + 12]) * STEP
        setColour(cells[base + c + 1], r, g, b)
    end
end

-- --------------------------------------------------------------------- driving it

local acc = 0

frame:SetScript("OnUpdate", function(self, elapsed)
    local dt = elapsed
    if dt == nil then dt = arg1 end
    acc = acc + (dt or 0)
    if acc < PERIOD then return end
    acc = acc - PERIOD
    -- After a loading screen or an alt-tab, elapsed arrives as one enormous value. Paying
    -- off that debt would paint a burst of identical frames and burn the sequence numbers
    -- the decoder uses to tell a hung addon from a misread, so the debt is dropped.
    if acc > PERIOD then acc = 0 end
    paint()
end)

frame:RegisterEvent("PLAYER_LOGIN")
frame:RegisterEvent("PLAYER_ENTERING_WORLD")
frame:RegisterEvent("UI_SCALE_CHANGED")
frame:RegisterEvent("DISPLAY_SIZE_CHANGED")
frame:SetScript("OnEvent", function(self, event)
    -- Relayout, never repaint: the payload comes round again in 100 ms anyway, but a
    -- stale cell size survives until something asks for a new one.
    layoutGrid()
end)

layoutGrid()
