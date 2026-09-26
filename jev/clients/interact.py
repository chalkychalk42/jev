"""Select an NPC, verify its current body point, and observe the resulting window.

Navigation supplies the approach. Shared Targeting owns body/corpse point verification;
this composition owns requested identity and gossip/quest/vendor outcome. No chat, pixel
drop or spawn-point centre click supplies missing visual evidence.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from jev.clients.hid import Hid
from jev.clients.targeting import ClickCode, PaintCode, Targeting
from jev.clients.travel import TURN_RATE_SEED
from jev.clients.windows import CloseCode, close_observed
from jev.guide.coords import ZoneBounds
from jev.perceive.radio_frame import name_id
from jev.perceive.units import Plate, Sighting, find_plates
from jev.run.evidence import event, operation, traced

# How many nameplates to try before giving up. Small: we are standing on the unit's spawn
# point, so the one we want is among the nearest few to the middle of the screen. Trying
# every plate on screen would be the sweep this file was rewritten to delete.
MAX_CANDIDATES = 3
# Looks at the candidates before giving up: straight ahead, then after each of three quarter
# turns. Stood beside Marshal McBride the character faced the Main Hall's wall with him out
# of view, selected and plateless, and the hand-in failed twice (run 20260924T033806-a3254d).
INTERACT_LOOKS = 4
QUARTER_TURN_S = math.radians(90.0) / TURN_RATE_SEED
# A unit selected and proved to be the one wanted whose body point will not hold still is
# waited for, not given up on: this long between tries, this many more tries. Walking up the
# Abbey's stairs to Khelden Bremen raised the subzone's title, "Northshire Abbey" in a
# nameplate's green, across his plate, and three proposals went stale as the plate
# finder's geometry jumped with it (the mage's check, 26 September). The title fades in a
# few seconds.
SETTLE_S = 2.0
SETTLE_TRIES = 2

# How close the character has to be for an NPC to talk, and therefore what "arrived" means
# for a node holding a unit.
#
# It is not the node radius. A node is a **spawn point**, which is the middle of the unit,
# and a unit is solid — so a path planned to it ends inside a collision capsule and the
# last couple of yards are unwalkable by construction. Asking the follower for 3 yards got
# 3.1 and four stuck events against McBride himself, which is arrival being reported as
# failure. Every NPC in the game has this shape.
GOSSIP_YARDS = 5.0


class Result(StrEnum):
    GOSSIP = "gossip"
    QUEST = "quest"
    VENDOR = "vendor"
    LOOT = "loot"
    TRAINER = "trainer"
    TAXI = "taxi"                  # a flight master's map, opened with no gossip first
    NO_TARGET = "no_target"        # clicked every candidate; none of them was the one
    NOT_VISIBLE = "not_visible"    # no nameplate on screen at all
    NO_WINDOW = "no_window"        # clicked the unit and nothing opened
    APPROACH_FAILED = "approach_failed"   # walked into a fence, a slope, the wrong thing
    BLIND = "blind"                # no readable frame at all
    REFUSED = "refused"
    INTERRUPTED = "interrupted"
    WINDOW_OPEN = "window_open"    # an existing window could not be observed closed

    @property
    def opened(self) -> bool:
        return self in (Result.GOSSIP, Result.QUEST, Result.VENDOR, Result.LOOT, Result.TRAINER,
                        Result.TAXI)


@dataclass
class Interact:
    hid: Hid
    bounds: ZoneBounds
    read: Callable[[], dict | None]                     # decoded radio values
    read_frame: Callable[[], object | None]             # raw pixels
    read_pos: Callable[[], tuple[float, float] | None]
    window_centre: tuple[int, int]
    window_origin: tuple[int, int] = (0, 0)
    # World point -> did we get there. Injected: the planner is the guide layer's.
    approach: Callable[[tuple[float, float, float]], bool] | None = None
    # An NPC has a nameplate only if the camera is pointing at him. The bot once stood
    # 4.6 yards from a targeted merchant, with his nameplate on screen, and reported
    # `not_visible` - correctly, because the camera was aimed at the character's feet.
    level: Callable[[], object] | None = None
    targeting: Targeting | None = None

    sighting: Sighting | None = field(default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    # Who answered each click, in order. The postmortem for "it opened the wrong NPC".
    tried: list[int | None] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)

    # -- the skill -----------------------------------------------------------

    @traced("interact")
    def open_on(self, name: str, node_world: tuple[float, float, float] | None = None,
                node_map: tuple[float, float] | None = None) -> Result:
        """Stand there, select via a nameplate, verify the body, and observe a window.

        `approach` is injected rather than built here: the planner belongs to the guide
        layer and this skill has no business knowing about navmeshes.

        The order matters. Walking first is what makes the looking work — nameplates only
        draw within range, and a unit fifty yards away has none. Acquiring before walking
        was the old shape, and it needed a name typed into chat to do it.
        """
        self.sighting = self.clicked = None
        self.tried = []
        self.detail = ""
        event("interact.request", data={"name": name, "node_world": node_world,
                                        "node_map": node_map})
        if self.level is not None and self.level() is False:
            self.detail = "camera input refused"
            return Result.REFUSED

        closed = self._close_open_window()
        if closed is not None:
            return closed
        if self.approach is not None and node_world is not None and not self.approach(node_world):
            self.detail = "the planner could not stand us on the node"
            return Result.APPROACH_FAILED

        wanted = name_id(name)
        for look in range(INTERACT_LOOKS):
            if look:
                event("interact.look", data={"look": look, "seconds": round(QUARTER_TURN_S, 3)})
                if not self.hid.hold(getattr(self.hid, "TURN_RIGHT", "d"), QUARTER_TURN_S):
                    self.detail = "look-round turn refused"
                    return Result.REFUSED
                if self._targeting().wait_for_paint().code is PaintCode.BLIND:
                    self.detail = "radio lost while looking round"
                    return Result.BLIND
            for plate in self._candidates():
                opened = self._try(plate, wanted)
                if opened is not None:
                    return opened
        if not self.tried:
            self.detail = "no observed nameplate for selection"
            return Result.NOT_VISIBLE
        self.detail = f"tried {len(self.tried)} nameplate(s); none was {wanted}: {self.tried}"
        return Result.NO_TARGET

    def _targeting(self) -> Targeting:
        return self.targeting or Targeting(self.hid, self.read, read_frame=self.read_frame,
                                           window_origin=self.window_origin)

    def _candidates(self) -> list[Plate]:
        """Nameplates worth a click, nearest the middle of the screen first.

        Nearest-to-centre because the character is standing on the unit's spawn point and
        facing it, so the wanted unit is the one the camera is pointed at. This is an
        ordering, not an identification — identity comes from the radio after the click,
        which is the only source that cannot be wrong about it.
        """
        frame = self.read_frame()
        if frame is None:
            return []
        cx = self._centre_x()
        plates = find_plates(frame)
        plates.sort(key=lambda p: abs(p.cx - cx))
        with operation("target.candidates") as span:
            if span.enabled:
                span.finish(code="observed", data={"count": len(plates),
                    "candidates": [asdict(p) for p in plates[:MAX_CANDIDATES]]})
        return plates[:MAX_CANDIDATES]

    @traced("interact.select")
    def _try(self, plate: Plate, wanted: int) -> Result | None:
        """Select via the nameplate, confirm who it is, then interact on the model.

        `None` means this was not the unit; try the next plate.

        Two clicks, and the split is the point. A nameplate is a *label*: clicking it
        selects reliably at any range, but it floats above the unit by an amount that
        depends on distance, so guessing the model's position from it lands on the floor.
        Selecting first makes the client draw the selection ring. Shared Targeting
        proposes points between ring and plate, then checks current geometry and fresh
        selected-unit ownership before it delivers the right-click.

        Identity is checked **between** the two clicks. Nothing is interacted with until
        the radio has said who is selected, so a wrong plate costs a selection, not an
        action.
        """
        ox, oy = self.window_origin
        self.clicked = (ox + round(plate.cx), oy + round(plate.cy))
        event("selection.request", data={"method": "plate", "point": self.clicked,
                                         "wanted_name_id": wanted})
        if not self.hid.click(*self.clicked):
            self.detail = "selection input refused"
            return Result.REFUSED
        paint = self._targeting().wait_for_paint()
        if paint.code is not PaintCode.FRESH:
            self.detail = paint.detail
            return Result.BLIND
        values = paint.after
        event("selection.observed", code="blind" if values is None else "readable",
              data={"wanted_name_id": wanted,
                    "observed_name_id": values.get("target.name_id") if values else None})
        if values is None:
            self.detail = "no readable frame after selecting"
            return Result.BLIND
        painted = values.get("target.name_id")
        self.tried.append(painted)
        if painted != wanted:
            return None

        action = self._click_body(wanted, plate)
        self.detail, self.clicked = action.detail, action.point
        self.sighting = action.proposal if isinstance(action.proposal, Sighting) else None
        event("interact.click", code=action.code.value,
              data={"point": self.clicked, "wanted_name_id": wanted})
        if not action.delivered:
            return {ClickCode.REFUSED: Result.REFUSED, ClickCode.BLIND: Result.BLIND,
                    ClickCode.INTERRUPTED: Result.INTERRUPTED,
                    ClickCode.NO_TARGET: Result.NO_TARGET,
                    ClickCode.WRONG_TARGET: Result.NO_TARGET}.get(action.code, Result.NOT_VISIBLE)
        time.sleep(0.9)
        return self._window_open() or Result.NO_WINDOW

    def _click_body(self, wanted: int, plate: Plate):
        """Right-click the selected unit's body, waiting out a view that will not hold
        still (`SETTLE_S`). Any other refusal is final, as before."""
        for attempt in range(1 + SETTLE_TRIES):
            if attempt:
                event("interact.settle", data={"attempt": attempt, "seconds": SETTLE_S})
                time.sleep(SETTLE_S)
            action = self._targeting().click_selected(kind="living", expected_name_id=wanted,
                                                       plate=plate)
            if action.delivered or action.code not in (ClickCode.STALE, ClickCode.NOT_VISIBLE):
                return action
        return action

    # -- pieces --------------------------------------------------------------

    def _centre_x(self) -> int:
        return self.window_centre[0] - self.window_origin[0]

    def _window_open(self) -> Result | None:
        v = self.read()
        event("interact.window", code="blind" if v is None else "readable",
              data={} if v is None else {key: v.get(key) for key in (
                  "ui.quest_frame", "ui.gossip", "ui.vendor", "ui.loot", "ui.trainer")})
        if v is None:
            return Result.BLIND
        for key, result in (("ui.quest_frame", Result.QUEST), ("ui.gossip", Result.GOSSIP),
                            ("ui.vendor", Result.VENDOR), ("ui.loot", Result.LOOT),
                            ("ui.trainer", Result.TRAINER), ("ui.taxi", Result.TAXI)):
            if v.get(key) is True:
                return result
        return None

    def _close_open_window(self) -> Result | None:
        closed = close_observed(self.hid, self.read,
                                wait_for_paint=self._targeting().wait_for_paint)
        if closed.code is CloseCode.CLOSED:
            return None
        self.detail = closed.detail
        return {CloseCode.REFUSED: Result.REFUSED, CloseCode.BLIND: Result.BLIND,
                CloseCode.NOT_CLOSED: Result.WINDOW_OPEN}[closed.code]
