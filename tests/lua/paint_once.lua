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
