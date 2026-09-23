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


# Facing. Nothing in 2.4.3 turns the character toward a unit: a right-click starts an
# attack or an interaction and leaves the heading where it was. Measured in run
# 20260922T192105-3d5fcf, frames 99-104: auto-attack on (the Attack slot flashing), the
# wolf at the character's side two yards away, full health throughout. The camera sits
# behind the character, so anything straight ahead is drawn on the screen's vertical centre
# line whatever its distance. Turning until the selected unit's own nameplate reaches that
# line is therefore facing it, observed rather than assumed.
FACE_TOLERANCE = 0.05       # of the client width either side of centre
# Seconds of turning per unit of offset for the first pulse. Offsets understate the angle
# to a unit close beside the character, so every later pulse uses the rate the previous
# pulse actually produced - the same "compare what was asked with what was got" rule the
# travel follower applies to its turn rate.
FACE_GAIN_S = 0.9
FACE_MIN_PULSE_S = 0.05
FACE_MAX_PULSE_S = 0.35
FACE_MAX_TURNS = 8
# Searching for a selected unit with no plate on screen: fixed steps in one direction,
# bounded by a little over a full turn at the measured 134 deg/s (travel.TURN_RATE_SEED).
FACE_SEARCH_STEP_S = 0.3
FACE_SEARCH_MAX_S = 2.8
_FACE_RATE_BOUNDS = (0.08, 4.0)   # offset per second; outside this a reading is noise
# Which plate is the target's. Selection does not reliably fade other plates - a rabbit's
# plate measured as bright as a selected wolf's - so a plate is proved by exact hover
# ownership once, then tracked: the nearest candidate to where the last proved plate was
# predicted to move. A health bar's fill shrinks, but its left edge and centre do not.
FACE_HOVER_PROBES = 3
TRACK_DX, TRACK_DY = 0.06, 0.05      # fractions of the client width / height
# A turn's outcome is uncertain by a share of its own size (the rate is still being
# measured), so the horizontal window grows with the predicted shift.
TRACK_SHIFT_SHARE = 0.6
PLATE_FILL_SLACK_PX = 10


class FaceCode(StrEnum):
    FACED = "faced"              # the selected unit's plate is on the centre line
    NOT_VISIBLE = "not_visible"  # no plate for it after a bounded search
    UNSETTLED = "unsettled"      # turns ran out before the plate reached the centre
    NO_TARGET = "no_target"
    WRONG_TARGET = "wrong_target"
    WRONG_KIND = "wrong_kind"
    INTERRUPTED = "interrupted"
    BLIND = "blind"
    REFUSED = "refused"


@dataclass(frozen=True)
class FaceResult:
    code: FaceCode
    detail: str
    offset: float | None = None      # plate centre minus screen centre, fraction of width
    plate: units.Plate | None = None
    turns: int = 0
    turned_s: float = 0.0

    @property
    def faced(self) -> bool:
        return self.code is FaceCode.FACED


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


_FACE_FROM_CLICK = {
    ClickCode.BLIND: FaceCode.BLIND, ClickCode.INTERRUPTED: FaceCode.INTERRUPTED,
    ClickCode.NO_TARGET: FaceCode.NO_TARGET, ClickCode.WRONG_TARGET: FaceCode.WRONG_TARGET,
    ClickCode.WRONG_KIND: FaceCode.WRONG_KIND,
}


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

    def probe(self, point: tuple[int, int], *, require_target: bool = True) -> HoverResult:
        """Move to screen coordinates and observe fresh, exact selected-unit equality.

        Name hashes are retained for diagnosis and detecting an obvious selection
        change. They never establish equality: two wolves can have the same name.
        Without `require_target` it reports what is under the pointer (OTHER for any
        unit that is not the selection) for callers that identify a unit before, or
        without, selecting it - its name and death come from the fresh paint.
        """
        with operation("target.hover", data={"point": list(point)}) as span:
            result = self._probe(point, require_target)
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
            seq = after.get("seq")
            if baseline_seq is None:
                # An addon older than the modulo-255 sequence paints "no sequence" once
                # every 256 paints. That paint cannot be ordered; the next one is the
                # baseline, and only a paint after it is fresh.
                baseline, baseline_seq = after, seq
                continue
            if seq is not None and seq != baseline_seq:
                return PaintResult(PaintCode.FRESH, baseline, after, "new paint after action")
        if baseline_seq is None:
            return PaintResult(PaintCode.UNKNOWN, baseline, after,
                               "no paint sequence after action")
        return PaintResult(PaintCode.UNKNOWN, baseline, after,
                           "no new paint after action before timeout")

    def _probe(self, point: tuple[int, int], require_target: bool = True) -> HoverResult:
        self._checkpoint()
        before = self.read()

        def result(code: HoverCode, detail: str, after: dict | None = None):
            return HoverResult(code, point, before, after, detail)

        if before is None:
            return result(HoverCode.BLIND, "no radio before pointer movement")
        if require_target and before.get("target.has") is not True:
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
        if require_target and after.get("target.has") is not True:
            return result(HoverCode.NO_TARGET, "selected unit lost during hover", after)
        if (after.get("target.has") is True or before.get("target.has") is True) and (
                after.get("target.name_id") != before.get("target.name_id")):
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

    def cancel_pending_spell(self, values: dict | None = None) -> bool:
        """Esc when the radio says a spell is waiting for a target click; `True` if it was.

        While one waits, a click meant to select or use a unit casts it there instead. Only
        on a positive observation: Esc with nothing pending clears the target or opens
        the game menu.
        """
        values = self.read() if values is None else values
        if not values or values.get("bars.targeting") is not True:
            return False
        event("spell.cancel", data={"reason": "a spell was waiting for a target click"})
        self.hid.tap("esc")
        self.wait_for_paint()
        return True

    def turn_toward(self, offset: float) -> bool:
        """One open-loop turn by a screen offset (fraction of the width, + is right).

        For a direction that is not a plate, such as a selection mark, where there is
        nothing to servo on. Bounded to half a screen, at the facing gain.
        """
        if not math.isfinite(offset):
            raise ValueError("turn offset must be finite")
        offset = max(-0.5, min(0.5, offset))
        seconds = abs(offset) * FACE_GAIN_S
        if seconds < FACE_MIN_PULSE_S:
            return True
        key = getattr(self.hid, "TURN_RIGHT", "d") if offset > 0 else getattr(self.hid, "TURN_LEFT", "a")
        event("face.turn", data={"key": key, "seconds": round(seconds, 3),
                                 "offset": round(offset, 4), "open_loop": True})
        return bool(self.hid.hold(key, seconds))

    def face_selected(self, *, expected_name_id: int | None = None,
                      tolerance: float = FACE_TOLERANCE, max_turns: int = FACE_MAX_TURNS,
                      search_s: float = FACE_SEARCH_MAX_S,
                      hint: units.Plate | None = None) -> FaceResult:
        """Turn until the selected living unit's own nameplate is on the centre line.

        The one facing primitive for closing to melee, turning back to a unit that walked
        round the character, and walking up to anything with a plate. It only presses the
        turn keys: nothing here clicks, walks or selects. Identity is the radio's selected
        target, the plate is found by the selection fade, and a tie between bright plates
        is settled by exact hover ownership, never by position alone.
        """
        if not 0 < tolerance < 0.5 or type(max_turns) is not int or max_turns < 0:
            raise ValueError("facing tolerance and turn budget are out of range")
        if not math.isfinite(search_s) or search_s < 0:
            raise ValueError("facing search budget must be finite and non-negative")
        with operation("target.face", data={"wanted_name_id": expected_name_id,
                       "hint": None if hint is None else [round(hint.cx), round(hint.cy)]}) as span:
            result = self._face(expected_name_id, tolerance, max_turns, search_s, hint)
            span.finish(code=result.code.value, detail=result.detail,
                        data={"offset": result.offset, "turns": result.turns,
                              "turned_s": round(result.turned_s, 3)})
            return result

    def _face(self, wanted, tolerance, max_turns, search_s, hint) -> FaceResult:
        left = getattr(self.hid, "TURN_LEFT", "a")
        right = getattr(self.hid, "TURN_RIGHT", "d")
        turns, turned, searched = 0, 0.0, 0.0
        rate = 1.0 / FACE_GAIN_S
        before = pulse = None          # offset measured before the last pulse, and its length
        tracked = (hint.cx, hint.cy) if hint is not None else None
        slack = 0.0                     # extra horizontal tracking tolerance, pixels
        search_key = left

        def done(code: FaceCode, detail: str, offset=None, plate=None) -> FaceResult:
            return FaceResult(code, detail, offset, plate, turns, turned)

        while True:
            view = self._view()
            error = self._eligible(view.values, wanted, "living")
            if error is not None:
                return done(_FACE_FROM_CLICK.get(error, FaceCode.BLIND),
                            f"target observation: {view.fault}")
            plate = self._target_plate(view, tracked, slack)
            if isinstance(plate, FaceResult):
                return done(plate.code, plate.detail)
            if plate is None:
                tracked = None
                if searched >= search_s:
                    return done(FaceCode.NOT_VISIBLE,
                                "no plate proved to be the selected unit's after the search turn")
                event("face.search", data={"key": search_key, "seconds": FACE_SEARCH_STEP_S,
                                           "searched_s": round(searched, 3)})
                if not self.hid.hold(search_key, FACE_SEARCH_STEP_S):
                    return done(FaceCode.REFUSED, "turn input refused")
                searched += FACE_SEARCH_STEP_S
                turned += FACE_SEARCH_STEP_S
                before = pulse = None
                self.wait_for_paint()
                continue
            width = view.frame.shape[1]
            offset = (plate.cx - width / 2) / width
            if before is not None and pulse:
                # Progress toward (or past) the centre per second of turning. A unit that
                # walked the other way makes no progress and teaches nothing.
                progress = (before - offset) * (1 if before > 0 else -1)
                if progress > 0:
                    estimate = progress / pulse
                    if _FACE_RATE_BOUNDS[0] <= estimate <= _FACE_RATE_BOUNDS[1]:
                        rate = estimate
            if abs(offset) <= tolerance:
                return done(FaceCode.FACED, "selected plate on the centre line", offset, plate)
            if turns >= max_turns:
                return done(FaceCode.UNSETTLED,
                            f"{turns} turns left the plate {offset:+.3f} of the width off centre",
                            offset, plate)
            pulse = min(FACE_MAX_PULSE_S, max(FACE_MIN_PULSE_S, abs(offset) / rate))
            key = right if offset > 0 else left
            search_key = key            # a plate lost mid-turn went the way we were turning
            event("face.turn", data={"key": key, "seconds": round(pulse, 3),
                                     "offset": round(offset, 4), "rate": round(rate, 3)})
            if not self.hid.hold(key, pulse):
                return done(FaceCode.REFUSED, "turn input refused", offset, plate)
            turns += 1
            turned += pulse
            before = offset
            # Where this pulse should have carried the proved plate: toward the centre.
            shift = min(abs(offset), rate * pulse) * width
            tracked = (plate.cx - shift if offset > 0 else plate.cx + shift, plate.cy)
            slack = TRACK_SHIFT_SHARE * shift
            self.wait_for_paint()

    def _target_plate(self, view: TargetView, tracked,
                      slack: float = 0.0) -> units.Plate | FaceResult | None:
        """The selected unit's plate: tracked from a proved one, else proved by hover."""
        height, width = view.frame.shape[:2]
        candidates = units.plate_candidates(view.frame,
                                            units.plate_colours(view.values.get("target.reaction")))
        # A health bar's fill is the unit's health times the plate width: measured 89 px at
        # 60% and 29 px at 20% against 147 px full. Yellow flowers pass the bar shape but
        # almost never at the one width the radio's health predicts.
        hp = view.values.get("target.hp")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool) and 0 < hp <= 1:
            expected = hp * units.PLATE_FULL_W_FRAC * width
            candidates = [p for p in candidates
                          if abs(p.w - expected) <= max(PLATE_FILL_SLACK_PX, 0.25 * expected)]
        if tracked is not None:
            near = [p for p in candidates if abs(p.cx - tracked[0]) <= TRACK_DX * width + slack
                    and abs(p.cy - tracked[1]) <= TRACK_DY * height]
            if near:
                return min(near, key=lambda p: math.hypot(p.cx - tracked[0], p.cy - tracked[1]))
        full = units.PLATE_FULL_W_FRAC * width
        # Whole bars first: a fresh unit's plate is full, and flowers are not 147 px wide.
        ordered = sorted(candidates, key=lambda p: (p.w < 0.9 * full, abs(p.cx - width / 2)))
        for plate in ordered[:FACE_HOVER_PROBES]:
            point = (self.window_origin[0] + round(plate.cx),
                     self.window_origin[1] + round(plate.cy))
            hover = self.probe(point)
            if hover.code is HoverCode.MATCH:
                return plate
            if hover.code is HoverCode.REFUSED:
                return FaceResult(FaceCode.REFUSED, "pointer input refused")
            if hover.code is HoverCode.BLIND:
                return FaceResult(FaceCode.BLIND, hover.detail)
            if hover.code in (HoverCode.NO_TARGET, HoverCode.TARGET_CHANGED):
                return FaceResult(FaceCode.NO_TARGET if hover.code is HoverCode.NO_TARGET
                                  else FaceCode.WRONG_TARGET, hover.detail)
        return None

    def click_corpse(self, *, expected_name_id: int | None = None,
                     anchor: units.Plate | None = None, max_probes: int = 16,
                     timeout_s: float = 8.0) -> ClickResult:
        """Right-click the corpse where a fresh hover reports it dead.

        A dead unit has no nameplate, so a fresh hover that reports a dead unit can only
        be its body - the nameplate ambiguity that forbids trusting a living unit's hover
        does not exist here. A corpse does not move, so the verified pointer is still on
        it when the button goes down. Proposals are the last living plate's column, then
        the centre line.

        With the corpse selected, the hover must be exactly the selection. The client
        can also clear the selection at the kill (measured on 23 September: the target
        vanished the moment XP arrived); then a dead unit of the killed unit's name is
        the corpse to take, and `expected_name_id` is required.
        """
        if type(max_probes) is not int or max_probes <= 0:
            raise ValueError("max_probes must be a positive integer")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("corpse deadline must be positive and finite")
        with operation("target.corpse", data={"wanted_name_id": expected_name_id,
                       "anchor": None if anchor is None else [round(anchor.cx), round(anchor.cy)],
                       "max_probes": max_probes}) as span:
            result = self._click_corpse(expected_name_id, anchor, max_probes, timeout_s)
            span.finish(code=result.code.value, detail=result.detail,
                        data={"point": result.point, "attempts": result.attempts})
            return result

    def _click_corpse(self, wanted, anchor, max_probes, timeout_s) -> ClickResult:
        deadline = time.monotonic() + timeout_s
        view = self._view()
        if self.cancel_pending_spell(view.values):
            view = self._view()              # a click now would cast it on the corpse
        values = view.values
        selected = values is not None and values.get("target.has") is True
        if selected or values is None:
            error = self._eligible(values, wanted, "corpse")
        elif (values.get("ui.modal") is True or values.get("vitals.dead") is True
              or values.get("vitals.ghost") is True):
            error = ClickCode.INTERRUPTED
        else:
            error = ClickCode.NO_TARGET if wanted is None else None
        if error is not None:
            self._retain("corpse-unavailable", view)
            return ClickResult(error, None, f"target observation: {view.fault}")
        points = units.corpse_probe_points(view.frame, anchor, limit=max_probes)
        terminal = {HoverCode.REFUSED: ClickCode.REFUSED, HoverCode.BLIND: ClickCode.BLIND,
                    HoverCode.NO_TARGET: ClickCode.NO_TARGET,
                    HoverCode.TARGET_CHANGED: ClickCode.WRONG_TARGET}
        attempts = 0
        last = ClickResult(ClickCode.NOT_VISIBLE, None, "no hover found the selected corpse")
        for local in points:
            if time.monotonic() >= deadline:
                last = ClickResult(ClickCode.STALE, last.point, "corpse search budget expired",
                                   attempts)
                break
            screen = (local[0] + self.window_origin[0], local[1] + self.window_origin[1])
            attempts += 1
            hover = self.probe(screen, require_target=selected)
            if hover.code in terminal:
                self._retain("corpse-rejected", view)
                return ClickResult(terminal[hover.code], screen,
                                   f"hover: {hover.code}: {hover.detail}", attempts)
            after = hover.after or {}
            if selected and hover.code is not HoverCode.MATCH:
                continue
            if not selected and (hover.code is not HoverCode.OTHER
                                 or after.get("cursor.name_id") != wanted):
                continue
            if after.get("cursor.dead") is not True:
                last = ClickResult(ClickCode.WRONG_KIND, screen,
                                   "selected unit under the pointer is not observed dead",
                                   attempts)
                continue
            cursor = self.hid.cursor_position()
            if cursor is None or cursor != hover.point:
                last = ClickResult(ClickCode.STALE, cursor, "pointer moved after fresh hover",
                                   attempts)
                continue
            try:
                delivered = self.hid.click(right=True)
                after = self._view()
            finally:
                self._retain("corpse-before-click", view)
            self._retain("corpse-after-click", after)
            return ClickResult(ClickCode.CLICKED if delivered else ClickCode.REFUSED, cursor,
                               "input delivered; effect unconfirmed" if delivered else
                               "button input refused", attempts)
        self._retain("corpse-not-found", view)
        return ClickResult(last.code, last.point, last.detail, attempts)

    def click_selected(self, *, kind: str = "living", expected_name_id: int | None = None,
                       plate: units.Plate | None = None, max_probes: int = 6,
                       timeout_s: float = 8.0, max_view_age_s: float = 0.5) -> ClickResult:
        """One bounded selected *living* unit action, used to open an NPC's window.

        Geometry supplies hypotheses. Exact hover ownership and pose are necessary but
        insufficient: a living unit's hover also matches its nameplate, so the pointer
        must still fit freshly captured body geometry before the button press. Return
        delivery only; each caller observes its own game outcome. Corpses use
        `click_corpse`, where a dead hover alone is sufficient.
        """
        if kind != "living":
            raise ValueError("click_selected handles living units; corpses use click_corpse")
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
            if self.cancel_pending_spell(view.values):
                continue                     # a click now would cast it on the unit
            error = self._eligible(view.values, wanted, kind)
            if error is not None:
                self._retain("target-unavailable", view)
                return ClickResult(error, None, f"target observation: {view.fault}", attempts)
            observed_name = view.values.get("target.name_id")
            # Ring brackets first; plate-anchored body points when the ring is hidden.
            proposals = (*units.candidates(view.frame, plate=plate),
                         *units.body_candidates(view.frame, plate=plate))
            candidate = next((p for p in proposals if p.torso not in tried), None)
            if candidate is None:
                self._retain("target-no-proposal", view)
                return ClickResult(last.code, last.point, last.detail, attempts)
            point = candidate.torso
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
            fresh = (units.revalidate(current.frame, local, plate=plate)
                     or units.revalidate_body(current.frame, local, plate=plate))
            if not owned or not isinstance(dead, bool):
                last = ClickResult(ClickCode.UNKNOWN, cursor, "fresh ownership unavailable", attempts)
            elif dead:
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
