"""One live client, wired up. The composition, in one place.

`tools/probe_slice.py` grew this by accretion — window, capture, a decoded read, a frame
read, a position read, an assembled quest log, a follower, and a planner-backed approach —
and it was right to. Every one of them is needed to do anything at all.

It lives here because the second thing that needs it is a second probe, and copying ninety
lines of wiring is how two clients start disagreeing about what "read the strip" means.
Nothing here is a skill: no clicking, no walking decisions, no graph. It hands out the
readers and the one composed action — *stand on a world point* — that every skill above
needs and none of them should know how to build.

The readers are plain callables rather than an interface, because that is what the skills
already take, and it keeps them testable with a lambda.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jev.clients import operator, win32
from jev.clients.capture import Backend, WindowCapture
from jev.clients.hid import Hid, Humaniser
from jev.clients.source import blind
from jev.clients.travel import Outcome, Travel
from jev.guide.coords import (
    ZoneBounds,
    bounds_by_radio_id,
    map_to_world,
    names_by_radio_id,
    world_to_map,
)
from jev.guide.path import PathQuery, PathStatus
from jev.guide.route_memory import AvoidingQuery, DangerAvoidingQuery
from jev.perceive import radio_frame
from jev.perceive.questlog import QuestLog
from jev.perceive.spellbook import SpellCensus
from jev.world.state_v1 import Pos, SenseFault

# Taking the window back: short waits first, doubling, capped.
#
# A flat twenty attempts two seconds apart is forty seconds of standing still inside a
# run, and most focus losses clear on the first try. The long patience is still available
# to whoever is starting up, where a Windows notification panel can genuinely hold the
# foreground for half a minute.
FOCUS_FIRST_WAIT_S = 0.25
FOCUS_MAX_WAIT_S = 4.0
FOCUS_PATIENCE_S = 40.0        # starting up
FOCUS_QUICK_S = 6.0            # mid-run, where standing still costs stations

# How long the strip may repeat a sequence number before it counts as frozen.
#
# Generous, because repeats are normal: the addon paints on a timer and this captures
# faster than it paints, so the same frame is read several times over. Frozen is a
# different thing entirely — the addon has stopped, usually on a Lua error — and it is
# worth naming because **a frozen strip is indistinguishable from a pinned character**.
# Both look like a position that never changes. An hour went into terrain that was never
# the problem before anyone looked at `seq`.
STALE_AFTER_S = 4.0

# Heights to start a plan at, the radio painting none: the ground where the last walk ended
# when that is near, then the destination's height, then either side of it. From beside
# Northshire's merchant wagons the destination's height snapped the start onto a wagon, and
# the planner answered with five yards of partial path that the walk then called arrival;
# four yards lower it answers with the whole 164-yard route (run 20260924T013702-7f5692).
# Then outward in four-yard steps: on Echo Ridge Mine's wooden platform the grind below
# was 44 yards down, and only a start between 88 and 96 was on the platform's mesh.
START_HEIGHTS = (0.0, -3.0, 3.0, -6.0, 6.0, -10.0,
                 *(sign * dz for dz in range(12, 61, 4) for sign in (1, -1)))
GROUND_MEMORY_YARDS = 15.0
# The height is tracked along the navmesh as the character moves, the radio painting none.
# In a building with floors one x and y is two places: the Lion's Pride Inn's hall and the
# room over it. A plan started on the wrong one walked a character round the upper floor
# for four minutes above William Pestle, the NPC it wanted, and then five more failing to
# walk out to Marshal Dughan, and both hand-ins were passed over (session 95). Floors are
# yards apart and stairs are continuous, so the surface under each new position nearest
# the last height is the floor the character is on. A surface found beside the position
# rather than under it is an edge the character has left - a balcony it fell from - and
# the floor is looked for below. (Stairs over a walkable floor stay ambiguous: the lower
# floor is nearer, as the inn's landing over its hall is.)
TRACK_EVERY_S = 0.5
UNDER_YARDS = 1.0
LOWER_STEPS = (3.0, 6.0, 9.0, 12.0)
# On a route being walked, the route's own height is the better evidence: the inn's stairs
# rise over its hall, where the hall is always the nearer surface, and continuity alone left
# a character walked up to the priest, rogue and mage trainers on the hall in 21 of 25
# samplings (review, 25 September). Within `ROUTE_YARDS` of the route its height is the
# reference; off it, continuity.
ROUTE_YARDS = 2.5
# Blocked where a plan already started, with other floors under the spot, the re-plan starts
# on one of those. Nothing paints a height, so the first plan's floor is a guess: sessions
# 110 and 111 began upstairs in the Lion's Pride Inn on plans from the hall below, and each
# re-plan, started at the height of the route being followed, walked the same hall route
# into the same upstairs walls for a whole session.
REPLAN_SPOT_YARDS = 8.0
# Indoors only: outdoors the other floors under a spot are tunnels, and above Fargodeep Mine
# a walk to a merchant was re-planned from the mine below the field (session 112). And only
# a storey away: the inn's floors are seven yards apart, and from the mine's tunnels the
# floor 25 yards up is the hill over them (session 117).
FLOOR_SWITCH_YARDS = 10.0
# An arrival is within this of the destination. A partial plan ends where the mesh does,
# and from somewhere no route leaves that is a few yards away: "arrived" there began a
# grind inside the inn and kept the hearthstone rule from counting the walk (session 110).
ARRIVED_NEAR_YARDS = 15.0


def _route_height(points, xy: tuple[float, float]) -> float | None:
    """The route's height at its point nearest `xy`, when that is within `ROUTE_YARDS`."""
    from jev.guide.route_memory import nearest_height

    near = nearest_height(points, xy)
    return near[1] if near is not None and near[0] <= ROUTE_YARDS else None
# A walk's limit grows with the route planned for it: twice the time at running pace, up to
# just under TRAVEL_TO's own 600 s. A flat 180 s was about the clean time for the 1,038
# yards between Northshire and Gerard Tiller, so one stuck corner failed the step.
RUN_YARDS_PER_S = 7.0
WALK_SLACK = 2.0
MAX_WALK_S = 540.0


class NotRunning(RuntimeError):
    """No client window, or it would not come to the foreground."""


@dataclass
class Client:
    """Readers and one composed action for a single game window."""

    hwnd: int
    hid: Hid
    cap: WindowCapture
    origin: tuple[int, int]
    size: tuple[int, int]
    client_id: str = "run"
    log: QuestLog = field(default_factory=QuestLog)
    # The main bar and the spellbook, assembled from their one-entry-per-paint censuses.
    spells: SpellCensus = field(default_factory=SpellCensus)
    _seq: int | None = field(default=None, init=False)
    _seq_at: float = field(default=0.0, init=False)
    _paint_generation: int = field(default=0, init=False)
    travel: Travel | None = field(default=None, init=False)
    query: PathQuery | None = field(default=None, init=False)
    # Spots where walking got stuck and the way round that worked (`RouteMemory`).
    route_memory: object | None = field(default=None, init=False)
    # How the last `approach` walk ended, for a caller that needs more than arrived or not.
    last_travel: object | None = field(default=None, init=False)
    # Yards the last walk brought the character nearer its destination, in a straight line,
    # and how far from it the walk began.
    last_headway: float | None = field(default=None, init=False)
    last_distance: float | None = field(default=None, init=False)
    bounds: ZoneBounds | None = field(default=None, init=False)
    coordinate_zones: dict[int, ZoneBounds] = field(default_factory=dict, init=False)
    coordinate_names: dict[int, str] = field(default_factory=dict, init=False)
    on_path: Callable[[str], None] | None = field(default=None, init=False)
    # Where the character is, with its height as last tracked (`TRACK_EVERY_S`).
    _ground: tuple[float, float, float] | None = field(default=None, init=False)
    _tracked_at: float = field(default=-math.inf, init=False)
    _following: tuple = field(default=(), init=False)     # the route being walked, if any
    # One window, one capture, one set of GDI handles. `WindowCapture` creates its device
    # context and bitmap once and reuses them, so two threads grabbing at the same time
    # tear each other's frame in half. The heartbeat samples on its own thread, so every
    # path that touches the capture takes this first.
    _capturing: threading.RLock = field(default_factory=threading.RLock, init=False,
                                        repr=False)

    # -- readers -------------------------------------------------------------

    def reading(self, tries: int = 6) -> radio_frame.RadioReading | None:
        """A whole decoded reading, or `None`. Feeds the quest log and the bar and
        spellbook censuses on the way past.

        `None` also means **frozen**, not only unreadable, and that is deliberate: every
        caller already treats `None` as "cannot see", which is the honest answer for a
        strip whose numbers stopped changing. Returning the last painted values would let
        a follower conclude the character is stuck when the addon is what stopped.
        """
        for _ in range(tries):
            with self._capturing:
                r = radio_frame.read(self.cap.grab().rgb)
                if r.ok:
                    self._note_seq(r.values.get("seq"))
                    if self.frozen_for() > STALE_AFTER_S:
                        return None
                    self.log.observe(r.values)
                    self.spells.observe(r.values)
                    return r
            time.sleep(0.05)
        return None

    def _note_seq(self, seq: int | None) -> None:
        now = time.monotonic()
        if seq != self._seq or self._seq_at == 0.0:
            self._seq, self._seq_at = seq, now
            self._paint_generation += 1

    def frozen_for(self) -> float:
        """Seconds since the strip's sequence number last advanced."""
        return 0.0 if self._seq_at == 0.0 else time.monotonic() - self._seq_at

    def read(self) -> dict | None:
        r = self.reading()
        return None if r is None else self._navigation_values(r.values)

    def _navigation_values(self, raw: dict) -> dict:
        """One transform for every body reader, state row, and corpse recovery.

        Raw zone identity remains intact. Only coordinates move into the pinned guide
        frame; an unknown zone or another continent supplies no usable position.
        """
        if self.bounds is None:
            return raw
        values = dict(raw)
        actual = self.coordinate_zones.get(raw.get("pos.zone_id"))
        values["pos.coord_zone_id"] = self.bounds.area_id
        for xkey, ykey in (("mx", "my"), ("corpse_mx", "corpse_my")):
            x, y = raw.get(f"pos.{xkey}"), raw.get(f"pos.{ykey}")
            values[f"pos.raw_{xkey}"], values[f"pos.raw_{ykey}"] = x, y
            converted = None
            missing_corpse = xkey == "corpse_mx" and (x, y) == (0, 0)
            if (actual is not None and actual.map_id == self.bounds.map_id
                    and x is not None and y is not None and not missing_corpse):
                if actual == self.bounds:
                    converted = x, y  # Preserve the measured same-map reader exactly.
                else:
                    world = map_to_world(x, y, actual)
                    if world is not None:
                        converted = world_to_map(*world, self.bounds)
            values[f"pos.{xkey}"], values[f"pos.{ykey}"] = converted or (None, None)
        return values

    def frame(self):
        try:
            with self._capturing:
                return self.cap.grab().rgb
        except Exception:
            return None

    def position(self) -> tuple[float, float] | None:
        v = self.read()
        if v is None or v.get("pos.mx") is None or v.get("pos.my") is None:
            return None
        self._track_height(v)
        return (v["pos.mx"], v["pos.my"])

    def _track_height(self, values: dict) -> None:
        """Keep `_ground` on the floor the character is on (`TRACK_EVERY_S`)."""
        if (values.get("flags.falling") is True or self.query is None or self.bounds is None
                or self._ground is None):
            return
        now = time.monotonic()
        if now - self._tracked_at < TRACK_EVERY_S:
            return
        self._tracked_at = now
        x, y = map_to_world(values["pos.mx"], values["pos.my"], self.bounds)
        if math.dist(self._ground[:2], (x, y)) > GROUND_MEMORY_YARDS:
            self._ground = None          # a hearth, a death or a gap: the height is unknown
            return
        reference = _route_height(self._following, (x, y))
        if reference is None:
            reference = self._ground[2]
        for drop in (0.0, *LOWER_STEPS):
            z = reference - drop
            snapped = self.query.path(self.bounds.map_id, (x, y, z), (x, y, z))
            # One point, the surface nearest: not a walkable route (`Path.usable`).
            if (snapped.status in (PathStatus.COMPLETE, PathStatus.PARTIAL) and snapped.points
                    and math.dist(snapped.points[0][:2], (x, y)) <= UNDER_YARDS):
                self._ground = (x, y, snapped.points[0][2])
                return

    def _height_near(self, w: tuple[float, float]) -> float | None:
        """The tracked height, when it was tracked near `w`."""
        if self._ground is not None and math.dist(self._ground[:2], w[:2]) <= GROUND_MEMORY_YARDS:
            return self._ground[2]
        return None

    def quest_ids(self, tries: int = 40) -> tuple[int, ...] | None:
        """The assembled log, or `None` while the cycle is still partial.

        A partial cycle is unread, never a short log — see `jev.perceive.questlog`.
        """
        for _ in range(tries):
            self.read()
            assembled = self.log.complete
            if assembled is not None:
                return tuple(q.quest_id for q in assembled)
            time.sleep(0.08)
        return None

    def state(self):
        """A `State` carrying the assembled log, which is what the tracker needs.

        `to_state` will not call one frame a log and is right not to, so the accumulated
        one is handed in. Without this the tracker sees an empty log on every tick.
        """
        with self._capturing:
            r = self.reading()
            if r is None:
                return None
            return self.state_from(r, captured_at=time.time())

    def state_from(self, reading, *, captured_at: float):
        """Assemble state from an owned observation without taking a second frame.

        Callers hold ``_capturing`` while updating/reading the assembled quest log.
        Both the ordinary tracker and visual action loop use the same map transform.
        """
        state = radio_frame.to_state(reading, t=captured_at, client_id=self.client_id,
                                     quests=self.log.complete)
        if self.bounds is None:
            return state
        values = self._navigation_values(reading.values)
        updates = {name: values.get(f"pos.{name}") for name in (
            "mx", "my", "corpse_mx", "corpse_my", "coord_zone_id",
            "raw_mx", "raw_my", "raw_corpse_mx", "raw_corpse_my")}
        updates["zone"] = self.coordinate_names.get(state.pos.zone_id)
        return state.model_copy(update={"pos": Pos.model_validate({
            **state.pos.model_dump(), **updates})})

    # -- the one composed action ---------------------------------------------

    def approach(self, world: tuple[float, float, float], *,
                 timeout_s: float = 180.0) -> bool:
        """Plan from here to a world point and follow it.

        The planner is the only thing that knows about terrain, and the skills above know
        only where to click. Nine yards of blind walking finds a fence the mesh had
        already routed around.
        """
        if self.travel is None or self.query is None or self.bounds is None:
            return False
        here = self.travel.position()
        if here is None:
            self._say("  cannot read a position")
            return False
        hw = map_to_world(here[0], here[1], self.bounds)
        path = self._plan(hw, world)
        self._say(f"  {path.status.value}: {len(path.points)} waypoints, "
                  f"{path.length_yards():.1f} yards")
        if not path.usable:
            return False
        if self._height_near(hw) is None:
            self._ground = (hw[0], hw[1], path.points[0][2])   # the plan's floor, tracked on
        timeout_s = max(timeout_s, min(MAX_WALK_S,
                                       path.length_yards() / RUN_YARDS_PER_S * WALK_SLACK))

        followed = [path]
        self._following = tuple(path.points)
        starts = [(hw[0], hw[1], path.points[0][2])]

        def replan(here_map):
            # The start's height is unknown (the radio paints map x/y). The destination's
            # snapped a character on Northshire Abbey's stone ledge - off the navmesh on
            # both sides of the back wall - onto the interior floor, and each re-plan was
            # a straight line into the wall: measured 23 September, 27 stuck events 8.2
            # yards from Marshal McBride. The route being followed got the character
            # there, so its nearest point's height is the side of the wall it is on.
            w = map_to_world(here_map[0], here_map[1], self.bounds)
            z = self._height_near(w)
            if z is None:
                z = min(followed[-1].points, key=lambda p: math.dist(p[:2], w[:2]))[2]
            # ...unless a plan already started near here, on that floor, and it is blocked
            # again: then another floor under the spot (`REPLAN_SPOT_YARDS`).
            tried = [s[2] for s in starts if math.dist(s[:2], w[:2]) <= REPLAN_SPOT_YARDS]
            if (any(abs(t - z) <= FLOOR_GAP for t in tried)
                    and (self.read() or {}).get("pos.indoors") is True):
                others = [f for f in surfaces_under(self.query, self.bounds.map_id, w[0], w[1])
                          if all(abs(f - t) > FLOOR_GAP for t in tried)
                          and abs(f - z) <= FLOOR_SWITCH_YARDS]
                if others:
                    z = min(others, key=lambda f: abs(f - z))
                    self._say(f"  blocked again here: planning from the floor at {z:.1f}")
            starts.append((w[0], w[1], z))
            planned = self.query.path(self.bounds.map_id, (w[0], w[1], z), world)
            if planned.usable:
                followed.append(planned)
                self._following = tuple(planned.points)
            return planned

        def reach(here_map):
            # The planner's walk from here to the destination, which an early arrival must
            # be near as well as the straight line (`travel.EARLY_WALK_YARDS`).
            w = map_to_world(here_map[0], here_map[1], self.bounds)
            z = self._height_near(w)
            if z is None:
                return None
            walk = self.query.path(self.bounds.map_id, (w[0], w[1], z), world)
            return walk.length_yards() if walk.status is PathStatus.COMPLETE else None

        try:
            result = self.travel.follow(path, timeout_s=timeout_s, replan=replan,
                                        memory=self.route_memory, reach=reach)
            if result.outcome is Outcome.REFUSED:
                # Nothing was pressed because the window was not focused - a notification
                # panel, or anything else that takes the foreground. `Hid` is right to
                # refuse, and `Travel` is right to say so rather than call it stuck, but
                # somebody has to take the window back. A live run made three kills and
                # then spent twelve stations refused, walking nowhere.
                self._say("  the window lost focus; taking it back")
                if self.focused(FOCUS_QUICK_S):
                    result = self.travel.follow(path, timeout_s=timeout_s, replan=replan,
                                                memory=self.route_memory, reach=reach)
        except BaseException:
            self._following = ()             # a walk given up is no route to track against
            raise
        ended = self.travel.position()
        self._following = ()
        arrived = result.outcome.value == "arrived"
        self.last_headway = None
        self.last_distance = math.dist(hw[:2], world[:2])
        if ended is not None:
            w = map_to_world(ended[0], ended[1], self.bounds)
            z = self._height_near(w)
            if z is None:
                z = min(followed[-1].points, key=lambda p: math.dist(p[:2], w[:2]))[2]
            self._ground = (w[0], w[1], z)
            short = math.dist(w[:2], world[:2])
            self.last_headway = math.dist(hw[:2], world[:2]) - short
            if arrived and short > ARRIVED_NEAR_YARDS:
                arrived = False
                self._say(f"  the route ended {short:.1f} yards from the destination")
        remaining = ("unknown" if result.remaining_yards is None
                     else f"{result.remaining_yards:.1f} yards")
        self._say(f"  {result.outcome.value}, {remaining} left, {result.turns} turns, "
                  f"{result.stuck_events} stuck"
                  + (f" - {result.detail}" if result.detail else ""))
        self.last_travel = result
        return arrived

    def plan_to(self, world: tuple[float, float, float]):
        """The plan `approach` would follow from here to `world`, without walking it."""
        if self.travel is None or self.query is None or self.bounds is None:
            return None
        here = self.travel.position()
        if here is None:
            return None
        return self._plan(map_to_world(here[0], here[1], self.bounds), world)

    def _plan(self, here: tuple[float, float], world: tuple[float, float, float]):
        """The first complete plan over `START_HEIGHTS`, else the partial one ending nearest."""
        heights = [world[2] + dz for dz in START_HEIGHTS]
        if (self._ground is not None
                and math.dist(self._ground[:2], here[:2]) <= GROUND_MEMORY_YARDS):
            heights.insert(0, self._ground[2])
        best = None
        for z in heights:
            path = self.query.path(self.bounds.map_id, (here[0], here[1], z), world)
            if path.usable and path.status is PathStatus.COMPLETE:
                return path
            if path.usable and (best is None or math.dist(path.points[-1][:2], world[:2])
                                < math.dist(best.points[-1][:2], world[:2])):
                best = path
        return best if best is not None else path

    def focused(self, patience_s: float = FOCUS_PATIENCE_S, *,
                checkpoint: Callable[[], None] | None = None) -> bool:
        """Bring the window forward, and say whether it actually came.

        Not assumed. A Windows notification panel holds the foreground and refuses to give
        it up, and `Hid` correctly declines to type into whatever is focused instead — so
        the caller needs to know the difference between "slow" and "blocked".
        """
        deadline = time.monotonic() + patience_s
        wait = FOCUS_FIRST_WAIT_S
        while True:
            if checkpoint is not None:
                checkpoint()
            if operator.suspected():
                # A person at the desk, or input that may be one (a click into another
                # window is one or two inputs): the window is theirs to give back.
                return win32.is_foreground(self.hwnd)
            if win32.focus(self.hwnd) and win32.is_foreground(self.hwnd):
                return True
            if time.monotonic() + wait >= deadline:
                return win32.is_foreground(self.hwnd)
            if checkpoint is None:
                time.sleep(wait)
            else:
                wake = time.monotonic() + wait
                while time.monotonic() < wake:
                    checkpoint()
                    time.sleep(min(0.05, max(0.0, wake - time.monotonic())))
            wait = min(FOCUS_MAX_WAIT_S, wait * 2)

    def close(self) -> None:
        if self.query is not None:
            self.query.close()
        self.cap.close()

    def _say(self, line: str) -> None:
        if self.on_path is not None:
            self.on_path(line)


def attach(client_id: str = "run", *, title: str = "World of Warcraft",
           backend: Backend = Backend.SCREEN) -> Client:
    """Find the window and wire it up. Raises `NotRunning` rather than returning `None`."""
    if not win32.available():
        raise NotRunning("this needs Windows Python; there is no window on this platform")
    try:
        hwnd = win32.game_window(title)
    except win32.GameWindowError as exc:
        raise NotRunning(str(exc)) from exc
    ox, oy, w, h = win32.client_rect(hwnd)
    return Client(
        hwnd=hwnd,
        hid=Hid(hwnd=hwnd, humaniser=Humaniser.for_client(client_id)),
        cap=WindowCapture(hwnd, backend=backend),
        origin=(ox, oy),
        size=(w, h),
        client_id=client_id,
    )


# Heights a spot's floors are looked for from, and how far apart two floors are. The
# planner answers the surface nearest the asked height, so a spread of asks finds each
# floor over a spot; one found beside it rather than under it is some other surface.
PROBE_HEIGHTS = (-40.0, 0.0, 40.0, 80.0, 120.0, 160.0, 200.0, 240.0, 280.0)
FLOOR_GAP = 3.0


def surfaces_under(query: PathQuery, map_id: int, x: float, y: float) -> list[float]:
    """The navmesh's floors under a spot, lowest first."""
    found: list[float] = []
    for z in PROBE_HEIGHTS:
        snapped = query.path(map_id, (x, y, z), (x, y, z))
        if (snapped.status in (PathStatus.COMPLETE, PathStatus.PARTIAL) and snapped.points
                and math.dist(snapped.points[0][:2], (x, y)) <= UNDER_YARDS):
            height = snapped.points[0][2]
            if all(abs(height - known) > FLOOR_GAP for known in found):
                found.append(height)
    return sorted(found)


def with_travel(client: Client, bounds: ZoneBounds, query: PathQuery, *,
                arrival_yards: float, say: Callable[[str], None] | None = None,
                zones: dict[int, ZoneBounds] | None = None,
                zone_names: dict[int, str] | None = None,
                route_memory=None) -> Client:
    """Give a client the ability to walk. Separate because reading needs no planner."""
    client.bounds = bounds
    client.route_memory = route_memory
    zones_path = str(Path(__file__).resolve().parents[2] / "data/zones-tbc-243.json")
    client.coordinate_zones = bounds_by_radio_id(zones_path) if zones is None else zones
    client.coordinate_names = names_by_radio_id(zones_path) if zone_names is None else zone_names
    # Every plan, first and re-plan, stays clear of the spots walking found blocked, and of
    # where the character recently died.
    client.query = (query if route_memory is None
                    else DangerAvoidingQuery(AvoidingQuery(query, route_memory), route_memory))
    if route_memory is not None:
        # Passages learned before heights were kept get their floor, once (`Passage.z`).
        route_memory.backfill(bounds.map_id,
                              lambda x, y: surfaces_under(query, bounds.map_id, x, y))
    client.on_path = say
    client.travel = Travel(hid=client.hid, bounds=bounds,
                           read_pos=client.position, arrival_yards=arrival_yards,
                           indoors=lambda: (client.read() or {}).get("pos.indoors") is True)
    return client


@dataclass
class ClientSource:
    """Expose the body's existing capture/log assembly through the runtime Source seam."""

    client: Client

    def read(self):
        try:
            state = self.client.state()
        except Exception:
            state = None
        if state is not None:
            return state
        fault = SenseFault.STALE if self.client.frozen_for() > STALE_AFTER_S else SenseFault.NOT_FOUND
        return blind(time.time(), self.client.client_id, fault)

    def close(self) -> None:
        self.client.close()
