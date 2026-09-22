"""Close observed unit windows once, and verify the resulting painted state."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from jev.clients.targeting import PaintCode, PaintResult, Targeting
from jev.run.evidence import operation

UNIT_WINDOWS = ("ui.quest_frame", "ui.gossip", "ui.vendor", "ui.loot")


class CloseCode(StrEnum):
    CLOSED = "closed"
    REFUSED = "refused"
    BLIND = "blind"
    NOT_CLOSED = "not_closed"


@dataclass(frozen=True)
class Closed:
    code: CloseCode
    detail: str
    values: dict | None


def close_observed(hid: object, read: Callable[[], dict | None],
                   fields: tuple[str, ...] = UNIT_WINDOWS, *,
                   values: dict | None = None, settle_s: float = 0.0,
                   wait_for_paint: Callable[[], PaintResult] | None = None) -> Closed:
    """Check closure without treating Escape delivery as a closed window.

    Optional settling preserves the existing wait for auto-loot to close itself. Only
    a still-observed open window receives Escape; a new radio paint must then show all
    requested windows closed. Missing fields remain unreadable. A modal appearing in
    the process is a blocking outcome, including an Escape menu opened by a UI race.
    Cancellation from readers or input propagates to the owning worker's cleanup.
    """
    if not fields:
        raise ValueError("window closure needs at least one observed field")
    if not math.isfinite(settle_s) or settle_s < 0:
        raise ValueError("window settling time must be nonnegative and finite")

    def settled(observed: dict | None) -> Closed | None:
        if observed is None:
            return Closed(CloseCode.BLIND, "window state unreadable", None)
        if observed.get("ui.modal") is True:
            return Closed(CloseCode.NOT_CLOSED, "a blocking modal remains open", observed)
        if any(observed.get(key) is True for key in fields):
            return None
        if any(observed.get(key) is not False for key in fields):
            return Closed(CloseCode.BLIND, "window visibility fields unavailable", observed)
        return Closed(CloseCode.CLOSED, "observed windows closed", observed)

    with operation("ui.close", data={"fields": list(fields)}) as span:
        before = values if values is not None else read()
        current = before
        result = settled(current)
        if result is None and settle_s:
            time.sleep(settle_s)
            current = read()
            result = settled(current)
        if result is None:
            if not hid.tap("esc"):
                result = Closed(CloseCode.REFUSED, "window close input refused", current)
            else:
                paint = (wait_for_paint or Targeting(hid, read).wait_for_paint)()
                if paint.code is not PaintCode.FRESH:
                    result = Closed(CloseCode.BLIND, paint.detail, paint.after)
                else:
                    result = settled(paint.after) or Closed(
                        CloseCode.NOT_CLOSED, "window remains open after Escape", paint.after)
        if span.enabled:
            span.finish(code=result.code.value, detail=result.detail, data={
                "before": None if before is None else {key: before.get(key) for key in fields},
                "after": None if result.values is None else
                {key: result.values.get(key) for key in (*fields, "ui.modal")},
            })
        return result
