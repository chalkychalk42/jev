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

dofile("addons/JevRadio/Supplies.lua")
dofile("addons/JevRadio/Helpers.lua")
dofile("addons/JevRadio/Fields.lua")
dofile("addons/JevRadio/JevRadio.lua")

-- Drive the addon's own OnUpdate rather than calling an exported test hook. There is no
-- hook, deliberately: a paint path that only tests can reach is not the paint path.
local f = _G["JevRadioFrame"]
assert(f, "JevRadioFrame was never created")
assert(f.scripts and f.scripts.OnUpdate, "the addon registered no OnUpdate")
local ticks = tonumber(os.getenv("JEV_TICKS") or "1")
for _ = 1, ticks do
    f.scripts.OnUpdate(f, 10.0)
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
