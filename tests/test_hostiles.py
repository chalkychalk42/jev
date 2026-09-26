"""Where units that attack on sight stand (V247): the generator's hostility and the lookup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from gen_hostile_spawns import hostile_sides, reputation_sides  # noqa: E402

from jev.world import hostiles  # noqa: E402

# (faction, flags, ours, friendly, hostile, enemy x4, friend x4), as dbc_FactionTemplate.
MONSTER = (14, 0, 8, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
BEAST_HATES_ALL = (29, 17, 8, 0, 1, 28, 0, 0, 0, 29, 0, 0, 0)
NEUTRAL_CREATURE = (7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
STORMWIND = (72, 0, 2, 2, 4, 0, 0, 0, 0, 0, 0, 0, 0)
# dbc_Faction for Stormwind: list 19; races (dwarf, night elf, gnome, draenei), (the Horde's),
# (human); classes; standings 3100, -42000, 4000; flags 17, 6 (at war), 17.
STORMWIND_FACTION = (19, 1100, 690, 1, 0, 0, 0, 0, 0, 3100, 2 ** 32 - 42000, 4000, 0,
                     17, 6, 17, 0)


@pytest.mark.parametrize(("template", "factions", "sides"), [
    (MONSTER, {}, 3),                          # a kobold: both sides
    (BEAST_HATES_ALL, {}, 3),                  # a Mangy Wolf's
    (NEUTRAL_CREATURE, {}, 0),                 # a boar, a deer
    (STORMWIND, {72: STORMWIND_FACTION}, 2),   # a guard: the Horde only, by standing
])
def test_hostility_is_the_servers_reaction_to_a_player(template, factions, sides):
    assert hostile_sides(template, factions) == sides


def test_a_faction_without_reputation_is_read_by_its_template():
    assert reputation_sides((2 ** 32 - 1, *([0] * 16))) is None


def test_near_keeps_the_side_the_level_and_the_radius(tmp_path, monkeypatch):
    rows = [[0.0, 10.0, 5.0, 5, 6, 3, 10.0],      # a wolf, 10 yards off
            [0.0, 15.0, 5.0, 1, 2, 3, 5.0],       # grey to a level 8
            [0.0, 20.0, 5.0, 60, 65, 2, 0.0],     # a guard, hostile to the Horde
            [0.0, 200.0, 5.0, 5, 6, 3, 10.0]]     # too far
    path = tmp_path / "hostile-spawns.json"
    path.write_text(json.dumps({"format": 1, "maps": {"0": rows}}))
    monkeypatch.setattr(hostiles, "HOSTILES_PATH", path)
    hostiles._index.cache_clear()
    try:
        assert hostiles.near(0, 0.0, 0.0, 70.0, side="alliance", level=8) == [(0.0, 10.0, 5.0)]
        assert len(hostiles.near(0, 0.0, 0.0, 70.0, side="horde", level=8)) == 2
        assert len(hostiles.near(0, 0.0, 0.0, 70.0, side="alliance", level=None)) == 2
        assert hostiles.near(0, 0.0, 0.0, 70.0, side=None, level=8) == []
        assert hostiles.near(1, 0.0, 0.0, 70.0, side="alliance", level=8) == []
    finally:
        hostiles._index.cache_clear()


def test_the_generated_index_names_the_mages_killers():
    """The Mangy Wolves beside session 219's first body, and none in Goldshire's inn."""
    near_body = hostiles.near(0, -9626.0, 505.0, 30.0, side="alliance", level=8)
    assert near_body, "the wolves round the body"
    assert hostiles.near(0, -9458.6, 28.6, 20.0, side="alliance", level=8) == []
