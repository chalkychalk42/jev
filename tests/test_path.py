"""The planner's contract, and the sidecar when one is built.

The `Path` type and the backend chain are pure. The navmesh query needs `jevpath`, which
is a build artifact, so those tests skip without it rather than pretending.
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

import pytest

from jev.guide.path import (
    FirstAvailable,
    MmapQuery,
    Path,
    PathStatus,
    RecordedQuery,
)

JEVPATH = pathlib.Path("tools/jevpath/jevpath")
MMAPS = pathlib.Path("~/cmangos/run/bin/mmaps").expanduser()
HAVE_MESH = JEVPATH.exists() and MMAPS.exists()

# Northshire courtyard to Marshal McBride: the leg that defeated straight-line travel
# nine times in a row before there was a planner.
COURTYARD = (-8944.81, -120.17, 82.02)
MCBRIDE = (-8902.59, -162.61, 82.02)


def test_a_path_needs_two_points_to_be_worth_walking():
    assert not Path(PathStatus.COMPLETE, points=((0, 0, 0),)).usable
    assert Path(PathStatus.COMPLETE, points=((0, 0, 0), (1, 1, 1))).usable


def test_a_partial_path_is_still_worth_walking():
    """It goes most of the way, and the follower finds out about the rest by arriving."""
    p = Path(PathStatus.PARTIAL, points=((0, 0, 0), (10, 0, 0)))
    assert p.usable


def test_nopath_and_unavailable_are_not_walkable():
    """Different causes — no route versus no backend — and neither is a route."""
    assert not Path(PathStatus.NOPATH, points=((0, 0, 0), (1, 1, 1))).usable
    assert not Path(PathStatus.UNAVAILABLE).usable


def test_length_is_measured_along_the_route_not_across_it():
    """A dogleg round a building is longer than the straight line it avoids, and a
    caller budgeting time needs the one it will actually walk."""
    p = Path(PathStatus.COMPLETE, points=((0, 0, 0), (10, 0, 0), (10, 10, 0)))
    assert p.length_yards() == pytest.approx(20.0)


def test_a_missing_sidecar_is_unavailable_not_a_crash():
    q = MmapQuery("/nonexistent/jevpath", "/nonexistent/mmaps")
    result = q.path(0, COURTYARD, MCBRIDE)
    assert result.status is PathStatus.UNAVAILABLE
    assert "no sidecar" in result.detail
    q.close()


@pytest.mark.parametrize("phase", ["startup", "query"])
def test_a_silent_sidecar_has_a_real_deadline_and_can_restart(tmp_path, phase):
    script = tmp_path / "sidecar.py"
    script.write_text('import time\n' + (
        'print(\'{"ready":true}\', flush=True)\n' if phase == "query" else '')
        + 'time.sleep(30)\n')
    q = MmapQuery(script, tmp_path, launcher=(sys.executable,), timeout_s=0.15)
    started = time.monotonic()
    try:
        result = q.path(0, COURTYARD, MCBRIDE)
        assert result.status is PathStatus.UNAVAILABLE
        assert "did not answer" in result.detail
        assert time.monotonic() - started < 3
        script.write_text('print(\'{"ready":true}\', flush=True)\n'
                          'input()\nprint(\'{"status":"nopath"}\', flush=True)\n')
        assert q.path(0, COURTYARD, MCBRIDE).status is PathStatus.NOPATH
    finally:
        q.close()


def test_a_cancelled_sidecar_releases_the_waiting_body(tmp_path):
    from jev.run.supervisor import Cancelled

    script = tmp_path / "sidecar.py"
    script.write_text('import time\nprint(\'{"ready":true}\', flush=True)\ntime.sleep(30)\n')
    cancelled = threading.Event()

    def checkpoint():
        if cancelled.is_set():
            raise Cancelled("operator stop")

    q = MmapQuery(script, tmp_path, launcher=(sys.executable,), timeout_s=10,
                  checkpoint=checkpoint)
    timer = threading.Timer(0.15, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled, match="operator stop"):
            q.path(0, COURTYARD, MCBRIDE)
        assert time.monotonic() - started < 3
        assert all(proc.poll() is not None for proc in q._procs.values())
    finally:
        timer.cancel()
        q.close()


def test_recorded_routes_are_a_seam_with_nothing_behind_them_yet():
    """Deliberately empty. The navmesh may make most of the recorder unnecessary, which
    is why it is not being filled in speculatively."""
    q = RecordedQuery("content/routes")
    assert q.path(0, COURTYARD, MCBRIDE).status is PathStatus.UNAVAILABLE


def test_the_chain_falls_through_to_the_next_backend():
    good = Path(PathStatus.COMPLETE, points=((0, 0, 0), (1, 1, 1)), source="second")

    class Dead:
        def path(self, *_a):
            return Path(PathStatus.UNAVAILABLE, source="first")

        def close(self):
            return

    class Live:
        def path(self, *_a):
            return good

        def close(self):
            return

    assert FirstAvailable(Dead(), Live()).path(0, COURTYARD, MCBRIDE).source == "second"


def test_the_chain_reports_the_last_failure_rather_than_inventing_one():
    class Dead:
        def path(self, *_a):
            return Path(PathStatus.NOPATH, source="mmap", detail="start off mesh")

        def close(self):
            return

    result = FirstAvailable(Dead()).path(0, COURTYARD, MCBRIDE)
    assert result.status is PathStatus.NOPATH
    assert result.detail == "start off mesh"


@pytest.mark.skipif(not HAVE_MESH, reason="needs tools/jevpath and extracted mmaps")
def test_the_navmesh_routes_around_northshire_abbey():
    """The whole point of the planner, against the real tiles.

    A straight line from the courtyard to Marshal McBride goes through the Abbey. The
    server walks its own NPCs around it using these exact tiles, so it can tell us how.
    """
    q = MmapQuery(JEVPATH, MMAPS)
    try:
        result = q.path(0, COURTYARD, MCBRIDE)
        assert result.status is PathStatus.COMPLETE, result.detail
        assert result.source == "mmap"
        assert len(result.points) >= 3, "a straight line is not a route around a building"
        straight = ((COURTYARD[0] - MCBRIDE[0]) ** 2
                    + (COURTYARD[1] - MCBRIDE[1]) ** 2) ** 0.5
        assert result.length_yards() > straight, "the detour has to be longer than the line"
    finally:
        q.close()


@pytest.mark.skipif(not HAVE_MESH, reason="needs tools/jevpath and extracted mmaps")
def test_a_point_off_the_mesh_says_which_end_failed():
    """Which end failed matters: a bad start is a bad position reading, a bad end is a
    bad node, and they need different fixes."""
    q = MmapQuery(JEVPATH, MMAPS)
    try:
        result = q.path(0, (99999.0, 99999.0, 0.0), MCBRIDE)
        assert result.status is PathStatus.NOPATH
        assert "start" in result.detail or "both" in result.detail
    finally:
        q.close()


@pytest.mark.skipif(not HAVE_MESH, reason="needs tools/jevpath and extracted mmaps")
def test_an_unreachable_destination_is_partial_not_complete():
    """The distinction the sidecar exists to make. Detour walks as far as the mesh
    allows; calling that "complete" would have the follower stop confidently on the
    wrong side of the world and report success.

    Measured: a point in open water off the continent snapped onto the mesh at the start
    and produced a 36-waypoint partial, not a failure — which is the honest answer, and
    a caller can tell it from an arrival because the status says so.
    """
    q = MmapQuery(JEVPATH, MMAPS)
    try:
        result = q.path(0, (0.0, 0.0, 0.0), MCBRIDE)
        assert result.status is PathStatus.PARTIAL
        assert result.usable, "a partial route is still worth walking"
    finally:
        q.close()


# Northshire Vineyards to the Gray Forest Wolves (the 7-9 grind): session 74 climbed a
# 55 degree face on this leg and slid back down it for four minutes; session 76, clear of
# the mesh's steep band, slid on the 40-50 degree hillside beside it. The way round by
# the road climbs no more than one in two anywhere.
VINEYARDS = (-9191.5, -349.9, 0.0)
GRAY_FOREST_WOLVES = (-9455.9, -566.2, 66.1)
CLIMBABLE_GRADE = 0.5


@pytest.mark.skipif(not HAVE_MESH, reason="needs tools/jevpath and extracted mmaps")
def test_routes_keep_to_ground_a_character_can_climb():
    import math

    q = MmapQuery(JEVPATH, MMAPS)
    try:
        result = q.path(0, VINEYARDS, GRAY_FOREST_WOLVES)
        assert result.status is PathStatus.COMPLETE, result.detail
        for a, b in zip(result.points, result.points[1:]):
            run = math.dist(a[:2], b[:2])
            if run > 1.0:
                assert (b[2] - a[2]) / run <= CLIMBABLE_GRADE, (a, b)
    finally:
        q.close()
