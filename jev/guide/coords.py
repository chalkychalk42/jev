"""World yards <-> zone map fractions, from the client's own `WorldMapArea.dbc`.

Two coordinate systems meet here and they do not agree about anything — not the axes, not
the direction, not the units:

  * **World** — yards. `+X` is north, `+Y` is west. This is what the server's creature
    spawns are stored in, and what a navmesh path is computed in.
  * **Zone map** — fractions of the current zone's map image, `0..1`, origin top-left.
    This is all `GetPlayerMapPosition` can return, so it is all the addon can paint.

The conversion is the zone's bounding box, and the axis swap is the part that catches
people: the map's **horizontal** axis comes from world **Y**, and the map's **vertical**
axis comes from world **X**.

Checked against a known landmark rather than asserted. Goldshire's inn sits near map
(42, 65) in Elwynn Forest, and the transform below puts world (-9460, 60) at
(0.425, 0.657). `tests/test_coords.py` pins that, so a future change to the table or the
sign convention fails loudly instead of quietly moving every node in the graph.

`WorldMapArea.dbc` stores its bounds as IEEE-754 floats; the mirror keeps them as the raw
uint32 bit patterns, so they are reinterpreted rather than cast.
"""

from __future__ import annotations

import pathlib
import sqlite3
import struct
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ZoneBounds:
    """One zone's map, as a world-space box."""

    area_id: int
    map_id: int
    left: float     # world Y at the map's left edge
    right: float    # world Y at the map's right edge
    top: float      # world X at the map's top edge
    bottom: float   # world X at the map's bottom edge

    @property
    def degenerate(self) -> bool:
        """Some rows have zero extent — continent maps and unused entries. They cannot
        convert, and silently returning 0.5 for them would put nodes in the sea."""
        return abs(self.left - self.right) < 1e-3 or abs(self.top - self.bottom) < 1e-3


def _as_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0]


@lru_cache(maxsize=4)
def load_bounds(db_path: str) -> dict[int, ZoneBounds]:
    """All zone bounds, keyed by area id.

    Where an area appears more than once the first row wins: duplicates are continent-
    level or phased variants, and the first is the one the client uses for a player
    standing in that area.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select c1, c2, c4, c5, c6, c7 from dbc_WorldMapArea order by id"
        ).fetchall()
    finally:
        con.close()

    out: dict[int, ZoneBounds] = {}
    for map_id, area_id, left, right, top, bottom in rows:
        if area_id in out:
            continue
        out[area_id] = ZoneBounds(
            area_id=area_id,
            map_id=map_id,
            left=_as_float(left),
            right=_as_float(right),
            top=_as_float(top),
            bottom=_as_float(bottom),
        )
    return out


def world_to_map(x: float, y: float, bounds: ZoneBounds) -> tuple[float, float] | None:
    """World yards -> `(mx, my)` in 0..1. `None` when the zone has no usable box.

    Returns the fraction even when it falls outside 0..1: a spawn just over a zone border
    is a real thing, and clamping it would silently move the node. Callers that need an
    on-map guarantee check the range themselves.
    """
    if bounds.degenerate:
        return None
    mx = (bounds.left - y) / (bounds.left - bounds.right)
    my = (bounds.top - x) / (bounds.top - bounds.bottom)
    return mx, my


def map_to_world(mx: float, my: float, bounds: ZoneBounds) -> tuple[float, float] | None:
    """`(mx, my)` in 0..1 -> world yards. The inverse of `world_to_map`."""
    if bounds.degenerate:
        return None
    y = bounds.left - mx * (bounds.left - bounds.right)
    x = bounds.top - my * (bounds.top - bounds.bottom)
    return x, y


@lru_cache(maxsize=2)
def bounds_by_radio_id(zones_json: str) -> dict[int, ZoneBounds]:
    """Zone bounds keyed by the id the radio actually paints.

    The addon cannot send an area id — 2.4.3 gives it `GetMapInfo()`, a map *file* name —
    so it sends a 16-bit hash of that name and the decoder inverts it here. Verified
    collision-free across all 68 zone maps in the table, and asserted rather than assumed
    because a collision would silently put a character in the wrong zone's coordinate
    frame and every distance after that would be wrong by a constant nobody could see.
    """
    import json

    from jev.perceive.radio_frame import zone_id

    entries = json.loads(pathlib.Path(zones_json).read_text(encoding="utf-8"))["zones"]
    out: dict[int, ZoneBounds] = {}
    for e in entries:
        key = zone_id(e["name"])
        if key in out:
            raise ValueError(f"zone hash collision: {e['name']} and {out[key].area_id}")
        out[key] = ZoneBounds(area_id=e["area_id"], map_id=e["map_id"],
                              left=e["left"], right=e["right"],
                              top=e["top"], bottom=e["bottom"])
    return out


def to_yards(dmx: float, dmy: float, bounds: ZoneBounds) -> tuple[float, float]:
    """A map-space delta in yards, along the map's own axes.

    **Map space is not isotropic and this is the correction for it.** Elwynn's map box is
    3,470.8 yards wide against 2,314.6 tall — a 1.5:1 stretch — so `atan2(dmy, dmx)` on
    raw fractions is wrong by up to 11 degrees near the diagonal, and a turn computed from
    that angle is wrong by the same amount. Measured, not assumed: an early turn-rate
    reading taken in map space was contaminated exactly this way.

    Returned as (along-mx, along-my) in yards, which is a proper Euclidean frame. It is
    still the *map's* orientation rather than the world's, and that is fine: the target is
    a map position and the reading is a map position, so nothing in the loop needs world
    axes.
    """
    return (dmx * abs(bounds.left - bounds.right),
            dmy * abs(bounds.top - bounds.bottom))


def heading_yards(a: tuple[float, float], b: tuple[float, float],
                  bounds: ZoneBounds) -> float | None:
    """True bearing from `a` to `b`, radians, or `None` if they are too close to mean it."""
    import math

    dx, dy = to_yards(b[0] - a[0], b[1] - a[1], bounds)
    if math.hypot(dx, dy) < 0.5:        # half a yard is inside the readout's own noise
        return None
    return math.atan2(dy, dx)


def distance_yards(a: tuple[float, float], b: tuple[float, float],
                   bounds: ZoneBounds) -> float:
    import math

    dx, dy = to_yards(b[0] - a[0], b[1] - a[1], bounds)
    return math.hypot(dx, dy)


def on_map(mx: float, my: float, slack: float = 0.02) -> bool:
    """Is this fraction actually inside the zone's map, allowing for border spawns?"""
    return -slack <= mx <= 1 + slack and -slack <= my <= 1 + slack
