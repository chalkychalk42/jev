-- Helpers.lua - a module body: the build wraps it in a function taking `Supplies` and
-- keeps what it returns. It defines no globals (see tools/gen_addon_fields.py).
--
-- Everything the generated Fields.lua getters call. Fields.lua is generated from
-- jev/perceive/fields.py and is a build error to hand-edit; this file is the half that
-- may change freely, because nothing on the wire depends on *how* a value is measured.
--
-- Two decisions shape the whole file.
--
-- 1. Helpers return NORMALISED values, never wire codes. `frac` yields 0..1, `angle`
--    yields radians, `tri` yields 0/1/2, the integer helpers yield integers. A helper
--    cannot know a field's bit width -- `frac` is called from an 8-bit field and a
--    14-bit one in the same table -- so the width-dependent encoding (fields.py's
--    round(v * (span-1)) and its siblings) belongs to the packer in JevRadio.lua, which
--    has each field's `bits` and `kind` in hand. The alternative, passing the width to
--    every helper, would have to be threaded through the generated call sites, and we do
--    not own those.
--
-- 2. nil out of a stock 2.4.3 API is the client answering "no", not "nobody looked".
--    IsIndoors, IsMounted, UnitAffectingCombat and the rest return 1 or nil. If `tri`
--    mapped nil to unknown, every flag would read unknown forever and Flags.present()
--    would never fire once. Genuine ignorance is said one level up: a getter that cannot
--    evaluate returns nil outright, and the packer paints the not-available code.
--
-- Nothing here presses a key, moves, or writes a CVar. See the header of JevRadio.lua.

local TAU = 2 * math.pi

-- --------------------------------------------------------------------- shared state
--
-- Kept in this file rather than the painter because these are answers to the client's
-- own events, and the helpers that report them are here. The painter only paints.

local lastError = 0          -- pending UI-error enum index, cleared once painted
local questHash = nil        -- cached; rebuilt on QUEST_LOG_UPDATE, not per paint
local castingByEvent = false -- fallback for clients without UnitCastingInfo
local inWorld = false        -- SetMapToCurrentZone before the world exists is an error, not a no-op
local mapDirty = true        -- the world map may not be showing the zone we are standing in
local mapTrusted = false     -- GetPlayerMapPosition/GetMapInfo answer about the right zone
local inventoryRevision = 0
local inventoryCursor = 0
local inventorySnapshot = {}
local merchantSnapshot = {}
local supplySnapshot = {}

-- --------------------------------------------------------------------- scalar helpers

local function frac(v)
    if v == nil then return nil end
    v = tonumber(v)
    if v == nil then return nil end
    if v < 0 then return 0 end
    if v > 1 then return 1 end
    return v
end

local function angle(v)
    if v == nil then return nil end
    v = tonumber(v)
    if v == nil then return nil end
    -- Lua 5.1's % on floats is a floor-modulo, so a negative facing wraps to the
    -- positive side rather than staying negative and encoding as garbage.
    return v % TAU
end

local function tri(v)
    -- 1 = false, 2 = true, 0 = unknown. Unknown is never produced here; see note 2 in
    -- the file header. A few 2.4.3 entry points hand back a numeric 0 instead of nil,
    -- which is truthy in Lua and would otherwise paint "true".
    if v == nil or v == false or v == 0 then return 1 end
    return 2
end

local function clamp(v, hi)
    if v == nil then return nil end
    v = tonumber(v)
    if v == nil then return nil end
    if v < 0 then return 0 end
    if v > hi then return hi end
    return math.floor(v)
end

-- --------------------------------------------------------------------- FNV-1a
--
-- Names travel as numbers. The Python side resolves the number back to a name against
-- the world DB, so both implementations must agree bit for bit; tests/test_radio_frame.py
-- holds the reference and the constants below are asserted against it.

local FNV_OFFSET = 2166136261
local FNV_PRIME  = 16777619
local UINT32     = 4294967296

local function xor8(a, b)
    -- 2.4.3 ships Lua 5.1 with no bitwise operators and no `bit` library, so xor is done
    -- a bit at a time. Eight iterations per input byte. A 256x256 lookup table was the
    -- alternative and costs 64k entries of memory to save ~20us a frame; the only caller
    -- that ever sees a long string is QUEST_HASH, and that one is cached behind an event.
    local r, place = 0, 1
    for _ = 1, 8 do
        local x, y = a % 2, b % 2
        if x ~= y then r = r + place end
        a, b, place = (a - x) / 2, (b - y) / 2, place * 2
    end
    return r
end

local function fnv1a32(s)
    if s == nil then return nil end
    local h = FNV_OFFSET
    for i = 1, string.len(s) do
        local low = h % 256
        h = h - low + xor8(low, string.byte(s, i))   -- xor with a byte touches only the low byte
        -- h * FNV_PRIME reaches 2^56 and a double carries only 53 bits exactly, so the
        -- multiply is split into halves and recombined mod 2^32. Getting this wrong is a
        -- silent failure: the hash still looks random, it just stops matching Python's.
        local hi = math.floor(h / 65536)
        local lo = h % 65536
        h = (lo * FNV_PRIME + ((hi * FNV_PRIME) % 65536) * 65536) % UINT32
    end
    return h
end

local function fnv1a16(s)
    local h = fnv1a32(s)
    if h == nil then return nil end
    -- Fold 32 bits to 16 by xor, not truncation: that is FNV's own recommendation, and
    -- truncating throws away the mixing the high half did.
    local hi = math.floor(h / 65536)
    local lo = h % 65536
    return xor8(hi % 256, lo % 256) + xor8(math.floor(hi / 256), math.floor(lo / 256)) * 256
end

local function nameid(s)
    local h = fnv1a16(s)
    if h == nil then return nil end
    -- 65535 is the 16-bit not-available code. A real name that hashed to it would paint
    -- "no target"; one name in 65536 reading as its neighbour is much cheaper than a
    -- phantom empty target, so collapse rather than report nothing.
    if h == 65535 then return 65534 end
    return h
end

-- --------------------------------------------------------------------- the character
--
-- Which character this strip is painted for: its name and realm, hashed to 31 bits, so
-- the bot keeps each character's place in the guide apart. Cached once known - it cannot
-- change without a logout, which reloads this file - and not before: until the client has
-- the name it answers "Unknown", and a key made from that would be every character's.

local characterKey

local function CHARACTER_KEY()
    if characterKey == nil then
        local name, realm = UnitName("player"), GetRealmName()
        if not name or name == "" or name == UNKNOWNOBJECT or not realm or realm == "" then
            return nil
        end
        characterKey = fnv1a32(name .. "-" .. realm) % 2147483647
    end
    return characterKey
end

-- --------------------------------------------------------------------- world objects
--
-- A crate or a bundle of wood has no unit behind it, so the mouseover token says nothing;
-- the stock tooltip names it. Only while the pointer is over the world with no unit under
-- it, and only a fully shown tooltip: one fading out still names what the pointer left.

local function CURSOR_OBJECT()
    if UnitExists("mouseover") then return nil end
    if type(GetMouseFocus) ~= "function" or not WorldFrame or GetMouseFocus() ~= WorldFrame then
        return nil
    end
    if not GameTooltip or not GameTooltip:IsVisible() or GameTooltip:GetAlpha() < 0.99 then
        return nil
    end
    local line = GameTooltipTextLeft1
    local text = line and line:GetText()
    if not text or text == "" then return nil end
    return nameid(text)
end

-- --------------------------------------------------------------------- unit identity
--
-- Two Kobold Workers share a name, a level and full health; the client's GUID tells them
-- apart. Hashed to 16 bits as names are: two units in one fight colliding is 1 in 65536.

local function TARGET_GUID()
    if type(UnitGUID) ~= "function" then return nil end
    local guid = UnitGUID("target")
    if not guid or guid == "" then return nil end
    return nameid(guid)
end

-- --------------------------------------------------------------------- enum tables
--
-- Keyed by the client's own locale-independent tokens. tests/test_radio_frame.py parses
-- these three tables out of this file and asserts they match the inverse tables in
-- jev/perceive/radio_frame.py, which is the only thing stopping them drifting -- they
-- are not generated, and they should be. See the report note on fields.py.
--
-- Index 0 is left unused in all three: an unrecognised token yields nil from the lookup,
-- the packer paints not-available, and the decoder says None rather than guessing class 0.

-- The **game's own** class and race ids, not a compact 1-N table of our own.
--
-- There was one, and it was 1-9 with SHAMAN = 6. The game numbers shaman 7, mage 8,
-- warlock 9 and druid 11, and `world_playercreateinfo_action` — where the combat
-- profiles come from — is keyed by the game's numbers. Human is 1 and paladin is 2 in
-- both, which is exactly why nothing looked wrong: the character this was built against
-- matched by luck, and a warlock would have been handed a druid's action bar.
--
-- Races had the same problem, worse: our Dwarf was 2 and the game's Orc is.
local CLASS_ID = {
    WARRIOR = 1, PALADIN = 2, HUNTER = 3, ROGUE = 4, PRIEST = 5,
    SHAMAN = 7, MAGE = 8, WARLOCK = 9, DRUID = 11,
}

local RACE_ID = {
    Human = 1, Orc = 2, Dwarf = 3, NightElf = 4, Scourge = 5,
    Tauren = 6, Gnome = 7, Troll = 8, BloodElf = 10, Draenei = 11,
}

local CLASSIFICATION_ID = {
    normal = 1, elite = 2, rare = 3, rareelite = 4, worldboss = 5,
}

-- --------------------------------------------------------------------- UI errors
--
-- UI_ERROR_MESSAGE carries localised text, so it is matched against the client's own
-- global strings rather than against English. Index 1 is "other" on purpose: knowing
-- that *something* failed is most of the value, and an unmatched error must not read as
-- no error at all. Order is the wire contract; append, never reorder.

local UI_ERRORS = {
    { key = "other" },
    { key = "out_of_range",     globals = { "SPELL_FAILED_OUT_OF_RANGE", "ERR_OUT_OF_RANGE", "ERR_BADATTACKPOS" } },
    { key = "not_facing",       globals = { "SPELL_FAILED_UNIT_NOT_INFRONT", "ERR_BADATTACKFACING" } },
    { key = "no_line_of_sight", globals = { "SPELL_FAILED_LINE_OF_SIGHT" } },
    { key = "bad_target",       globals = { "SPELL_FAILED_BAD_TARGETS", "SPELL_FAILED_TARGET_FRIENDLY", "ERR_NO_ATTACK_TARGET" } },
    { key = "no_target",        globals = { "SPELL_FAILED_BAD_IMPLICIT_TARGETS", "ERR_GENERIC_NO_TARGET" } },
    { key = "target_dead",      globals = { "SPELL_FAILED_TARGETS_DEAD" } },
    { key = "no_power",         globals = { "ERR_OUT_OF_MANA", "ERR_OUT_OF_RAGE", "ERR_OUT_OF_ENERGY", "ERR_OUT_OF_FOCUS", "SPELL_FAILED_NO_POWER" } },
    { key = "not_ready",        globals = { "SPELL_FAILED_NOT_READY", "ERR_SPELL_COOLDOWN", "ERR_ITEM_COOLDOWN" } },
    { key = "already_casting",  globals = { "SPELL_FAILED_SPELL_IN_PROGRESS", "ERR_SPELL_FAILED_ANOTHER_IN_PROGRESS" } },
    { key = "moving",           globals = { "SPELL_FAILED_MOVING" } },
    { key = "immune",           globals = { "SPELL_FAILED_IMMUNE" } },
    { key = "too_close",        globals = { "SPELL_FAILED_TOO_CLOSE" } },
    { key = "player_dead",      globals = { "ERR_PLAYER_DEAD", "SPELL_FAILED_CASTER_DEAD" } },
    { key = "bags_full",        globals = { "ERR_INV_FULL" } },
    { key = "quest_log_full",   globals = { "ERR_QUEST_LOG_FULL" } },
    { key = "not_enough_money", globals = { "ERR_NOT_ENOUGH_MONEY" } },
    { key = "cannot_do_that",   globals = { "SPELL_FAILED_CANT_DO_THAT_RIGHT_NOW", "ERR_CLIENT_LOCKED_OUT" } },
}

local errorIndex = {}   -- localised text -> enum index, built once at load

local function buildErrorIndex()
    for i = 2, #UI_ERRORS do
        local entry = UI_ERRORS[i]
        for _, name in ipairs(entry.globals) do
            local text = _G[name]
            -- Some of these globals do not exist in every 2.4.3 build, and some carry
            -- format specifiers, which never compare equal to a rendered message. Both
            -- are skipped so a missing name costs one bucket, not a load error.
            if type(text) == "string" and string.find(text, "%%") == nil then
                errorIndex[text] = i
            end
        end
    end
end

buildErrorIndex()   -- FrameXML's GlobalStrings are loaded before any addon, so this is safe here

local function LAST_ERROR()
    local v = lastError
    lastError = 0   -- cleared once painted: the field reports an edge, not a level
    return v
end

-- --------------------------------------------------------------------- world queries

local function ZONE_ID()
    -- mx/my are a percentage of a map, and a percentage means nothing without the map it
    -- is a percentage of. While the player is browsing the world map we cannot fix it to
    -- the current zone without fighting them, so the zone goes unknown and the consumer
    -- is obliged to throw mx/my away with it.
    if not mapTrusted then return nil end
    local file = GetMapInfo()
    if file == nil then return nil end   -- instance, or an area with no world map at all
    -- 14 bits, so 16383 is the not-available code and the hash is taken modulo it. The
    -- 68 zone map files in data/zones-tbc-243.json are collision-free under this, which
    -- tests/test_radio_frame.py asserts rather than assumes.
    return fnv1a16(file) % 16383
end

local function BAG_FREE()
    local total = 0
    for bag = 0, 4 do
        local free, kind = GetContainerNumFreeSlots(bag)
        if free then
            -- kind is the bag's item-class restriction. A quiver's free slots cannot take
            -- loot, so counting them would walk the bot past the vendor it needed. The
            -- second return does not exist on every 2.4.3 build, hence the nil case.
            if kind == nil or kind == 0 then total = total + free end
        end
    end
    return total
end

local FIRST_EQUIPPED, LAST_EQUIPPED = 1, 18   -- head..ranged; tabard and bags have no durability

local function DURABILITY_MIN()
    local worst = nil
    for slot = FIRST_EQUIPPED, LAST_EQUIPPED do
        local cur, max = GetInventoryItemDurability(slot)
        if cur and max and max > 0 then
            local v = cur / max
            if worst == nil or v < worst then worst = v end
        end
    end
    -- nil when nothing equipped has durability. That is no observation, not full repair:
    -- a naked corpse-run character would otherwise report a pristine 1.0.
    return worst
end

-- --------------------------------------------------------------------- facing

-- `GetPlayerFacing` arrives in 3.0 and this is 2.4.3. The minimap draws its player arrow
-- as a `Model`, and a `Model` has `GetFacing`, so AVR-TBC reads a heading off it and V29
-- said this client could too.
--
-- **Measured here, it cannot.** With `rotateMinimap` set to 0 and the client reloaded,
-- the arrow is found by its model path and `GetFacing()` returns 0.0 at every heading:
-- `tools/verify_facing.py` walked five times, the direction of travel varied across 160
-- degrees, and the painted value did not move at all.
--
-- A constant is worse than nothing, because a navigator will act on it. So nothing is
-- painted and V17's closed-loop turning stands: take a heading from a position delta
-- while moving, turn for angle/turn_rate, re-measure.
--
-- What would settle it without guessing: this addon can record what it sees into
-- SavedVariables on load - which model paths the minimap's children actually carry, and
-- what each returns - and that file can be read off disk. That is client-local and
-- involves no chat. Typing `/script` to ask instead is how a half-written line ends up
-- sitting in the Say box.
local function PLAYER_FACING()
    if GetPlayerFacing then return GetPlayerFacing() end   -- 3.0+, and free if it lands
    return nil
end

-- --------------------------------------------------------------------- stock frames

-- The button that moves a quest frame forward, in screen pixels.
--
-- Accept, Complete and Continue are the same intent at different moments in a quest, so
-- they are painted through one pair of fields: whichever is showing. A caller that had to
-- know which one it was would need a state machine to track quests it cannot see.
--
-- Read from the client's own layout, which is exact and survives any resolution or UI
-- scale. The alternative was sweeping the panel for yellow buttons, and sweeping is what
-- this addon exists to make unnecessary.
local ADVANCE_BUTTONS = {
    "QuestFrameAcceptButton",
    "QuestFrameCompleteQuestButton",
    "QuestFrameCompleteButton",
    "QuestFrameContinueButton",
    -- Repair All, which is the same intent again: a merchant that can repair is a step
    -- forward and the button only exists while one is open. Ahead of the popup for the
    -- same reason the quest frames are — a real frame beats a dialog.
    "MerchantRepairAllButton",
    -- Train, the same intent at a class trainer: buy the selected service. The stock
    -- frame selects the first learnable one on opening and again after every purchase,
    -- and disables the button when that one is unaffordable or not yet learnable, so it
    -- is painted only while enabled (below) and a reader stops when it goes.
    "ClassTrainerTrainButton",
    -- Last, so a quest frame always wins. A StaticPopup's first button is the same
    -- intent as Accept — *move this forward* — and Release Spirit and Resurrect are
    -- both one. It is painted, not pressed: whether pressing is right is the caller's
    -- decision, and the caller checks `vitals.dead` before it makes it.
    "StaticPopup1Button1",
}

-- A list of things to click, and which one is which.
--
-- Two frames draw the same widget under different names: `GossipTitleButton` for a
-- gossip, `QuestTitleButton` for the greeting panel an NPC shows when it has several
-- quests and nothing else to say. Reading both here is what makes one skill above cover
-- both, rather than a second skill that differs only in a string.
--
-- The text hash is `nameid`, the same function that identifies a target, so the bot
-- compares against a title the guide already holds instead of recognising anything.
local LIST_FRAMES = { "GossipTitleButton", "QuestTitleButton" }

local function listButton(i)
    for f = 1, table.getn(LIST_FRAMES) do
        local btn = getglobal(LIST_FRAMES[f] .. i)
        if btn and btn.IsVisible and btn:IsVisible() then return btn end
    end
    return nil
end

-- The client draws a gossip line as markup, not as a title: a quest hand-in arrives as
-- `|cXXXXXXXXA Threat Within|r`, twenty-seven characters for a fifteen-character quest.
--
-- Hashing that as-is is wrong twice over. It does not match the title the guide holds,
-- which is the immediate bug; and the colour is the quest's **difficulty relative to the
-- character's level**, so the same quest hashes differently at level 1 and level 10. That
-- second one would not have failed here — it would have started failing weeks later, on a
-- quest that used to work.
--
-- So the escapes come off first. Textures and hyperlinks are stripped too, because a
-- gossip option can carry either and the rule is about markup, not about quests.
local function plain(s)
    if s == nil then return nil end
    s = string.gsub(s, "|c%x%x%x%x%x%x%x%x", "")
    s = string.gsub(s, "|r", "")
    s = string.gsub(s, "|T.-|t", "")
    s = string.gsub(s, "|H.-|h(.-)|h", "%1")
    s = string.gsub(s, "^%s+", "")
    s = string.gsub(s, "%s+$", "")
    return s
end

local function LIST_LINE(i, axis)
    local btn = listButton(i)
    if btn == nil then return nil end
    if axis == "hash" then
        local text = plain(btn.GetText and btn:GetText())
        if text == nil or text == "" then return nil end
        return nameid(text)
    end
    local x, y = btn:GetCenter()
    if x == nil or y == nil then return nil end
    -- Fractions of UIParent, for the reason written against ADVANCE_BUTTON: converting
    -- to pixels here mixes the button's effective scale with the interface's and is wrong
    -- by a hundred on the vertical.
    local ratio = btn:GetEffectiveScale() / UIParent:GetEffectiveScale()
    if axis == "x" then
        return (x * ratio) / UIParent:GetWidth()
    end
    return 1 - (y * ratio) / UIParent:GetHeight()
end

-- The reward to take when a quest's reward page offers a choice.
--
-- Complete Quest does nothing until one is chosen. The default is usable by this
-- character first, then higher quality, then the earlier item: a fixed rule, painted as a
-- position, so whoever clicks can still choose another. Reads the stock reward panel.
local function questChoiceShown()
    local panel = QuestFrameRewardPanel
    return panel ~= nil and panel.IsVisible ~= nil and panel:IsVisible() and panel or nil
end

local function QUEST_CHOICE(what)
    local panel = questChoiceShown()
    local n = panel and (GetNumQuestChoices() or 0) or 0
    if what == "count" then return clamp(n, 6) end
    if panel == nil or n < 1 then return nil end
    if what == "made" then return (panel.itemChoice or 0) > 0 end
    local best, bestUsable, bestQuality = nil, -1, -1
    for i = 1, n do
        local _, _, _, quality, usable = GetQuestItemInfo("choice", i)
        usable = usable and 1 or 0
        quality = quality or 0
        if usable > bestUsable or (usable == bestUsable and quality > bestQuality) then
            best, bestUsable, bestQuality = i, usable, quality
        end
    end
    local btn = best and getglobal("QuestRewardItem" .. best)
    if not (btn and btn.IsVisible and btn:IsVisible()) then return nil end
    local x, y = btn:GetCenter()
    if x == nil or y == nil then return nil end
    -- The same ratio as ADVANCE_BUTTON, and for the same reason.
    local ratio = btn:GetEffectiveScale() / UIParent:GetEffectiveScale()
    if what == "x" then return (x * ratio) / UIParent:GetWidth() end
    return 1 - (y * ratio) / UIParent:GetHeight()
end

-- Buttons that are the way forward only while enabled. The quest buttons keep their old
-- rule (painted whenever showing), which the hand-in skills were measured against.
local ONLY_ENABLED = { ClassTrainerTrainButton = true }

-- 2.4.3 answers IsEnabled with 1 or nil; later clients with 1 or 0. Either way, off is off.
local function enabled(btn)
    if not btn.IsEnabled then return true end
    local e = btn:IsEnabled()
    return e ~= nil and e ~= 0 and e ~= false
end

local function ADVANCE_BUTTON(axis)
    for i = 1, #ADVANCE_BUTTONS do
        local btn = getglobal(ADVANCE_BUTTONS[i])
        if btn and btn.IsVisible and btn:IsVisible()
                and (not ONLY_ENABLED[ADVANCE_BUTTONS[i]] or enabled(btn)) then
            local x, y = btn:GetCenter()
            if x and y then
                -- A ratio inside one coordinate system, so no screen height and no scale
                -- arithmetic. Converting to pixels instead was wrong by a hundred on the
                -- vertical: it mixed the button's effective scale with UIParent's, and
                -- UIParent's pixel height is not the client height.
                local ratio = btn:GetEffectiveScale() / UIParent:GetEffectiveScale()
                if axis == "x" then
                    return (x * ratio) / UIParent:GetWidth()
                end
                -- WoW's UI origin is bottom-left; every reader here works top-left.
                return 1 - (y * ratio) / UIParent:GetHeight()
            end
        end
    end
    return nil
end

-- Inventory and merchant rows are observations of stock UI, never commands. Slot IDs
-- come from the visible button's GetID and its container's GetID, not visual ordering:
-- stock ContainerFrame deliberately reverses item-button order on screen.
-- Checked against Blizzard 2.4.3 FrameXML ContainerFrame.lua / MerchantFrame.lua:
-- https://github.com/MOUZU/Blizzard-WoW-Interface/tree/master/2.4.3/FrameXML
local function itemID(link)
    if type(link) ~= "string" then return nil end
    return tonumber(string.match(link, "item:(%d+)"))
end

local function point(btn, axis)
    if not btn or not btn.IsVisible or not btn:IsVisible() then return nil end
    if btn.IsEnabled and btn:IsEnabled() == 0 then return nil end
    local x, y = btn:GetCenter()
    if not x or not y then return nil end
    local ratio = btn:GetEffectiveScale() / UIParent:GetEffectiveScale()
    if axis == "x" then return x * ratio / UIParent:GetWidth() end
    return 1 - y * ratio / UIParent:GetHeight()
end

local function snapshotInventory()
    inventorySnapshot, merchantSnapshot, supplySnapshot = {}, {}, {}
    if not GetContainerNumSlots or not GetContainerItemInfo or not GetContainerItemLink then
        return
    end
    local slots, owned, complete = {}, {}, true
    for bag = 0, 4 do
        local size = GetContainerNumSlots(bag)
        if not size then return end
        for slot = 1, size do
            local texture, count, locked, quality = GetContainerItemInfo(bag, slot)
            local id = itemID(GetContainerItemLink(bag, slot))
            if texture and not id then complete = false end
            if not texture then id, count = 0, 0 end
            -- The container gives no quality (-1) for trade goods, food and grey trade junk
            -- (Melted Candle, Chipped Claw) while armour gives its own; the item's record
            -- has it for anything in the bags.
            if id and id > 0 and (not quality or quality < 0) and GetItemInfo then
                local _, _, rarity = GetItemInfo(id)
                quality = rarity
            end
            if id and id > 0 and count then owned[id] = (owned[id] or 0) + count end
            slots[#slots + 1] = { bag = bag, slot = slot, item_id = id,
                count = count, quality = quality, locked = tri(locked) }
        end
    end
    inventoryCursor = inventoryCursor + 1
    local ordinal = (#slots > 0) and ((inventoryCursor - 1) % #slots + 1) or 0
    local row = slots[ordinal] or {}
    row.ordinal, row.total, row.revision = ordinal, #slots, inventoryRevision
    local container
    for i = 1, (NUM_CONTAINER_FRAMES or 13) do
        local candidate = _G["ContainerFrame" .. i]
        if candidate and candidate.IsVisible and candidate:IsVisible()
            and candidate:GetID() == row.bag then
            container = candidate
            for j = 1, (MAX_CONTAINER_ITEMS or 36) do
                local btn = _G["ContainerFrame" .. i .. "Item" .. j]
                if btn and btn.GetID and btn:GetID() == row.slot then
                    row.x, row.y = point(btn, "x"), point(btn, "y")
                    break
                end
            end
            break
        end
    end
    -- Only paint an opener when that specific bag is currently closed. The body
    -- rechecks before clicking so a toggle never closes a bag it meant to open.
    if row.bag and not container then
        local opener = row.bag == 0 and _G["MainMenuBarBackpackButton"]
            or _G["CharacterBag" .. (row.bag - 1) .. "Slot"]
        row.open_x, row.open_y = point(opener, "x"), point(opener, "y")
    end
    inventorySnapshot = row
    local class = CLASS_ID[select(2, UnitClass("player"))]
    local race = RACE_ID[select(2, UnitRace("player"))]
    local profile = Supplies and Supplies[tostring(race) .. ":" .. tostring(class)]
    if profile and complete then
        for role, id in pairs(profile) do
            supplySnapshot[role .. "_id"] = id
            supplySnapshot[role .. "_count"] = owned[id] or 0
        end
    end
    if not MerchantFrame or not MerchantFrame:IsVisible() or MerchantFrame.selectedTab ~= 1 then
        return
    end
    local merchant = { name_id = nameid(UnitName("NPC")), page = MerchantFrame.page,
                       total = GetMerchantNumItems and GetMerchantNumItems() }
    if GetCursorInfo and IsShiftKeyDown and IsControlKeyDown and IsAltKeyDown and InRepairMode then
        merchant.ready = tri(GetCursorInfo() == nil and tri(IsShiftKeyDown()) == 1
            and tri(IsControlKeyDown()) == 1 and tri(IsAltKeyDown()) == 1
            and tri(InRepairMode()) == 1)
    end
    merchant.prev_x = point(_G["MerchantPrevPageButton"], "x")
    merchant.prev_y = point(_G["MerchantPrevPageButton"], "y")
    merchant.next_x = point(_G["MerchantNextPageButton"], "x")
    merchant.next_y = point(_G["MerchantNextPageButton"], "y")
    local display = (inventoryCursor - 1) % (MERCHANT_ITEMS_PER_PAGE or 10) + 1
    local button = _G["MerchantItem" .. display .. "ItemButton"]
    if button and button.IsVisible and button:IsVisible() and button.GetID then
        local index = button:GetID()
        local _, _, price, quantity, available, _, extended = GetMerchantItemInfo(index)
        local id = itemID(GetMerchantItemLink(index))
        merchant.index, merchant.item_id = index, id
        merchant.price, merchant.quantity = price, quantity
        if available and available >= 0 then merchant.stock = available end
        merchant.unlimited = tri(available == -1)
        merchant.extended = tri(extended)
        merchant.owned = (complete and id) and (owned[id] or 0) or nil
        merchant.x, merchant.y = point(button, "x"), point(button, "y")
    end
    merchantSnapshot = merchant
end

local function INVENTORY(key) return inventorySnapshot[key] end
local function MERCHANT(key) return merchantSnapshot[key] end
local function SUPPLY(key) return supplySnapshot[key] end

local function VENDOR_GOSSIP_ID()
    -- Stock 2.4.3 GossipFrameOptionsUpdate receives (localized text, icon type)
    -- pairs from GetGossipOptions. The semantic type identifies the vendor; the
    -- existing list reader carries its displayed text hash and coordinates.
    if not GossipFrame or not GossipFrame:IsVisible() or not GetGossipOptions then return nil end
    local options = { GetGossipOptions() }
    local found, optionID
    for i = 1, #options, 2 do
        if options[i + 1] == "vendor" then
            if found then return nil end  -- two vendor branches need a further choice
            local text = plain(options[i])
            if not text or text == "" then return nil end
            found = nameid(text)
            optionID = (i + 1) / 2
        end
    end
    if not found then return nil end
    -- A vendor beyond the five lines on the wire is unselectable. Bind the observed
    -- semantic option to its actual visible stock button before exposing the hash;
    -- another option with the same text is not evidence of this vendor branch.
    for i = 1, 5 do
        local btn = _G["GossipTitleButton" .. i]
        if btn and btn:IsVisible() and btn.type == "Gossip" and btn:GetID() == optionID
            and nameid(plain(btn:GetText())) == found then
            return found
        end
    end
    return nil
end

-- --------------------------------------------------------------------- quests

-- The log is painted one entry per frame.
--
-- 2.4.3 has no notion of a "current" quest beyond the watch list, and nothing watches, so
-- keying every objective field to the first watch reported the whole quest log as unknown
-- on a live client that had quests in it. Cycling costs nothing here and needs no channel
-- into the addon: the decoder reassembles the log from `slot` and `count`, and throws the
-- assembly away when `log_hash` changes.
local questSlot = 0

local function questIndices()
    -- Real log indices, headers removed, in log order. Headers are collapsed zone
    -- separators; treating them as quests would paint "Elwynn Forest" as a step.
    local out = {}
    local n = GetNumQuestLogEntries()
    for i = 1, n do
        local title, _, _, _, isHeader = GetQuestLogTitle(i)
        if title and not isHeader then out[#out + 1] = i end
    end
    return out
end

local function advanceQuestSlot()
    local count = #questIndices()
    if count == 0 then
        questSlot = 0
    else
        questSlot = (questSlot + 1) % count
    end
end

local function QUEST_COUNT()
    return clamp(#questIndices(), 30)
end

local function QUEST_SLOT()
    return clamp(questSlot, 30)
end

local function focusQuest()
    local idx = questIndices()
    if #idx == 0 then return nil end
    return idx[(questSlot % #idx) + 1]
end

local function QUEST_SLOT_ID()
    local q = focusQuest()
    if q == nil then return nil end
    -- 2.4.3 has no GetQuestLogQuestID. The quest hyperlink carries the real id, and if
    -- this build has no GetQuestLink the field goes unknown rather than silently becoming
    -- a title hash -- the decoder could not tell the two apart.
    if GetQuestLink == nil then return nil end
    local link = GetQuestLink(q)
    if link == nil then return nil end
    local id = tonumber(string.match(link, "quest:(%d+)"))
    if id == nil or id >= 65535 then return nil end
    return id
end

local function QUEST_SLOT_COMPLETE()
    local q = focusQuest()
    if q == nil then return nil end
    local _, _, _, _, _, _, isComplete = GetQuestLogTitle(q)
    return isComplete == 1 and 1 or false
end

local function rebuildQuestHash()
    local parts = {}
    local n = GetNumQuestLogEntries()
    for i = 1, n do
        local title, level, _, _, isHeader, _, isComplete = GetQuestLogTitle(i)
        if title and not isHeader then
            table.insert(parts, title .. "\1" .. tostring(level) .. "\1" .. tostring(isComplete))
            for j = 1, GetNumQuestLeaderBoards(i) do
                local text = GetQuestLogLeaderBoard(j, i)
                if text then table.insert(parts, text) end
            end
        end
    end
    -- Objective text is included deliberately: a kill counter ticking from 3/8 to 4/8 is
    -- exactly the change this hash exists to announce, and the titles alone would miss it.
    local h = fnv1a16(table.concat(parts, "\2"))
    if h == 65535 then h = 65534 end
    questHash = h
end

local function QUEST_HASH()
    if questHash == nil then rebuildQuestHash() end
    return questHash
end

local function WATCHED_QUEST_ID()
    local q = focusQuest()
    if q == nil then return nil end
    -- 2.4.3 has no GetQuestLogQuestID. The quest hyperlink carries the real id, and if
    -- this build has no GetQuestLink the field goes unknown rather than silently becoming
    -- a title hash -- the decoder could not tell the two apart.
    if GetQuestLink == nil then return nil end
    local link = GetQuestLink(q)
    if link == nil then return nil end
    local id = tonumber(string.match(link, "quest:(%d+)"))
    if id == nil or id >= 65535 then return nil end
    return id
end

local function OBJ(i, which)
    local q = focusQuest()
    if q == nil then return nil end
    local text = GetQuestLogLeaderBoard(i, q)
    if text == nil then return nil end
    local have, need = string.match(text, "(%d+)%s*/%s*(%d+)")
    -- An exploration or event objective has no counter at all. Reporting 0/1 for it would
    -- invent a progress bar the quest does not have.
    if have == nil then return nil end
    if which == "have" then return clamp(tonumber(have), 126) end
    return clamp(tonumber(need), 126)
end

-- --------------------------------------------------------------------- action bars

local BAR_SLOTS = 12

-- No sentinel collision to work around any more. `bars.usable` and `bars.ready` carry
-- thirteen bits precisely so that the all-twelve-slots mask (4095) is an ordinary value
-- rather than the not-available code — which matters because out of combat with a full
-- bar, every slot ready *is* the normal reading, and the old twelve-bit layout reported
-- the common case as unknown. Genuine ignorance is still expressible: return nil.
local function BAR_BITS(which)
    local v, place = 0, 1
    for slot = 1, BAR_SLOTS do
        local on = false
        if HasAction(slot) then
            if which == "usable" then
                on = IsUsableAction(slot) and true or false
            else
                local start, duration = GetActionCooldown(slot)
                on = duration == nil or duration == 0 or (start + duration) <= GetTime()
            end
        end
        if on then v = v + place end
        place = place * 2
    end
    return v
end

local function GCD_FRAC()
    local now, best, any = GetTime(), 0, false
    for slot = 1, BAR_SLOTS do
        if HasAction(slot) then
            any = true
            local start, duration = GetActionCooldown(slot)
            -- A cooldown of at most 1.5 s on an action that has one is the global
            -- cooldown; anything longer is the ability's own. TBC's GCD is 1.5 s for
            -- spells and 1.0 s for energy abilities, so 1.5 is the ceiling that
            -- separates them.
            if start and duration and duration > 0 and duration <= 1.5 then
                local left = (start + duration - now) / duration
                if left > best then best = left end
            end
        end
    end
    -- With an empty bar there is no cooldown to read, so the GCD is genuinely unobserved
    -- rather than zero.
    if not any then return nil end
    return frac(best)
end

local function CASTING()
    if UnitCastingInfo then
        if UnitCastingInfo("player") ~= nil then return true end
        if UnitChannelInfo and UnitChannelInfo("player") ~= nil then return true end
        return false
    end
    return castingByEvent
end

-- --------------------------------------------------------------------- bar and spellbook
--
-- Two censuses, one entry each per paint (fields.py, schema 15): which spell each main-bar
-- button holds and where the button is, and each spellbook entry with its button, or the
-- tab or page button that brings it into view. Only stock reads: GetActionInfo,
-- GetSpellLink, the stock frames' own page state. Picking a spell up and putting it on a
-- button is a drag the body makes, with the mouse, like any other click.

local barRevision, spellRevision = 0, 0
local barCursor, spellCursor = 0, 0
local barSnapshot, spellSnapshot = {}, {}
local SPELLS_PER_PAGE = 12
local BOOK = BOOKTYPE_SPELL or "spell"

local function spellLinkID(link)
    if type(link) ~= "string" then return nil end
    return tonumber(string.match(link, "spell:(%d+)"))
end

-- The spell on an action, by id. GetActionInfo's second value is a spellbook index on
-- 2.4.3 and a spell id on later clients; whichever reading's icon is the button's own is
-- the one believed, and neither matching is unknown rather than a guess.
--
-- Except for a form. A paladin's auras are shapeshift forms on 2.4.3, as a warrior's
-- stances are, and a form that is on shows its active icon on the bar (Spell.dbc's
-- ActiveIconID), not its spellbook one; the stock client does not call it the current
-- action either. Devotion Aura read as unknown from the moment it was first pressed, and
-- the bot put it on four more slots. A form's spellbook entry is believed as it stands:
-- the live client answered GetActionInfo with spellbook indices throughout (a read-only
-- probe of every bar slot, 24 September).
local function isForm(name, icon)
    if not GetNumShapeshiftForms or not GetShapeshiftFormInfo then return false end
    for i = 1, GetNumShapeshiftForms() do
        local texture, formName = GetShapeshiftFormInfo(i)
        if (name ~= nil and formName == name) or (icon ~= nil and texture == icon) then
            return true
        end
    end
    return false
end

local function actionSpell(action)
    local kind, id, book = GetActionInfo(action)
    if kind ~= "spell" or id == nil then return nil end
    local icon = GetActionTexture(action)
    local on = (IsCurrentAction ~= nil and IsCurrentAction(action))
        or isForm(GetSpellName(id, book or BOOK), icon)
    if icon == nil and not on then return nil end
    if on or GetSpellTexture(id, book or BOOK) == icon then
        local sid = spellLinkID(GetSpellLink(id, book or BOOK))
        if sid then return sid end
    end
    if GetSpellInfo then
        local _, _, texture = GetSpellInfo(id)
        if texture ~= nil and (on or texture == icon) then return id end
    end
    return nil
end

local function snapshotBar()
    barCursor = barCursor % BAR_SLOTS + 1
    local slot = barCursor
    local row = { slot = slot, revision = barRevision }
    local btn = _G["ActionButton" .. slot]
    -- The action the button's key sends: the button's paged action, as the stock bar
    -- keeps it, else the slot itself on page one.
    local action = (btn and btn.action) or slot
    if not HasAction(action) then
        row.spell = 0
    elseif GetActionInfo then
        -- One slot the client cannot describe paints unknown; raising here would leave the
        -- previous slot's row painted and this slot never, and no census would be whole.
        local ok, spell = pcall(actionSpell, action)
        row.spell = ok and spell or nil
    end
    row.x, row.y = point(btn, "x"), point(btn, "y")
    barSnapshot = row
end

local function SPELLBOOK_OPEN()
    return SpellBookFrame ~= nil and SpellBookFrame:IsVisible() and SpellBookFrame.bookType == BOOK
end

local function snapshotSpells()
    spellSnapshot = { revision = spellRevision }
    if not GetNumSpellTabs or not GetSpellTabInfo or not GetSpellLink then return end
    local tabs, total = {}, 0
    for t = 1, GetNumSpellTabs() do
        local _, _, offset, count = GetSpellTabInfo(t)
        tabs[t] = { offset = offset or 0, count = count or 0 }
        if (offset or 0) + (count or 0) > total then total = (offset or 0) + (count or 0) end
    end
    spellSnapshot.total = total
    if total == 0 then return end
    spellCursor = spellCursor % total + 1
    local i = spellCursor
    spellSnapshot.index = i
    local ok, link = pcall(GetSpellLink, i, BOOK)
    spellSnapshot.id = ok and spellLinkID(link) or nil
    local known, passive = pcall(IsPassiveSpell, i, BOOK)
    if known then spellSnapshot.passive = tri(passive) end
    if not SPELLBOOK_OPEN() then return end
    local tab
    for t = 1, #tabs do
        if i > tabs[t].offset and i <= tabs[t].offset + tabs[t].count then tab = t; break end
    end
    if tab == nil then return end
    local within = i - tabs[tab].offset
    local page = math.floor((within - 1) / SPELLS_PER_PAGE) + 1
    local shownTab = SpellBookFrame.selectedSkillLine
    local shownPage = (SPELLBOOK_PAGENUMBERS and shownTab and SPELLBOOK_PAGENUMBERS[shownTab]) or 1
    local btn
    if tab ~= shownTab then
        btn = _G["SpellBookSkillLineTab" .. tab]
        spellSnapshot.go_x, spellSnapshot.go_y = point(btn, "x"), point(btn, "y")
    elseif page ~= shownPage then
        btn = page > shownPage and _G["SpellBookNextPageButton"] or _G["SpellBookPrevPageButton"]
        spellSnapshot.go_x, spellSnapshot.go_y = point(btn, "x"), point(btn, "y")
    else
        local k = (within - 1) % SPELLS_PER_PAGE + 1
        btn = _G["SpellButton" .. k]
        if btn and btn.GetID and btn:GetID() == k then
            spellSnapshot.x, spellSnapshot.y = point(btn, "x"), point(btn, "y")
        end
    end
end

local function BAR_CENSUS(key) return barSnapshot[key] end
local function SPELL_CENSUS(key) return spellSnapshot[key] end

-- --------------------------------------------------------------------- melee
--
-- Stock 2.4.3 ActionButton_UpdateFlash flashes the Attack button when
-- IsAttackAction(slot) and IsCurrentAction(slot) are both true, so that pair *is* the
-- client's own auto-attack state. All six bar pages are scanned because the Attack
-- action can sit on any of them; 120 HasAction calls are cheap next to a paint.
--
-- Without an Attack action anywhere, PLAYER_ENTER_COMBAT and PLAYER_LEAVE_COMBAT are the
-- same state as edges. Before the first edge after loading nothing has been observed, and
-- that is unknown (nil), not "not attacking".
local attackingByEvent = nil

local function attackSlot()
    if not IsAttackAction then return nil end
    for slot = 1, 120 do
        if HasAction(slot) and IsAttackAction(slot) then return slot end
    end
    return nil
end

local function ATTACKING()
    local slot = attackSlot()
    if slot and IsCurrentAction then
        return IsCurrentAction(slot) and true or false
    end
    return attackingByEvent
end

-- The range check the stock bar uses to redden the Attack hotkey: 1 in reach, 0 out,
-- nil when the question has no answer (no target, or nothing to ask it about).
local function MELEE_RANGE()
    if not UnitExists("target") then return nil end
    local slot = attackSlot()
    if not slot or not IsActionInRange then return nil end
    local r = IsActionInRange(slot)
    if r == 1 then return true end
    if r == 0 then return false end
    return nil
end

-- The character's own melee swings that resolved - landed or missed - counted from the
-- combat log. A swing only resolves when the target is in reach and in front, so a new
-- one is the reach signal the Attack action cannot give (IsActionInRange answers nil for
-- it on 2.4.3). A counter, like errors, so a reader between paints still sees each one.
local swingCount = 0

local function SWINGS()
    return swingCount
end

-- `LAST_ERROR` is an edge painted once. A reader sampling at 2 Hz misses most of those,
-- so the last error is also held for a short window and every error bumps a counter.
local RECENT_ERROR_S = 1.5
local recentError, recentErrorAt, errorCount = 0, nil, 0

local function noteError(index)
    recentError, recentErrorAt = index, GetTime()
    errorCount = (errorCount + 1) % 15
end

local function RECENT_ERROR(which)
    if which == "count" then return errorCount end
    if recentErrorAt ~= nil and GetTime() - recentErrorAt <= RECENT_ERROR_S then
        return recentError
    end
    return 0
end

-- --------------------------------------------------------------------- UI

local function MODAL_UP()
    local n = STATICPOPUP_NUMDIALOGS or 4
    for i = 1, n do
        local f = _G["StaticPopup" .. i]
        if f and f:IsVisible() then return true end
    end
    -- These stock panels capture input without creating a StaticPopup. In particular,
    -- GameMenuFrame blocks mouse-look, so a camera calibration behind it is invalid.
    -- Stock 2.4.3 BasicControls.xml uses ScriptErrors for both message() dialogs and
    -- script errors. UIParentLoadAddOn reports every load failure through that dialog.
    local panels = {"GameMenuFrame", "OptionsFrame", "InterfaceOptionsFrame",
                    "VideoOptionsFrame", "AudioOptionsFrame", "KeyBindingFrame",
                    "AddonList", "ScriptErrors"}
    for _, name in ipairs(panels) do
        local f = _G[name]
        if f and f.IsVisible and f:IsVisible() then return true end
    end
    return false
end

-- --------------------------------------------------------------------- events

-- Unnamed: a frame name is a global, and this addon owns none.
local watcher = CreateFrame("Frame")
watcher:RegisterEvent("PLAYER_ENTERING_WORLD")
watcher:RegisterEvent("PLAYER_LOGIN")
watcher:RegisterEvent("ZONE_CHANGED")
watcher:RegisterEvent("ZONE_CHANGED_INDOORS")
watcher:RegisterEvent("ZONE_CHANGED_NEW_AREA")
watcher:RegisterEvent("QUEST_LOG_UPDATE")
watcher:RegisterEvent("BAG_UPDATE")
watcher:RegisterEvent("UI_ERROR_MESSAGE")
watcher:RegisterEvent("UNIT_SPELLCAST_START")
watcher:RegisterEvent("UNIT_SPELLCAST_STOP")
watcher:RegisterEvent("UNIT_SPELLCAST_FAILED")
watcher:RegisterEvent("UNIT_SPELLCAST_INTERRUPTED")
watcher:RegisterEvent("UNIT_SPELLCAST_SUCCEEDED")
watcher:RegisterEvent("UNIT_SPELLCAST_CHANNEL_START")
watcher:RegisterEvent("UNIT_SPELLCAST_CHANNEL_STOP")
watcher:RegisterEvent("PLAYER_ENTER_COMBAT")
watcher:RegisterEvent("PLAYER_LEAVE_COMBAT")
watcher:RegisterEvent("COMBAT_LOG_EVENT_UNFILTERED")
watcher:RegisterEvent("ACTIONBAR_SLOT_CHANGED")
watcher:RegisterEvent("ACTIONBAR_PAGE_CHANGED")
watcher:RegisterEvent("UPDATE_BONUS_ACTIONBAR")
watcher:RegisterEvent("SPELLS_CHANGED")
watcher:RegisterEvent("LEARNED_SPELL_IN_TAB")

watcher:SetScript("OnEvent", function(self, event, a1)
    -- 2.4.3 delivers event arguments in the globals arg1..argN; named handler parameters
    -- only arrived in 3.0. Reading both means this file works on either without a fork.
    local p1 = a1
    if p1 == nil then p1 = arg1 end
    local ev = event
    if ev == nil then ev = _G["event"] end

    if ev == "UI_ERROR_MESSAGE" then
        if type(p1) == "string" then
            lastError = errorIndex[p1] or 1   -- 1 = "other": something failed, we just do not model it
            noteError(lastError)
        end
    elseif ev == "PLAYER_ENTER_COMBAT" then
        attackingByEvent = true               -- the swing timer started: auto-attack is on
    elseif ev == "PLAYER_LEAVE_COMBAT" then
        attackingByEvent = false
    elseif ev == "COMBAT_LOG_EVENT_UNFILTERED" then
        -- 2.4.3: timestamp, sub-event, source GUID, ... in arg1..argN.
        local sub, source = arg2, arg3
        if (sub == "SWING_DAMAGE" or sub == "SWING_MISSED") and source ~= nil
                and UnitGUID and source == UnitGUID("player") then
            swingCount = (swingCount + 1) % 15
        end
    elseif ev == "ACTIONBAR_SLOT_CHANGED" or ev == "ACTIONBAR_PAGE_CHANGED"
        or ev == "UPDATE_BONUS_ACTIONBAR" then
        barRevision = (barRevision + 1) % 255
    elseif ev == "SPELLS_CHANGED" or ev == "LEARNED_SPELL_IN_TAB" then
        spellRevision = (spellRevision + 1) % 255
    elseif ev == "BAG_UPDATE" then
        inventoryRevision = (inventoryRevision + 1) % 65535
    elseif ev == "QUEST_LOG_UPDATE" then
        questHash = nil                       -- recomputed lazily; the log can fire this several times a second
    elseif ev == "PLAYER_ENTERING_WORLD" then
        inventoryRevision = (inventoryRevision + 1) % 65535
        inWorld = true
        mapDirty = true
        mapTrusted = false
        questHash = nil
    elseif ev == "UNIT_SPELLCAST_START" or ev == "UNIT_SPELLCAST_CHANNEL_START" then
        if p1 == "player" then castingByEvent = true end
    elseif ev == "UNIT_SPELLCAST_STOP" or ev == "UNIT_SPELLCAST_CHANNEL_STOP"
        or ev == "UNIT_SPELLCAST_FAILED" or ev == "UNIT_SPELLCAST_INTERRUPTED"
        or ev == "UNIT_SPELLCAST_SUCCEEDED" then
        if p1 == "player" then castingByEvent = false end
    else
        mapDirty = true                       -- a zone event; the world map is now showing the wrong zone
        mapTrusted = false
        questHash = nil
    end
end)

local function syncMap()
    -- Called once per paint, not once per frame, and it does nothing at all unless a zone
    -- event has fired. GetPlayerMapPosition answers 0,0 unless the world map is set to the
    -- zone the player is standing in, but SetMapToCurrentZone yanks the map out from under
    -- anyone reading it, so it waits until the map is closed. Calling this every frame
    -- would make the world map unusable for a human watching the run.
    --
    -- Nothing happens before PLAYER_ENTERING_WORLD: the addon is loaded and painting
    -- during the loading screen, and asking the client about a zone it has not entered
    -- raises rather than answering.
    if not inWorld then return end
    if WorldMapFrame and WorldMapFrame:IsVisible() then
        mapDirty = true
        mapTrusted = false
        return
    end
    if mapDirty then
        SetMapToCurrentZone()
        mapDirty = false
        mapTrusted = true
    end
end

-- --------------------------------------------------------------------- export
--
-- Returned as one table rather than set as globals. The generated getters call bare names
-- like `frac` and `tri`, which are far too generic to own in WoW's single shared global
-- namespace; the painter gives each getter a function environment holding these, falling
-- through to _G for the client API. A second addon defining its own `frac` then cannot
-- change what this strip paints, and nothing here is visible to anyone else.

return {
    frac = frac,
    angle = angle,
    tri = tri,
    clamp = clamp,
    nameid = nameid,
    fnv1a16 = fnv1a16,
    CHARACTER_KEY = CHARACTER_KEY,
    CURSOR_OBJECT = CURSOR_OBJECT,
    TARGET_GUID = TARGET_GUID,
    ZONE_ID = ZONE_ID,
    PLAYER_FACING = PLAYER_FACING,
    BAG_FREE = BAG_FREE,
    DURABILITY_MIN = DURABILITY_MIN,
    ADVANCE_BUTTON = ADVANCE_BUTTON,
    QUEST_CHOICE = QUEST_CHOICE,
    LIST_LINE = LIST_LINE,
    QUEST_HASH = QUEST_HASH,
    QUEST_COUNT = QUEST_COUNT,
    QUEST_SLOT = QUEST_SLOT,
    QUEST_SLOT_ID = QUEST_SLOT_ID,
    QUEST_SLOT_COMPLETE = QUEST_SLOT_COMPLETE,
    advanceQuestSlot = advanceQuestSlot,
    OBJ = OBJ,
    BAR_BITS = BAR_BITS,
    BAR_CENSUS = BAR_CENSUS,
    SPELL_CENSUS = SPELL_CENSUS,
    SPELLBOOK_OPEN = SPELLBOOK_OPEN,
    snapshotBar = snapshotBar,
    snapshotSpells = snapshotSpells,
    GCD_FRAC = GCD_FRAC,
    CASTING = CASTING,
    ATTACKING = ATTACKING,
    MELEE_RANGE = MELEE_RANGE,
    RECENT_ERROR = RECENT_ERROR,
    SWINGS = SWINGS,
    MODAL_UP = MODAL_UP,
    LAST_ERROR = LAST_ERROR,
    CLASS_ID = CLASS_ID,
    RACE_ID = RACE_ID,
    CLASSIFICATION_ID = CLASSIFICATION_ID,
    UI_ERRORS = UI_ERRORS,
    syncMap = syncMap,
    snapshotInventory = snapshotInventory,
    INVENTORY = INVENTORY,
    MERCHANT = MERCHANT,
    SUPPLY = SUPPLY,
    VENDOR_GOSSIP_ID = VENDOR_GOSSIP_ID,
    buildErrorIndex = buildErrorIndex,
    SEQ = 0,
}
