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

# How far the walk-in may travel before giving up. A unit that was visible and centred
# when it was aimed at is a few yards away, not a hundred — which is where a timeout-only
# bound put the character on a live run.
WALK_BUDGET_YARDS = 12.0

# Shortest yaw worth sending. Long enough that the client registers the turn and brings
# the camera behind the character; short enough that it barely moves the aim.
MIN_YAW_S = 0.08


class Result(StrEnum):
    GOSSIP = "gossip"
    QUEST = "quest"
    VENDOR = "vendor"
    LOOT = "loot"
    NO_TARGET = "no_target"        # /target found nothing, or the wrong thing
    NOT_VISIBLE = "not_visible"    # targeted, but no ring and plate on screen
    NO_WINDOW = "no_window"        # clicked the unit and nothing opened
    APPROACH_FAILED = "approach_failed"   # walked into a fence, a slope, the wrong thing
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
    used_centre: bool = field(default=False, init=False)
    walked_too_far: bool = field(default=False, init=False)
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

        # Always. Not only when off-centre.
        #
        # The locator reports where a unit is relative to the **camera**, and walking is
        # relative to the **character** — and in this client those are not the same
        # heading. The camera can sit rotated away from the character's facing, so a unit
        # dead centre on screen can be well off to one side of where W will go. Measured:
        # a unit centred and requiring no correction, walked at for twelve yards, never
        # reached.
        #
        # A keyboard turn moves the character and brings the camera round behind it, so
        # the yaw is doing two jobs — aiming, and making "centred" mean "in front". It is
        # still one turn.
        self._yaw_toward(sighting)
        sighting = self._look()
        if sighting is None:
            self.detail = "lost it while centring"
            return Result.NOT_VISIBLE

        # A unit that was visible and centred is within a few yards. Twelve is generous
        # for that and nowhere near the hundred a timeout-only bound allowed.
        collided = self._walk_in(walk_timeout_s, max_yards=WALK_BUDGET_YARDS)
        if not collided:
            self.detail = ("walked past the budget without stopping" if self.walked_too_far
                           else "walked without stopping against anything")
            return Result.NOT_VISIBLE

        ox, oy = self.window_origin
        sighting = self._look()                 # we moved; where is it now
        if sighting is not None:
            self.sighting = sighting
            self.clicked = (ox + sighting.torso[0], oy + sighting.torso[1])
        else:
            # The ring is gone. Two very different reasons, and the duel-range flag tells
            # them apart — not as "can I gossip", which it cannot answer, but as a
            # **negative**: if the target is not within eleven yards, we certainly did not
            # walk into it.
            v = self.read()
            if v is None or v.get("target.in_melee") is not True:
                self.detail = "stopped against something that was not the target"
                return Result.APPROACH_FAILED
            # Within range and no ring: pressed against the model, where it can be
            # underfoot and out of frame. One click at centre. `window_centre` is already
            # in screen coordinates.
            self.used_centre = True
            self.clicked = self.window_centre
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
        # A floor on the duration: a turn of zero length presses nothing, and pressing
        # nothing is what leaves the camera where it was.
        seconds = max(MIN_YAW_S, min(0.6, abs(error) / 1200.0))
        self.hid.hold("d" if error > 0 else "a", seconds)
        time.sleep(0.3)

    def _walk_in(self, timeout_s: float, max_yards: float = 12.0) -> bool:
        """Hold forward until the body stops. **The locator is not consulted in here.**

        The temptation is to re-find every tick and steer, and that was tried: the walk
        changes pitch, distance and which pixels the ring occupies, so requiring a fresh
        sighting on every frame turns a snapshot into a homing missile and the skill
        fails on the first frame that blinks. Six-miss tolerances and re-finds are the
        beginning of the same sprawl this module was rewritten to remove.

        So the caller aims once, and this walks. Arrival is the client's own collision —
        the character stops because something is in the way, and at the end of a walk
        aimed at a unit, that something is usually the unit.

        Returns True if it stopped against something, False otherwise. A failure is a
        failed approach — a fence, a slope, a bad aim — and not a reason to grow the
        locator.

        **Bounded by distance, not only by time.** A unit that was six yards away when it
        was aimed at is not twenty seconds of walking away, so twenty seconds of walking
        means the aim was wrong and every further step makes it worse. A live run did
        exactly that and ended a hundred yards from where it started. The caller knows
        roughly how far the unit is; past a few times that, this stops.
        """
        deadline = time.perf_counter() + timeout_s
        start = self.read_pos()
        last: tuple[float, float] | None = None
        still_since: float | None = None

        self.hid.key_down("w")
        try:
            while time.perf_counter() < deadline:
                now = time.perf_counter()
                here = self.read_pos()
                if here is None:
                    continue
                if start is not None and distance_yards(start, here, self.bounds) > max_yards:
                    self.walked_too_far = True
                    return False
                if last is not None and distance_yards(last, here, self.bounds) < STILL_YARDS:
                    still_since = still_since or now
                    if now - still_since > STILL_FOR_S:
                        return True
                else:
                    still_since = None
                last = here
        finally:
            self.hid.release_all()
        return False

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
