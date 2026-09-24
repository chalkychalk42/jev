# jevpath

The planner's navmesh backend. A standalone Detour query against the tiles the server
already uses to walk its own NPCs.

## Build

```sh
SRC=~/cmangos/mangos-tbc/dep/recastnavigation
g++ -O2 -std=c++17 -DDT_POLYREF64 -o jevpath jevpath.cpp \
    -I"$SRC/Detour/Include" \
    ~/cmangos/build/dep/recastnavigation/Detour/libDetour.a
```

**`-DDT_POLYREF64` is mandatory.** CMaNGOS's `dep/recastnavigation/CMakeLists.txt` adds it,
so `dtPolyRef` in the compiled library is 64-bit. Without the flag every Detour symbol
fails to link with a signature that looks identical except for `unsigned int` where the
library has `unsigned long`.

The binary is a build artifact and is not committed.

## Use

```sh
echo "-8944.81 -120.17 82.02 -8902.59 -162.61 82.02" | ./jevpath ~/cmangos/run/bin/mmaps 0
{"status":"ready","map":0,"tiles":513}
{"status":"complete","filter":"player","points":[[-8944.8,-120.2,83.3],[-8913.1,-141.3,82.4],[-8902.6,-162.6,82.9]]}
```

Reads `x1 y1 z1 x2 y2 z2` in world yards per line, writes one JSON object per query. The
`ready` line comes first so a caller waits for start-up rather than guessing at it: map 0
is 513 tiles, 2.2 seconds and 577 MB resident, which is why the process is long-lived and
`MmapQuery` keeps one per map.

`z` may be wrong or zero — a map fraction converts to x and y and says nothing about
height — so the vertical search extent is 200 yards and the query snaps onto whatever
polygon is under the guess.

Routes keep to ground a character can climb: the filter is the one the server gives the
bots it drives itself - ground and water, water twenty times as costly, never lava, slime
or the mesh's steep ground (50 to 60 degrees, which the server's creatures walk and a
character slides down). Only where that finds no complete route is everything allowed,
and the answer says so: `"filter":"any"` and a `detail`.
