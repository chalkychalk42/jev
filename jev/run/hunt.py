"""Work an objective until its counter is full. The radius, not the pin.

A `quest_objective` node carries a world point and a radius `r`, and the radius is the
part that matters. The point is one spawn out of a camp full of them, so standing on it
and waiting is how a bot gets to 1/10 and stops: the first live run killed a kobold, found
nothing else within arm's reach, and reported "not visible" twenty times in a row while
the rest of the camp wandered about fifteen yards away.

So when there is nothing to fight, move. Stations are points on the disk the node already
describes, walked with the same planner as everything else — there is no second
pathfinder here and no camp-shaped special case. Every kill quest in the game is this
loop, because every kill quest is a place and a counter.

The counter is `quests.o0_have`, which is the server's tally. Nothing here counts its own
kills: a swing that missed, a mob somebody else tagged, and a kill that did not belong to
this quest are all indistinguishable from the inside.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.clients.fight import Fight, Fought
from jev.clients.rest import Rest, Rested
from jev.world.combat import EAT_BELOW

# Fractions of the radius to ring, nearest first, and how many points on each ring.
#
# Outward rather than straight to the edge. A node's `r` is a map fraction, and in a zone
# as small as Northshire 0.06 of the map is two hundred yards — so a single ring at
# two-thirds of it sends the character a hundred and forty yards away from a camp it was
# standing next to. Searching near before far is not camp knowledge; it is the obvious
# order to look in.
RINGS = (0.15, 0.35, 0.6, 1.0)
PER_RING = 4
STATIONS = 1 + len(RINGS) * PER_RING

# Fruitless looks at one station before moving on. Two, because a camp is a moving crowd
# and one empty look says very little.
DRY_LOOKS = 2

# What to walk when a node does not say. **Not** the node's `r`: `r` is the tracker's
# arrival slop in map fractions, and a hunt that borrowed it walked a two-hundred-yard
# disk that contained Northshire Abbey, stood in the Main Hall facing a wall, and
# correctly reported that it could not see any kobolds. A wrong default is recoverable;
# reaching for a field that means something else is how that bug comes back.
DEFAULT_HUNT_YARDS = 30.0


class Hunted(StrEnum):
    DONE = "done"                # the counter reached its requirement
    DIED = "died"
    NO_FOOD = "no_food"          # too hurt to continue and nothing to eat
    UNREACHABLE = "unreachable"  # could not stand anywhere on the disk
    TIMEOUT = "timeout"
    BLIND = "blind"

    @property
    def ok(self) -> bool:
        return self is Hunted.DONE


def stations(centre: tuple[float, float, float], radius_yards: float,
             rings: tuple[float, ...] = RINGS,
             per_ring: int = PER_RING) -> list[tuple[float, float, float]]:
    """Points to stand on, working outward from the node.

    The centre first, because the node is usually a reasonable place to be, then rings at
    increasing fractions of the radius. Each ring is offset half a step from the last so
    the points do not line up on spokes and re-walk the same ground.
    """
    cx, cy, cz = centre
    out = [(cx, cy, cz)]
    for r, fraction in enumerate(rings):
        reach = radius_yards * fraction
        for i in range(per_ring):
            angle = 2.0 * math.pi * (i + 0.5 * r) / per_ring
            out.append((cx + reach * math.cos(angle), cy + reach * math.sin(angle), cz))
    return out


@dataclass
class Hunt:
    fight: Fight
    rest: Rest
    read: Callable[[], dict | None]
    approach: Callable[[tuple[float, float, float]], bool]
    progress: Callable[[], tuple[int | None, int | None]]
    say: Callable[[str], None] = print

    kills: int = field(default=0, init=False)
    _outdoors: bool | None = field(default=None, init=False)
    moves: int = field(default=0, init=False)
    detail: str = field(default="", init=False)

    def run(self, centre: tuple[float, float, float], radius_yards: float,
            name_id: int | None = None, *, timeout_s: float = 900.0) -> Hunted:
        self.kills = self.moves = 0
        self._outdoors = None
        self.detail = ""
        deadline = time.monotonic() + timeout_s
        posts = stations(centre, radius_yards)
        post = 0
        dry = 0
        stood = False

        while time.monotonic() < deadline:
            have, need = self.progress()
            if need is not None and have is not None and have >= need:
                self.say(f"  objective complete: {have}/{need}")
                return Hunted.DONE

            if not stood:
                if post >= len(posts):
                    self.detail = "walked the whole disk and found nothing to fight"
                    return Hunted.UNREACHABLE
                target = posts[post]
                post += 1
                self.moves += 1
                if not self.approach(target):
                    continue          # a station we cannot stand on is not a dead end
                if self._wrong_side_of_a_door():
                    continue
                stood = True
                dry = 0

            outcome = self.fight.run(name_id)
            self.say(f"    {outcome.value} ({have}/{need}) "
                     f"pressed {self.fight.pressed} closed {self.fight.closed} "
                     f"heals {self.fight.heals_landed}/{self.fight.heals_ignored}"
                     + (f" - {self.fight.detail}" if self.fight.detail else ""))

            if outcome is Fought.DIED:
                self.detail = "died on the objective"
                return Hunted.DIED
            if outcome is Fought.BLIND:
                return Hunted.BLIND
            if outcome is Fought.KILLED:
                self.kills += 1
                dry = 0
            elif outcome in (Fought.TOO_HURT, Fought.LOSING):
                if self._recover() is Rested.NO_FOOD:
                    self.detail = "out of food and too hurt to carry on"
                    return Hunted.NO_FOOD
            else:
                # Nothing here worth swinging at. Two empty looks and the camp has moved
                # on without us; go and stand somewhere else.
                dry += 1
                if dry >= DRY_LOOKS:
                    stood = False

        have, need = self.progress()
        self.detail = f"{timeout_s:.0f}s and the counter is {have}/{need}"
        return Hunted.TIMEOUT

    def _wrong_side_of_a_door(self) -> bool:
        """Is this station indoors when the camp is not?

        `pos.indoors` is a flag the strip already paints, so this is not knowledge about
        the Abbey — it is one bit that says a station cannot be the camp. The first
        station is the node itself and settles which kind of place this is.
        """
        v = self.read()
        if v is None:
            return False
        inside = v.get("pos.indoors")
        if self._outdoors is None:
            self._outdoors = inside is False
            return False
        if self._outdoors and inside is True:
            self.say("    indoors, and the camp is not; trying another station")
            return True
        return False

    def _recover(self) -> Rested:
        """Eat if it is a moment to eat. Interrupted is not a failure: it means something
        is already hitting us, and the next pass fights it."""
        outcome = self.rest.until(max(EAT_BELOW + 0.3, 0.9))
        self.say(f"    rest: {outcome.value}"
                 + (f" - {self.rest.detail}" if self.rest.detail else ""))
        return outcome
