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
from jev.world.combat import EAT_BELOW, HEAL_OUT_OF_COMBAT, Role, drink_to, for_class, rest_mana

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
# One spawn point is one mob, a named one, and not there it has been killed and is coming
# back: its spawn is waited at, a look every `LONE_LOOK_S`, until the hunt's own time is up.
# Goldtooth respawns in six minutes; the hunt looked four times in 90 s, called the disk
# empty, and the step was passed over (session 115).
LONE_LOOK_S = 10.0

# What to walk when a node does not say. **Not** the node's `r`: `r` is the tracker's
# arrival slop in map fractions, and a hunt that borrowed it walked a two-hundred-yard
# disk that contained Northshire Abbey, stood in the Main Hall facing a wall, and
# correctly reported that it could not see any kobolds. A wrong default is recoverable;
# reaching for a field that means something else is how that bug comes back.
DEFAULT_HUNT_YARDS = 30.0

# Health to take a pull at. The same band the top-up heals to, because arriving at a
# kobold below it is the thing the top-up exists to prevent.
PULL_LINE = HEAL_OUT_OF_COMBAT

# How long a hunt's place on its tour (`Place`) is kept for the same objective armed again:
# across the fight that cut its walk and the meal after it (V343). Of the 5,495 hunts the hive
# began again on the same objective after a walk cut by combat (4 Oct 16:26-18:30), the next
# began 34 s after at the median, 104 s at the 90th percentile, 5,462 within this.
PLACE_KEEP_S = 300.0


class Hunted(StrEnum):
    DONE = "done"                # the counter reached its requirement
    DIED = "died"
    NO_FOOD = "no_food"          # too hurt to continue and nothing to eat
    UNREACHABLE = "unreachable"  # could not stand anywhere on the disk
    CAMP = "camp"                # every station in or behind a death camp (V334)
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


# Spawn points closer than this to a station already on the tour add nothing to it: a plate
# shows about twenty yards off, and two stances ten yards apart see the same mobs.
SPAWN_MERGE_YARDS = 10.0
# Times round the spawn points before the camp is called empty. Mobs respawn - Northshire's
# Young Wolves in fifteen seconds - so a second lap finds what the first one killed.
SPAWN_LAPS = 2

# A mob's aggro reach at the character's own level (CMaNGOS `Creature::GetAttackDistance`:
# twenty yards, a yard more for each level it is above). Spawns closer together than this
# are one pull for a character that cannot pull from range.
PACK_YARDS = 20.0
# What each other spawn inside `PACK_YARDS` adds to a point's distance on the tour, so a
# lone spawn is walked to before a pack up to this much further off per packmate.
PACK_PENALTY_YARDS = 40.0
# A hostile spawn this close to one of the target's own is that spawn (`spawn_tour`'s others).
SAME_SPAWN_YARDS = 1.0


def spawn_stations(spawns) -> list[tuple[float, float, float]]:
    """`spawn_tour`, `SPAWN_LAPS` times round."""
    return spawn_tour(spawns) * SPAWN_LAPS


def spawn_tour(spawns, others=()) -> list[tuple[float, float, float]]:
    """Where the target actually spawns, as a walk: lone spawns before packs, each step to
    the nearest point left once its packmates are counted, from the cluster's centre (the
    generator lists it first). A packmate is any unit that attacks on sight (`others`, the
    hostile spawns round the hunt, V284), not only another of the target: at Patrolling
    Westfall's Riverpaw camp, 29 hostile spawns within 60 yards of the objective, a Mongrel
    alone among its own kind stood among Herbalists and Brutes, and the level 12 mage died
    there three times in 35 minutes (sessions 262-266).

    Rings round a centre stand where the mobs may not be. Northshire's Young Wolves spawn
    24 to 170 yards from their cluster's centre, and rings at 0, 13 and 31 yards looked 38
    times and found nothing (run 20260923T233909-8b1484).

    The centre is the pack's core, though. Elwynn's level 5-7 wolf camp opens on a spawn
    with three others inside twenty yards and seven inside thirty: the walk to it pulled
    four Mangy Wolves onto a level 6 paladin, which died at the first (run
    20260924T045140-ec8686). A pack is still walked, last, for a camp that is all pack.
    """
    left = [tuple(p) for p in spawns]
    # The target's own spawns are hostile too, and among `others` already.
    rest = [tuple(o[:3]) for o in others
            if all(math.dist(o[:2], p[:2]) > SAME_SPAWN_YARDS for p in left)]
    crowd = [sum(1 for q in (*left, *rest) if q is not p and math.dist(p[:2], q[:2]) < PACK_YARDS)
             for p in left]
    packed = [i for i, n in enumerate(crowd) if n >= 2]

    def cost(here, i: int) -> float:
        # Walking past a pack pulls it as surely as walking to it: the way from one lone
        # spawn to the next can cross a camp's core.
        through = sum(1 for j in packed if j != i
                      and _passes(here, left[i], left[j], PACK_YARDS / 2))
        return (math.dist(here[:2], left[i][:2])
                + PACK_PENALTY_YARDS * (crowd[i] + through))

    order = list(range(len(left)))
    tour: list[tuple[float, float, float]] = []
    here = left[0] if left else None
    while order:
        pick = min(order, key=lambda i: cost(here, i))
        order.remove(pick)
        point = left[pick]
        if all(math.dist(point[:2], t[:2]) > SPAWN_MERGE_YARDS for t in tour):
            tour.append(point)
            here = point
    return tour


def _clock(wall: float) -> str:
    """A wall time as the logs read it."""
    return time.strftime("%H:%M:%S", time.localtime(wall))


def _passes(a, b, c, reach: float) -> bool:
    """Does the straight walk from `a` to `b` come within `reach` of `c`?"""
    (ax, ay), (bx, by), (cx, cy) = a[:2], b[:2], c[:2]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / length2))
    return math.dist((ax + t * dx, ay + t * dy), (cx, cy)) < reach


@dataclass
class Place:
    """Where a hunt had got to on its tour, kept by the body for its objective (V343): the
    tour, its posts (laps in their learned order), the post walked to or stood at, the
    stations whose walk failed, what it fights and its kills and arrivals so far. A hunt of
    the same tour armed again within `PLACE_KEEP_S` goes on from it - after the fight that cut
    its walk, the meal after the fight - instead of drawing a new tour from its head.

    In the hive a combat-cut walk ended 6,549 of 17,234 hunts (4 Oct 16:26-18:30). Of the
    5,495 begun again on the same objective, 3,460 walked first to the station the cut hunt
    had begun at and 2,219 to one already stood at in the lap; 5,917 walks went back to
    stations stood at, 25.0 of the 84.2 hours those hunts walked."""

    tour: tuple = ()
    posts: list = field(default_factory=list)
    post: int = 0                          # the post walked to or stood at, not yet left
    lap: int = 1                           # posts a lap
    failed: set = field(default_factory=set)
    name: object = None                    # what it fights, once widened (V337)
    widened: bool = False
    kills: int = 0
    arrived: int = 0
    at: float | None = None                # when the hunt last went on, on the hunt's clock

    def fresh(self, tour, now: float) -> bool:
        """Is this the place of a hunt of `tour` that went on within `PLACE_KEEP_S`, posts
        left to walk?"""
        return (bool(self.posts) and self.tour == tuple(tuple(p) for p in tour)
                and self.at is not None and 0.0 <= now - self.at < PLACE_KEEP_S
                and self.post < len(self.posts))

    def begin(self, tour, posts: list, lap: int) -> None:
        self.clear()
        self.tour, self.posts, self.lap = tuple(tuple(p) for p in tour), posts, max(1, lap)

    def clear(self) -> None:
        self.tour, self.posts, self.post, self.lap = (), [], 0, 1
        self.failed = set()
        self.name, self.widened, self.kills, self.arrived, self.at = None, False, 0, 0, None

    def resume(self, here, walkable: Callable[[tuple], bool]) -> int:
        """The post to go on from: of the lap's posts not yet left, the one nearest `here`
        that `walkable` takes, moved up to be next; the posts left this lap are not walked
        again. The post it was walking to when the walk was cut is among them, so the walk
        is taken up again from where the character stands, unless another is nearer."""
        end = min(len(self.posts), (self.post // self.lap + 1) * self.lap)
        free = [i for i in range(self.post, end) if walkable(tuple(self.posts[i]))]
        if here is not None and free:
            pick = min(free, key=lambda i: math.dist(self.posts[i][:2], here[:2]))
            self.posts[self.post], self.posts[pick] = self.posts[pick], self.posts[self.post]
        return self.post


# How a hunt ends that leaves nothing to go on from: the next begins its tour afresh (V343).
ENDED = frozenset({Hunted.DONE, Hunted.DIED, Hunted.UNREACHABLE, Hunted.TIMEOUT, Hunted.CAMP,
                   Hunted.NO_FOOD})


@dataclass
class Hunt:
    fight: Fight
    rest: Rest
    read: Callable[[], dict | None]
    approach: Callable[..., bool]
    progress: Callable[[], tuple[int | None, int | None]]
    say: Callable[[str], None] = print
    loot: Loot | None = None
    is_complete: Callable[[], bool | None] | None = None
    service_needed: Callable[[], str | None] | None = None
    sleep: Callable[[float], None] = time.sleep
    # Which station next, by what each has yielded before (`jev.learn.choices.Stations`);
    # `None` walks the tour in its own order.
    stations: object | None = None
    # How far short of each station the hunt stands: a caster's reach, not its feet (V167).
    standoff_yards: float = 0.0
    # After each meal: a caster makes its water and food (`LiveBody._conjure`, V166). The
    # hunt drinks before most pulls, and a stock made only after the policy's rests ran
    # out mid-hunt (review, 25 September).
    conjure: Callable[[], None] | None = None
    # When the death camp a station lies in ends, as wall time, `None` for one in none
    # (`RouteMemory.camp_at`, V307): such a station is not walked to (V334).
    camp_until: Callable[[tuple], float | None] | None = None
    # How the last walk that did not arrive went: whether a route was planned for it, and the
    # end of the death camp it was refused through, if it was (V334); `None`, not known.
    walk_note: Callable[[], tuple[bool, float | None]] | None = None
    # A grind rib's wider prey (`jev.clients.fight.Kinds`, V337): what its hunt fights after
    # its laps went by with nothing killed, for as many laps more; `None`, nothing wider.
    widen: Callable[[], object | None] | None = None
    # The objective's (have, need, complete) from a reading the hunt has just taken (V338);
    # `None` asks `progress` and `is_complete`, each of which reads again.
    observe: Callable[[dict | None], tuple[int | None, int | None, bool | None]] | None = None
    # Where the last hunt of this objective got to (`Place`, V343), kept by the body; `None`
    # begins every hunt at its tour's head. `where` is the character's world position, for
    # the post a resumed hunt goes on from; `None`, the post it was at.
    place: Place | None = None
    where: Callable[[], tuple | None] | None = None
    # Why the next pull is not taken, the gear's repair coming first (V393), from the pass's
    # reading; `None` pulls. Asked after the meal before the pull, which the service check at
    # the top of the pass is not: a repair waits for a meal (V259), and a character losing
    # fights with a broken weapon was hurt at every pass, ate, and pulled again.
    pull_refused: Callable[[dict | None], str | None] | None = None
    clock: Callable[[], float] = time.monotonic
    resumed: bool = field(default=False, init=False)
    _found: bool = field(default=False, init=False)

    kills: int = field(default=0, init=False)
    _outdoors: bool | None = field(default=None, init=False)
    moves: int = field(default=0, init=False)
    arrived: int = field(default=0, init=False)          # stations stood at
    detail: str = field(default="", init=False)
    # For `CAMP`, when the first camp in the way ends (wall time): the step waits for it (V334).
    until: float | None = field(default=None, init=False)
    # `UNREACHABLE` with no route planned to any station: the character never moved (V335).
    stuck: bool = field(default=False, init=False)
    widened: bool = field(default=False, init=False)     # fought wider after dry laps (V337)

    @traced("hunt")
    def run(self, centre: tuple[float, float, float], radius_yards: float,
            name_id: int | None = None, *, timeout_s: float = 900.0,
            spawns=(), others=(), also=(), also_first: bool = False) -> Hunted:
        """Hunt round `centre` for `name_id` until the objective is done; `spawns` its own
        creature's spawn points, `others` the hostile ones round them, and `also` the spawn
        points of the other held quests' creatures round the hunt (V363), toured after its own
        or, `also_first`, before them."""
        self._found = False
        try:
            outcome = self._hunt(centre, radius_yards, name_id, timeout_s=timeout_s,
                                 spawns=spawns, others=others, also=also,
                                 also_first=also_first)
            if self.place is not None and outcome in ENDED:
                self.place.clear()           # nothing to go on from: the next begins afresh
            return outcome
        finally:
            if self.place is not None and self.place.posts:
                # However it ended - a walk cut by combat raises through here - the place
                # is as it got to, kept from now (V343).
                self.place.kills, self.place.arrived = self.kills, self.arrived
                self.place.at = self.clock()
            if self.stations is not None:
                if self._dead():
                    # Killed at it, whether in the hunt's own fight or after it, the hunt
                    # cancelled: a station to stand at less (V274).
                    self.stations.died()
                else:
                    self.stations.leave(self._found)     # the station it was at, however it ended

    def _dead(self) -> bool:
        try:
            values = self.read() or {}
        except Exception:
            return False
        return values.get("vitals.dead") is True or values.get("vitals.ghost") is True

    def _hunt(self, centre, radius_yards, name_id, *, timeout_s, spawns, others=(), also=(),
              also_first=False) -> Hunted:
        self.kills = self.moves = self.arrived = 0
        self._outdoors = None
        self.detail = ""
        self.until = None
        self.stuck = self.widened = False
        # The kind's own name id, a grind's pull for experience included (`Paying`, V344): the
        # station learning reads its objective from it (`choices.backfill_hunts`).
        event("hunt.request", data={"centre": centre, "radius_yards": radius_yards,
                                    "wanted_name_id": getattr(name_id, "own", name_id),
                                    "timeout_s": timeout_s, "spawns": len(spawns),
                                    "also": len(also)})
        deadline = time.monotonic() + timeout_s
        # Where the target spawns when the guide knows it; rings round the centre when not.
        # Each lap's order is learned, when there is a choice to learn (`stations`).
        whole, laps = ((spawn_tour(spawns, others), SPAWN_LAPS) if spawns
                       else (stations(centre, radius_yards), 1))
        lone = len({tuple(p) for p in spawns}) == 1
        if also and not lone:
            # The other held quests' creatures' spawns round it (V363), each a station not
            # already stood at: after the step's own, or before a grind's own creature. A lone
            # spawn, a named one, is waited at as ever.
            extra = [p for p in spawn_tour(also, others)
                     if all(math.dist(p[:2], q[:2]) > SPAWN_MERGE_YARDS for q in whole)]
            if extra:
                whole, laps = (extra + whole if also_first else whole + extra), SPAWN_LAPS
        tour, camps = self._out_of_camps(whole)
        if not tour:
            # "Deaths" in its detail: a quest step failed over for it grinds a level, as one
            # whose deaths failed it does, not the short rib it would retry from (V334).
            self.until = min(camps)
            self.detail = (f"every station lies in a death camp, held by deaths at its level "
                           f"until {_clock(self.until)}")
            return Hunted.CAMP
        chooser = self.stations if len({tuple(p) for p in tour}) > 1 else None
        clear = {tuple(p) for p in tour}           # out of every death camp now (V334)
        # Stations whose walk failed, not walked to again this hunt (V334), and the ends of
        # the death camps such walks were refused through.
        failed: set[tuple] = set()
        place = self.place
        self.resumed = place is not None and place.fresh(whole, self.clock())
        if self.resumed:
            # The same objective's hunt, cut short - its walk by a fight, or handed back for a
            # meal or a service - goes on where it got to (V343): its lap, the stations it
            # failed to reach, what it fights, from the post nearest the character.
            posts, failed = place.posts, place.failed
            here = self.where() if self.where is not None else None
            post = place.resume(here, lambda p: p in clear and p not in failed)
            self.kills, self.arrived = place.kills, place.arrived
            if place.widened:
                name_id, self.widened = place.name, True
            event("hunt.resumed", data={"post": post, "posts": len(posts), "lap": place.lap,
                                        "destination": list(posts[post]) if post < len(posts)
                                        else None, "here": list(here) if here else None})
        else:
            posts = ([p for _ in range(laps) for p in chooser.order(tour)] if chooser is not None
                     else tour * laps)
            post = 0
            if place is not None:
                place.begin(whole, posts, len(tour))
                place.failed = failed
        dry = 0
        stood = False
        close = False                # a lone spawn looked for from its own spot (V221)
        walks_failed = planned = 0
        refused: list[float] = []

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

            # One reading a pass (V338): the grind's level and the quest's counter are in the
            # one just taken, where its readers each read again - three readings between a
            # fight's end and the next look, 1.86 s at the median in the hive (29 Sep).
            if self.observe is not None:
                have, need, complete = self.observe(v)
            else:
                have, need = self.progress()
                complete = (self.is_complete() if self.is_complete is not None else
                            need is not None and have is not None and have >= need)
            event("objective.observed", data={"have": have, "need": need,
                                               "complete": complete})
            walked = not stood
            if complete is True:
                self.say(f"  objective complete: {have}/{need}")
                return Hunted.DONE

            if self.service_needed is not None:
                # The policy's services own full bags (V217): a merchant visit that found
                # nothing to sell is not asked for again until the bags change, and the hunt
                # goes on without the loot. Handed back regardless, session 186 asked 583
                # times in a row, "bags are full; service before the next pull", until the
                # watchdog failed the step over.
                if reason := self.service_needed():
                    self.detail = reason
                    return Hunted.SERVICE_NEEDED
            elif v is not None and v.get("bags.free") == 0 and v.get("vitals.combat") is False:
                self.detail = "bags are full; service before the next pull"
                return Hunted.BAGS_FULL

            if not stood:
                # A station whose walk failed is passed on the next lap (V334): bot 224 asked
                # for the same 12 refused walks lap after lap, 4,879 in one session.
                # A resumed hunt's posts were drawn before the camps it now sees (V343).
                while post < len(posts) and (tuple(posts[post]) in failed
                                             or tuple(posts[post]) not in clear):
                    post += 1
                if post >= len(posts):
                    if not self.arrived and refused and len(refused) == walks_failed:
                        # Every walk refused through a death camp (V307): the step waits for
                        # the first of them to end, not walked again at once (V334).
                        self.until = min((*refused, *camps))
                        self.detail = (f"every walk to a station is refused through a death "
                                       f"camp, held by deaths at its level until "
                                       f"{_clock(self.until)}")
                        return Hunted.CAMP
                    wider = self._wider(laps)
                    if wider is not None:
                        # Its laps with nothing killed: another as many, fighting wider (V337).
                        name_id = wider
                        posts += ([p for _ in range(laps) for p in chooser.order(tour)]
                                  if chooser is not None else tour * laps)
                        if place is not None:
                            place.name, place.widened = wider, True
                        continue
                    # Not a yard walked: no route to any station could be planned (V335).
                    self.stuck = not self.arrived and walks_failed > 0 and not planned
                    self.detail = ("no route to any station could be planned" if self.stuck
                                   else "walked the whole disk and found nothing to fight")
                    return Hunted.UNREACHABLE
                target = posts[post]
                if place is not None:
                    place.post = post                # walked to, then stood at (V343)
                post += 1
                self.moves += 1
                if chooser is not None:
                    chooser.leave(self._found)       # the last station's visit, as it went
                    self._found = False
                    chooser.walking()                # a visit's walk is its cost too (V310)
                with operation("hunt.approach", data={"destination": target}) as span:
                    standoff = 0.0 if close else self.standoff_yards
                    arrived = (self.approach(target, stop_short=standoff)
                               if standoff else self.approach(target))
                    span.finish(code="true" if arrived else "false")
                if not arrived:
                    failed.add(tuple(target))
                    walks_failed += 1
                    note = self.walk_note() if self.walk_note is not None else None
                    if note is not None and note[1] is not None:
                        refused.append(note[1])
                    planned += note is None or note[0] is True     # unknown: it may have walked
                    continue          # a station we cannot stand on is not a dead end
                if self._wrong_side_of_a_door():
                    continue
                if chooser is not None:
                    # A visit counts from the station itself: a walk cut short on the way -
                    # a fight, a wall - says nothing of what stands there. 42 of 49 visits
                    # were scored lost in sessions 205-217, most of them never arrived (V252).
                    chooser.arrive(target)
                stood = True
                self.arrived += 1
                dry = 0

            # Before the next plate, not after selected outcomes. Topping up only after
            # a kill or a break-off meant a `timeout` fell through the dry-look branch
            # and the next mob was pulled at whatever health the last fight left - which
            # is how a run reported `top up 0` and died without killing anything.
            # The pass's reading, when it walked nowhere since (V338).
            if not self._ready_to_pull(None if walked else v):
                self.detail = "too hurt to pull, and nothing left to fix it with"
                return Hunted.NO_FOOD
            if self.pull_refused is not None and (reason := self.pull_refused(v)):
                self.detail = reason
                return Hunted.SERVICE_NEEDED

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
                if chooser is not None:
                    chooser.died()               # a station to stand at less (V274)
                self.detail = "died on the objective"
                return Hunted.DIED
            stopped = {Fought.BLIND: Hunted.BLIND, Fought.REFUSED: Hunted.REFUSED,
                       Fought.INTERRUPTED: Hunted.INTERRUPTED}.get(outcome)
            if stopped is not None:
                self.detail = self.fight.detail
                return stopped
            if outcome is Fought.HELD:
                continue                    # the unit attacking next, the held one after (V287)
            if outcome is Fought.KILLED:
                self.kills += 1
                self._found = True
                dry = 0
                stopped = self._loot()
                if stopped is not None:
                    return stopped
            else:
                # Nothing here worth swinging at. Two empty looks and the camp has moved
                # on without us; go and stand somewhere else.
                dry += 1
                if lone and self.standoff_yards and not close and dry >= DRY_LOOKS:
                    # Not seen from a caster's stand-off, which walks nothing when already
                    # within it and so never faces the spawn: from the spawn's own spot,
                    # walked to (V221). The mage stood 18 yards from Garrick Padfoot with
                    # its back to him for two sessions, "no nameplate", his plate at the
                    # screen's edge by the shack.
                    close, stood = True, False
                elif lone:
                    self.sleep(LONE_LOOK_S)
                elif dry >= DRY_LOOKS:
                    stood = False

        have, need = self.progress()
        self.detail = f"{timeout_s:.0f}s and the counter is {have}/{need}"
        return Hunted.TIMEOUT

    def _wider(self, laps: int):
        """What a dry rib fights from now on (`widen`, V337), once a hunt, after its laps stood
        at a station and killed nothing; `None` for nothing wider. Grind ribs hunt one kind
        and fight anything else only when it attacks first (`Fight._acceptable`), and in the
        hive's crowded ribs logs show 14-station tours of "no nameplate and no Tab target
        worth fighting" (`184.log`, `192.log`; 38-40 bots on one rib at the peak, 29 Sep)."""
        if self.widen is None or self.widened or self.kills or not self.arrived:
            return None
        wider = self.widen()
        if not wider:
            return None
        self.widened = True
        names = sorted(getattr(wider, "names", ()))
        event("hunt.widened", data={"laps": laps, "names": names,
                                    "levels": [getattr(wider, "low", None),
                                               getattr(wider, "high", None)]})
        self.say(f"    {laps} lap(s) and nothing worth fighting: any of {len(names)} more kinds "
                 f"at levels {getattr(wider, 'low', '?')}-{getattr(wider, 'high', '?')}")
        return wider

    def _out_of_camps(self, tour) -> tuple[list, list[float]]:
        """The tour without its stations in a death camp, and when each such camp ends (V334).
        A camp is where the character died twice in ten minutes at its level, and of the
        hive's walks that arrived at a station inside one, 6.1% ended in a death near it
        within three minutes, 1.0% elsewhere (03:00-09:30 on 29 Sep)."""
        if self.camp_until is None:
            return list(tour), []
        kept, camps = [], []
        for point in tour:
            until = self.camp_until(tuple(point))
            if until is None:
                kept.append(point)
            else:
                camps.append(until)
        if camps:
            event("hunt.camped", data={"stations": len(camps), "kept": len(kept),
                                       "until": min(camps)})
        return kept, camps

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
                                name_id=self.fight.killed_name_id,
                                far=getattr(self.fight, "ended_far", False))
        if outcome is not Looted.NO_CORPSE:
            self.say(f"    loot: {outcome.value}"
                     + (f" - {self.loot.detail}" if self.loot.detail else ""))
        stopped = {Looted.BLIND: Hunted.BLIND, Looted.REFUSED: Hunted.REFUSED,
                   Looted.INTERRUPTED: Hunted.INTERRUPTED, Looted.BAGS_FULL: Hunted.BAGS_FULL,
                   Looted.WINDOW_OPEN: Hunted.WINDOW_OPEN}.get(outcome)
        if (stopped is Hunted.BAGS_FULL and self.service_needed is not None
                and not self.service_needed()):
            return None                      # no merchant can make room: hunt on (V217)
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

    def _ready_to_pull(self, values: dict | None = None) -> bool:
        """Heal, then eat, until fit to take the next pull. `False` means do not pull.

        The whole between-engagements contract in one place: in combat there is nothing
        to decide, and out of combat the order is heal (mana and seconds), then food
        (twenty seconds), then refuse. Pulling below the line with neither available is
        how a character arrives at a kobold already half dead. `values`, a reading just
        taken, is looked at instead of another (V338).
        """
        v = values if values is not None else self.read()
        if v is None:
            return True                      # unreadable is not a reason to stand still
        if v.get("vitals.combat") is True:
            return True                      # already in it; Fight decides
        # A caster's mana is its damage: below its line it drinks before the pull (V164).
        mana = v.get("vitals.power")
        if (for_class(v.get("char.class_id"), v.get("char.race_id")).caster
                and v.get("vitals.power_type") in (0, None)             # 0: mana
                and isinstance(mana, (int, float))
                and mana < ((self.fight.mana_line() if hasattr(self.fight, "mana_line") else None)
                            or rest_mana(True))):
            drank = self.rest.until(drink_to(True), role=Role.DRINK)
            self.say(f"    drink: {drank.value}"
                     + (f" - {self.rest.detail}" if self.rest.detail else ""))
            if drank is Rested.INTERRUPTED:
                return True                  # something is hitting us; fight it
            if self.conjure is not None:
                self.conjure()
            v = self.read() or v
        if hasattr(self.fight, "buff_up"):
            self.fight.buff_up()              # a caster's lasting buffs, before the pull (V176)
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
        if outcome is not Rested.INTERRUPTED and self.conjure is not None:
            self.conjure()
        return outcome
