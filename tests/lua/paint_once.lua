-- Load the real addon under the stub, force one paint, print the painted cells.
--
-- Output is one line per payload cell, "r g b", which the Python side feeds straight
-- back through `radio.unpack`. If the values a test set come back out, the addon's
-- packer and the decoder agree — proven by execution rather than by reading both.

dofile("tests/lua/wow_stub.lua")

local overrides = loadstring(os.getenv("JEV_STATE") or "return {}")
if overrides then
    for k, v in pairs(overrides() or {}) do STATE[k] = v end
end
-- Test the real getters when a stock entry point is absent or throws. The addon's
-- pcall boundary must encode unknown rather than turn unavailable APIs into false.
if STATE.missingApi then _G[STATE.missingApi] = nil end
if STATE.throwingApi then
    _G[STATE.throwingApi] = function() error("unavailable test API") end
end
if STATE.visiblePanel then
    local panel = CreateFrame("Frame", STATE.visiblePanel)
    function panel:IsVisible() return STATE.panelHidden ~= 1 end
end
if STATE.inventoryFixture then
    local function button(name, id, x, y, visible)
        local b = CreateFrame("Button", name)
        function b:GetID() return id end
        function b:IsVisible() return visible ~= false end
        function b:IsEnabled() return 1 end
        function b:GetCenter() return x, y end
        return b
    end
    button("ContainerFrame1", 0, 0, 0)
    -- Deliberately reversed screen order, as stock TBC does. ID, not ordinal, wins.
    button("ContainerFrame1Item2", 1, 1400, 400, STATE.bagHidden ~= 1)
    button("ContainerFrame1Item1", 2, 1350, 400)
    if STATE.bagHidden then
        function ContainerFrame1:IsVisible() return false end
        button("MainMenuBarBackpackButton", 0, 1500, 25)
    end
    button("MerchantFrame", 0, 0, 0)
    MerchantFrame.selectedTab = STATE.buyback and 2 or 1
    MerchantFrame.page = 1
    button("MerchantItem1ItemButton", 1, 150, 600)
end
if STATE.vendorGossip then
    local b = CreateFrame("Button", "GossipTitleButton2")
    b.type = "Gossip"
    function b:GetID() return STATE.wrongGossipID and 1 or 2 end
    function b:GetText() return "|cffffffffBrowse my wares.|r" end
    function b:IsVisible() return STATE.vendorLineHidden ~= 1 end
    function b:GetCenter() return 200, 300 end
end

-- A spellbook of two tabs (General: Attack; Holy: fourteen spells over two pages), a main
-- bar holding three of them and two items, and the stock buttons that show both.
if STATE.spellFixture then
    BOOKTYPE_SPELL = "spell"
    local book = {6603, 20154, 635, 639, 465, 19740, 20271, 498, 853, 1152, 21082, 3127,
                  633, 1022, 10290}
    local tabs = {{0, 1}, {1, 14}}
    function GetNumSpellTabs() return #tabs end
    function GetSpellTabInfo(t) return "tab" .. t, "icon", tabs[t][1], tabs[t][2] end
    function GetSpellLink(i, kind)
        if book[i] == nil then return nil end
        return "|cff71d5ff|Hspell:" .. book[i] .. "|h[spell]|h|r"
    end
    function GetSpellTexture(i, kind) return book[i] and ("tex" .. book[i]) end
    function GetSpellInfo(id) return "spell", "Rank 1", "tex" .. id end
    function IsPassiveSpell(i, kind) return book[i] == 3127 end
    local bar = {[1] = 1, [2] = 2, [3] = 3}
    local items = {[11] = true, [12] = true}
    function HasAction(slot) return bar[slot] ~= nil or items[slot] == true end
    function GetActionInfo(slot)
        if bar[slot] then return "spell", bar[slot], "spell" end
        if items[slot] then return "item", 159 end
    end
    function GetActionTexture(slot)
        if bar[slot] then return STATE.wrongIcon and "other" or ("tex" .. book[bar[slot]]) end
        if items[slot] then return "itemtex" end
    end
    local function button(name, id, x, y, enabled)
        local b = CreateFrame("Button", name)
        function b:GetID() return id end
        function b:IsVisible() return true end
        function b:IsEnabled() if enabled == false then return nil end return 1 end
        function b:GetCenter() return x, y end
        return b
    end
    for slot = 1, 12 do
        button("ActionButton" .. slot, slot, 100 + 40 * slot, 40).action = slot
    end
    local frame = button("SpellBookFrame", 0, 200, 500)
    function frame:IsVisible() return STATE.bookOpen == 1 end
    frame.bookType = "spell"
    frame.selectedSkillLine = STATE.shownTab or 2
    SPELLBOOK_PAGENUMBERS = {1, STATE.shownPage or 1}
    for k = 1, 12 do
        button("SpellButton" .. k, k, 60 + (k % 2) * 140, 700 - math.floor((k - 1) / 2) * 50)
    end
    button("SpellBookSkillLineTab1", 1, 330, 650)
    button("SpellBookSkillLineTab2", 2, 330, 600)
    button("SpellBookPrevPageButton", 0, 40, 100)
    button("SpellBookNextPageButton", 0, 300, 100)
    if STATE.trainer then
        button("ClassTrainerTrainButton", 0, 224, 420, STATE.trainEnabled == 1)
    end
end

-- The addon exactly as a client installs it: the one file `tools/gen_addon_fields.py`
-- builds. It owns no global names, so any global it assigns fails the run, attributed by
-- the chunk doing the assigning - the event arguments this harness sets (arg2, ...) are
-- not the addon's. Recorded rather than raised, because a getter runs inside a pcall and
-- an error there would only paint unknown.
local bundle = assert(os.getenv("ADDON_BUNDLE"), "ADDON_BUNDLE must name the built addon")
local leaked = {}
local function byAddon(level)
    local info = debug.getinfo(level + 1, "S")
    return info ~= nil and info.source == "@" .. bundle
end
setmetatable(_G, {__newindex = function(t, k, v)
    if byAddon(2) then leaked[#leaked + 1] = tostring(k) end
    rawset(t, k, v)
end})
-- A frame name is a global too, one the client creates rather than the addon's own code.
local createFrame = CreateFrame
CreateFrame = function(kind, name, ...)
    if name ~= nil and byAddon(2) then leaked[#leaked + 1] = tostring(name) end
    return createFrame(kind, name, ...)
end
local first = #CREATED_FRAMES + 1
dofile(bundle)

-- Its frames are unnamed, so they are found by what they do: the watcher handles events
-- and nothing else, and the painter is the one with an OnUpdate.
local function addonFrame(has, lacks)
    for i = first, #CREATED_FRAMES do
        local f = CREATED_FRAMES[i]
        if f.scripts[has] and not (lacks and f.scripts[lacks]) then return f end
    end
end

-- Client events, delivered through the addon's own watcher before the paint, in the
-- 2.4.3 style: the handler receives the event name and its first argument.
if STATE.events then
    local w = addonFrame("OnEvent", "OnUpdate")
    assert(w, "the addon registered no event watcher")
    for _, e in ipairs(STATE.events) do
        -- Later arguments arrive only as globals on 2.4.3 (arg2, arg3, ...).
        for i = 3, #e do _G["arg" .. (i - 1)] = e[i] end
        w.scripts.OnEvent(w, e[1], e[2])
        for i = 3, #e do _G["arg" .. (i - 1)] = nil end
    end
end
if STATE.paintTime then STATE.time = STATE.paintTime end

-- Drive the addon's own OnUpdate rather than calling an exported test hook. There is no
-- hook, deliberately: a paint path that only tests can reach is not the paint path.
local f = addonFrame("OnUpdate")
assert(f, "the addon registered no OnUpdate")
local ticks = tonumber(os.getenv("JEV_TICKS") or "1")
for _ = 1, ticks do
    f.scripts.OnUpdate(f, 10.0)
end

if #leaked > 0 then
    io.stderr:write("the addon set globals: " .. table.concat(leaked, ", ") .. "\n")
    os.exit(1)
end

local painted = GetTexturesPainted()
local n = 0
for i = 1, #painted do
    local t = painted[i]
    if t.r then n = n + 1 end
end
print("CELLS " .. n)
for i = 1, #painted do
    local t = painted[i]
    print(string.format("%d %d %d",
        math.floor((t.r or 0) * 255 + 0.5),
        math.floor((t.g or 0) * 255 + 0.5),
        math.floor((t.b or 0) * 255 + 0.5)))
end
