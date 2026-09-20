"""The axis swap between world space and map space, pinned against a real landmark."""

from __future__ import annotations

import pytest

from jev.guide.coords import (
    ZoneBounds,
    load_bounds,
    map_to_world,
    on_map,
    world_to_map,
)

DB = "data/knowledge/tbc-243.sqlite"
ELWYNN = 12


@pytest.fixture(scope="module")
def bounds():
    return load_bounds(DB)


def test_goldshire_lands_where_the_client_puts_it(bounds):
    """The landmark check. If this moves, every node in the graph has moved with it."""
    mx, my = world_to_map(-9460.0, 60.0, bounds[ELWYNN])
    assert mx == pytest.approx(0.425, abs=0.01), "map horizontal comes from world Y"
    assert my == pytest.approx(0.657, abs=0.01), "map vertical comes from world X"


def test_the_transform_inverts(bounds):
    x, y = -9460.0, 60.0
    mx, my = world_to_map(x, y, bounds[ELWYNN])
    bx, by = map_to_world(mx, my, bounds[ELWYNN])
    assert bx == pytest.approx(x, abs=0.1)
    assert by == pytest.approx(y, abs=0.1)


def test_a_zone_with_no_box_refuses_rather_than_guessing(bounds):
    """Returning a plausible 0.5 for a degenerate row would put nodes in the sea."""
    flat = ZoneBounds(area_id=0, map_id=0, left=1.0, right=1.0, top=5.0, bottom=0.0)
    assert flat.degenerate
    assert world_to_map(0.0, 0.0, flat) is None


def test_a_border_spawn_is_reported_not_clamped(bounds):
    """Clamping would silently relocate a node that is genuinely just outside."""
    b = bounds[ELWYNN]
    outside_x = b.top + 500.0
    _, my = world_to_map(outside_x, 0.0, b)
    assert my < 0.0, "a point north of the map must report as north of it"
    assert not on_map(0.5, my)


def test_every_starting_zone_has_a_usable_box(bounds):
    """If a 1-12 zone cannot convert, the spine cannot be generated for it."""
    for area in (12, 1, 14, 85, 215, 3524, 3430):
        assert area in bounds, f"area {area} missing from WorldMapArea"
        assert not bounds[area].degenerate, f"area {area} has no usable map box"
