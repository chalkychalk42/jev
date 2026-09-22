"""Measure ownership of a proposed world point before deciding how to use it.

Geometry proposes points; fresh hover and geometry jointly authorize a button press.
Delivery never establishes engagement, range, damage or loot. A new radio paint after
pointer arrival is required; a sample during movement cannot validate the endpoint.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum

from jev.perceive import radio_frame, units
from jev.run.evidence import event, operation


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


class ClickCode(StrEnum):
    CLICKED = "clicked"  # Windows accepted the input; a game effect remains unobserved.
    REFUSED = "refused"
    BLIND = "blind"
    NO_TARGET = "no_target"
    WRONG_TARGET = "wrong_target"
    WRONG_KIND = "wrong_kind"
    NOT_VISIBLE = "not_visible"
    UNKNOWN = "unknown"
    STALE = "stale"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class TargetView:
    frame: object | None
    values: dict | None
    captured_at: float
    started: float
    fault: str = "none"


@dataclass(frozen=True)
class ClickResult:
    code: ClickCode
    point: tuple[int, int] | None
    detail: str
    attempts: int = 0
    proposal: object | None = None

    @property
    def delivered(self) -> bool:
        return self.code is ClickCode.CLICKED


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
    read_frame: Callable[[], object | None] | None = None
    window_origin: tuple[int, int] = (0, 0)
    record_frame: Callable[..., dict] | None = None

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

        # Absolute input may round by a pixel. Ownership belongs to the physical
        # endpoint, which must remain unchanged through the subsequent observation.
        cursor_position = getattr(self.hid, "cursor_position", None)
        if cursor_position is not None:
            actual = cursor_position()
            if actual is None:
                return result(HoverCode.REFUSED, "cursor position unavailable after movement")
            point = actual

        paint = self.wait_for_paint()
        if paint.code is not PaintCode.FRESH:
            code = HoverCode.BLIND if paint.code is PaintCode.BLIND else HoverCode.UNKNOWN
            return result(code, paint.detail, paint.after)
        if cursor_position is not None and cursor_position() != point:
            return result(HoverCode.UNKNOWN, "pointer changed while waiting for paint", paint.after)
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

    def _view(self) -> TargetView:
        """Geometry and radio come from the same captured pixel array."""
        self._checkpoint()
        started, captured = time.monotonic(), time.time()
        frame = self.read_frame() if self.read_frame is not None else None
        if frame is None:
            return TargetView(None, None, captured, started, "missing_frame")
        reading = radio_frame.read(frame)
        return TargetView(frame, reading.values if reading.ok else None, captured, started,
                          reading.fault.value)

    @staticmethod
    def _eligible(values: dict | None, wanted: int | None, kind: str) -> ClickCode | None:
        if values is None:
            return ClickCode.BLIND
        if (values.get("ui.modal") is True or values.get("vitals.dead") is True
                or values.get("vitals.ghost") is True):
            return ClickCode.INTERRUPTED
        if values.get("target.has") is not True:
            return ClickCode.NO_TARGET
        if wanted is not None and values.get("target.name_id") != wanted:
            return ClickCode.WRONG_TARGET
        hp = values.get("target.hp")
        if hp is not None and ((kind == "living" and hp == 0)
                               or (kind == "corpse" and hp > 0)):
            return ClickCode.WRONG_KIND
        return None

    def _retain(self, label: str, view: TargetView) -> None:
        if view.frame is None:
            event("target.frame", code=view.fault, data={"label": label})
            return
        metadata = {"label": label, "captured_at": view.captured_at,
                    "seq": view.values.get("seq") if view.values else None,
                    "fault": view.fault,
                    "frame_sha256": hashlib.sha256(view.frame.tobytes()).hexdigest()}
        if self.record_frame is not None:
            metadata["image"] = self.record_frame(label, view.frame,
                                                  captured_at=view.captured_at)
        event("target.frame", code="observed", data=metadata)

    def click_selected(self, *, kind: str = "living", expected_name_id: int | None = None,
                       plate: units.Plate | None = None, max_probes: int = 6,
                       timeout_s: float = 8.0, max_view_age_s: float = 0.5) -> ClickResult:
        """One bounded selected-unit action shared by interaction, combat and looting.

        Geometry supplies hypotheses. Exact hover ownership and pose are necessary but
        insufficient: the pointer must still fit freshly captured geometry before the
        button press. Return delivery only; each caller observes its own game outcome.
        """
        if kind not in ("living", "corpse"):
            raise ValueError("target kind must be living or corpse")
        if type(max_probes) is not int or max_probes <= 0:
            raise ValueError("max_probes must be a positive integer")
        if any(not math.isfinite(v) or v <= 0 for v in (timeout_s, max_view_age_s)):
            raise ValueError("target deadlines must be positive and finite")
        with operation("target.click", data={"kind": kind,
                       "wanted_name_id": expected_name_id, "max_probes": max_probes}) as span:
            result = self._click_selected(kind, expected_name_id, plate, max_probes,
                                          timeout_s, max_view_age_s)
            span.finish(code=result.code.value, detail=result.detail,
                        data={"point": result.point, "attempts": result.attempts})
            return result

    def _click_selected(self, kind, wanted, plate, max_probes, timeout_s, max_age):
        deadline = time.monotonic() + timeout_s
        tried = set()
        attempts = 0
        last = ClickResult(ClickCode.NOT_VISIBLE, None, "no eligible target geometry")
        while attempts < max_probes and time.monotonic() < deadline:
            self._checkpoint()
            view = self._view()
            error = self._eligible(view.values, wanted, kind)
            if error is not None:
                self._retain("target-unavailable", view)
                return ClickResult(error, None, f"target observation: {view.fault}", attempts)
            observed_name = view.values.get("target.name_id")
            proposals = (units.candidates(view.frame, plate=plate) if kind == "living"
                         else units.corpse_candidates(view.frame))
            candidate = next((p for p in proposals
                              if (p.torso if kind == "living" else p.point) not in tried), None)
            if candidate is None:
                self._retain("target-no-proposal", view)
                return ClickResult(last.code, last.point, last.detail, attempts)
            point = candidate.torso if kind == "living" else candidate.point
            tried.add(point)
            screen = (point[0] + self.window_origin[0], point[1] + self.window_origin[1])
            attempts += 1
            event("target.proposal", data={"point": screen, "geometry": asdict(candidate),
                                          "seq": view.values.get("seq"), "attempt": attempts})
            if time.monotonic() >= deadline:
                self._retain("target-expired", view)
                return ClickResult(ClickCode.STALE, screen, "proposal budget expired", attempts)
            hover = self.probe(screen)
            if hover.code is not HoverCode.MATCH:
                self._retain("target-rejected", view)
                terminal = {HoverCode.REFUSED: ClickCode.REFUSED, HoverCode.BLIND: ClickCode.BLIND,
                            HoverCode.NO_TARGET: ClickCode.NO_TARGET,
                            HoverCode.TARGET_CHANGED: ClickCode.WRONG_TARGET}
                code = terminal.get(hover.code, ClickCode.UNKNOWN)
                last = ClickResult(code, screen, f"hover: {hover.code}: {hover.detail}", attempts)
                if hover.code in terminal:
                    return last
                continue
            cursor = self.hid.cursor_position()
            if cursor is None:
                return ClickResult(ClickCode.REFUSED, screen, "cursor position unavailable", attempts)
            if cursor != hover.point:
                self._retain("target-pointer-changed", view)
                last = ClickResult(ClickCode.STALE, cursor, "pointer moved after fresh hover", attempts)
                continue
            current = self._view()
            error = self._eligible(current.values, observed_name, kind)
            if error is not None:
                self._retain("target-changed", current)
                return ClickResult(error, cursor, "target changed during verification", attempts)
            values = current.values
            seq, hover_seq = values.get("seq"), hover.after.get("seq")
            if seq is None or hover_seq is None or (seq - hover_seq) % 256 >= 128:
                self._retain("target-old-frame", current)
                last = ClickResult(ClickCode.STALE, cursor, "frame predates fresh hover paint", attempts)
                continue
            owned = (values.get("cursor.has") is True and values.get("cursor.is_target") is True
                     and values.get("cursor.world") is True)
            dead = values.get("cursor.dead")
            local = cursor[0] - self.window_origin[0], cursor[1] - self.window_origin[1]
            fresh = (units.revalidate(current.frame, local, plate=plate) if kind == "living"
                     else units.revalidate_corpse(current.frame, local))
            if not owned or not isinstance(dead, bool):
                last = ClickResult(ClickCode.UNKNOWN, cursor, "fresh ownership unavailable", attempts)
            elif dead != (kind == "corpse"):
                self._retain("target-wrong-kind", current)
                return ClickResult(ClickCode.WRONG_KIND, cursor, "mouseover pose differs", attempts)
            elif (fresh is None or time.monotonic() - current.started > max_age
                  or self.hid.cursor_position() != cursor or time.monotonic() >= deadline):
                last = ClickResult(ClickCode.STALE, cursor, "point no longer has current geometry",
                                   attempts)
            else:
                # No second move_to: the verified physical pointer is already in place.
                # Persist the pre-click pixels after delivery so PNG I/O cannot age it.
                try:
                    delivered = self.hid.click(right=True)
                    after = self._view()
                finally:
                    self._retain("target-before-click", current)
                self._retain("target-after-click", after)
                return ClickResult(ClickCode.CLICKED if delivered else ClickCode.REFUSED, cursor,
                                   "input delivered; effect unconfirmed" if delivered else
                                   "button input refused", attempts, fresh)
            self._retain("target-stale", current)
        return ClickResult(last.code, last.point, last.detail, attempts)
