"""Measure ownership of a proposed world point before deciding how to use it.

This primitive only moves the pointer. It neither invents an aim point nor interprets
hover equality as engagement, range, damage or loot. A new radio paint after pointer
arrival is required; a sample collected during movement cannot validate the endpoint.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from jev.run.evidence import operation


class HoverCode(StrEnum):
    MATCH = "match"
    OTHER = "other"
    GROUND = "ground"
    UI = "ui"
    UNKNOWN = "unknown"
    BLIND = "blind"
    REFUSED = "refused"
    NO_TARGET = "no_target"
    TARGET_CHANGED = "target_changed"


class PaintCode(StrEnum):
    FRESH = "fresh"
    BLIND = "blind"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PaintResult:
    """Post-action paint evidence; freshness alone says nothing about action success."""

    code: PaintCode
    baseline: dict | None
    after: dict | None
    detail: str


@dataclass(frozen=True)
class HoverResult:
    code: HoverCode
    point: tuple[int, int]
    before: dict | None
    after: dict | None
    detail: str


@dataclass
class Targeting:
    hid: object
    read: Callable[[], dict | None]
    wait_s: float = 1.0
    poll_s: float = 0.05

    def __post_init__(self) -> None:
        if (not math.isfinite(self.wait_s) or not math.isfinite(self.poll_s)
                or self.wait_s <= 0 or self.poll_s <= 0):
            raise ValueError("hover wait and polling interval must be positive and finite")

    def probe(self, point: tuple[int, int]) -> HoverResult:
        """Move to screen coordinates and observe fresh, exact selected-unit equality.

        Name hashes are retained for diagnosis and detecting an obvious selection
        change. They never establish equality: two wolves can have the same name.
        """
        with operation("target.hover", data={"point": list(point)}) as span:
            result = self._probe(point)
            if span.enabled:
                span.finish(code=result.code.value, detail=result.detail, data={
                    "before": result.before, "after": result.after,
                })
            return result

    def _checkpoint(self) -> None:
        checkpoint = getattr(self.hid, "checkpoint", None)
        if checkpoint is not None:
            checkpoint()

    def wait_for_paint(self) -> PaintResult:
        """Observe a new paint after an action has returned.

        The immediate sample is only a sequence baseline: it can still depict the
        action in progress. Selection and pointer arrival share this exact rule.
        A fresh result is an observation, never proof that a click selected anything.
        """
        self._checkpoint()
        baseline = self.read()
        if baseline is None:
            return PaintResult(PaintCode.BLIND, None, None, "no radio after action")
        baseline_seq = baseline.get("seq")
        if baseline_seq is None:
            return PaintResult(PaintCode.UNKNOWN, baseline, baseline,
                               "no paint sequence after action")
        deadline = time.monotonic() + self.wait_s
        after = baseline
        while time.monotonic() < deadline:
            self._checkpoint()
            time.sleep(min(self.poll_s, max(0.0, deadline - time.monotonic())))
            self._checkpoint()
            after = self.read()
            if after is None:
                return PaintResult(PaintCode.BLIND, baseline, None,
                                   "radio lost while waiting for fresh paint")
            if after.get("seq") is not None and after["seq"] != baseline_seq:
                return PaintResult(PaintCode.FRESH, baseline, after, "new paint after action")
        return PaintResult(PaintCode.UNKNOWN, baseline, after,
                           "no new paint after action before timeout")

    def _probe(self, point: tuple[int, int]) -> HoverResult:
        self._checkpoint()
        before = self.read()

        def result(code: HoverCode, detail: str, after: dict | None = None):
            return HoverResult(code, point, before, after, detail)

        if before is None:
            return result(HoverCode.BLIND, "no radio before pointer movement")
        if before.get("target.has") is not True:
            return result(HoverCode.NO_TARGET, "no observed selected unit")
        if not self.hid.move_to(*point):
            return result(HoverCode.REFUSED, "pointer movement was not delivered")

        paint = self.wait_for_paint()
        if paint.code is not PaintCode.FRESH:
            code = HoverCode.BLIND if paint.code is PaintCode.BLIND else HoverCode.UNKNOWN
            return result(code, paint.detail, paint.after)
        after = paint.after
        if after.get("target.has") is not True:
            return result(HoverCode.NO_TARGET, "selected unit lost during hover", after)
        if after.get("target.name_id") != before.get("target.name_id"):
            return result(HoverCode.TARGET_CHANGED, "selection name changed during hover", after)
        world = after.get("cursor.world")
        has = after.get("cursor.has")
        same = after.get("cursor.is_target")
        if world is False:
            return result(HoverCode.UI, "pointer focus belongs to a UI frame", after)
        if world is not True or not isinstance(has, bool) or not isinstance(same, bool):
            return result(HoverCode.UNKNOWN, "cursor ownership fields are unavailable", after)
        if has is False:
            if same is True:
                return result(HoverCode.UNKNOWN, "inconsistent cursor ownership fields", after)
            return result(HoverCode.GROUND, "world focus without a mouseover unit", after)
        if same is not True:
            return result(HoverCode.OTHER, "mouseover unit differs from selected unit", after)
        return result(HoverCode.MATCH, "fresh world hover belongs to selected unit", after)
