"""Open an NPC's window. One target, one look, one walk, one click.

2.4.3 has no `INTERACTTARGET` and no `InteractUnit` — both arrived in 3.0 — so
interacting means right-clicking the model, which means knowing where it is. (It does
have facing, off the minimap arrow; see V29. That was wrong here for a while, and it
would not have helped: knowing which way the character points is not knowing where the
merchant stands.)
`jev.perceive.units` answers that from the nameplate and ring the client draws around
every unit, so this file does not guess, sweep, or aim with coordinates.

**Nothing here types.** An earlier version opened chat and sent `/target <name>` to
acquire the unit before looking for it. That is one mangled keystroke away from a public
sentence, and it got there: with Caps Lock on — machine state no part of the bot could
see — `/target Marshal McBride` left the client as `?target mARSHAL mCbRIDE`, said out
loud in Northshire. The keystrokes all "succeeded"; there was nothing to detect.

Right-clicking a nameplate does the same job without a keyboard. It targets *and* opens
in one action, and the radio then says who was targeted — so identity is **confirmed
after the fact from the client's own state** rather than asserted beforehand by a string.
A wrong unit is a closed window and the next candidate, not a message anyone can read.

It replaces a version that grew a method per failure: preflight, approach, leave_range,
turn_to, find_on_screen, locate, a click offset and a fourteen-point sweep. Each was a
patch on the last one's symptom. The caps below are the point of rewriting it.

    no typing            ever; a bot that can talk is a bot that can say the wrong thing
    one look             nothing visible is a failure, not a reason to spin
    walk until stopped   the mesh stands us there; this only decides where to click
    one click per unit   at the nameplate's unit, nearest the middle of the screen first
    one confirmation     the radio names who was targeted, or the skill failed

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
from jev.perceive.radio_frame import name_id
from jev.perceive.units import Plate, Sighting, find, find_plates

# How close to the node counts as "standing on it", for the one case where the ring is
# hidden because the character is on top of the unit.
AT_NODE_YARDS = 6.0

# How many nameplates to try before giving up. Small: we are standing on the unit's spawn
# point, so the one we want is among the nearest few to the middle of the screen. Trying
# every plate on screen would be the sweep this file was rewritten to delete.
MAX_CANDIDATES = 3

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
    NO_TARGET = "no_target"        # clicked every candidate; none of them was the one
    NOT_VISIBLE = "not_visible"    # no nameplate on screen at all
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
    # An NPC has a nameplate only if the camera is pointing at him. The bot once stood
    # 4.6 yards from a targeted merchant, with his nameplate on screen, and reported
    # `not_visible` - correctly, because the camera was aimed at the character's feet.
    level: Callable[[], object] | None = None

    sighting: Sighting | None = field(default=None, init=False)
    clicked: tuple[int, int] | None = field(default=None, init=False)
    used_centre: bool = field(default=False, init=False)
    # Who answered each click, in order. The postmortem for "it opened the wrong NPC".
    tried: list[int | None] = field(default_factory=list, init=False)
    detail: str = field(default="", init=False)

    # -- the skill -----------------------------------------------------------

    def open_on(self, name: str, node_world: tuple[float, float, float] | None = None,
                node_map: tuple[float, float] | None = None) -> Result:
        """Stand there, look, right-click a nameplate, confirm who answered.

        `approach` is injected rather than built here: the planner belongs to the guide
        layer and this skill has no business knowing about navmeshes.

        The order matters. Walking first is what makes the looking work — nameplates only
        draw within range, and a unit fifty yards away has none. Acquiring before walking
        was the old shape, and it needed a name typed into chat to do it.
        """
        self.sighting = self.clicked = None
        self.used_centre = False
        self.tried = []
        self.detail = ""
        if self.level is not None:
            self.level()

        self._close_open_window()
        if self.approach is not None and node_world is not None and not self.approach(node_world):
            self.detail = "the planner could not stand us on the node"
            return Result.APPROACH_FAILED

        wanted = name_id(name)
        for plate in self._candidates():
            opened = self._try(plate, wanted)
            if opened is not None:
                return opened

        if self._at(node_map):
            # Standing on the spawn and facing it, so the unit is in the middle of the
            # screen whether or not its nameplate was clickable. A health bar is five
            # pixels tall and at three yards it can sit under the frame or off the top,
            # which is why this is a fallback and not an error — and it is still not a
            # guess, because the radio names whoever answers before anything else happens.
            opened = self._try_centre(wanted)
            if opened is not None:
                return opened

        if not self.tried:
            self.detail = "no nameplate on screen, and not standing on the node"
            return Result.NOT_VISIBLE
        self.detail = (f"tried {len(self.tried)} nameplate(s); none was {wanted}: "
                       f"{self.tried}")
        return Result.NO_TARGET

    def _try_centre(self, wanted: int) -> Result | None:
        """Right-click the middle of the screen. `None` means it was not the right unit."""
        self.used_centre = True
        self.clicked = self.window_centre
        self.hid.click(*self.clicked, right=True)
        time.sleep(0.9)
        values = self.read()
        if values is None:
            self.detail = "no readable frame after the centre click"
            return Result.BLIND
        painted = values.get("target.name_id")
        self.tried.append(painted)
        if painted != wanted:
            self._close_open_window()
            return None
        return self._window_open() or Result.NO_WINDOW

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
        return plates[:MAX_CANDIDATES]

    def _try(self, plate: Plate, wanted: int) -> Result | None:
        """Select via the nameplate, confirm who it is, then interact on the model.

        `None` means this was not the unit; try the next plate.

        Two clicks, and the split is the point. A nameplate is a *label*: clicking it
        selects reliably at any range, but it floats above the unit by an amount that
        depends on distance, so guessing the model's position from it lands on the floor.
        Selecting first makes the client draw the selection ring, and `units.find` then
        brackets the model between ring and plate — feet and head — which is the measured
        way to get a torso and is already proven on this NPC.

        Identity is checked **between** the two clicks. Nothing is interacted with until
        the radio has said who is selected, so a wrong plate costs a selection, not an
        action.
        """
        ox, oy = self.window_origin
        self.clicked = (ox + round(plate.cx), oy + round(plate.cy))
        self.hid.click(*self.clicked)
        time.sleep(0.6)

        values = self.read()
        if values is None:
            self.detail = "no readable frame after selecting"
            return Result.BLIND
        painted = values.get("target.name_id")
        self.tried.append(painted)
        if painted != wanted:
            return None

        frame = self.read_frame()
        self.sighting = None if frame is None else find(frame)
        if self.sighting is None:
            self.detail = "selected the right unit, but no ring and plate to aim at"
            return Result.NOT_VISIBLE
        self.clicked = (ox + self.sighting.torso[0], oy + self.sighting.torso[1])
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
