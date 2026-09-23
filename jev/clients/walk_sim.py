"""A character that walks with keys through obstacles, on a virtual clock. No client.

`Travel` steers without a facing API, from motion it measures, so its behaviour near
trees, fences and walls depends on the whole loop - pulses, deadbands, the heading window,
unstick moves - and cannot be judged by reading it. This stands in for the client just
far enough to run the real `Travel` against the obstacles that stopped it live:

* keys held on the fake HID move the character at measured speeds (run 7 yd/s, back
  4.5 yd/s, strafe 7 yd/s, turn 134 deg/s - the seed `Travel` itself starts from);
* circles (trunks, pillars) and segments (walls, fences) block it, sliding along them as
  the client does; an obstacle lower than a jump is cleared while airborne;
* positions come back as map fractions through a zone box, so `Travel`'s own geometry
  is exercised unchanged;
* time is virtual: `Travel`'s `time.sleep` advances the world.

Coordinates here are map-yards: x east along the map's `mx`, y south along `my`, the
same frame `coords.heading_yards` measures in, where turning right increases a heading.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from jev.guide.coords import ZoneBounds, map_to_world

RUN_YD_S = 7.0
BACK_YD_S = 4.5
STRAFE_YD_S = 7.0
TURN_RAD_S = math.radians(134.0)
CHARACTER_RADIUS = 0.4
JUMP_AIRTIME_S = 0.75
JUMP_CLEARS = 1.0          # obstacles lower than this are cleared while airborne
SUBSTEP_S = 0.01


@dataclass(frozen=True)
class Circle:
    x: float
    y: float
    r: float
    height: float = 10.0


@dataclass(frozen=True)
class Segment:
    ax: float
    ay: float
    bx: float
    by: float
    thickness: float = 0.2
    height: float = 10.0


def _closest_on_segment(px, py, s: Segment) -> tuple[float, float]:
    dx, dy = s.bx - s.ax, s.by - s.ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - s.ax) * dx + (py - s.ay) * dy) / length2))
    return s.ax + t * dx, s.ay + t * dy


@dataclass
class WalkWorld:
    """The character, its obstacles and the clock."""

    x: float
    y: float
    heading: float
    obstacles: tuple = ()
    width_yards: float = 3470.8       # Elwynn's map box, as `coords.to_yards` measures it
    height_yards: float = 2314.6
    noise_yards: float = 0.0
    turn_rad_s: float = TURN_RAD_S
    seed: int = 0

    t: float = field(default=0.0, init=False)
    keys: set = field(default_factory=set, init=False)
    airborne_s: float = field(default=0.0, init=False)
    presses: list = field(default_factory=list, init=False)
    _rng: random.Random = field(init=False)

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    # -- time --------------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 1e-9:
            dt = min(SUBSTEP_S, remaining)
            self._step(dt)
            remaining -= dt
            self.t += dt

    def _step(self, dt: float) -> None:
        if "d" in self.keys:
            self.heading += self.turn_rad_s * dt
        if "a" in self.keys:
            self.heading -= self.turn_rad_s * dt
        forward = (RUN_YD_S if "w" in self.keys else 0.0) - (BACK_YD_S if "s" in self.keys else 0.0)
        side = (STRAFE_YD_S if "e" in self.keys else 0.0) - (STRAFE_YD_S if "q" in self.keys else 0.0)
        vx = forward * math.cos(self.heading) + side * math.cos(self.heading + math.pi / 2)
        vy = forward * math.sin(self.heading) + side * math.sin(self.heading + math.pi / 2)
        speed = math.hypot(vx, vy)
        if speed > RUN_YD_S:                      # diagonal movement is not faster
            vx, vy = vx * RUN_YD_S / speed, vy * RUN_YD_S / speed
        if self.airborne_s > 0:
            self.airborne_s = max(0.0, self.airborne_s - dt)
        nx, ny = self.x + vx * dt, self.y + vy * dt
        for _ in range(3):                         # push out of anything entered, sliding
            nx, ny, moved = self._resolve(nx, ny)
            if not moved:
                break
        self.x, self.y = nx, ny

    def _resolve(self, nx: float, ny: float) -> tuple[float, float, bool]:
        pushed = False
        for ob in self.obstacles:
            if self.airborne_s > 0 and ob.height < JUMP_CLEARS:
                continue
            if isinstance(ob, Circle):
                cx, cy, reach = ob.x, ob.y, ob.r + CHARACTER_RADIUS
            else:
                cx, cy = _closest_on_segment(nx, ny, ob)
                reach = ob.thickness / 2 + CHARACTER_RADIUS
            dx, dy = nx - cx, ny - cy
            dist = math.hypot(dx, dy)
            if dist < reach:
                if dist < 1e-9:
                    dx, dy, dist = 1.0, 0.0, 1.0
                nx, ny = cx + dx / dist * reach, cy + dy / dist * reach
                pushed = True
        return nx, ny, pushed

    # -- what the radio would paint ------------------------------------------------------
    def map_position(self) -> tuple[float, float]:
        jitter = self.noise_yards
        x = self.x + (self._rng.gauss(0, jitter) if jitter else 0.0)
        y = self.y + (self._rng.gauss(0, jitter) if jitter else 0.0)
        return x / self.width_yards, y / self.height_yards

    def world(self, x: float, y: float, bounds: ZoneBounds, z: float = 0.0):
        """A map-yard point as the world coordinates a planner speaks in."""
        wx, wy = map_to_world(x / self.width_yards, y / self.height_yards, bounds)
        return wx, wy, z


class SimTime:
    """Stands in for the `time` module inside `Travel`: sleeping advances the world."""

    def __init__(self, world: WalkWorld):
        self.world = world

    def perf_counter(self) -> float:
        return self.world.t

    monotonic = perf_counter

    def time(self) -> float:
        return 1_000_000.0 + self.world.t

    def sleep(self, seconds: float) -> None:
        self.world.advance(seconds)


class SimHid:
    """The HID surface `Travel` uses, acting on a `WalkWorld`."""

    TURN_LEFT, TURN_RIGHT = "a", "d"
    STRAFE_LEFT, STRAFE_RIGHT = "q", "e"

    def __init__(self, world: WalkWorld):
        self.world = world
        self.refused = 0

    def key_down(self, key: str) -> bool:
        self.world.keys.add(key)
        self.world.presses.append(("down", key, round(self.world.t, 3)))
        return True

    def key_up(self, key: str) -> bool:
        self.world.keys.discard(key)
        return True

    def tap(self, key: str) -> bool:
        self.world.presses.append(("tap", key, round(self.world.t, 3)))
        if key == "space":
            self.world.airborne_s = JUMP_AIRTIME_S
            return True
        self.world.keys.add(key)
        self.world.advance(0.05)
        self.world.keys.discard(key)
        return True

    def hold(self, key: str, seconds: float, *, tick_s: float | None = None, on_tick=None,
             **_) -> bool:
        self.key_down(key)
        self.world.advance(seconds)
        self.key_up(key)
        return True

    def release_all(self) -> None:
        self.world.keys.clear()

    def keys_down(self) -> list[str]:
        return sorted(self.world.keys)
