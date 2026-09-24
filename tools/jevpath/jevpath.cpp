// jevpath — answer path queries against the navmesh the server already uses.
//
//   jevpath <mmaps_dir> <map_id>
//   stdin:  "x1 y1 z1 x2 y2 z2\n"   (world yards; z may be 0 — see below)
//   stdout: {"status":"complete|partial|nopath|error","points":[[x,y,z],...]}
//
// Why a sidecar rather than anything cleverer
// -------------------------------------------
// The tiles under `mmaps/` are the same Recast tiles the server uses to walk its own
// NPCs around Northshire Abbey. They are already extracted, Detour is already built as
// `libDetour.a`, and the format is a five-field header followed by raw tile data. So the
// cheapest honest implementation is to link that library and ask it — not to reimplement
// a mesh, and not to drive the `.mmap path` GM command, which needs a logged-in character
// and a chat parser to read its own answer back.
//
// CMaNGOS's own `PathFinder` takes a `Unit*` and reaches into the game object model, so
// it cannot be called standalone. Everything it does on top of Detour is smoothing and
// unit-specific filtering; the query itself is what is wanted here.
//
// Coordinates
// -----------
// Detour's axes are not the game's. Read from `PathFinder.cpp`, not remembered:
//
//     detour = { world.y, world.z, world.x }
//     world  = { detour[2], detour[0], detour[1] }
//
// The caller usually does not know z: a map fraction converts to x and y and says nothing
// about height. So the vertical search extent is deliberately huge — the query snaps onto
// whatever polygon is under or over the guess, which is right in a world where the only
// thing at a given x/y is the ground.

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "DetourNavMesh.h"
#include "DetourNavMeshQuery.h"
#include "DetourCommon.h"

static const unsigned int MMAP_MAGIC = 0x4d4d4150;  // 'MMAP'
static const int MAX_POLYS = 2048;

// Horizontal tolerance is tight: a start point that is metres from any polygon is a bad
// reading, not a long walk. Vertical is enormous because z is usually unknown.
static const float EXTENT_XZ = 6.0f;
static const float EXTENT_Y = 200.0f;

// What a character can walk, as the server tells the bots it drives itself
// (`PathFinder::createFilter` under ENABLE_PLAYERBOTS): ground and water, never lava or
// slime, and never steep ground - the 50 to 60 degree band the mesh generator marks
// "walkable for mobs, unwalkable for players" (`rcModAlmostUnwalkableTriangles`; the
// comment in MoveMapSharedDefines.h calling that area unused is stale). Water costs
// twenty times ground there as well: swimming is slow.
//
// Measured 24 September: the leg out of Northshire Vineyards to the Gray Forest Wolves
// crossed seven steep polygons, up a 55 degree face, and the character slid back down it
// for four minutes until the watchdog failed the step. Asked this way, the same leg climbs
// the hill by a 44 degree slope, 75 yards longer. All 110 of the guide's legs still plan
// complete; 45 had crossed steep ground and 38 had swum, and the longest detour is 1.46
// times the old route, round a lake.
static const unsigned short NAV_GROUND = 0x01;         // MoveMapSharedDefines.h
static const unsigned short NAV_GROUND_STEEP = 0x02;
static const unsigned short NAV_WATER = 0x04;
static const unsigned short NAV_MAGMA_SLIME = 0x08;
static const unsigned char NAV_AREA_WATER = 9;
static const unsigned char NAV_AREA_GROUND = 11;
static const float WATER_COST = 20.0f;

// Ground a character climbs badly. The steep band starts at 50 degrees and the character
// slides well before that: over 13,400 half-second ticks of sessions 60-76, the share of
// moving ticks spent falling rose with the mean slope of the polygon underfoot - 2-7%
// below 25 degrees, 20-26% from 30 to 45, 37-53% above 45. Session 76, on a route clear of
// the steep band, slid for three minutes on a vineyard hillside whose polygons average 42
// to 51 degrees. So ground costs more the steeper it is: a polygon whose detail triangles
// average 30 degrees or more costs four times its length, 40 or more twenty times. The
// guide's 110 legs grow 1.7% in all, 1.19 times at most; the vineyard leg goes round by the
// road, 1,077 yards for 424, and no stretch of it climbs more than one in two.
static const float SLOPE_DEG[2] = {30.0f, 40.0f};
static const unsigned char SLOPE_AREA[2] = {20, 21};   // areas the mesh does not use
static const float SLOPE_COST[2] = {4.0f, 20.0f};

struct MmapTileHeader {
    unsigned int mmapMagic;
    unsigned int dtVersion;
    unsigned int mmapVersion;
    unsigned int size;
    unsigned int usesLiquids;
};

static void emitError(const char* msg) {
    printf("{\"status\":\"error\",\"detail\":\"%s\"}\n", msg);
    fflush(stdout);
}

static bool loadTiles(dtNavMesh* mesh, const std::string& dir, int mapId, int& loaded) {
    loaded = 0;
    for (int x = 0; x < 64; ++x) {
        for (int y = 0; y < 64; ++y) {
            char name[512];
            snprintf(name, sizeof(name), "%s/%03i%02i%02i.mmtile", dir.c_str(), mapId, x, y);
            FILE* f = fopen(name, "rb");
            if (!f) continue;

            MmapTileHeader header;
            if (fread(&header, sizeof(header), 1, f) != 1 || header.mmapMagic != MMAP_MAGIC) {
                fclose(f);
                continue;
            }
            unsigned char* data = (unsigned char*)dtAlloc(header.size, DT_ALLOC_PERM);
            if (!data) { fclose(f); return false; }
            if (fread(data, header.size, 1, f) != 1) {
                dtFree(data);
                fclose(f);
                continue;
            }
            fclose(f);

            // DT_TILE_FREE_DATA: the mesh owns it from here, and this process is
            // long-lived, so leaking a tile per query would be a slow death.
            if (dtStatusFailed(mesh->addTile(data, header.size, DT_TILE_FREE_DATA, 0, nullptr)))
                dtFree(data);
            else
                ++loaded;
        }
    }
    return true;
}

// Area-weighted mean slope of a polygon's detail triangles, in degrees from level.
static float meanSlope(const dtMeshTile* tile, const dtPoly* poly) {
    const dtPolyDetail* pd = &tile->detailMeshes[poly - tile->polys];
    float area = 0.0f, weighted = 0.0f;
    for (int j = 0; j < pd->triCount; ++j) {
        const unsigned char* t = &tile->detailTris[(pd->triBase + j) * 4];
        const float* v[3];
        for (int k = 0; k < 3; ++k)
            v[k] = t[k] < poly->vertCount
                ? &tile->verts[poly->verts[t[k]] * 3]
                : &tile->detailVerts[(pd->vertBase + (t[k] - poly->vertCount)) * 3];
        float e0[3], e1[3], n[3];
        dtVsub(e0, v[1], v[0]);
        dtVsub(e1, v[2], v[0]);
        dtVcross(n, e0, e1);
        const float len = dtVlen(n);   // twice the triangle's area; Detour's y is up
        if (len < 1e-6f) continue;
        area += len;
        weighted += len * acosf(fabsf(n[1]) / len) * (180.0f / 3.14159265f);
    }
    return area > 0.0f ? weighted / area : 0.0f;
}

// Relabel sloping ground so the walking filter can cost it.
static void costSlopes(dtNavMesh* mesh) {
    const dtNavMesh* view = mesh;
    for (int i = 0; i < view->getMaxTiles(); ++i) {
        const dtMeshTile* tile = view->getTile(i);
        if (!tile || !tile->header) continue;
        const dtPolyRef base = view->getPolyRefBase(tile);
        for (int ip = 0; ip < tile->header->polyCount; ++ip) {
            const dtPoly* poly = &tile->polys[ip];
            if (poly->getType() != DT_POLYTYPE_GROUND || poly->getArea() != NAV_AREA_GROUND)
                continue;
            const float slope = meanSlope(tile, poly);
            for (int k = 1; k >= 0; --k) {
                if (slope >= SLOPE_DEG[k]) {
                    mesh->setPolyArea(base | (dtPolyRef)ip, SLOPE_AREA[k]);
                    break;
                }
            }
        }
    }
}

// One filter's answer: the straight path, or why there is none.
struct Route {
    bool complete = false;
    int count = 0;
    const char* detail = "";
    char where[16] = "";
    float straight[MAX_POLYS * 3];
};

static void plan(dtNavMeshQuery* query, const dtQueryFilter& filter,
                 const float* start, const float* end, Route& out) {
    const float extents[3] = {EXTENT_XZ, EXTENT_Y, EXTENT_XZ};
    dtPolyRef startRef = 0, endRef = 0;
    float startPt[3], endPt[3];
    query->findNearestPoly(start, extents, &filter, &startRef, startPt);
    query->findNearestPoly(end, extents, &filter, &endRef, endPt);
    if (!startRef || !endRef) {
        snprintf(out.where, sizeof(out.where), "%s",
                 !startRef ? (!endRef ? "both ends" : "start") : "end");
        out.detail = "off mesh";
        return;
    }

    dtPolyRef polys[MAX_POLYS];
    int polyCount = 0;
    query->findPath(startRef, endRef, startPt, endPt, &filter, polys, &polyCount, MAX_POLYS);
    if (polyCount == 0) {
        out.detail = "no polygon path";
        return;
    }

    // A path whose last polygon is not the destination's is a *partial* one: Detour
    // got as close as the mesh allows. Saying "complete" there would have the caller
    // walk confidently to the wrong side of a wall.
    out.complete = polys[polyCount - 1] == endRef;
    float target[3];
    dtVcopy(target, endPt);
    if (!out.complete)
        query->closestPointOnPoly(polys[polyCount - 1], endPt, target, nullptr);

    unsigned char flags[MAX_POLYS];
    dtPolyRef refs[MAX_POLYS];
    query->findStraightPath(startPt, target, polys, polyCount,
                            out.straight, flags, refs, &out.count, MAX_POLYS);
}

int main(int argc, char** argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: jevpath <mmaps_dir> <map_id>\n");
        return 2;
    }
    const std::string dir = argv[1];
    const int mapId = atoi(argv[2]);

    char paramsName[512];
    snprintf(paramsName, sizeof(paramsName), "%s/%03i.mmap", dir.c_str(), mapId);
    FILE* pf = fopen(paramsName, "rb");
    if (!pf) { fprintf(stderr, "cannot open %s\n", paramsName); return 1; }
    dtNavMeshParams params;
    if (fread(&params, sizeof(params), 1, pf) != 1) {
        fprintf(stderr, "bad params in %s\n", paramsName);
        fclose(pf);
        return 1;
    }
    fclose(pf);

    dtNavMesh* mesh = dtAllocNavMesh();
    if (!mesh || dtStatusFailed(mesh->init(&params))) {
        fprintf(stderr, "navmesh init failed\n");
        return 1;
    }
    int loaded = 0;
    if (!loadTiles(mesh, dir, mapId, loaded)) {
        fprintf(stderr, "tile load failed\n");
        return 1;
    }

    costSlopes(mesh);

    dtNavMeshQuery* query = dtAllocNavMeshQuery();
    if (!query || dtStatusFailed(query->init(mesh, 65535))) {
        fprintf(stderr, "query init failed\n");
        return 1;
    }

    // Ready line first, so a caller can wait for it rather than guess how long half a
    // gigabyte of tiles takes to load.
    printf("{\"status\":\"ready\",\"map\":%d,\"tiles\":%d}\n", mapId, loaded);
    fflush(stdout);

    dtQueryFilter walkable;
    walkable.setIncludeFlags(NAV_GROUND | NAV_WATER);
    walkable.setExcludeFlags(NAV_GROUND_STEEP | NAV_MAGMA_SLIME);
    walkable.setAreaCost(NAV_AREA_WATER, WATER_COST);
    for (int k = 0; k < 2; ++k)
        walkable.setAreaCost(SLOPE_AREA[k], SLOPE_COST[k]);
    // Everything the mesh has, as before: for a character already standing on steep
    // ground, a destination on it, or a place only steep ground reaches. The old route
    // there beats no route.
    dtQueryFilter anything;
    anything.setIncludeFlags(0xffff);
    anything.setExcludeFlags(0);

    static Route player, any;
    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        float ax, ay, az, bx, by, bz;
        if (sscanf(line, "%f %f %f %f %f %f", &ax, &ay, &az, &bx, &by, &bz) != 6) {
            emitError("expected six floats: x1 y1 z1 x2 y2 z2");
            continue;
        }

        const float start[3] = {ay, az, ax};
        const float end[3] = {by, bz, bx};

        player = Route();
        plan(query, walkable, start, end, player);
        const Route* best = &player;
        const char* used = "player";
        if (!player.complete) {
            any = Route();
            plan(query, anything, start, end, any);
            if (any.complete || player.count == 0) {
                best = &any;
                used = "any";
            }
        }

        if (best->count == 0) {
            if (best->where[0])
                printf("{\"status\":\"nopath\",\"detail\":\"%s %s\",\"filter\":\"%s\","
                       "\"points\":[]}\n", best->where, best->detail, used);
            else
                printf("{\"status\":\"nopath\",\"detail\":\"%s\",\"filter\":\"%s\","
                       "\"points\":[]}\n", best->detail, used);
            fflush(stdout);
            continue;
        }

        printf("{\"status\":\"%s\",\"filter\":\"%s\",", best->complete ? "complete" : "partial",
               used);
        if (best == &any)
            printf("\"detail\":\"no route on ground a character can climb; steep ground allowed\",");
        printf("\"points\":[");
        for (int i = 0; i < best->count; ++i) {
            const float* p = &best->straight[i * 3];
            printf("%s[%.3f,%.3f,%.3f]", i ? "," : "", p[2], p[0], p[1]);
        }
        printf("]}\n");
        fflush(stdout);
    }

    dtFreeNavMeshQuery(query);
    dtFreeNavMesh(mesh);
    return 0;
}
