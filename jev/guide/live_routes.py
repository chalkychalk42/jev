"""Every race and class's route, a live guide (V412).

The hive plays a route for each of the 52 race and class pairs (JevHive `hive.routes`, built
by `hive.convert` from the Guidelime routes players wrote, on this server's world database):
`<race>_<class>.json` and its spawns beside it. They are their authors' and stay on this
machine, as `data/` does: `tools/install_routes.py` copies them into `data/routes/`, where the
live runner finds them (`jev.run.cli --route NAME`, or `--route auto` for the logged-in
character's race and class). Each is one guide from level 1 to 20: no guide follows it
(`NEXT_GUIDE`), so it is outgrown at its own end (V295), and its map frame is its race's first
land (`Graph.coord_zone_id`), every step on that land's continent.

What a route needs that the live client may lack is read from where its steps are
(`readiness`): another continent (a boat or a zeppelin, which Jev takes none of), Thunder
Bluff's mesas (islands on the navmesh: a lift, which the keys ride only by the platform's times
(V407), a flight, the tauren's from their creation, or the hearthstone), Darkshore for a night
elf (no walk from Teldrassil: the flight Rut'theran Village to Auberdine, known from the
creation and taken where no walk finishes, V408), Darnassus (its portal, walked through, V305).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "data" / "routes"
# Where the hive keeps them, on this machine; `JEV_ROUTES` names another place.
HIVE_ROUTES = Path("/home/ash/JevHive/content/routes")

RACES = {1: "human", 2: "orc", 3: "dwarf", 4: "nightelf", 5: "undead", 6: "tauren",
         7: "gnome", 8: "troll", 10: "bloodelf", 11: "draenei"}
CLASSES = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 7: "shaman",
           8: "mage", 9: "warlock", 11: "druid"}
# The pairs a character can be created as on 2.4.3 (`hive.routes.PAIRS`).
PAIRS = {"human": ("warrior", "paladin", "rogue", "priest", "mage", "warlock"),
         "dwarf": ("warrior", "paladin", "hunter", "rogue", "priest"),
         "nightelf": ("warrior", "hunter", "rogue", "priest", "druid"),
         "gnome": ("warrior", "rogue", "mage", "warlock"),
         "draenei": ("warrior", "paladin", "hunter", "priest", "shaman", "mage"),
         "orc": ("warrior", "hunter", "rogue", "shaman", "warlock"),
         "undead": ("warrior", "rogue", "priest", "mage", "warlock"),
         "tauren": ("warrior", "hunter", "shaman", "druid"),
         "troll": ("warrior", "hunter", "rogue", "priest", "shaman", "mage"),
         "bloodelf": ("paladin", "hunter", "rogue", "priest", "mage", "warlock")}
NAMES = tuple(f"{race}_{cls}" for race, classes in PAIRS.items() for cls in classes)

# Thunder Bluff's mesas: the city's steps over this height stand on islands of the navmesh.
MESA_ABOVE = 100.0


def _step_kinds():
    from jev.world.state_v1 import StepKind

    return frozenset({StepKind.QUEST_ACCEPT, StepKind.QUEST_TURNIN, StepKind.QUEST_OBJECTIVE,
                      StepKind.GRIND, StepKind.DING_GATE})


# The steps a route walks to, not the services it lists (vendors, repairers, flight masters,
# trainers, inns), which the body chooses among by their walks.
STEP_KINDS = _step_kinds()


def route_name(race_id: int | None, class_id: int | None) -> str | None:
    """The route of a race and class by the strip's ids, or `None` for a pair with none."""
    race, cls = RACES.get(race_id), CLASSES.get(class_id)
    name = f"{race}_{cls}" if race and cls else None
    return name if name in NAMES else None


def search_path() -> tuple[Path, ...]:
    extra = os.environ.get("JEV_ROUTES")
    return tuple(p for p in ((Path(extra),) if extra else ()) + (ROUTES, HIVE_ROUTES))


def find(name: str, dirs: tuple[Path, ...] | None = None) -> Path | None:
    """`<name>.json` in the first place that has it (`search_path`), or `None`."""
    for d in dirs or search_path():
        path = Path(d) / f"{name}.json"
        if path.is_file():
            return path
    return None


@dataclass
class Readiness:
    """What one route is to the live runner."""

    name: str
    graph_id: str = ""
    steps: int = 0
    supported_steps: int = 0
    excluded_quests: int = 0
    frame: str | None = None              # its map frame's zone, resolved
    maps: tuple[int, ...] = ()
    zones: tuple[str, ...] = ()
    needs: dict[str, int] = field(default_factory=dict)   # what the live client may lack: steps
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def readiness(name: str, path: Path, *, world_db: Path | None = None) -> Readiness:
    """Load `path` as the live runner does, compile it to its supported route, resolve its
    map frame and each step's place in it, and say what its steps need."""
    from jev.guide.coords import bounds_by_radio_id, navigation_frame
    from jev.guide.graph import Graph
    from jev.guide.route import compile_route
    from jev.run.body import LiveBody
    from jev.run.cli import outgrown_at, worthless_quests

    out = Readiness(name)
    try:
        graph = Graph.load(path)
    except (OSError, ValueError) as exc:
        out.problems.append(f"does not load: {exc}")
        return out
    out.graph_id, out.steps = graph.graph_id, len(graph.nodes)
    route = compile_route(graph, available_skills=LiveBody.available,
                          worthless=worthless_quests(world_db, graph))
    out.supported_steps = len(route.graph.nodes)
    out.excluded_quests = len(route.excluded)
    if out.supported_steps == 0:
        out.problems.append("its supported route is empty")
    if outgrown_at(graph.graph_id) is not None:
        out.problems.append("a guide follows it (NEXT_GUIDE): a route is one guide to 20")
    zones = bounds_by_radio_id(str(ROOT / "data" / "zones-tbc-243.json"))
    frame = next((z for z in zones.values() if z.area_id == graph.coord_zone_id), None)
    if frame is None or frame.degenerate:
        out.problems.append(f"no map frame for zone {graph.coord_zone_id}")
        return out
    names = {z.area_id: n for n, z in _named(zones).items()}
    out.frame = names.get(frame.area_id, str(frame.area_id))
    by_area = {z.area_id: (rid, z) for rid, z in zones.items()}
    placed = [n for n in route.graph.nodes if n.world is not None]
    out.maps = tuple(sorted({n.map_id for n in placed if n.map_id is not None}))
    out.zones = tuple(sorted({n.zone for n in placed if n.zone}))
    for node in placed:
        if node.map_id != frame.map_id:
            out.needs["boat or zeppelin"] = out.needs.get("boat or zeppelin", 0) + 1
            continue
        # A step's own zone, as a character standing there reads it, is pinned to the frame.
        entry = by_area.get(node.zone_id)
        if entry is not None and navigation_frame(graph.coord_zone_id, entry[0], zones) is None:
            out.problems.append(f"{node.id}: zone {node.zone_id} not in the frame's map")
        need = _need(name, node) if node.kind in STEP_KINDS else None
        if need:
            out.needs[need] = out.needs.get(need, 0) + 1
    return out


def _named(zones) -> dict:
    import json

    raw = json.loads((ROOT / "data" / "zones-tbc-243.json").read_text(encoding="utf-8"))
    by_area = {z.area_id: z for z in zones.values()}
    return {e["name"]: by_area[e["area_id"]] for e in raw["zones"] if e["area_id"] in by_area}


def _need(name: str, node) -> str | None:
    """What a step needs that the live client may lack, by where it stands - not by the zone
    its quest is filed under: Cairne Bloodhoof on his mesa gives a quest of the Barrens."""
    x, y, z = node.world
    if _inside("ThunderBluff", node.map_id, x, y) and z > MESA_ABOVE:
        return "Thunder Bluff's mesas (a lift by its times, a flight, or the hearthstone)"
    if _inside("Moonglade", node.map_id, x, y):
        return "Moonglade (a druid's Teleport: Moonglade, which Jev does not cast)"
    if (name.startswith("nightelf_") and _inside("Darkshore", node.map_id, x, y)
            and not _inside("Teldrassil", node.map_id, x, y)):
        return "Darkshore (the flight from Rut'theran, known from the creation)"
    if _inside("Darnassis", node.map_id, x, y):
        return "Darnassus (its portal, walked through)"
    return None


def _inside(zone: str, map_id: int | None, x: float, y: float) -> bool:
    """Whether a world point lies on the zone map's box (`data/zones-tbc-243.json`)."""
    box = _boxes().get(zone)
    if box is None or box[0] != map_id:
        return False
    _, left, right, top, bottom = box
    return min(left, right) <= y <= max(left, right) and min(top, bottom) <= x <= max(top, bottom)


def _boxes() -> dict[str, tuple]:
    import json
    from functools import lru_cache

    @lru_cache(maxsize=1)
    def load():
        raw = json.loads((ROOT / "data" / "zones-tbc-243.json").read_text(encoding="utf-8"))
        return {e["name"]: (e["map_id"], e["left"], e["right"], e["top"], e["bottom"])
                for e in raw["zones"]}

    return load()
