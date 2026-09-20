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

# How close to the node counts as "standing on it", for the one case where the ring is
# hidden because the character is on top of the unit.
AT_NODE_YARDS = 6.0

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
    # World point -> did we get there. Injected: the planner is the guide layer's.
    approach: Callable[[tuple[float, float, float]], bool] | None = None

    sighting: Sighting | None = field(default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    used_centre: bool = field(default=False, init=False)
    detail: str = field(default="", init=False)

    # -- the skill -----------------------------------------------------------

    def open_on(self, name: str, node_world: tuple[float, float, float] | None = None,
                node_map: tuple[float, float] | None = None) -> Result:
        """Target, stand there, look, click once, confirm.

        `approach` is injected rather than built here: the planner belongs to the guide
        layer and this skill has no business knowing about navmeshes.
        """
        self.sighting = self.clicked = None
        self.used_centre = False
        self.detail = ""

        self._close_open_window()
        if not self._target(name):
            return Result.NO_TARGET

        if self.approach is not None and node_world is not None and not self.approach(node_world):
            self.detail = "the planner could not stand us on the node"
            return Result.APPROACH_FAILED

        ox, oy = self.window_origin
        sighting = self._look()
        if sighting is not None:
            self.sighting = sighting
            self.clicked = (ox + sighting.torso[0], oy + sighting.torso[1])
        elif self._at(node_map):
            # No ring, but standing on the node — the model is underfoot and its ring is
            # behind the character. One click at centre.
            self.used_centre = True
            self.clicked = self.window_centre
        else:
            self.detail = "no ring and nameplate, and not standing on the node"
            return Result.NOT_VISIBLE

        self.hid.click(*self.clicked, right=True)
        time.sleep(0.9)
        return self._window_open() or Result.NO_WINDOW

    # -- pieces --------------------------------------------------------------

    def _at(self, node_map: tuple[float, float] | None) -> bool:
        """Close enough to the node to believe the unit is underfoot."""
        if node_map is None:
            return False
        here = self.read_pos()
        return here is not None and distance_yards(here, node_map, self.bounds) <= AT_NODE_YARDS

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
