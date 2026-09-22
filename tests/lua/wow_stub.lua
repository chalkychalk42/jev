-- A stub of the 2.4.3 client API, just deep enough to run JevRadio for real.
--
-- The point is to execute *the addon itself* rather than a transcription of it. A
-- transcription proves the format; only running the real file catches a nil index, a
-- wrong arity or a helper that was renamed on one side — and those would otherwise first
-- appear on a client that has to be restarted to try again.
--
-- Values come from a table the harness sets, so a test can paint a known character and
-- assert the decoder reads that character back.

STATE = STATE or {}

local function pick(key, default)
    local v = STATE[key]
    if v == nil then return default end
    return v
end

-- ---------------------------------------------------------------- frames

TOUCHED = {}
local textures = {}

local function newTexture()
    local t = setmetatable({r = 0, g = 0, b = 0, shown = true}, {
        __index = function(tbl, key)
            TOUCHED[key] = (TOUCHED[key] or 0) + 1
            local noop = function() return nil end
            rawset(tbl, key, noop)
            return noop
        end,
    })
    function t:SetTexture(r, g, b) self.r, self.g, self.b = r, g, b end
    function t:SetAllPoints() end
    function t:SetPoint() end
    function t:SetWidth(w) self.w = w end
    function t:SetHeight(h) self.h = h end
    function t:SetDrawLayer() end
    function t:Show() self.shown = true end
    function t:Hide() self.shown = false end
    textures[#textures + 1] = t
    return t
end

-- Frame methods the addon calls that this stub has not modelled become recorded no-ops
-- rather than errors. A WoW frame has well over a hundred methods and enumerating them
-- would be busywork; what matters is that the addon *runs*, and that we can see the API
-- surface it actually touches. TOUCHED is printed by the harness for exactly that.
TOUCHED = {}

local frameMeta = {
    __index = function(tbl, key)
        TOUCHED[key] = (TOUCHED[key] or 0) + 1
        local noop = function() return nil end
        rawset(tbl, key, noop)
        return noop
    end,
}

local function newFrame()
    local f = setmetatable({scripts = {}}, frameMeta)
    function f:CreateTexture() return newTexture() end
    function f:SetScript(k, fn) self.scripts[k] = fn end
    function f:GetScript(k) return self.scripts[k] end
    function f:SetWidth(w) self.w = w end
    function f:SetHeight(h) self.h = h end
    function f:SetPoint() end
    function f:SetAllPoints() end
    function f:SetFrameStrata() end
    function f:SetFrameLevel() end
    function f:RegisterEvent() end
    function f:UnregisterEvent() end
    function f:Show() end
    function f:Hide() end
    function f:IsVisible() return false end
    function f:GetEffectiveScale() return pick("uiScale", 1.0) end
    function f:GetWidth() return 1600 end
    function f:GetHeight() return 900 end
    return f
end

-- WoW registers a named frame as a global of that name, and addons rely on it. A stub
-- that dropped the name left `JevRadioFrame` nil, so the harness could not reach the
-- frame's OnUpdate and the strip was never painted at all — every payload cell came out
-- black and read as a schema mismatch rather than as "nothing ran".
function CreateFrame(_, name, _)
    local f = newFrame()
    if name then _G[name] = f end
    return f
end
UIParent = newFrame()
WorldFrame = newFrame()

function GetTexturesPainted() return textures end

-- ---------------------------------------------------------------- unit API

function UnitClass() return "Mage", pick("class", "MAGE") end
function UnitRace() return "Human", pick("race", "Human") end
function UnitLevel(u)
    if u == "target" then return pick("targetLevel", 4) end
    return pick("level", 4)
end
function UnitXP() return pick("xp", 300) end
function UnitXPMax() return pick("xpMax", 1000) end
function UnitHealth(u)
    if u == "target" then return pick("targetHp", 40) end
    return pick("hp", 80)
end
function UnitHealthMax(u)
    if u == "target" then return pick("targetHpMax", 100) end
    return pick("hpMax", 100)
end
function UnitMana() return pick("mana", 60) end
function UnitManaMax() return pick("manaMax", 100) end
function UnitPowerType() return pick("powerType", 0) end
function UnitAffectingCombat() return pick("combat", nil) end
function UnitIsDead() return pick("dead", nil) end
function UnitIsGhost() return pick("ghost", nil) end
function UnitIsAFK() return pick("afk", nil) end
function UnitOnTaxi() return pick("taxi", nil) end
function UnitExists(u) return pick("hasTarget", 1) end
function UnitName(u) return pick("targetName", "Kobold Vermin") end
function UnitReaction() return pick("reaction", 2) end
function UnitClassification() return pick("classification", "normal") end
function UnitIsUnit() return pick("targetsMe", nil) end
function CheckInteractDistance() return pick("inMelee", 1) end

function IsMounted() return pick("mounted", nil) end
function IsSwimming() return pick("swimming", nil) end
function IsFalling() return pick("falling", nil) end
function IsIndoors() return pick("indoors", nil) end
function IsResting() return pick("resting", nil) end
function IsStealthed() return pick("stealthed", nil) end

function GetPlayerFacing() return pick("facing", 1.57) end
function GetPlayerMapPosition() return pick("mx", 0.4817), pick("my", 0.4294) end
function SetMapToCurrentZone() end
function GetMapInfo() return pick("mapFile", "Elwynn") end
function GetCurrentMapAreaID() return pick("areaId", 12) end
function GetMoney() return pick("money", 91234) end
function GetTime() return pick("time", 1000.0) end

-- ---------------------------------------------------------------- bags, bars, quests

function GetContainerNumFreeSlots(bag) return pick("freePerBag", 2), nil end
function GetContainerNumSlots(bag)
    if STATE.inventoryFixture then return bag == 0 and 2 or 0 end
    return 16
end
function GetContainerItemInfo(bag, slot)
    if STATE.inventoryFixture and bag == 0 then
        if slot == 1 then
            return "texture", pick("itemCount", 2), pick("itemLocked", false), pick("itemQuality", 0)
        end
        if slot == 2 and STATE.foodCount then return "food", STATE.foodCount, nil, 1 end
    end
    return nil
end
function GetContainerItemLink(bag, slot)
    if STATE.inventoryFixture and bag == 0 then
        if slot == 1 and not STATE.itemUnread then return "|Hitem:7073:0:0:0|h[Broken Fang]|h" end
        if slot == 2 and STATE.foodCount then return "|Hitem:2070:0:0:0|h[Darnassian Bleu]|h" end
    end
    return nil
end
function GetMerchantNumItems() return pick("merchantTotal", 1) end
function GetCursorInfo() return pick("cursorType", nil) end
function IsShiftKeyDown() return pick("shiftHeld", nil) end
function IsControlKeyDown() return nil end
function IsAltKeyDown() return nil end
function InRepairMode() return pick("repairMode", nil) end
function GetGossipOptions()
    if STATE.duplicateVendor then return "First shop", "vendor", "Second shop", "vendor" end
    if STATE.vendorGossip then return "Other option", "gossip", "|cffffffffBrowse my wares.|r", "vendor" end
    return "Other option", "gossip"
end
function GetMerchantItemInfo(index)
    return "Darnassian Bleu", "texture", pick("offerPrice", 25), 5, -1, 1,
        pick("extendedCost", nil)
end
function GetMerchantItemLink(index) return "|Hitem:2070:0:0:0|h[Darnassian Bleu]|h" end
function GetInventoryItemDurability(slot)
    if slot == 16 then return pick("dur", 90), 100 end
    return nil
end
function HasAction(slot) return slot <= pick("actionSlots", 6) end
function IsUsableAction(slot) return slot <= pick("usableSlots", 4), nil end
function GetActionCooldown(slot) return 0, 0, 1 end

function GetNumQuestLogEntries() return pick("questCount", 1), pick("questCount", 1) end
function GetQuestLogTitle(i)
    return "Kobold Camp Cleanup", 3, 0, nil, nil, nil, pick("questComplete", false), pick("questId", 7)
end
function GetNumQuestLeaderBoards(i) return 1 end
function GetQuestLogLeaderBoard(j, i)
    return "Kobold Vermin slain: " .. pick("have", 2) .. "/10", "monster", nil
end
function GetQuestLogSelection() return 1 end
function SelectQuestLogEntry() end
function GetNumQuestWatches() return 1 end
function GetQuestIndexForWatch() return 1 end
function GetQuestLink() return "|Hquest:7:3|h[Kobold Camp Cleanup]|h" end

-- ---------------------------------------------------------------- misc globals

LootFrame, GossipFrame, MerchantFrame = newFrame(), newFrame(), newFrame()
QuestFrame, ClassTrainerFrame, MailFrame = newFrame(), newFrame(), newFrame()
STATICPOPUP_NUMDIALOGS = 4
function getglobal(name) return _G[name] end
_G = _G or getfenv(0)
