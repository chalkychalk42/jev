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
from jev.clients.loot import Loot, Looted
from jev.clients.rest import Rest, Rested
from jev.run.evidence import event, operation, traced
from jev.world.combat import EAT_BELOW, HEAL_OUT_OF_COMBAT

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

# Health to take a pull at. The same band the top-up heals to, because arriving at a
# kobold below it is the thing the top-up exists to prevent.
PULL_LINE = HEAL_OUT_OF_COMBAT


class Hunted(StrEnum):
    DONE = "done"                # the counter reached its requirement
    DIED = "died"
    NO_FOOD = "no_food"          # too hurt to continue and nothing to eat
    UNREACHABLE = "unreachable"  # could not stand anywhere on the disk
    TIMEOUT = "timeout"
    BLIND = "blind"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"
    WINDOW_OPEN = "window_open"
    BAGS_FULL = "bags_full"      # hand the same guide step back to the service policy
    SERVICE_NEEDED = "service_needed"

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
    loot: Loot | None = None
    is_complete: Callable[[], bool | None] | None = None
    service_needed: Callable[[], str | None] | None = None

    kills: int = field(default=0, init=False)
    _outdoors: bool | None = field(default=None, init=False)
    moves: int = field(default=0, init=False)
    detail: str = field(default="", init=False)

    @traced("hunt")
    def run(self, centre: tuple[float, float, float], radius_yards: float,
            name_id: int | None = None, *, timeout_s: float = 900.0) -> Hunted:
        self.kills = self.moves = 0
        self._outdoors = None
        self.detail = ""
        event("hunt.request", data={"centre": centre, "radius_yards": radius_yards,
                                    "wanted_name_id": name_id, "timeout_s": timeout_s})
        deadline = time.monotonic() + timeout_s
        posts = stations(centre, radius_yards)
        post = 0
        dry = 0
        stood = False

        while time.monotonic() < deadline:
            # A ghost cannot fight, heal or eat, and every skill below reports something
            # that sounds like a camp problem instead. A live run died to the wolves and
            # then spent the rest of its window saying `no_target` ten times over,
            # because the death check lived inside the fight loop - which a ghost never
            # reaches, since acquiring a target fails first.
            v = self.read()
            if v is not None and (v.get("vitals.dead") is True
                                  or v.get("vitals.ghost") is True):
                self.detail = "dead; nothing here can be done until that is fixed"
                return Hunted.DIED

            have, need = self.progress()
            complete = (self.is_complete() if self.is_complete is not None else
                        need is not None and have is not None and have >= need)
            event("objective.observed", data={"have": have, "need": need,
                                               "complete": complete})
            if complete is True:
                self.say(f"  objective complete: {have}/{need}")
                return Hunted.DONE

            if v is not None and v.get("bags.free") == 0 and v.get("vitals.combat") is False:
                self.detail = "bags are full; service before the next pull"
                return Hunted.BAGS_FULL
            if self.service_needed is not None and (reason := self.service_needed()):
                self.detail = reason
                return Hunted.SERVICE_NEEDED

            if not stood:
                if post >= len(posts):
                    self.detail = "walked the whole disk and found nothing to fight"
                    return Hunted.UNREACHABLE
                target = posts[post]
                post += 1
                self.moves += 1
                with operation("hunt.approach", data={"destination": target}) as span:
                    arrived = self.approach(target)
                    span.finish(code="true" if arrived else "false")
                if not arrived:
                    continue          # a station we cannot stand on is not a dead end
                if self._wrong_side_of_a_door():
                    continue
                stood = True
                dry = 0

            # Before the next plate, not after selected outcomes. Topping up only after
            # a kill or a break-off meant a `timeout` fell through the dry-look branch
            # and the next mob was pulled at whatever health the last fight left - which
            # is how a run reported `top up 0` and died without killing anything.
            if not self._ready_to_pull():
                self.detail = "too hurt to pull, and nothing left to fix it with"
                return Hunted.NO_FOOD

            outcome = self.fight.run(name_id)
            event("fight.summary", code=outcome.value, detail=self.fight.detail,
                  data={"pressed_slots": self.fight.pressed, "closed": self.fight.closed,
                        "heals_landed": self.fight.heals_landed,
                        "heals_ignored": self.fight.heals_ignored})
            self.say(f"    {outcome.value} ({have}/{need}) "
                     f"pressed {self.fight.pressed} closed {self.fight.closed} "
                     f"heals {self.fight.heals_landed}/{self.fight.heals_ignored}"
                     + (" [broken gear]" if self.fight.broken else "")
                     + (f" - {self.fight.detail}" if self.fight.detail else ""))

            if outcome is Fought.DIED:
                self.detail = "died on the objective"
                return Hunted.DIED
            stopped = {Fought.BLIND: Hunted.BLIND, Fought.REFUSED: Hunted.REFUSED,
                       Fought.INTERRUPTED: Hunted.INTERRUPTED}.get(outcome)
            if stopped is not None:
                self.detail = self.fight.detail
                return stopped
            if outcome is Fought.KILLED:
                self.kills += 1
                dry = 0
                stopped = self._loot()
                if stopped is not None:
                    return stopped
            else:
                # Nothing here worth swinging at. Two empty looks and the camp has moved
                # on without us; go and stand somewhere else.
                dry += 1
                if dry >= DRY_LOOKS:
                    stood = False

        have, need = self.progress()
        self.detail = f"{timeout_s:.0f}s and the counter is {have}/{need}"
        return Hunted.TIMEOUT

    def _loot(self) -> Hunted | None:
        """Take what the corpse is holding, straight after the kill.

        Here rather than inside `Fight` because looting is not fighting: the corpse is
        not a target, an empty one is not a failure, and a full bag is a vendor problem.
        A great many quests are "bring me eight of these" and the eight come off corpses,
        so a kill counter can fill while the quest never does.
        """
        if self.loot is None:
            return
        # The counter is the first thing worth believing about a corpse, and `Hunt` is
        # what knows how to ask for it.
        outcome = self.loot.run(progress=self.progress, anchor=self.fight.last_plate,
                                name_id=self.fight.killed_name_id)
        if outcome is not Looted.NO_CORPSE:
            self.say(f"    loot: {outcome.value}"
                     + (f" - {self.loot.detail}" if self.loot.detail else ""))
        stopped = {Looted.BLIND: Hunted.BLIND, Looted.REFUSED: Hunted.REFUSED,
                   Looted.INTERRUPTED: Hunted.INTERRUPTED, Looted.BAGS_FULL: Hunted.BAGS_FULL,
                   Looted.WINDOW_OPEN: Hunted.WINDOW_OPEN}.get(outcome)
        if stopped is not None:
            self.detail = f"loot: {self.loot.detail}"
        return stopped

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

    def _ready_to_pull(self) -> bool:
        """Heal, then eat, until fit to take the next pull. `False` means do not pull.

        The whole between-engagements contract in one place: in combat there is nothing
        to decide, and out of combat the order is heal (mana and seconds), then food
        (twenty seconds), then refuse. Pulling below the line with neither available is
        how a character arrives at a kobold already half dead.
        """
        v = self.read()
        if v is None:
            return True                      # unreadable is not a reason to stand still
        if v.get("vitals.combat") is True:
            return True                      # already in it; Fight decides
        hp = v.get("vitals.hp")
        if hp is None or hp >= PULL_LINE:
            return True

        if self._top_up():
            return True
        outcome = self._recover()
        if outcome is Rested.HEALTHY:
            return True
        if outcome is Rested.INTERRUPTED:
            return True                      # something is hitting us; fight it
        after = self.read()
        return after is not None and (after.get("vitals.hp") or 0.0) >= PULL_LINE

    def _top_up(self) -> bool:
        """Heal between fights, before reaching for food.

        This is the step that was missing. A heal in a fight is a global cooldown not
        spent swinging and it cannot finish under pushback anyway; between fights it
        costs mana and a few seconds, and going into the next pull at 80% rather than
        45% is the difference between winning it and a corpse run.
        """
        healed = self.fight.top_up()
        if self.fight.top_ups:
            self.say(f"    top up: {self.fight.top_ups_landed}/{self.fight.top_ups} landed"
                     + ("" if healed else " - still short, eating instead"))
        return healed

    def _recover(self) -> Rested:
        """Eat if it is a moment to eat. Interrupted is not a failure: it means something
        is already hitting us, and the next pass fights it."""
        outcome = self.rest.until(max(EAT_BELOW + 0.3, 0.9))
        self.say(f"    rest: {outcome.value}"
                 + (f" - {self.rest.detail}" if self.rest.detail else ""))
        return outcome
