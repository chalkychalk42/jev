"""Kill one unit. Select it, engage it, hold the rotation, confirm it died.

The three hard parts are not the rotation.

**Finding it** is the nameplate, exactly as in `Interact`, and `Tab` only as a fallback.
Tab was the obvious choice and was wrong: it selects by distance in the *world*, so it
happily picks a kobold thirty yards off through a tent, and the first live run spent
ninety seconds pressing abilities at a unit it could neither see nor reach — `in_melee`
false, no sighting, target at full health throughout. A unit with a nameplate on screen is
by construction one the client is drawing near enough to fight. Identity still comes from
`target.name_id` after the click, never from the plate.

**Facing it** is turning until the unit's own nameplate sits on the screen's centre line,
through the shared `Targeting.face_selected`. The camera is behind the character, so a
unit on that line is straight ahead at any distance. A right-click does **not** do this:
it starts an attack and leaves the heading alone. Measured in run 20260922T192105-3d5fcf,
frames 99-104: auto-attack on, the wolf two yards away at the character's side, full health
throughout, while the old code walked `W` down a heading nothing had set.

**Closing to it** is one continuous walk: forward held while the unit's plate is tracked
and steered on, released the moment a swing resolves (`combat.swings`, schema 12) or
damage lands - the reach signal 2.4.3 does not give the Attack action. `target.in_melee`
(`CheckInteractDistance` index 3, about ten yards) only bounds how far past it the walk may
run. Reach expires when no swing resolves for a swing timer and a margin, so a unit that
runs is followed.

**Seeing it** comes first. The client draws nameplates only near the character, and the
camera shows about a hundred degrees of that circle, so before `Tab` the character looks
round in quarter turns for a plate of the unit it wants. `Tab` reaches well beyond
nameplate distance: measured 23 September, a Young Wolf in plain view with its selection
ring and name but no plate, and turning to look for it swung it out of view. So a `Tab`
pick with no plate is located by the ring and name the client draws the moment it is
selected (the selection colour that appears between frames either side of the key), turned
toward, and walked toward in strides, looking after each one, until its plate shows. A
pick with no such mark on screen is not walked at: four blind walks in one run found none. A selection kept from before this
fight is not assumed to be ahead: when its plate is not on screen it is dropped for a new
acquisition. The same run kept one such wolf selected for five minutes and twenty-eight
fights while the hunt walked between spots.

**Swinging** is melee auto-attack, a toggle. It is pressed only when the radio says it is
off (`bars.attacking`, the stock Attack-button flash state): pressing it while it is on
switches it off, which is what the old rotation did straight after every right-click.

**Knowing it died** is the one that invites lying. A target that vanishes has either died
or been lost, and those are the same observation. So the health it was last seen at
decides: gone from full is `LOST`, gone from nothing is `KILLED`. The caller that wants
certainty counts `quests.o0_have` instead, which is the server's own tally.

The rotation is the easy part and is deliberately dumb: press the highest-priority slot
the client says is ready, respecting the global cooldown. Priorities are data
(`jev.world.combat`), because what is in slot 3 is configuration, not something to deduce.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np

from jev.clients.targeting import FACE_SEARCH_MAX_S, FaceCode, HoverCode, PaintCode, Targeting
from jev.clients.travel import TURN_RATE_SEED
from jev.perceive.radio_frame import UI_ERROR_KEYS
from jev.perceive.units import Plate, find_plates, plate_colours, selection_marks
from jev.run.evidence import event, operation, traced
from jev.world.combat import (
    HEAL_IN_COMBAT,
    HEAL_OUT_OF_COMBAT,
    MIN_MANA_TO_HEAL,
    SELF_CAST_MODIFIER,
    Ability,
    CombatProfile,
    Role,
    for_class,
)

# The radio fraction preserves zero exactly. Low health is still a living target.
DEAD_HP = 0.0

# Nameplates to try before falling back to Tab. Small, and ordered by how central they
# are: the character is standing in the camp facing it, so the nearest plate to the middle
# of the screen is the thing in front of it.
MAX_CANDIDATES = 3

# How far another nameplate has to be before a target counts as **alone**.
#
# A level 1 paladin beats a level 1 kobold and loses to three, and it lost to three twice:
# both live deaths were a pull in the middle of a camp, not a fight it could not win. So
# an isolated plate is preferred over a central one.
#
# Pixels are a proxy for yards and an imperfect one — two mobs thirty yards away look
# close together — so this is a *preference*, not a filter. When nothing is isolated the
# most central plate is still tried, because refusing to fight at all is worse than
# fighting carefully.
CROWD_PX = 260

# Health to start a fight at, and health to break one off at. Below the first, rest; below
# the second, the fight is lost and pressing on is how a character ends up running back
# from the graveyard.
MIN_START_HP = 0.55
FLEE_HP = 0.30

# After a target vanishes, how long to watch for the experience that proves a kill.
SETTLE_LOOKS = 3
SETTLE_LOOK_S = 0.25

# Tab presses before giving up on finding something attackable.
MAX_SELECTS = 4

# Closing to melee is one continuous walk. Strides with a facing look between each walked
# three yards, stood, walked three yards, stood, and shuffled in nudges until a swing
# landed - watched by the operator on 23 September: "4 paces, then 4 paces, then a couple
# tiny steps until it swings". Forward is now held while the plate is steered on.
ENGAGE_LOOKS = 5
CLOSE_LOOK_S = 0.1              # how often the walk reads the radio
CLOSE_STEER_S = 0.3             # how often it looks at the plate to steer
CLOSE_STEER_TOLERANCE = 0.04    # of the width off centre before a correcting turn
CLOSE_TRACK_DY = 0.10           # a plate falls down the screen as its unit comes closer
CLOSE_MAX_S = 6.0               # about forty yards; farther is not this fight's to walk
# Past `in_melee` (about ten yards) the walk runs this long at most - about five and a half
# yards, which ends inside reach of a unit standing still without walking through it.
NEAR_OVERRUN_S = 0.8
# Near, and still no swing: a step of a yard and a half, then a look for one.
CLOSE_STEP_S = 0.2
SWING_WAIT_S = 0.5
# Approaches without new reach before giving up: walks and steps, with room for a unit
# that moves, and at most this much time spent walking and stepping - about a hundred
# yards; a unit not reached in that is behind something.
MAX_CLOSE_BURSTS = 12
MAX_APPROACH_S = 15.0
# Reach is a swing or damage this recent. A melee swing comes every two to four seconds,
# so none for longer means the target moved out of reach - a kobold at low health runs -
# and the character closes again instead of swinging at air.
REACH_HOLD_S = 4.5

# A toggle's new state reaches the radio a paint or two after the key. Pressing it again
# inside this window would read the old state and switch it straight back.
TOGGLE_SETTLE_S = 1.0

# Looking round for a plate before Tab: quarter turns at the measured turn rate. The
# camera shows about a hundred degrees, so three turns and the starting view see all of
# the circle within nameplate distance.
SCAN_TURNS = 3
SCAN_TURN_S = math.radians(90.0) / TURN_RATE_SEED

# Walking toward a Tab pick that has no plate yet. Tab reaches past nameplate range;
# eight half-second strides are about twenty-five yards at run speed, looking after each.
SIGHT_STRIDES = 8
SIGHT_STRIDE_S = 0.5

# Re-face after this long without the target losing any health, or at once when the
# client reports "facing the wrong way". Health coming off the target is the only evidence
# the character still points at it: a unit that walked round the character leaves it
# swinging at air. Watched live: getting attacked and not retaliating.
REAIM_AFTER_S = 3.5

# Ignored heals before the heal row is dropped for the rest of this fight.
#
# The confirmation exists to be acted on. Three live runs reported `heals 0/4`, `0/5` and
# `0/5`: Holy Light is a two and a half second cast on a level 1 paladin being hit in a
# camp, and it does not complete. Every attempt costs a global cooldown not spent
# swinging, so a heal that has already failed twice in this fight is worse than no heal.
#
# The tally deliberately survives the fight. Scoping it per `run()` meant re-learning the
# same lesson on every engagement: a later run pressed `[3, 2, 3]` and burned two more
# global cooldowns discovering again that a heal it had already abandoned twice does not
# land. A landed heal clears it, so nothing is permanent - at a level where the cast
# finishes, the first one lands and the counter never reaches two.
#
# Not a class rule. A warrior has no heal row to give up on.
HEAL_GIVE_UP = 2

# Which key an action slot is. The default bindings run 1-9, then 0, then the two keys
# left of Backspace — which is where a fresh character's food and water sit, so getting
# 10-12 wrong is not academic.
SLOT_KEYS: dict[int, str] = {
    **{n: str(n) for n in range(1, 10)}, 10: "0", 11: "minus", 12: "equals",
}


class Fought(StrEnum):
    KILLED = "killed"
    NO_TARGET = "no_target"          # Tab found nothing attackable
    NOT_VISIBLE = "not_visible"      # selected, but not clickable, so not faceable
    LOST = "lost"                    # target gone while still healthy: fled, or evaded
    UNREACHABLE = "unreachable"      # engaged, but never got close enough to land a hit
    TOO_HURT = "too_hurt"            # not healthy enough to start
    LOSING = "losing"                # broke off; the caller decides what to do about it
    DIED = "died"                    # we did
    TIMEOUT = "timeout"
    BLIND = "blind"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"

    @property
    def ok(self) -> bool:
        return self is Fought.KILLED


@dataclass
class Fight:
    hid: object
    read: Callable[[], dict | None]
    read_frame: Callable[[], object | None]
    window_origin: tuple[int, int] = (0, 0)
    window_centre_x: int = 800
    profile: CombatProfile | None = None

    # Point the camera at the world before looking at it. Injected rather than built
    # here for the same reason `approach` is: this skill actuates and perceives, and
    # where the camera points is neither. `None` means whoever wired it up is confident
    # the camera is already level, which nothing was, for an evening.
    level: Callable[[], object] | None = None
    targeting: Targeting | None = None

    pressed: list[int] = field(default_factory=list, init=False)
    closed: int = field(default=0, init=False)
    # Something equipped is at zero durability. Advisory: reported so the caller can
    # decide to go and repair, never a reason to refuse the fight.
    broken: bool = field(default=False, init=False)
    # The plate that produced the current selection, if a plate did.
    selected_plate: Plate | None = field(default=None, init=False)
    heals_landed: int = field(default=0, init=False)
    heals_ignored: int = field(default=0, init=False)
    # Counted apart from the in-combat tally on purpose: a heal that cannot finish under
    # pushback says nothing about one cast standing still, and letting the in-combat
    # give-up silence the top-up would be the wrong lesson learned twice.
    top_ups: int = field(default=0, init=False)
    top_ups_landed: int = field(default=0, init=False)
    _toggled: bool = field(default=False, init=False)
    _pending_heal: tuple[float, float] | None = field(default=None, init=False)
    _damage_mark: float | None = field(default=None, init=False)
    _damage_at: float = field(default=0.0, init=False)
    _last_aim_at: float = field(default=0.0, init=False)
    last_hp: float | None = field(default=None, init=False)
    _selected_name_id: int | None = field(default=None, init=False)
    _aim_code: FaceCode | None = field(default=None, init=False)
    # The selected unit's plate at the last facing look: where a corpse will lie.
    last_plate: Plate | None = field(default=None, init=False)
    # The name of the unit the last fight killed, for finding its corpse by hover.
    killed_name_id: int | None = field(default=None, init=False)
    _xp_start: tuple | None = field(default=None, init=False)
    _strides: int = field(default=0, init=False)
    _approach_s: float = field(default=0.0, init=False)
    # The last evidence a swing reached (a resolved swing or damage), and the swing count.
    _reach_at: float | None = field(default=None, init=False)
    _swings: int | None = field(default=None, init=False)
    # The current selection came from Tab in this fight, so it lies ahead of the character.
    _ahead: bool = field(default=False, init=False)
    # Where the Tab pick's selection mark appeared, as a fraction of the width off centre.
    _mark_offset: float | None = field(default=None, init=False)
    _error_count: int | None = field(default=None, init=False)
    _damage_seen: bool = field(default=False, init=False)
    _input_refused: bool = field(default=False, init=False)
    detail: str = field(default="", init=False)
    _last_use: dict[int, float] = field(default_factory=dict, init=False)

    # -- the skill -----------------------------------------------------------

    @traced("fight")
    def run(self, name_id: int | None = None, *, timeout_s: float = 45.0) -> Fought:
        """Select, engage, and hold the rotation until something settles it."""
        self.pressed = []
        self.closed = 0
        self.broken = False
        if self.level is not None and self.level() is False:
            self.detail = "camera input refused"
            return Fought.REFUSED
        self._toggled = False
        self._pending_heal = None
        self._damage_mark = None
        self._damage_seen = self._input_refused = False
        self._selected_name_id = None
        self._aim_code = None
        self.last_plate = None
        self.killed_name_id = None
        self._xp_start = None
        self._strides = 0
        self._approach_s = 0.0
        self._ahead = False
        self._error_count = None
        self._damage_at = time.monotonic()
        self.last_hp = None
        self.detail = ""
        self._last_use = {}
        event("fight.request", data={"wanted_name_id": name_id, "timeout_s": timeout_s})

        v = self.read()
        if self._targeting().cancel_pending_spell(v):
            v = self.read()                  # its click would cast, not select
        self._observe(v)
        if v is None:
            return Fought.BLIND
        self._error_count = v.get("ui.error_count")   # errors before the fight are not news
        self._swings = v.get("combat.swings")           # and neither are earlier swings
        self._reach_at = None
        self._xp_start = (v.get("char.level"), v.get("char.xp_pct"))
        # The health guard is about **picking** fights, not about surviving one already
        # under way. Refusing to swing back because health is low is how a character
        # stands there being hit at 49%, declines to eat because it is in combat, and
        # does nothing at all until it falls over.
        in_combat = v.get("vitals.combat") is True

        # Broken gear is a **preference, not a veto**. A weapon at zero durability does
        # unarmed damage and the fight is worth far less - but refusing to start it
        # protects nothing, because at zero there is no durability left for a death to
        # cost. Vetoing it built a deadlock instead: no fight, so no loot, so no copper,
        # so no repair, forever. Choosing to repair is the loop's decision and it needs
        # money to make it; all this does is make the state impossible to miss again,
        # after an evening of `bags.durability_min` reading 0.0 with no reader.
        self.broken = v.get("bags.durability_min") == 0.0

        hp = v.get("vitals.hp")
        if not in_combat and hp is not None and hp < MIN_START_HP:
            self.detail = f"{hp:.0%} health; not starting a fight on that"
            return Fought.TOO_HURT

        # Already engaged with something alive: that is the fight, and shopping for a
        # better one just adds a second attacker.
        engaged = (in_combat and v.get("target.has") is True
                   and v.get("target.hp") is not None and v["target.hp"] > DEAD_HP)
        # Already selected and alive, and the unit we came for: that is the fight. Whoever
        # selected it - the tutor, a previous look - re-acquiring could only swap it for
        # another of the same name, or for something else entirely.
        chosen = (not engaged and name_id is not None and v.get("target.has") is True
                  and v.get("target.name_id") == name_id
                  and isinstance(v.get("target.hp"), (int, float)) and v["target.hp"] > DEAD_HP)
        if chosen:
            engaged = True
        if not engaged:
            # In combat the name filter loosens, but it does not come off. Dropping it
            # entirely meant that after killing a kobold the next plate could be a Timber
            # Wolf minding its own business, and the character attacked it for no reason.
            #
            # What the filter is for is self-defence: a Kobold Worker beat this character
            # to 27% health while every attempt refused to fight anything but a Kobold
            # Vermin. So a different name is accepted only when it is **attacking us**,
            # which `target.attacking_me` says outright.
            acquired = self.acquire(name_id, defend=in_combat)
            if acquired is not None:
                return acquired
        else:
            self._selected_name_id = v.get("target.name_id")
            self._damage_mark = v.get("target.hp")
            self.selected_plate = None
        if not self.engage(v):
            if not (chosen and self._aim_code is FaceCode.NOT_VISIBLE):
                return self._aim_failure()
            # Kept from before this fight and not on screen: nothing says it is ahead or
            # near, so choose again from what is. Measured: one stale far selection
            # absorbed twenty-eight fights while the hunt walked between spots.
            event("selection.dropped", data={"name_id": self._selected_name_id,
                                             "reason": self.detail})
            acquired = self.acquire(name_id, defend=in_combat)
            if acquired is not None:
                return acquired
            if not self.engage(v):
                return self._aim_failure()
        # Acquisition/verification time is not time spent trying to deal damage.
        self._damage_at = self._last_aim_at = time.monotonic()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            v = self.read()
            self._observe(v)
            if v is None:
                return Fought.BLIND
            if v.get("vitals.dead") is True or v.get("vitals.ghost") is True:
                self.detail = "the character died"
                return Fought.DIED
            if v.get("ui.modal") is True:
                self.detail = "modal interrupted the fight"
                return Fought.INTERRUPTED
            mine = v.get("vitals.hp")
            if (mine is not None and mine < FLEE_HP
                    and v.get("vitals.combat") is not True):
                # Breaking off is only a choice when nothing is hitting us. In combat it
                # is not a choice, it is standing still: this returned LOSING on the first
                # iteration, before the rotation, so the caller rested, was interrupted
                # because something was attacking, tried again, and got LOSING again -
                # eight times, pressing nothing, while health went 29, 27, 21, 18, 18, 15,
                # 9, 6, dead.
                #
                # A guard picks fights. It does not freeze one already started.
                self.detail = f"broke off at {mine:.0%} health"
                return Fought.LOSING

            if self._targeting().cancel_pending_spell(v):
                continue                       # its click would have cast, not selected
            if v.get("target.has") is not True:
                return self._settle(v)
            if (self._selected_name_id is not None
                    and v.get("target.name_id") != self._selected_name_id):
                self.detail = "selected target changed during fight"
                return Fought.LOST
            hp = v.get("target.hp")
            if hp is not None:
                self.last_hp = hp
            if hp == DEAD_HP:
                return self._settle(v)

            # Walking and swinging are the same loop, not one after the other.
            #
            # Closing used to be a gate: walk until the target takes damage, *then* start
            # the rotation. Damage comes from swinging, swinging is the rotation, and the
            # rotation was behind the gate — so a live run reported
            # `unreachable pressed [] closed 8` eight times over. It had walked at the
            # kobold and never once pressed anything at it.
            if self._note_damage(v) or self._note_swing(v):
                self._strides = 0              # progress: the approach budget starts again
                self._approach_s = 0.0

            # In reach: the Attack action's own range check when the addon has one (it
            # answers nil on 2.4.3), else a recent resolved swing or recent damage.
            #
            # Closing used to end only when the target lost health, so a character that
            # was facing slightly wrong walked *through* the kobold and out the other
            # side, still holding W, for all eight bursts - watched live: "we target and
            # try to attack but then just keep running forwards and passed them".
            melee = v.get("target.melee_range")
            near = v.get("target.in_melee") is True
            now = time.monotonic()
            reached = self._reach_at is not None and now - self._reach_at < REACH_HOLD_S
            in_reach = melee is True or (melee is None and reached)
            stalled = now - max(self._damage_at, self._last_aim_at,
                                self._reach_at or 0.0) > REAIM_AFTER_S
            wrong_way = self._new_error(v) == "not_facing"
            if in_reach:
                # Stand and swing. Turn back only on evidence the swings are not landing.
                if (wrong_way or stalled) and not self.engage(v):
                    return self._aim_failure()
            elif self._strides < MAX_CLOSE_BURSTS and self._approach_s < MAX_APPROACH_S:
                # Not while casting: movement cancels a cast, and the only thing being
                # cast here is a heal that is keeping us alive.
                if v.get("bars.casting") is not True:
                    if not self.engage(v):     # face before walking, never walk blind
                        return self._aim_failure()
                    self._close(v, near, deadline=deadline)
                    if self._input_refused:
                        return Fought.REFUSED
            else:
                # Out of strides with no new damage. Whether anything was *pressed* says
                # nothing about whether it was reached - a seal lands on the character,
                # not on the kobold - and requiring "pressed nothing" here let two live
                # fights walk eight bursts and then stand in the rotation for the full
                # forty-five seconds: `pressed [2, 1, 2] closed 8`, twice.
                self.detail = (f"closed {self.closed} times over {self._approach_s:.0f}s and "
                               "never came within reach; cannot reach it")
                return Fought.UNREACHABLE

            self._rotate(v)
            if self._input_refused:
                return Fought.REFUSED
            time.sleep(0.2)

        self.detail = f"{timeout_s:.0f}s and it is still standing"
        return Fought.TIMEOUT

    # -- pieces --------------------------------------------------------------

    @traced("target.acquire")
    def acquire(self, name_id: int | None, *, defend: bool = False) -> Fought | None:
        """Select something worth fighting. `None` means it worked.

        Nameplates first, because a plate means the client is drawing the unit near enough
        to fight, and `Tab` does not care how far away or how occluded its pick is. With
        no wanted plate in view the character looks round in quarter turns before `Tab`;
        not in self-defence, where whatever is hitting us is chosen by `Tab` and found by
        the facing search.
        """
        self.selected_plate = None
        self._ahead = False
        self._mark_offset = None
        self._targeting().cancel_pending_spell()
        turn = getattr(self.hid, "TURN_RIGHT", "d")
        for look in range(1 if defend else SCAN_TURNS + 1):
            if look:
                event("acquire.scan", data={"look": look, "key": turn, "seconds": round(SCAN_TURN_S, 3)})
                if not self.hid.hold(turn, SCAN_TURN_S):
                    self.detail = "scan input refused"
                    return Fought.REFUSED
                if self._targeting().wait_for_paint().code is PaintCode.BLIND:
                    self.detail = "radio lost while looking round"
                    return Fought.BLIND
            picked = self._pick_plate(name_id, defend)
            if picked is not False:
                return picked
        return self.select(name_id, defend=defend)

    def _pick_plate(self, name_id: int | None, defend: bool) -> Fought | bool | None:
        """Select a plate in the current view: `None` selected, `False` none acceptable."""
        frame = self.read_frame()
        if frame is not None:
            for plate in self._candidates(frame):
                point = (self.window_origin[0] + round(plate.cx),
                         self.window_origin[1] + round(plate.cy))
                # Ask the client whose plate this is before selecting it. Clicking the
                # nearest plate selected a rabbit on every look of the first live run.
                # Self-defence has no name to ask for: whatever is attacking us is chosen
                # by the radio after the click, exactly as before.
                if name_id is not None and not defend:
                    hover = self._targeting().probe(point, require_target=False)
                    event("selection.hover", code=hover.code.value,
                          data={"point": list(point), "wanted_name_id": name_id,
                                "name_id": (hover.after or {}).get("cursor.name_id")})
                    if hover.code in (HoverCode.REFUSED, HoverCode.BLIND):
                        self.detail = hover.detail
                        return Fought.REFUSED if hover.code is HoverCode.REFUSED else Fought.BLIND
                    after = hover.after or {}
                    if (after.get("cursor.has") is not True or after.get("cursor.dead") is True
                            or after.get("cursor.name_id") != name_id):
                        continue
                event("selection.request", data={"method": "plate", "wanted_name_id": name_id,
                      "point": list(point)})
                if not self.hid.click(*point):
                    self.detail = "selection input refused"
                    return Fought.REFUSED
                paint = self._targeting().wait_for_paint()
                if paint.code is not PaintCode.FRESH:
                    self.detail = paint.detail
                    return Fought.BLIND
                if self._acceptable(name_id, defend=defend, values=paint.after) is True:
                    self.selected_plate = self.last_plate = plate
                    return None
        return False

    def _candidates(self, frame) -> list[Plate]:
        """Plates worth clicking: alone first, then central. An ordering, not an ID.

        Isolation leads because a pull in the middle of a camp is what killed this
        character twice, and a plate with no neighbour is the best available evidence that
        a mob has none either.
        """
        plates = find_plates(frame)
        centre = self.window_centre_x
        # A snapshot, because `list.sort` empties the list while it computes keys — so a
        # key function that reads `plates` sees nothing, every plate looks isolated, and
        # the ordering silently collapses back to plain centrality.
        others = list(plates)

        def crowding(plate: Plate) -> float:
            near = [abs(p.cx - plate.cx) for p in others if p is not plate]
            return min(near) if near else float("inf")

        plates.sort(key=lambda p: (crowding(p) < CROWD_PX, abs(p.cx - centre)))
        with operation("target.candidates") as span:
            if span.enabled:
                span.finish(code="observed", data={"count": len(plates),
                    "candidates": [asdict(p) for p in plates[:MAX_CANDIDATES]]})
        return plates[:MAX_CANDIDATES]

    def _acceptable(self, name_id: int | None, *, defend: bool = False,
                    values: dict | None = None) -> bool | None:
        """Is what we just selected worth fighting? `None` if nothing is readable."""
        v = values if values is not None else self.read()
        self._observe(v)
        event("selection.expected", data={"wanted_name_id": name_id, "defend": defend})
        if v is None:
            return None
        if v.get("target.has") is not True:
            return False
        hp = v.get("target.hp")
        if hp is not None and hp <= DEAD_HP:
            return False                       # a corpse is selectable and not a fight
        if (name_id is None or v.get("target.name_id") == name_id
                or (defend and v.get("target.attacking_me") is True)):
            self._selected_name_id = v.get("target.name_id")
            self._damage_mark = hp
            return True
        # Not what we came for. Worth fighting only if it is already hitting us.
        return False

    @traced("target.select")
    def select(self, name_id: int | None, *, defend: bool = False) -> Fought | None:
        """`Tab`, as a fallback when no nameplate was clickable.

        The client picks; the radio says what it picked. Kept because a plate can be
        occluded by terrain while the unit is perfectly fightable.
        """
        for _ in range(MAX_SELECTS):
            event("selection.request", data={"method": "tab", "wanted_name_id": name_id,
                                             "defend": defend})
            before = self.read_frame()
            if not self.hid.tap("tab"):
                self.detail = "selection input refused"
                return Fought.REFUSED
            paint = self._targeting().wait_for_paint()
            if paint.code is not PaintCode.FRESH:
                self.detail = paint.detail
                return Fought.BLIND
            v = paint.after
            self._observe(v)
            if v is None:
                return Fought.BLIND
            if v.get("target.has") is not True:
                continue
            if v.get("target.hp") is not None and v["target.hp"] <= DEAD_HP:
                continue                       # a corpse is selectable and not a fight
            if (name_id is not None and v.get("target.name_id") != name_id
                    and not (defend and v.get("target.attacking_me") is True)):
                continue
            self._selected_name_id = v.get("target.name_id")
            self._damage_mark = v.get("target.hp")
            self._ahead = True
            self._mark_offset = self._mark(before, v)
            return None
        self.detail = "no nameplate and no Tab target worth fighting"
        return Fought.NO_TARGET

    def _mark(self, before, values: dict) -> float | None:
        """Where the pick's ring and name appeared, as an offset from centre, or `None`."""
        after = self.read_frame()
        if not isinstance(before, np.ndarray) or not isinstance(after, np.ndarray):
            return None
        try:
            marks = selection_marks(before, after, plate_colours(values.get("target.reaction")))
        except ValueError:
            return None
        event("selection.marks", data={"count": len(marks), "marks": [
            {"cx": round(m.cx), "cy": round(m.cy), "area": m.area} for m in marks[:3]]})
        if not marks:
            return None
        width = after.shape[1]
        return (marks[0].cx - width / 2) / width

    def _targeting(self) -> Targeting:
        return self.targeting or Targeting(self.hid, self.read, read_frame=self.read_frame,
                                           window_origin=self.window_origin)

    @traced("target.engage")
    def engage(self, values: dict | None = None) -> bool:
        """Face the selected unit, then have melee auto-attack on. Damage stays observed.

        Facing is the shared `Targeting.face_selected`: turn until the unit's own plate is
        on the centre line. Auto-attack is pressed only when the radio says it is off.
        `values` is the caller's latest reading, used only to size the search.
        """
        v = values or {}
        # A unit already fighting us can be anywhere, including behind: search a full turn.
        # Otherwise turning is not how an unseen unit is found: a Tab pick lies ahead,
        # beyond nameplate range, and is walked toward; a kept selection is re-chosen.
        fighting = v.get("vitals.combat") is True or v.get("target.attacking_me") is True
        result = self._targeting().face_selected(
            expected_name_id=self._selected_name_id, hint=self.last_plate,
            search_s=FACE_SEARCH_MAX_S if fighting else 0.0)
        if result.code is FaceCode.NOT_VISIBLE and self._ahead and not fighting:
            result = self._close_to_sight(result)
        self._aim_code = result.code
        self.detail = result.detail
        event("engage.request", code=result.code.value,
              data={"offset": result.offset, "turns": result.turns,
                    "turned_s": round(result.turned_s, 3)})
        if result.plate is not None:
            self.last_plate = result.plate
        if not result.faced:
            return False
        self._last_aim_at = time.monotonic()
        return self._ensure_attacking()

    def _note_damage(self, values: dict) -> bool:
        """The target lost health since the last look: a swing reached it."""
        hp = values.get("target.hp")
        if hp is None:
            return False
        hit = self._damage_mark is not None and hp < self._damage_mark
        now = time.monotonic()
        if hit:
            self._damage_seen = True
            self._damage_at = self._reach_at = now
        if self._damage_mark is None:
            self._damage_at = now
        self._damage_mark = hp
        return hit

    def _note_swing(self, values: dict) -> bool:
        """A swing of ours resolved since the last look, landed or missed (schema 12)."""
        count = values.get("combat.swings")
        if count is None:
            return False
        new = self._swings is not None and count != self._swings
        self._swings = count
        if new:
            self._reach_at = time.monotonic()
        return new

    def _close(self, values: dict, near: bool, *, deadline: float | None = None) -> bool:
        """Walk at the faced target until a swing can reach it. `True` once one has.

        Far off, one continuous walk steered on the plate; near, a short step and a look.
        Either stops on the first resolved swing or damage, on a facing error, or when the
        selection is no longer this fight's living target.
        """
        began = time.monotonic()
        try:
            return self._approach(near, deadline)
        finally:
            self._approach_s += time.monotonic() - began

    def _approach(self, near: bool, deadline: float | None) -> bool:
        if near:
            event("approach.request", data={"key": "w", "mode": "step",
                                            "duration_s": CLOSE_STEP_S, "closed": self.closed})
            if not self.hid.hold("w", CLOSE_STEP_S):
                self._input_refused = True
                self.detail = "approach input refused"
                return False
            self.closed += 1
            self._strides += 1
            wait_until = time.monotonic() + SWING_WAIT_S
            if deadline is not None:
                wait_until = min(wait_until, deadline)
            while time.monotonic() < wait_until:
                time.sleep(min(CLOSE_LOOK_S, max(0.0, wait_until - time.monotonic())))
                v = self.read()
                if v is None or not self._same_fight(v):
                    return False
                if self._note_damage(v) or self._note_swing(v):
                    return True
            return False

        targeting = self._targeting()
        plate = self.last_plate
        event("approach.request", data={"key": "w", "mode": "walk", "closed": self.closed})
        if not self.hid.key_down("w"):
            self._input_refused = True
            self.detail = "approach input refused"
            return False
        self.closed += 1
        self._strides += 1
        started = time.monotonic()
        stop_at = started + CLOSE_MAX_S if deadline is None else min(started + CLOSE_MAX_S, deadline)
        steer_at = started + CLOSE_STEER_S
        near_at = None
        try:
            while time.monotonic() < stop_at:
                time.sleep(min(CLOSE_LOOK_S, max(0.0, stop_at - time.monotonic())))
                v = self.read()
                if v is None or not self._same_fight(v):
                    return False
                if self._note_damage(v) or self._note_swing(v):
                    return True
                if self._new_error(v) == "not_facing":
                    return False
                if v.get("target.in_melee") is True:
                    if v.get("target.attacking_me") is True:
                        return False           # it is coming to us; the swing decides
                    near_at = near_at if near_at is not None else time.monotonic()
                    if time.monotonic() - near_at >= NEAR_OVERRUN_S:
                        return False
                if time.monotonic() >= steer_at:
                    steer_at = time.monotonic() + CLOSE_STEER_S
                    seen = targeting.track_selected(plate, window_dy=CLOSE_TRACK_DY)
                    if seen is not None:
                        plate, offset = seen
                        self.last_plate = plate
                        if abs(offset) > CLOSE_STEER_TOLERANCE and not targeting.turn_toward(offset):
                            self._input_refused = True
                            self.detail = "turn input refused"
                            return False
            return False
        finally:
            self.hid.key_up("w")

    def _same_fight(self, values: dict) -> bool:
        """Still this fight's living target, and nothing that stops a fight."""
        hp = values.get("target.hp")
        return (values.get("target.has") is True
                and (self._selected_name_id is None
                     or values.get("target.name_id") == self._selected_name_id)
                and not (isinstance(hp, (int, float)) and hp <= DEAD_HP)
                and values.get("vitals.dead") is not True and values.get("ui.modal") is not True)

    def _close_to_sight(self, result):
        """Turn toward a Tab pick's mark, then walk at it until its plate shows.

        Bounded, and looks after every stride. No mark, no walk: the pick is somewhere
        the camera does not show, and walking the current heading is a guess.
        """
        if self._mark_offset is None:
            return result
        event("approach.mark", data={"offset": round(self._mark_offset, 4)})
        if not self._targeting().turn_toward(self._mark_offset):
            self._input_refused = True
            self.detail = "turn input refused"
            return result
        self._mark_offset = None               # the turn used it; it is no longer where it was
        for stride in range(SIGHT_STRIDES):
            event("approach.request", data={"key": "w", "duration_s": SIGHT_STRIDE_S,
                                            "closed": self.closed, "sight": stride + 1})
            if not self.hid.hold("w", SIGHT_STRIDE_S):
                self._input_refused = True
                self.detail = "approach input refused"
                return result
            self.closed += 1
            v = self.read()
            self._observe(v)
            fighting = v is not None and (v.get("vitals.combat") is True
                                          or v.get("target.attacking_me") is True)
            result = self._targeting().face_selected(
                expected_name_id=self._selected_name_id, hint=None,
                search_s=FACE_SEARCH_MAX_S if fighting else 0.0)
            if result.code is not FaceCode.NOT_VISIBLE or fighting:
                return result
        return result

    def _ensure_attacking(self) -> bool:
        """Press the melee toggle only when it is observed off (ARCHITECTURE section 6).

        An older addon cannot say; then the toggle is pressed at most once per fight and
        never after damage has landed, which is the previous rule and the best available.
        """
        v = self.read()
        self._observe(v)
        if v is None:
            self.detail = "radio lost before starting the attack"
            return False
        profile = self.profile or for_class(v.get("char.class_id"), v.get("char.race_id"))
        toggle = next((a for a in profile.by_role(Role.ATTACK) if a.toggle), None)
        if toggle is None or not self._toggle_needed(toggle, v):
            return True
        if not self._press(toggle):
            return False
        self._toggled = True
        return True

    def _toggle_needed(self, toggle: Ability, values: dict) -> bool:
        attacking = values.get("bars.attacking")
        if attacking is True:
            return False
        last = self._last_use.get(toggle.slot)
        if last is not None and time.monotonic() - last < TOGGLE_SETTLE_S:
            return False                       # the radio has not painted the press yet
        if attacking is False:
            return True
        return not (self._toggled or self._damage_seen)

    def _new_error(self, values: dict) -> str | None:
        """A UI error the client raised since the last look, from the held schema 9 field."""
        count = values.get("ui.error_count")
        if count is None or count == self._error_count:
            return None
        self._error_count = count
        error = values.get("ui.error_last")
        return UI_ERROR_KEYS[error] if isinstance(error, int) and 0 < error < len(UI_ERROR_KEYS) else None

    def _aim_failure(self) -> Fought:
        if self._input_refused:
            return Fought.REFUSED
        return {FaceCode.REFUSED: Fought.REFUSED, FaceCode.BLIND: Fought.BLIND,
                FaceCode.INTERRUPTED: Fought.INTERRUPTED,
                FaceCode.NO_TARGET: Fought.LOST, FaceCode.WRONG_TARGET: Fought.LOST,
                FaceCode.WRONG_KIND: Fought.LOST}.get(self._aim_code, Fought.NOT_VISIBLE)

    def _rotate(self, values: dict) -> None:
        """Press the highest-priority row the client says is ready.

        Roles, not classes. There is no `if paladin` here and there is not going to be:
        the engine asks for a row with `role=heal` and presses it if the bars say it is
        ready, so a warrior is the same list with one fewer row and nothing changes.
        """
        if values.get("bars.casting") is True:
            return
        gcd = values.get("bars.gcd")
        if gcd is not None and gcd > 0.0:
            return
        ready, usable = values.get("bars.ready"), values.get("bars.usable")
        if ready is None or usable is None:
            return

        profile = self.profile or for_class(values.get("char.class_id"),
                                            values.get("char.race_id"))
        self._watch_heal(values, ready)

        def pressable(a: Ability) -> bool:
            bit = 1 << (a.slot - 1)
            return bool(ready & bit) and bool(usable & bit)

        # 1. Stay alive. A heal is a global cooldown not spent swinging, so the line is
        #    low — but standing there at 20% because healing is "not the rotation" is how
        #    a character ends up running back from the graveyard.
        heal = profile.first(Role.HEAL)
        hp = values.get("vitals.hp")
        giving_up = self.heals_ignored >= HEAL_GIVE_UP and self.heals_landed == 0
        if (heal is not None and pressable(heal) and not giving_up
                # One at a time. `_watch_heal` is what decides whether the last one
                # landed, and pressing again before it answers is how a live fight got
                # `pressed [2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3]` - fifteen
                # Holy Lights, none of which healed anything, on a 2.5 second cast.
                and self._pending_heal is None
                and values.get("vitals.combat") is True
                and hp is not None and hp < HEAL_IN_COMBAT
                and self._has_mana_for(heal, values)):
            if self._press(heal):
                self._pending_heal = (hp, time.monotonic())
            return

        # 2. Keep the buff up, and only when it is actually lapsing: `bars.ready` says a
        #    seal is pressable on every single tick, so without the interval the
        #    character stands there re-sealing and never swings.
        now = time.monotonic()
        for buff in profile.by_role(Role.BUFF):
            last = self._last_use.get(buff.slot)
            if pressable(buff) and (last is None or now - last >= buff.every_s):
                self._press(buff)
                return

        # 3. Swing. A toggle is pressed at most once and only before anything has landed,
        #    because pressing melee auto-attack while already swinging **stops** it.
        for attack in profile.by_role(Role.ATTACK):
            if not pressable(attack):
                continue
            if attack.toggle and not self._toggle_needed(attack, values):
                continue
            if self._press(attack) and attack.toggle:
                self._toggled = True
            return

    @traced("heal.top_up")
    def top_up(self, target: float = HEAL_OUT_OF_COMBAT, *, tries: int = 4,
               settle_s: float = 3.5) -> bool:
        """Heal between fights. Returns whether we reached `target`.

        Out of combat only, and deliberately not gated on the in-combat give-up: those
        heals fail to pushback, which says nothing about one cast standing still. Holy
        Light completes fine when nothing is hitting the character, and going into the
        next pull at 80% rather than 45% is the difference between winning it and a
        two-hundred-yard corpse run.

        Cheaper than food, so it is tried first; the caller falls through to `Rest` when
        this reports it could not get there.
        """
        for _ in range(tries):
            v = self.read()
            self._observe(v)
            if v is None or v.get("vitals.combat") is True:
                return False
            hp = v.get("vitals.hp")
            if hp is None:
                return False
            if hp >= target:
                return True

            profile = self.profile or for_class(v.get("char.class_id"),
                                                v.get("char.race_id"))
            heal = profile.first(Role.HEAL)
            ready, usable = v.get("bars.ready"), v.get("bars.usable")
            if heal is None or ready is None or usable is None:
                return False
            bit = 1 << (heal.slot - 1)
            if not (ready & bit) or not (usable & bit):
                return False
            if not self._has_mana_for(heal, v):
                self.detail = "out of mana to top up with"
                return False

            if not self._press(heal):
                return False
            self.top_ups += 1
            if self._watch_top_up(hp, settle_s):
                self.top_ups_landed += 1
            else:
                return False          # it did not land standing still; food is next
        v = self.read()
        self._observe(v)
        return v is not None and (v.get("vitals.hp") or 0.0) >= target

    def _watch_top_up(self, before: float, settle_s: float) -> bool:
        """Did health actually rise? The same confirmation as in combat, waited on."""
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(0.4)
            v = self.read()
            self._observe(v)
            if v is None:
                return False
            hp = v.get("vitals.hp")
            if hp is not None and hp > before + 0.02:
                return True
        return False

    def pressed_keys(self) -> list[str]:
        """The slots pressed this fight, as the keys they were sent as."""
        return [SLOT_KEYS.get(slot, str(slot)) for slot in self.pressed]

    def _press(self, ability: Ability) -> bool:
        key = SLOT_KEYS.get(ability.slot)
        if key is None:
            self._input_refused = True
            self.detail = f"ability slot {ability.slot} has no configured key"
            return False
        event("ability.request", data={"slot": ability.slot, "key": key,
                                       "role": ability.role.value, "self_cast": ability.self_cast})
        pressed = (self.hid.chord(SELF_CAST_MODIFIER, key) if ability.self_cast
                   else self.hid.tap(key))
        if not pressed:
            self._input_refused = True
            self.detail = f"ability slot {ability.slot} input refused"
            return False
        self._last_use[ability.slot] = time.monotonic()
        self.pressed.append(ability.slot)
        return True

    def _has_mana_for(self, ability: Ability, values: dict) -> bool:
        """Enough mana for this, and enough left afterwards to matter.

        Out of mana is not a reason to keep pressing: it falls through to swinging, and
        the caller falls through to food, a vendor, or breaking the fight off. There is
        deliberately no drinking inside a fight.
        """
        frac = values.get("vitals.power")
        if frac is None:
            return True                      # unreadable is not a refusal
        if frac < MIN_MANA_TO_HEAL:
            return False
        pool = values.get("vitals.power_max")
        if pool and ability.mana:
            return frac * pool >= ability.mana
        return True

    def _watch_heal(self, values: dict, ready: int) -> None:
        """Did health rise after the last heal request?

        A slot going unready can mean casting or cooldown; it cannot confirm healing.
        Retain that observation separately from the measured health change.
        """
        if self._pending_heal is None:
            return
        at_press, when = self._pending_heal
        hp = values.get("vitals.hp")
        heal = (self.profile or for_class(values.get("char.class_id"),
                                          values.get("char.race_id"))).first(Role.HEAL)
        went_unready = heal is not None and not (ready & (1 << (heal.slot - 1)))
        event("heal.observed", data={"hp_before": at_press, "hp_after": hp,
                                     "slot_went_unready": went_unready})
        if hp is not None and hp > at_press + 0.02:
            self.heals_landed += 1
            self._pending_heal = None
        elif time.monotonic() - when > 2.5:
            self.heals_ignored += 1
            self._pending_heal = None

    @traced("fight.settle")
    def _settle(self, values: dict | None = None) -> Fought:
        """It is gone. Did we kill it?

        Vanishing is one observation with two causes. Health seen at zero decides it, and
        so does experience: the client cleared the selection at the moment of a live kill
        (23 September, the wolf at 20% one paint and gone the next, with XP arriving on the
        same tick), and in a fight nothing but a kill grants experience. Experience can
        lag the disappearance by a paint, so a vanished target is watched briefly for it.
        A caller that needs certainty still counts `quests.o0_have`.
        """
        event("fight.last_health", data={"target_hp": self.last_hp})
        if self.last_hp == 0.0 or self._gained(values):
            self.killed_name_id = self._selected_name_id
            return Fought.KILLED
        for _ in range(SETTLE_LOOKS):
            time.sleep(SETTLE_LOOK_S)
            later = self.read()
            self._observe(later)
            if self._gained(later):
                self.killed_name_id = self._selected_name_id
                return Fought.KILLED
        self.detail = "target disappeared without observed death"
        return Fought.LOST

    def _gained(self, values: dict | None) -> bool:
        """Experience or a level above what the fight started with."""
        if values is None or self._xp_start is None:
            return False
        level, xp = values.get("char.level"), values.get("char.xp_pct")
        start_level, start_xp = self._xp_start
        if isinstance(level, int) and isinstance(start_level, int) and level > start_level:
            return True
        return (level == start_level and isinstance(xp, (int, float))
                and isinstance(start_xp, (int, float)) and xp > start_xp)

    @staticmethod
    def _observe(values: dict | None) -> None:
        event("combat.observed", code="blind" if values is None else "readable",
              data={} if values is None else {key: values.get(key) for key in (
                  "vitals.hp", "vitals.power", "vitals.combat", "vitals.dead", "vitals.ghost",
                  "target.has", "target.name_id", "target.hp", "target.in_melee",
                  "target.attacking_me", "bars.casting", "bars.ready", "bars.usable")})
