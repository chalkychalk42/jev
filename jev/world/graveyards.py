"""Where the server sends a ghost: its graveyards, each with a Spirit Healer (V301).

A ghost gets up at the Spirit Healer where it appeared (`Recover.run_spirit_healer`), and one
that never saw where it appeared - a session begun as a ghost, with none kept from the last -
had nowhere to go: two orcs who drowned off Ratchet before V301 kept their graveyard went to the
healer, as V301 has it, and were refused at once, "no graveyard seen", every run (the hive, 28
Sep 12:17-12:19). The world DB has the graveyards the server itself uses: the safe locations
(`world_world_safe_locs`) linked to the zones they serve, for a side or both
(`world_game_graveyard_zone`). The one nearest the ghost among those serving its zone is where
the server would have sent it, as it chose Ratchet's for the two orcs, their bodies in the
Barrens nearer Durotar's; and any Spirit Healer raises a ghost (CMaNGOS
`HandleSpiritHealerActivateOpcode`, which then sets it down by its body's graveyard).
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
from functools import cache
from pathlib import Path

WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"
# The factions `world_game_graveyard_zone` names a side by; 0 serves both.
TEAMS = {"alliance": 469, "horde": 67}
# `link_kind` of a graveyard linked to a zone (another kind links it to a whole map).
ZONE_LINK = 0


@cache
def _graveyards() -> tuple[tuple[int, float, float, float, int, int, int], ...]:
    """(map, x, y, z, zone or map it serves, link kind, faction) for every linked graveyard;
    none without the world DB."""
    from jev.play.world_knowledge import readonly_uri

    # The live session's Python opens it over the WSL share: `readonly_uri` keeps a UNC path
    # whole where `Path.as_uri` would hand SQLite its host as the URI's authority.
    uri = readonly_uri(WORLD_DB)
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            return tuple(db.execute(
                "select s.map, s.x, s.y, s.z, g.ghost_loc, g.link_kind, g.faction "
                "from world_game_graveyard_zone g join world_world_safe_locs s on s.id = g.id"))
    except sqlite3.Error:
        return ()


def nearest(map_id: int, x: float, y: float, *, side: str | None,
            zone: int | None = None) -> tuple[float, float, float] | None:
    """The graveyard nearest (x, y) on `map_id` that the server sends a ghost of `side` to, in
    world yards: among those serving `zone` when there are any, else any on the map. `None` for
    an unknown side or none known."""
    team = TEAMS.get(side or "")
    if team is None:
        return None
    rows = [r for r in _graveyards() if r[0] == map_id and r[6] in (0, team)]
    served = [r for r in rows if zone is not None and r[5] == ZONE_LINK and r[4] == zone]
    chosen = served or rows
    if not chosen:
        return None
    best = min(chosen, key=lambda r: math.dist(r[1:3], (x, y)))
    return best[1], best[2], best[3]
