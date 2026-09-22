"""Read-only, bounded retrieval from the existing exact-server world/DBC snapshot.

These are template and spawn facts, never live positions or observations. The tutor
supplies an entity name/ID, never SQL. The game process is not read or contacted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

DEFAULT_WORLD_DB = Path(__file__).resolve().parents[2] / "data/knowledge/tbc-243.sqlite"

_ENTITIES = {
    "quest": ("world_quest_template", "entry", "Title", (
        "entry", "Title", "MinLevel", "MaxLevel", "QuestLevel", "RequiredClasses", "RequiredRaces",
        "PrevQuestId", "NextQuestId", "Objectives", "SrcItemId", "SrcItemCount",
        *(f"{key}{i}" for key in ("ReqItemId", "ReqItemCount", "ReqCreatureOrGOId",
                                   "ReqCreatureOrGOCount", "ReqSpellCast") for i in range(1, 5)))),
    "npc": ("world_creature_template", "Entry", "Name", (
        "Entry", "Name", "SubName", "MinLevel", "MaxLevel", "Faction", "NpcFlags", "Rank",
        "SpeedWalk", "SpeedRun", "MeleeBaseAttackTime", "LootId", "MovementType",
        "TrainerType", "TrainerClass", "TrainerTemplateId", "VendorTemplateId")),
    "item": ("world_item_template", "entry", "name", (
        "entry", "name", "class", "subclass", "Quality", "BuyCount", "BuyPrice", "SellPrice",
        "RequiredLevel", "RequiredSkill", "stackable", "ContainerSlots", "InventoryType",
        "AllowableClass", "AllowableRace", "delay", "armor", "startquest", "description",
        *(f"{key}{i}" for key in ("spellid_", "spelltrigger_", "spellcooldown_") for i in range(1, 6)))),
    "spell": ("world_spell_template", "Id", "SpellName", (
        "Id", "SpellName", "Rank1", "SpellLevel", "BaseLevel", "CastingTimeIndex", "RangeIndex",
        "DurationIndex", "ManaCost", "ManaCostPercentage", "PowerType", "RecoveryTime",
        "StartRecoveryTime", "SchoolMask", "InterruptFlags", "FacingCasterFlags",
        *(f"{key}{i}" for key in ("Effect", "EffectBasePoints", "EffectApplyAuraName",
                                   "EffectTriggerSpell") for i in range(1, 4)))),
    "object": ("world_gameobject_template", "entry", "name", (
        "entry", "name", "type", "faction", "flags", *(f"data{i}" for i in range(24)))),
}
_ALIASES = {"creature": "npc", "gameobject": "object", "ability": "spell"}


def readonly_uri(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    # SQLite treats //host as URI authority; four slashes retain a Windows UNC path.
    return "file:" + ("//" if value.startswith("//") else "") + quote(value, safe="/:") + "?mode=ro&immutable=1"


class WorldKnowledge:
    def __init__(self, path: Path | str = DEFAULT_WORLD_DB):
        self.path = Path(path)
        self.available = self.path.is_file()
        self.sha256 = None
        self._identity = None
        self.tables = set()
        self.metadata = {}
        if self.available:
            self._identity = self._stat()
            digest = hashlib.sha256()
            with self.path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            self.sha256 = digest.hexdigest()
            if self._stat() != self._identity:
                raise ValueError("world knowledge changed while its snapshot was identified")
            with self._connect() as db:
                self.tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "metadata" not in self.tables:
                    raise ValueError("world knowledge has no snapshot provenance")
                raw = dict(db.execute("SELECT key,value FROM metadata"))
                self.metadata = {key: json.loads(raw[key]) for key in (
                    "schema_version", "ruleset", "complete", "pack_id", "sources", "scope") if key in raw}
                if (self.metadata.get("schema_version") != 1
                        or self.metadata.get("ruleset") != "tbc-2.4.3-8606"
                        or self.metadata.get("complete") is not True):
                    raise ValueError("world knowledge must be a complete supported TBC 2.4.3 snapshot")

    def _stat(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    @contextmanager
    def _connect(self):
        if not self.available or self._stat() != self._identity:
            raise ValueError("world knowledge snapshot is missing or changed; rebuild context")
        db = sqlite3.connect(readonly_uri(self.path), uri=True, timeout=1)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
            yield db
        finally:
            db.close()

    @property
    def source(self):
        return {"name": "world_database", "available": self.available, "sha256": self.sha256,
                "origin": "local world/DBC snapshot, read-only", "live_observation": False,
                "metadata": json.loads(json.dumps(self.metadata)),
                "entity_categories": [kind for kind, spec in _ENTITIES.items() if spec[0] in self.tables],
                "coverage": "Bounded named entity, spawn, quest, loot, vendor, trainer and spell mechanics lookups; no arbitrary tables or live character data."}

    def _spawns(self, db, kind, entry, limit):
        table = "world_creature" if kind == "npc" else "world_gameobject"
        random = "world_creature_spawn_entry" if kind == "npc" else "world_gameobject_spawn_entry"
        if table not in self.tables:
            return [], False
        columns = "c.guid,c.map,c.position_x,c.position_y,c.position_z"
        statements = [f'SELECT {columns},c.id AS template_id,\'direct\' AS source FROM "{table}" c WHERE c.id=?']
        args = [entry]
        if random in self.tables:
            statements.append(f'SELECT {columns},e.entry AS template_id,\'random_entry\' AS source '
                              f'FROM "{table}" c JOIN "{random}" e ON e.guid=c.guid WHERE e.entry=?')
            args.append(entry)
        if {"world_spawn_group", "world_spawn_group_entry", "world_spawn_group_spawn"} <= self.tables:
            statements.append(f'SELECT {columns},e.Entry AS template_id,\'spawn_group\' AS source '
                              f'FROM "{table}" c JOIN world_spawn_group_spawn s ON s.Guid=c.guid '
                              'JOIN world_spawn_group_entry e ON e.Id=s.Id '
                              'JOIN world_spawn_group g ON g.Id=s.Id WHERE e.Entry=? AND g.Type=?')
            args.extend((entry, 0 if kind == "npc" else 1))
        rows = self._rows(db, " UNION ".join(statements) + " ORDER BY guid,source LIMIT ?",
                          (*args, limit + 1))
        return rows[:limit], len(rows) > limit

    def _reference_sources(self, db, item, *, node_limit=32, depth_limit=4):
        """Find parent reference IDs, preserving raw conditions instead of invented chances."""
        table = "world_reference_loot_template"
        if table not in self.tables:
            return [], False
        rows = self._rows(db, f'SELECT DISTINCT entry FROM "{table}" WHERE item=? '
                               'AND mincountOrRef>=0 ORDER BY entry LIMIT ?', (item, node_limit + 1))
        pending = [(row["entry"], 0) for row in rows[:node_limit]]
        found, truncated = {}, len(rows) > node_limit
        while pending and len(found) < node_limit:
            entry, depth = pending.pop(0)
            if entry in found:
                continue
            found[entry] = depth
            parents = self._rows(db, f'SELECT DISTINCT entry FROM "{table}" WHERE mincountOrRef=? '
                                      'ORDER BY entry LIMIT ?', (-entry, node_limit + 1))
            truncated |= len(parents) > node_limit
            if depth >= depth_limit:
                truncated |= bool(parents)
            else:
                pending.extend((row["entry"], depth + 1) for row in parents[:node_limit]
                               if row["entry"] not in found)
        return sorted(found), bool(truncated or pending)

    @staticmethod
    def _rows(db, query, args=()):
        return [dict(row) for row in db.execute(query, args)]

    def _related(self, db, kind, row, *, limit):
        related = {"truncated": [],
                   "coverage": "At most the requested limit per relation; template conditions and reference chances are raw facts, not live availability."}
        entry = row.get("entry", row.get("Entry", row.get("Id")))

        def add(key, sql, args=()):
            rows = self._rows(db, sql + " LIMIT ?", (*args, limit + 1))
            related[key] = rows[:limit]
            if len(rows) > limit:
                related["truncated"].append(key)

        if "entities" in self.tables:
            rows = self._rows(db, "SELECT name,rank,details FROM entities WHERE kind=? AND id=? LIMIT 1",
                              (kind, entry))
            if rows:
                related["indexed_description"] = {**rows[0], "details": json.loads(rows[0]["details"])}

        if kind in {"npc", "object"}:
            spawns, truncated = self._spawns(db, kind, entry, limit)
            related["possible_spawns"] = spawns
            if truncated:
                related["truncated"].append("possible_spawns")
        if kind == "npc":
            for key, table, identity in (
                    ("loot", "world_creature_loot_template", row.get("LootId")),
                    ("vendor", "world_npc_vendor", entry),
                    ("vendor_template", "world_npc_vendor_template", row.get("VendorTemplateId")),
                    ("trainer", "world_npc_trainer", entry),
                    ("trainer_template", "world_npc_trainer_template", row.get("TrainerTemplateId"))):
                if identity and table in self.tables:
                    add(key, f'SELECT * FROM "{table}" WHERE entry=? ORDER BY rowid', (identity,))
            if "world_reference_loot_template" in self.tables:
                pending = [(-r["mincountOrRef"], 0) for r in related.get("loot", ())
                           if r.get("mincountOrRef", 0) < 0]
                visited, references = set(), []
                while pending and len(visited) < limit:
                    identity, depth = pending.pop(0)
                    if identity in visited:
                        continue
                    visited.add(identity)
                    rows = self._rows(db, "SELECT * FROM world_reference_loot_template "
                                         "WHERE entry=? ORDER BY rowid LIMIT ?", (identity, limit + 1))
                    references.append({"entry": identity, "depth": depth, "rows": rows[:limit]})
                    if len(rows) > limit:
                        related["truncated"].append("reference_loot_rows")
                    children = [-r["mincountOrRef"] for r in rows[:limit]
                                if r.get("mincountOrRef", 0) < 0 and -r["mincountOrRef"] not in visited]
                    if depth >= 4 and children:
                        related["truncated"].append("reference_loot_depth")
                    elif depth < 4:
                        pending.extend((child, depth + 1) for child in children)
                related["reference_loot"] = references
                if pending:
                    related["truncated"].append("reference_loot")
        if kind == "quest":
            for prefix, label in (("creature", "npc"), ("gameobject", "object")):
                for suffix, key in (("questrelation", "givers"), ("involvedrelation", "turn_in")):
                    table = f"world_{prefix}_{suffix}"
                    if table in self.tables:
                        # Exact server schema is id,quest; entry belongs to template tables.
                        add(f"{label}_{key}", f'SELECT id FROM "{table}" WHERE quest=? ORDER BY id', (entry,))
        if kind == "item":
            if {"world_creature_loot_template", "world_creature_template"} <= self.tables:
                add("direct_loot_sources",
                    "SELECT c.Entry,c.Name,l.ChanceOrQuestChance,l.mincountOrRef,l.maxcount "
                    "FROM world_creature_loot_template l JOIN world_creature_template c ON c.LootId=l.entry "
                    "WHERE l.item=? AND l.mincountOrRef>=0 ORDER BY c.Entry", (entry,))
                references, truncated = self._reference_sources(db, entry)
                if references:
                    placeholders = ",".join("?" for _ in references)
                    add("reference_loot_sources",
                        "SELECT c.Entry,c.Name,-l.mincountOrRef AS reference_entry,l.ChanceOrQuestChance "
                        "FROM world_creature_loot_template l JOIN world_creature_template c ON c.LootId=l.entry "
                        f"WHERE -l.mincountOrRef IN ({placeholders}) ORDER BY c.Entry", references)
                if truncated:
                    related["truncated"].append("reference_loot_walk")
            if {"world_gameobject_loot_template", "world_gameobject_template"} <= self.tables:
                add("direct_object_loot_sources",
                    "SELECT g.entry,g.name,l.ChanceOrQuestChance,l.mincountOrRef,l.maxcount "
                    "FROM world_gameobject_loot_template l JOIN world_gameobject_template g "
                    "ON g.data1=l.entry AND g.type=3 WHERE l.item=? AND l.mincountOrRef>=0 ORDER BY g.entry",
                    (entry,))
            if "world_npc_vendor" in self.tables:
                add("direct_vendors", "SELECT entry,item,maxcount,incrtime FROM world_npc_vendor "
                                      "WHERE item=? ORDER BY entry", (entry,))
            if {"world_npc_vendor_template", "world_creature_template"} <= self.tables:
                add("template_vendors",
                    "SELECT c.Entry,c.Name,v.item,v.maxcount,v.incrtime,v.entry AS vendor_template "
                    "FROM world_npc_vendor_template v JOIN world_creature_template c "
                    "ON c.VendorTemplateId=v.entry WHERE v.item=? ORDER BY c.Entry", (entry,))
        if kind == "spell":
            if "world_npc_trainer" in self.tables:
                add("direct_trainers", "SELECT * FROM world_npc_trainer WHERE spell=? ORDER BY entry", (entry,))
            if {"world_npc_trainer_template", "world_creature_template"} <= self.tables:
                add("template_trainers", "SELECT c.Entry,c.Name,t.spell,t.spellcost,t.reqlevel,"
                    "t.entry AS trainer_template FROM world_npc_trainer_template t "
                    "JOIN world_creature_template c ON c.TrainerTemplateId=t.entry "
                    "WHERE t.spell=? ORDER BY c.Entry", (entry,))
            for key, table, index in (("cast_time", "dbc_SpellCastTimes", row.get("CastingTimeIndex")),
                                      ("duration", "dbc_SpellDuration", row.get("DurationIndex")),
                                      ("range", "dbc_SpellRange", row.get("RangeIndex"))):
                if index and table in self.tables:
                    rows = self._rows(db, f'SELECT * FROM "{table}" WHERE id=? LIMIT 1', (index,))
                    if rows:
                        raw = rows[0]
                        related[key + "_dbc"] = raw
                        if key == "cast_time":
                            related["base_cast_ms"] = raw.get("c1")
                        if key == "duration":
                            value = raw.get("c1")
                            # DBC stores unsigned cells; duration -1 means indefinite.
                            related["base_duration_ms"] = (value - 2**32 if type(value) is int
                                                          and 2**31 <= value < 2**32 else value)
                        if key == "range":
                            for name, column in (("min_range_yards", "c1"), ("max_range_yards", "c2")):
                                value = raw.get(column)
                                if type(value) is int and 0 <= value < 2**32:
                                    decoded = struct.unpack("<f", struct.pack("<I", value))[0]
                                    if math.isfinite(decoded):
                                        related[name] = decoded
        related["truncated"] = sorted(set(related["truncated"]))
        return related

    def search(self, query: str, *, limit: int = 6) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 240:
            raise ValueError("world lookup requires a short entity name or ID")
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError("world lookup limit must be 1..12")
        result = {"source": self.source, "query": query, "records": [], "truncated": False,
                  "unknowns": ["Template/spawn facts do not prove a unit is currently present or a skill is learned."]}
        if not self.available:
            result["unknowns"].append("Local world/DBC snapshot is unavailable.")
            return result
        prefix, _, rest = query.strip().partition(" ")
        category = _ALIASES.get(prefix.casefold(), prefix.casefold())
        special = category if category in {"vendor", "trainer", "loot"} else None
        if special:
            category = "item" if special == "loot" else "npc"
        kinds = (category,) if category in _ENTITIES and rest else tuple(_ENTITIES)
        needle = rest.strip() if len(kinds) == 1 else query.strip()
        numeric = int(needle) if re.fullmatch(r"[0-9]{1,10}", needle) else None
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._connect() as db:
            for index, kind in enumerate(kinds):
                table, identity, name, columns = _ENTITIES[kind]
                if table not in self.tables:
                    continue
                available = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                selected = [column for column in columns if column in available]
                if not {identity, name} <= available:
                    continue
                where, value = (f'"{identity}"=?', numeric) if numeric is not None else (
                    f'"{name}" LIKE ? ESCAPE \'\\\'', f"%{escaped}%")
                if special in {"vendor", "trainer"} and "NpcFlags" in available:
                    where += f" AND (NpcFlags & {128 if special == 'vendor' else 16}) != 0"
                selection = ",".join(f'"{column}"' for column in selected)
                rows = self._rows(db, f'SELECT {selection} FROM "{table}" WHERE {where} '
                                  f'ORDER BY "{identity}" LIMIT ?', (value, limit + 1))
                remaining = limit - len(result["records"])
                result["truncated"] |= len(rows) > remaining
                for row in rows[:remaining]:
                    result["records"].append({"kind": kind, "source": "world_database",
                                               "data": row, "related": self._related(db, kind, row, limit=limit)})
                if len(result["records"]) >= limit:
                    result["truncated"] |= index < len(kinds) - 1
                    break
        if not result["records"]:
            result["unknowns"].append("No matching world record. Try 'spell 635', 'npc Young Wolf', or an exact item name.")
        return result
