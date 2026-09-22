"""Bounded retrieval over this installation's generated world facts.

This is a snapshot, not a web search and not an encyclopedia invented by the model.
Hashes travel with every result so a learned policy can be invalidated when content
changes. Spawn clusters describe possible locations; they do not observe a live unit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from jev.guide.graph import Graph

CONTENT = Path(__file__).resolve().parents[2] / "content" / "tbc"


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _ids(value: Any, category: str | None = None) -> set[int]:
    """Only identity fields can match an ID query; coordinates/counts are not IDs."""
    keys = {"quest_id", "npc_id", "target_id", "required_id", "spell", "item", "entry", "item_id"}
    if category == "quest":
        keys = {"quest_id"}
    elif category == "spell":
        keys = {"spell"}
    elif category == "npc":
        keys = {"npc_id", "target_id", "entry"}
    elif category == "item":
        keys = {"item", "item_id"}
        if isinstance(value, dict) and value.get("kind") in {"loot", "delivery"}:
            keys.add("required_id")
    found: set[int] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, int) and not isinstance(child, bool):
                found.add(child)
            elif key == "items" and isinstance(child, list) and category in {None, "item"}:
                found.update(item for item in child if isinstance(item, int))
            elif isinstance(child, (dict, list)):
                found.update(_ids(child, category))
    elif isinstance(value, list):
        for child in value:
            found.update(_ids(child, category))
    return found


class LocalKnowledge:
    """One immutable JSON snapshot of the guide, starting bars and merchant catalog.

    ``context(observation)`` is the cheap default retrieval; ``search(query)`` allows
    the tutor to ask about a different quest, creature, item, ability or merchant.
    Missing content remains explicitly unknown. No database or network side effects.
    """

    def __init__(self, guide: Graph | str | Path | None = None, *,
                 content_dir: str | Path = CONTENT, world_db: str | Path | None = None) -> None:
        directory = Path(content_dir)
        self.sources: list[dict[str, Any]] = []
        self.unknowns: list[str] = []
        self._records: list[dict[str, Any]] = []
        if isinstance(guide, Graph):
            graph_doc = guide.model_dump(mode="json")
            self.sources.append({"name": "guide", "sha256": _digest(graph_doc),
                                 "origin": "provided GuideGraph", "available": True})
        else:
            graph_doc = self._read(Path(guide) if guide else directory / "ally_human_1_12.json",
                                   "guide")
        if graph_doc is not None:
            # Unknown schema/dangling objectives must not quietly become model context.
            graph = Graph.model_validate(graph_doc)
            self._nodes = {node.id: node.model_dump(mode="json") for node in graph.nodes}
            self.graph_id: str | None = graph.graph_id
        else:
            self._nodes = {}
            self.graph_id = None
        self._profiles = self._read(directory / "combat-profiles.json", "combat_profiles") or {}
        self._vendors = self._read(directory / "vendor-catalog.json", "vendor_catalog") or {}
        if self._vendors and self._vendors.get("schema") != 1:
            raise ValueError("unsupported vendor catalog schema")
        self._world = None
        if world_db is not None:
            from jev.play.world_knowledge import WorldKnowledge

            self._world = WorldKnowledge(world_db)
            self.sources.append(self._world.source)
            if not self._world.available:
                self.unknowns.append("Exact-server world/DBC snapshot is unavailable.")
        self.fingerprint = _digest(self.sources)
        self._build_index()

    def _read(self, path: Path, name: str) -> dict[str, Any] | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            self.sources.append({"name": name, "sha256": None, "available": False})
            self.unknowns.append(f"{name} is unavailable")
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(f"{name} must contain a JSON object")
        self.sources.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest(),
                             "available": True})
        return result

    def _build_index(self) -> None:
        for node in self._nodes.values():
            self._records.append({"kind": "guide_node", "source": "guide", "data": node})
            for target in node["objective_targets"]:
                self._records.append({"kind": "objective_source", "source": "guide",
                                      "data": {"quest_id": node["quest_id"],
                                               "title": node["title"], **target}})
        # Deduplicate the same ability across race starting bars. Preserve all profiles
        # it belongs to rather than asserting a spell is on the live character's bar.
        abilities: dict[str, dict[str, Any]] = {}
        for profile_id, profile in sorted(self._profiles.items()):
            self._records.append({"kind": "starting_profile", "source": "combat_profiles",
                                  "data": {"profile_id": profile_id, **profile}})
            for row in profile.get("rows", ()):
                key = _digest(row)
                record = abilities.setdefault(key, {"kind": "ability", "source": "combat_profiles",
                                                     "data": {**row, "profiles": []}})
                record["data"]["profiles"].append(profile_id)
        self._records.extend(abilities.values())
        for vendor in self._vendors.get("vendors", ()):
            self._records.append({"kind": "merchant", "source": "vendor_catalog",
                                  "data": vendor})
        self._search_text = [json.dumps(record["data"], sort_keys=True).casefold()
                             for record in self._records]

    def _envelope(self) -> dict[str, Any]:
        return {"fingerprint": self.fingerprint, "sources": self.sources,
                "scope": ("local guide plus bounded exact-server world/DBC entity retrieval"
                          if self._world and self._world.available else
                          "local generated TBC server content; not the entire game database"),
                "unknowns": [*self.unknowns,
                             "Generated starting bars do not prove the current action-bar mapping.",
                             "Spawn locations do not establish current visibility, facing or range.",
                             "Unlisted spell ranges, cast times and loot probabilities are unknown.",
                             "Vendor stock, current prices and successful transactions require observation."]}

    def context(self, observation: dict[str, Any], *, vendor_limit: int = 4) -> dict[str, Any]:
        if not 0 <= vendor_limit <= 12:
            raise ValueError("vendor_limit must be between 0 and 12")
        result = self._envelope()
        context = observation.get("context") or {}
        values = observation.get("values") or {}
        state = observation.get("state") or {}
        node = self._nodes.get(context.get("step_id"))
        result["current_node"] = node
        result["next_nodes"] = [self._nodes[name] for name in node["next"][:3]] if node else []
        result["objective_sources"] = node["objective_targets"] if node else []
        if node is None:
            result["unknowns"].append("Current guide node is not in the content snapshot.")
        race, cls = values.get("char.race_id"), values.get("char.class_id")
        profile_id = f"{race}:{cls}"
        profile = self._profiles.get(profile_id)
        result["starting_profile"] = {"profile_id": profile_id, **profile} if profile else None
        result["supplies"] = self._vendors.get("supplies", {}).get(profile_id)
        if profile is None:
            result["unknowns"].append("No exact observed race/class starting profile; no fallback assumed.")
        map_id = context.get("map_id")
        if map_id is None and node:
            map_id = node.get("map_id")
        world = (state.get("pos") or {}).get("world") or context.get("world")
        merchants = [dict(v) for v in self._vendors.get("vendors", ()) if v["map_id"] == map_id]
        if isinstance(world, (list, tuple)) and len(world) >= 2 and all(
                isinstance(n, (int, float)) and math.isfinite(n) for n in world[:2]):
            for merchant in merchants:
                merchant["distance_yards_from_observed_position"] = math.hypot(
                    merchant["world"][0] - world[0], merchant["world"][1] - world[1])
            merchants.sort(key=lambda m: m["distance_yards_from_observed_position"])
        result["merchants"] = merchants[:vendor_limit]
        result["merchants_omitted"] = max(0, len(merchants) - vendor_limit)
        result["lookup"] = {"available": True, "query_limit_chars": 240,
                            "world_database_available": bool(self._world and self._world.available),
                            "examples": ["npc Young Wolf", "quest 33", "spell Holy Light", "item 159",
                                         "vendor Andrew Krighton", "object Copper Vein"]}
        # Return detached JSON; callers cannot mutate the pinned snapshot through context.
        return json.loads(json.dumps(result, allow_nan=False))

    def search(self, query: str, *, limit: int = 6) -> dict[str, Any]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 240:
            raise ValueError("lookup query must contain 1..240 characters")
        if not 1 <= limit <= 12:
            raise ValueError("lookup limit must be between 1 and 12")
        terms = re.findall(r"[\w]+", query.casefold())
        ignored = {"the", "a", "an", "about", "find", "for", "quest", "item", "spell", "npc"}
        terms = [word for word in terms if word not in ignored]
        id_query = re.fullmatch(r"\s*(quest|item|spell|npc)\s+(\d+)\s*", query.casefold())
        category = id_query.group(1) if id_query else None
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, (record, text) in enumerate(zip(self._records, self._search_text, strict=True)):
            matches = [int(word) in _ids(record["data"], category)
                       if word.isdigit() else word in text for word in terms]
            if terms and all(matches):
                exact_name = str(record["data"].get("name", record["data"].get("target_name", "")))
                score = 10 if exact_name.casefold() == query.casefold().strip() else 0
                if category in {"spell", "item"} and record["kind"] == "ability":
                    score += 5
                scored.append((score, -index, record))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        result = self._envelope()
        result.update(query=query, records=[row[2] for row in scored[:limit]],
                      matches=len(scored), omitted=max(0, len(scored) - limit))
        if self._world is not None:
            world = self._world.search(query, limit=limit)
            result["world_lookup"] = world
        if not scored and not result.get("world_lookup", {}).get("records"):
            result["unknowns"].append("No matching fact in this content snapshot; do not invent one.")
        return json.loads(json.dumps(result, allow_nan=False))
