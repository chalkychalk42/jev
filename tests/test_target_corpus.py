"""Original targeting evidence must replay without an ignored local run directory.

These tests check source integrity and annotation provenance. They deliberately do not
turn the current locator's output into a reference answer or claim a clickable hitbox.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from jev.perceive.radio_frame import read

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
MANIFEST = json.loads((FIXTURES / "target-localization.json").read_text(encoding="utf-8"))
CASES = MANIFEST["cases"]
EVALUATION = json.loads(
    (FIXTURES / "target-localization-evaluation.json").read_text(encoding="utf-8")
)
EVALUATION_CASES = EVALUATION["cases"]


def load_case_frame(case: dict, *, root: Path = ROOT) -> np.ndarray:
    """Load the declared original array; fail on missing or changed source bytes."""
    source = case["source"]
    path = root / source["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"], case["id"]
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as archive:
            frame = archive[source["array_key"]]
    else:
        assert path.suffix == ".png", path
        assert source["array_key"] is None
        with Image.open(path) as image:
            assert image.mode == "RGB", (case["id"], image.mode)
            frame = np.array(image)
    assert frame.dtype == np.uint8, case["id"]
    assert list(frame.shape) == source["shape"], case["id"]
    assert hashlib.sha256(frame.tobytes()).hexdigest() == source["frame_sha256"], case["id"]
    return frame


@pytest.fixture(scope="module", params=CASES, ids=lambda case: case["id"])
def replay(request):
    case = request.param
    return case, load_case_frame(case)


def test_every_reference_source_is_portable():
    assert MANIFEST["schema_version"] == 1
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        source = case["source"]
        relative = Path(source["path"])
        assert not relative.is_absolute(), case["id"]
        assert ".." not in relative.parts, case["id"]
        assert (ROOT / relative).resolve().is_relative_to(FIXTURES), case["id"]
        assert source["tracked_source"] is True, case["id"]


def test_original_frame_and_recorded_radio_facts_replay(replay):
    case, frame = replay
    observed = read(frame)
    assert observed.fault.value == case["radio"]["fault"]
    if observed.ok:
        assert observed.values is not None
        for key, expected in case["radio"]["facts"].items():
            assert observed.values[key] == expected, (case["id"], key)
    else:
        # Historical checksum failures remain unknown; names are visual annotations.
        assert observed.values is None
        assert case["radio"]["facts"] == {}


def test_annotation_regions_still_refer_to_the_original_frame(replay):
    case, frame = replay
    annotation = case["annotation"]
    height, width, _ = frame.shape

    def valid(rectangle):
        assert len(rectangle) == 4, (case["id"], rectangle)
        left, top, right, bottom = rectangle
        assert all(type(value) is int for value in rectangle)
        assert 0 <= left < right <= width, (case["id"], rectangle)
        assert 0 <= top < bottom <= height, (case["id"], rectangle)

    for key in ("body_extent", "visible_ring_extent", "health_bar_extent"):
        if annotation[key] is not None:
            valid(annotation[key])
    for region in annotation["negative_regions"]:
        valid(region["extent"])
    for core in annotation["visible_body_cores"]:
        valid(core)
        body = annotation["body_extent"]
        assert body is not None
        assert body[0] <= core[0] < core[2] <= body[2]
        assert body[1] <= core[1] < core[3] <= body[3]
    if annotation["target_visibility"] == "not_visible":
        assert annotation["living_bracket_expectation"] == "abstain"
        assert annotation["body_extent"] is None
        assert annotation["visible_ring_extent"] is None
        assert annotation["health_bar_extent"] is None
        assert annotation["visible_body_cores"] == []


def test_vendored_screenshots_preserve_run_provenance_and_independence():
    groups_by_run = defaultdict(set)
    historical_run_ids = set()
    for case in CASES:
        source = case["source"]
        if "screenshot_index" not in source:
            continue
        original = Path(source["original_path"])
        current = Path(source["path"])
        assert original.parts[0] == "runs"
        assert original.parent.name == "screenshots"
        assert current.parent == Path("tests/fixtures/target-localization")
        assert original.name == current.name
        index, timestamp = original.stem.split("-", 1)
        assert int(index) == source["screenshot_index"]
        observed_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
        assert abs(observed_at.timestamp() - source["captured_at"]) < 0.00001
        groups_by_run[original.parent.parent].add(source["independence_group"])
        if original.parts[1] == "20260922T131407-487841":
            historical_run_ids.add(case["id"])
            assert source["independence_group"] == "wolf-run-20260922"
    assert all(len(groups) == 1 for groups in groups_by_run.values())
    assert historical_run_ids == {
        "selected_target_not_in_view",
        "wolf_distant_grass_distractor",
        "wolf_side_pose_grass_distractor",
        "wolf_ring_and_body_split_by_grass",
        "wolf_red_ring_yellow_health_bar",
        "wolf_near_front_pose",
        "wolf_clipped_by_frame_and_tooltip",
    }


@pytest.mark.parametrize("case", EVALUATION_CASES, ids=lambda case: case["id"])
def test_evaluation_originals_and_same_frame_facts_replay(case):
    source = case["source"]
    path = Path(source["path"])
    assert not path.is_absolute() and ".." not in path.parts
    assert (ROOT / path).resolve().is_relative_to(FIXTURES)
    assert source["tracked_source"] is True
    frame = load_case_frame(case)
    reading = read(frame)
    assert reading.ok
    assert reading.fault.value == case["radio"]["fault"]
    assert reading.seq == case["radio"]["seq"]
    for key, value in case["radio"]["facts"].items():
        assert reading.values[key] == value, (case["id"], key)
    height, width, _ = frame.shape
    annotation = case["annotation"]
    regions = [annotation[key] for key in
               ("body_extent", "visible_ring_extent", "health_bar_extent")
               if annotation[key] is not None]
    regions.extend(annotation["visible_body_cores"])
    for left, top, right, bottom in regions:
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height


@pytest.mark.parametrize("case", EVALUATION_CASES, ids=lambda case: case["id"])
def test_evaluation_action_points_keep_the_original_coordinate_frame(case):
    source, execution = case["source"], case["execution"]
    original = Path(source["original_path"])
    assert original.parts[:3] == ("runs", EVALUATION["run_id"], "screenshots")
    assert original.name == Path(source["path"]).name
    index, timestamp = original.stem.split("-", 1)
    assert int(index) == source["screenshot_index"]
    observed_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    assert abs(observed_at.timestamp() - source["captured_at"]) < 0.00001
    assert execution["arm_id"].startswith(EVALUATION["run_id"] + ":")
    assert execution["decision_id"].startswith(EVALUATION["run_id"] + ":")
    if execution["screen_point"] is None:
        assert execution["frame_point"] is None
        assert execution["attempts"] == 0
        assert execution["code"] == "not_visible"
        assert case["annotation"]["delivered_point_surface"] is None
    else:
        screen, origin = execution["screen_point"], execution["window_origin"]
        x, y = execution["frame_point"]
        assert [x, y] == [screen[0] - origin[0], screen[1] - origin[1]]
        assert 0 <= x < source["shape"][1] and 0 <= y < source["shape"][0]
        assert execution["code"] == "clicked"
        assert execution["input"] == {"right": True, "coordinates_supplied": False}
        assert case["annotation"]["delivered_point_surface"] in {"terrain", "visible_model"}
        # Follow-up facts are attributed to their own original source, never to the
        # portable before-image. Their PNGs are explicitly outside this minimum set.
        after = case["reviewed_after"]
        assert after["vendored"] is False
        assert Path(after["original_path"]).parent == original.parent
        assert after["captured_at"] > source["captured_at"]


def test_post_change_evaluation_is_one_new_independent_group():
    assert EVALUATION["schema_version"] == 1
    assert EVALUATION["role"] == "post_change_evaluation"
    assert len({case["id"] for case in EVALUATION_CASES}) == len(EVALUATION_CASES)
    groups = {case["source"]["independence_group"] for case in EVALUATION_CASES}
    assert groups == {"production-evaluation-" + EVALUATION["run_id"]}
    assert groups.isdisjoint(case["source"]["independence_group"] for case in CASES)
