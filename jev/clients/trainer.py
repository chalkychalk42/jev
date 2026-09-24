"""Buy what a class trainer will teach: press Train until there is nothing left to buy.

The stock trainer window selects the first service the character can learn when it opens,
and again after every purchase, and enables Train only while that service is learnable now
and affordable (Blizzard_TrainerUI 2.4.3). The addon paints Train as the advance button
only while it is enabled, so the loop is: press it, see the money fall, wait for the next
selection, and stop when the button goes. A press is a purchase only when the money falls.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced

OPEN_S = 4.0
BOUGHT_S = 3.0
# The server answers a purchase with the trainer's new list, and only then does the frame
# select the next service and enable Train again.
NEXT_S = 2.5
MAX_PURCHASES = 40


class Trained(StrEnum):
    DONE = "done"
    NOTHING = "nothing"
    NO_TRAINER = "no_trainer"
    BLIND = "blind"
    INTERRUPTED = "interrupted"
    REFUSED = "refused"
    TIMEOUT = "timeout"

    @property
    def ok(self) -> bool:
        return self in (Trained.DONE, Trained.NOTHING)


class _Stop(Exception):
    def __init__(self, result: Trained, detail: str):
        super().__init__(detail)
        self.result, self.detail = result, detail


@dataclass
class TrainerDesk:
    hid: object
    read: Callable[[], dict | None]
    visit: Callable[[], bool]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    bought: int = field(default=0, init=False)
    spent: int = field(default=0, init=False)
    detail: str = field(default="", init=False)
    _deadline: float = field(default=0.0, init=False)

    @traced("trainer")
    def run(self, *, timeout_s: float = 300.0) -> Trained:
        self.bought = self.spent = 0
        self.detail = ""
        self._deadline = self.clock() + timeout_s
        interrupted = False
        try:
            self._safe(self.read())
            if not self.visit():
                raise _Stop(Trained.NO_TRAINER, "could not open the trainer's window")
            values = self._await(lambda v: v.get("ui.trainer") is True, OPEN_S, "trainer window")
            while self.bought < MAX_PURCHASES:
                if self._train_point(values) is None:
                    values = self._await_optional(lambda v: self._train_point(v) is not None,
                                                  NEXT_S, window=True)
                    if values is None:
                        break
                money = values.get("bags.money_copper")
                if money is None:
                    raise _Stop(Trained.BLIND, "money unread")
                point = self._train_point(values)
                event("trainer.train", data={"point": list(point), "money": money})
                if self.hid.click(*point) is False:
                    raise _Stop(Trained.REFUSED, "Train click refused")
                after = self._await_optional(lambda v, m=money: _spent(v, m), BOUGHT_S,
                                             window=True)
                if after is None:
                    self.detail = "Train pressed and no money spent"
                    break
                self.bought += 1
                self.spent += money - after["bags.money_copper"]
                values = after
            return Trained.DONE if self.bought else Trained.NOTHING
        except _Stop as stop:
            self.detail = stop.detail
            return stop.result
        except BaseException:
            interrupted = True
            raise
        finally:
            latest = None if interrupted else self.read()
            if latest and latest.get("ui.trainer") is True and latest.get("ui.modal") is False:
                self.hid.tap("esc")

    def _safe(self, values: dict | None) -> dict:
        if values is None:
            raise _Stop(Trained.BLIND, "strip unreadable")
        if values.get("vitals.combat") is True or values.get("vitals.dead") is True \
                or values.get("vitals.ghost") is True:
            raise _Stop(Trained.INTERRUPTED, "combat or death at the trainer")
        if self.clock() >= self._deadline:
            raise _Stop(Trained.TIMEOUT, "training deadline exhausted")
        return values

    def _train_point(self, values: dict) -> tuple[int, int] | None:
        """Train, where it is: the advance button while the trainer window alone is up."""
        if values.get("ui.trainer") is not True or values.get("ui.modal") is not False:
            return None
        x, y = values.get("ui.advance_x"), values.get("ui.advance_y")
        if x is None or y is None or not 0 <= x <= 1 or not 0 <= y <= 1:
            return None
        ox, oy = self.window_origin
        w, h = self.window_size
        return ox + round(x * w), oy + round(y * h)

    def _await(self, predicate, seconds: float, what: str) -> dict:
        values = self._await_optional(predicate, seconds)
        if values is None:
            raise _Stop(Trained.TIMEOUT, f"no {what} within {seconds:.0f} s")
        return values

    def _await_optional(self, predicate, seconds: float, *, window: bool = False) -> dict | None:
        """The first look `predicate` accepts within `seconds`, else `None`. With `window`,
        the trainer window closing is the end of training, not something to wait out."""
        until = min(self._deadline, self.clock() + seconds)
        while self.clock() < until:
            values = self._safe(self.read())
            if predicate(values):
                return values
            if window and values.get("ui.trainer") is False:
                raise _Stop(Trained.NO_TRAINER, "the trainer window closed")
            self.sleep(0.05)
        return None


def _spent(values: dict, before: int) -> bool:
    money = values.get("bags.money_copper")
    return money is not None and money < before
