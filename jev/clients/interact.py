"""Open an NPC's window. One target, one look, one walk, one click.

2.4.3 has no `INTERACTTARGET`, no `InteractUnit` and no facing API — all arrived in 3.0 —
so interacting means right-clicking the model, which means knowing where it is.
`jev.perceive.units.find` answers that from the ring and nameplate the client draws around
every unit, so this file does not guess, sweep, or aim with coordinates.

It replaces a version that grew a method per failure: preflight, approach, leave_range,
turn_to, find_on_screen, locate, a click offset and a fourteen-point sweep. Each was a
patch on the last one's symptom. The caps below are the point of rewriting it.

    one `/target`        confirmed by name hash, never retyped
    one look             nothing visible is a failure, not a reason to spin
    at most one yaw      only to centre a sighting that is already visible
    walk until stopped   the client's own collision, not a range flag
    one click            at the torso the locator returned
    one confirmation     from the radio, or the skill failed

**`in_melee` deliberately gates nothing here.** It is `CheckInteractDistance` index 3 —
duel range, about eleven yards — and an NPC talks at roughly five. Believing it is how a
character stood well back and right-clicked a model it could see and could not reach.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.clients.hid import Hid
from jev.guide.coords import ZoneBounds, distance_yards
from jev.perceive.units import Sighting, find

# How far off centre a sighting may be before one yaw is spent bringing it in. Generous:
# the click point comes from the locator, so centring is only about not clicking a model
# clipped by the screen edge.
CENTRED_PX = 260

# Walking is done when the character stops moving. A unit's hitbox is what stops it, and
# that is unambiguous at any range for any unit.
STILL_YARDS = 0.3
STILL_FOR_S = 0.7


class Result(StrEnum):
    GOSSIP = "gossip"
    QUEST = "quest"
    VENDOR = "vendor"
    LOOT = "loot"
    NO_TARGET = "no_target"        # /target found nothing, or the wrong thing
    NOT_VISIBLE = "not_visible"    # targeted, but no ring and plate on screen
    NO_WINDOW = "no_window"        # clicked the unit and nothing opened
    BLIND = "blind"                # no readable frame at all

    @property
    def opened(self) -> bool:
        return self in (Result.GOSSIP, Result.QUEST, Result.VENDOR, Result.LOOT)


@dataclass
class Interact:
    hid: Hid
    bounds: ZoneBounds
    read: Callable[[], dict | None]                     # decoded radio values
    read_frame: Callable[[], object | None]             # raw pixels
    read_pos: Callable[[], tuple[float, float] | None]
    window_centre: tuple[int, int]
    window_origin: tuple[int, int] = (0, 0)

    sighting: Sighting | None = field(default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    yawed: bool = field(default=False, init=False)
    detail: str = field(default="", init=False)

    # -- the skill -----------------------------------------------------------

    def open_on(self, name: str, *, walk_timeout_s: float = 20.0) -> Result:
        self.sighting = self.clicked = None
        self.yawed = False
        self.detail = ""

        self._close_open_window()
        if not self._target(name):
            return Result.NO_TARGET

        sighting = self._look()
        if sighting is None:
            self.detail = "no ring and nameplate on screen"
            return Result.NOT_VISIBLE

        if abs(sighting.torso[0] - self._centre_x()) > CENTRED_PX:
            self._yaw_toward(sighting)
            sighting = self._look()
            if sighting is None:
                self.detail = "lost it while centring"
                return Result.NOT_VISIBLE

        self._walk_in(walk_timeout_s)

        sighting = self._look()                 # we moved; where is it now
        if sighting is None:
            self.detail = "lost it on the way in"
            return Result.NOT_VISIBLE

        self.sighting = sighting
        ox, oy = self.window_origin
        self.clicked = (ox + sighting.torso[0], oy + sighting.torso[1])
        self.hid.click(*self.clicked, right=True)
        time.sleep(0.9)
        return self._window_open() or Result.NO_WINDOW

    # -- pieces --------------------------------------------------------------

    def _centre_x(self) -> int:
        return self.window_centre[0] - self.window_origin[0]

    def _target(self, name: str) -> bool:
        """Typed once. A failure here has never been the command; it has been the game
        window not having focus, and retyping hides that behind identical chat lines."""
        from jev.perceive.radio_frame import name_id

        if self.hid.hwnd is not None and not self.hid.ready():
            self.detail = "the game window is not focused"
            return False
        if not self.hid.slash(f"/target {name}"):
            self.detail = f"could not type the command: {self.hid.unsendable}"
            return False
        time.sleep(0.7)
        v = self.read()
        if v is None:
            self.detail = "no readable frame after targeting"
            return False
        if v.get("target.has") is not True:
            self.detail = "nothing selected"
            return False
        painted = v.get("target.name_id")
        if painted is not None and painted != name_id(name):
            self.detail = f"selected {painted}, wanted {name_id(name)}"
            return False
        return True

    def _look(self) -> Sighting | None:
        frame = self.read_frame()
        return None if frame is None else find(frame)

    def _yaw_toward(self, sighting: Sighting) -> None:
        """One turn, proportional to the error. Not a search — the unit is already in
        view, this only stops a click landing on a model clipped by the screen edge."""
        self.yawed = True
        error = sighting.torso[0] - self._centre_x()
        self.hid.hold("d" if error > 0 else "a", min(0.6, abs(error) / 1200.0))
        time.sleep(0.3)

    def _walk_in(self, timeout_s: float) -> None:
        """Forward until the character stops moving, which is the unit's hitbox.

        No range flag decides this. The one available reads duel range and says "near"
        when the answer needed is "close enough to speak to".
        """
        deadline = time.perf_counter() + timeout_s
        last: tuple[float, float] | None = None
        still_since: float | None = None
        self.hid.key_down("w")
        try:
            while time.perf_counter() < deadline:
                now = time.perf_counter()
                here = self.read_pos()
                if here is None:
                    continue
                if last is not None and distance_yards(last, here, self.bounds) < STILL_YARDS:
                    still_since = still_since or now
                    if now - still_since > STILL_FOR_S:
                        return
                else:
                    still_since = None
                last = here
        finally:
            self.hid.release_all()

    def _window_open(self) -> Result | None:
        v = self.read()
        if v is None:
            return Result.BLIND
        for key, result in (("ui.quest_frame", Result.QUEST), ("ui.gossip", Result.GOSSIP),
                            ("ui.vendor", Result.VENDOR), ("ui.loot", Result.LOOT)):
            if v.get(key) is True:
                return result
        return None

    def _close_open_window(self) -> None:
        if self._window_open() not in (None, Result.BLIND):
            self.hid.tap("esc")
            time.sleep(0.4)
