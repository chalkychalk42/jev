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

    dtNavMeshQuery* query = dtAllocNavMeshQuery();
    if (!query || dtStatusFailed(query->init(mesh, 65535))) {
        fprintf(stderr, "query init failed\n");
        return 1;
    }

    // Ready line first, so a caller can wait for it rather than guess how long half a
    // gigabyte of tiles takes to load.
    printf("{\"status\":\"ready\",\"map\":%d,\"tiles\":%d}\n", mapId, loaded);
    fflush(stdout);

    dtQueryFilter filter;
    filter.setIncludeFlags(0xffff);
    filter.setExcludeFlags(0);

    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        float ax, ay, az, bx, by, bz;
        if (sscanf(line, "%f %f %f %f %f %f", &ax, &ay, &az, &bx, &by, &bz) != 6) {
            emitError("expected six floats: x1 y1 z1 x2 y2 z2");
            continue;
        }

        const float start[3] = {ay, az, ax};
        const float end[3] = {by, bz, bx};
        const float extents[3] = {EXTENT_XZ, EXTENT_Y, EXTENT_XZ};

        dtPolyRef startRef = 0, endRef = 0;
        float startPt[3], endPt[3];
        query->findNearestPoly(start, extents, &filter, &startRef, startPt);
        query->findNearestPoly(end, extents, &filter, &endRef, endPt);
        if (!startRef || !endRef) {
            printf("{\"status\":\"nopath\",\"detail\":\"%s off mesh\",\"points\":[]}\n",
                   !startRef ? (!endRef ? "both ends" : "start") : "end");
            fflush(stdout);
            continue;
        }

        dtPolyRef polys[MAX_POLYS];
        int polyCount = 0;
        query->findPath(startRef, endRef, startPt, endPt, &filter, polys, &polyCount, MAX_POLYS);
        if (polyCount == 0) {
            printf("{\"status\":\"nopath\",\"detail\":\"no polygon path\",\"points\":[]}\n");
            fflush(stdout);
            continue;
        }

        // A path whose last polygon is not the destination's is a *partial* one: Detour
        // got as close as the mesh allows. Saying "complete" there would have the caller
        // walk confidently to the wrong side of a wall.
        const bool complete = polys[polyCount - 1] == endRef;
        float target[3];
        dtVcopy(target, endPt);
        if (!complete)
            query->closestPointOnPoly(polys[polyCount - 1], endPt, target, nullptr);

        float straight[MAX_POLYS * 3];
        unsigned char flags[MAX_POLYS];
        dtPolyRef refs[MAX_POLYS];
        int count = 0;
        query->findStraightPath(startPt, target, polys, polyCount,
                                straight, flags, refs, &count, MAX_POLYS);

        printf("{\"status\":\"%s\",\"points\":[", complete ? "complete" : "partial");
        for (int i = 0; i < count; ++i) {
            const float* p = &straight[i * 3];
            printf("%s[%.3f,%.3f,%.3f]", i ? "," : "", p[2], p[0], p[1]);
        }
        printf("]}\n");
        fflush(stdout);
    }

    dtFreeNavMeshQuery(query);
    dtFreeNavMesh(mesh);
    return 0;
}
