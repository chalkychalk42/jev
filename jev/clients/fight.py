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
round in turns of about a quarter for a plate of the unit it wants. `Tab` reaches well beyond
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
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np

from jev.clients.hid import held, humaniser, pace
from jev.clients.targeting import FACE_SEARCH_MAX_S, FaceCode, HoverCode, PaintCode, Targeting
from jev.clients.travel import MIN_TRAVEL_FOR_HEADING, TURN_RATE_SEED
from jev.guide.coords import ZoneBounds, distance_yards
from jev.perceive.radio_frame import UI_ERROR_KEYS
from jev.perceive.units import (
    PROPOSAL_COLOURS,
    Plate,
    find_plates,
    plate_colours,
    selection_marks,
)
from jev.run.evidence import event, operation, traced
from jev.world.combat import (
    HEAL_IN_COMBAT,
    HEAL_OUT_OF_COMBAT,
    LAST_RESORT_BELOW,
    MIN_MANA_TO_HEAL,
    SELF_CAST_MODIFIER,
    Ability,
    CombatProfile,
    Role,
    for_class,
    grey_level,
    ranged,
)

# The radio fraction preserves zero exactly. Low health is still a living target.
DEAD_HP = 0.0
# A selection whose health rises this far between two readings is another unit of the same
# name: nothing in a fight heals a mob that fast.
REPLACED_HP_RISE = 0.4

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

# A press the client acts on shows within a look or two: a cast, the global cooldown or its
# own. Nothing by `PRESS_ANSWER_S` and it was dropped (`_press_answered`); a first look
# after `PRESS_TELL_S` comes too late to tell, the 1.5 s global cooldown being over.
PRESS_ANSWER_S = 0.8
PRESS_TELL_S = 1.4
# Unanswered presses of one slot in a row before the last is counted anyway. A stun lasts
# two seconds, three presses; a press the client will never answer - an aura already up,
# a Judgement on a unit out of reach - must not hold the rotation on that one row.
PRESS_GIVE_UP = 3

# After a target vanishes, how long to watch for the experience that proves a kill.
SETTLE_LOOKS = 3
SETTLE_LOOK_S = 0.25
# A grey target's kill grants no experience to prove it, so the vanish is the evidence:
# gone from the selection below half its health, it died. Back in Northshire at level 9,
# six level 3-4 Defias were each last seen at 3-34% and settled "lost", none looted
# (session 74, run 20260924T133800-b58ab2).
GREY_KILL_HP = 0.5

# Tab presses before giving up on finding something attackable. With a humaniser the count
# is drawn per search, so a camp is not searched with the same burst every time.
MAX_SELECTS = 4
SELECTS_DRAWN = (3, 5)

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
# Forward held this long without a heading's worth of travel is blocked: travel's stuck
# test. At Echo Ridge Mine on 23 September a Kobold Laborer stood in plain view past a pit
# prop, and three walks of six seconds each pressed into the prop until the fight gave up,
# "closed 3 times over 18s and never came within reach" (run 20260923T184413-a386ff).
BLOCKED_AFTER_S = 1.5
BLOCKED_YARDS = MIN_TRAVEL_FOR_HEADING
# Near, a step of a yard and a half that moved less than this went nowhere; this many in a
# row is blocked.
STEP_STILL_YARDS = 0.5
STILL_STEPS = 2
# Blocked, the character strafes off the line, and the next approach faces the unit again.
# First one way and then twice as long the other, as travel's detour searches: a post is
# cleared on whichever side is open, and a side walled off is not tried twice at the same
# length. Half a second is three and a half yards at run speed; a pit prop is one.
SIDESTEP_S = 0.5
MAX_SIDESTEP_S = 2.0
# A strafe that went nowhere is walled on that side too, and the character backs off the
# way it came - about two yards, what travel's unstick measured always works. Inside Echo
# Ridge Mine a character stood on a stack of crates in a nook, rock to one side and a pit
# prop ahead, and ten strafes moved it not at all (run 20260923T191946-2b79ed).
BACK_OFF_S = 0.5
# Steps at a unit that is biting us in melee, none of them bringing a swing or a hit: it is
# somewhere a swing does not reach, and the character backs off for it to follow out. A
# Mangy Wolf stood inside the trunk of a tree and bit a level 7 paladin from 69% to 35%
# while the fight stepped into the bark twelve times, gave up "unreachable", and started
# again (run 20260924T053651-ac99b2). What chases us comes out of the tree.
UNANSWERED_STEPS = 4
DRAW_OUT_S = 1.5
# Backing off keeps facing the attacker, so from a tree's roots with the wolf below it
# backed further up the trunk, five times, and the character died with the wolf at full
# health (run 20260924T054447-632295). Every other draw-out turns round and runs clear
# instead; forward is the way a walk gets down off whatever the character is standing on.
RUN_CLEAR_S = 2.0

# A toggle's new state reaches the radio a paint or two after the key. Pressing it again
# inside this window would read the old state and switch it straight back.
TOGGLE_SETTLE_S = 1.0

# Looking round for a plate before Tab, at the measured turn rate. The camera shows about
# a hundred degrees, so views at most 96 degrees apart, the last within 96 of the first,
# see all of the circle within nameplate distance: the starting view and three quarter
# turns. With a humaniser the way round is drawn per look-round and each turn is a drawn
# 84-96 degrees, with a fourth whenever three fell short - the same coverage, without the
# same three quarter turns to the right at every stop.
SCAN_TURN_S = math.radians(90.0) / TURN_RATE_SEED
SCAN_TURN_DEG = (84.0, 96.0)
SCAN_SWEEP_DEG = 264.0

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

# Hold the heal while the target will die this much sooner than the character would.
#
# Holy Light is a two and a half second cast that stops the swings, and pushback makes it
# four. A level 6 paladin between two Mangy Wolves healed at 44% with its target at 18%,
# two swings from dead; the target sat at 18% through two casts, the mana ran out, and it
# died with both wolves alive (run 20260924T050644-f9f9fa). Killing one first halves what
# the heal has to outpace. Rates only once each has this much evidence behind it, and
# never below the floor, where there is no margin left to be wrong with.
#
# The floor is a quarter. At 15% a trained paladin held its heal nine times against a
# wolf "a second or three from dead" that took five more; it won at 14%, spent Divine
# Protection on the kill, and died to the next wolf with nothing left (run
# 20260924T114311-570633). A save and a cast need a few seconds of health: below a
# quarter, the heal - behind its save - comes now.
# A save (Divine Protection: six seconds immune) is worth only the heal it clears the way
# for: the heal comes next, whatever the usual line, while the save still has time for a
# cast to finish inside it. At 40% the save went up, then a stun, and health sat at exactly
# 40% - not below the heal's line - until the immunity ran out; the heal came after it and
# was pushed back to nothing (run 20260924T122236-108178).
SAVE_HEAL_WINDOW_S = 3.5
SAVE_HEAL_BELOW = 0.8
# How long a fight begun below the heal's line spends on its save and heal before it
# looks for the attacker, and how long with nothing pressable before it gives that up.
HEAL_FIRST_S = 8.0
# In a fight, facing the target may take this long by the clock before the fight falls back
# to swinging by the client's own errors (`Targeting.face_selected`, `deadline_s`).
FIGHT_FACE_S = 3.0
# The heal lines a fight may hold (`Fight.heal_below`), learned from how fights went
# (`jev.learn.choices`, "fight.heal_below"): a fight that fell below `BAD_FIGHT_HP`, or
# died, went badly. Of 999 fights where something attacked first, 42% of those that began
# below 60% health went badly against 4% above it (25 September).
HEAL_LINES = ("0.40", "0.50", "0.60")
# A caster whose spell does not reach yet (the client's "out of range") steps this long
# toward the unit, facing it first, at most this many times a fight (V164).
RANGED_STEP_S = 0.8
MAX_RANGED_STEPS = 8
BAD_FIGHT_HP = 0.15
HEAL_FIRST_IDLE_S = 0.8

FINISH_MARGIN = 0.8
FINISH_EVIDENCE_S = 3.0
FINISH_WINDOW_S = 6.0
FINISH_FLOOR = 0.25

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
    # A right-button nudge: mouse-look turns the character to face where the camera looks
    # (`Camera.face`), which a turn by the keys cannot, since the camera turns with it.
    realign: Callable[[], object] | None = None
    targeting: Targeting | None = None
    # The zone's map box, to measure the approach in yards; without it, blocked walks are
    # not noticed.
    bounds: ZoneBounds | None = None
    # The health a fight heals below; each fight's is a learned choice when `choices` is
    # given (a `jev.learn.choices.Choice`).
    heal_below: float = HEAL_IN_COMBAT
    choices: object | None = None

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
    # The selected unit's own identity, where the strip paints it (schema 14).
    _selected_guid: int | None = field(default=None, init=False)
    _realigned: bool = field(default=False, init=False)
    _aim_code: FaceCode | None = field(default=None, init=False)
    # The selected unit's plate at the last facing look: where a corpse will lie.
    last_plate: Plate | None = field(default=None, init=False)
    # The name of the unit the last fight killed, for finding its corpse by hover.
    killed_name_id: int | None = field(default=None, init=False)
    _xp_start: tuple | None = field(default=None, init=False)
    _target_level: int | None = field(default=None, init=False)
    _strides: int = field(default=0, init=False)
    _approach_s: float = field(default=0.0, init=False)
    # Strafes off a blocked approach this fight, the side the next one goes, and how long.
    sidesteps: int = field(default=0, init=False)
    # Steps in a row at an attacker in melee that brought no swing and no hit, and how
    # often this fight has moved off for such an attacker to follow.
    _unanswered: int = field(default=0, init=False)
    _draw_outs: int = field(default=0, init=False)
    _side: int = field(default=1, init=False)
    _sidestep_s: float = field(default=SIDESTEP_S, init=False)
    _still_steps: int = field(default=0, init=False)
    # The last evidence a swing reached (a resolved swing or damage), and the swing count.
    _reach_at: float | None = field(default=None, init=False)
    _swings: int | None = field(default=None, init=False)
    # The current selection came from Tab in this fight, so it lies ahead of the character.
    _ahead: bool = field(default=False, init=False)
    # Where the Tab pick's selection mark appeared, as a fraction of the width off centre.
    _mark_offset: float | None = field(default=None, init=False)
    _error_count: int | None = field(default=None, init=False)
    # Facing by the client's own errors, the selected plate unproved: an attacker in melee.
    _blind_melee: bool = field(default=False, init=False)
    _damage_seen: bool = field(default=False, init=False)
    _input_refused: bool = field(default=False, init=False)
    detail: str = field(default="", init=False)
    _last_use: dict[int, float] = field(default_factory=dict, init=False)
    # When each lasting buff (an aura, a blessing) was last pressed, by name. Kept between
    # fights, unlike `_last_use`: a ten-minute blessing pressed every fight is a global
    # cooldown thrown away each time. A death takes them all (`buffs_lost`).
    _lasting: dict[str, float] = field(default_factory=dict, init=False)
    # When this fight's save went up: the heal comes next (`SAVE_HEAL_WINDOW_S`).
    _saved_at: float | None = field(default=None, init=False)
    # The last press not yet answered by the client (`_press_answered`): the ability, when,
    # and the clocks as they were before it, to put back if it came to nothing.
    _pending_press: tuple | None = field(default=None, init=False)
    # The slot whose presses have gone unanswered, and how many times in a row.
    _dropped: tuple[int, int] = field(default=(0, 0), init=False)
    # (time, our health, target health, casting, target guid), this fight: who dies first.
    _race: list[tuple] = field(default_factory=list, init=False)
    # The last look the evidence clocks were advanced to (`_hold_clocks_while_casting`).
    _look_at: float | None = field(default=None, init=False)
    # The lowest health this fight saw, for how it went.
    _low_hp: float | None = field(default=None, init=False)
    # A caster's fight cast from range (V164): its last look was cast from where the unit
    # was found, not in melee, so a kill lies out there (`ended_far`) and the loot walks to it.
    ended_far: bool = field(default=False, init=False)
    _from_range: bool = field(default=False, init=False)
    _last_near: bool = field(default=False, init=False)
    _ranged_steps: int = field(default=0, init=False)

    # -- the skill -----------------------------------------------------------

    @traced("fight")
    def run(self, name_id: int | None = None, *, timeout_s: float = 45.0) -> Fought:
        """Select, engage, and hold the rotation until something settles it.

        With `choices`, the fight's heal line is drawn from what each line has done, and
        how the fight went is recorded for it: badly when it died or fell below
        `BAD_FIGHT_HP`. A fight that never came to blows teaches nothing."""
        line = None
        # A class with no heal has no line to draw, and its fights say nothing of one.
        heals = self.profile is None or self.profile.first(Role.HEAL) is not None
        if self.choices is not None and heals:
            line = self.choices.pick("all", HEAL_LINES)
            self.heal_below = float(line)
        self._low_hp = None
        self.ended_far = self._from_range = self._last_near = False
        self._ranged_steps = 0
        started = time.monotonic()
        result = None
        try:
            result = self._fight(name_id, timeout_s)
            self.ended_far = (result is Fought.KILLED and self._from_range
                              and not self._last_near)
            return result
        finally:
            low = self._low_hp
            went_badly = result is Fought.DIED or (low is not None and low < BAD_FIGHT_HP)
            came_to_blows = result in (Fought.KILLED, Fought.DIED, Fought.LOSING, Fought.TIMEOUT,
                                       Fought.UNREACHABLE, Fought.LOST)
            # Cut short (a death, a stop): known only when it was going badly.
            if line is not None and (came_to_blows or (result is None and went_badly)):
                self.choices.outcome("all", line, not went_badly, time.monotonic() - started)

    def _fight(self, name_id: int | None, timeout_s: float) -> Fought:
        self.pressed = []
        self.closed = 0
        self.broken = False
        if self.level is not None and self.level() is False:
            self.detail = "camera input refused"
            return Fought.REFUSED
        self._toggled = False
        self._pending_heal = None
        self._pending_press = None
        self._dropped = (0, 0)
        self._race = []
        self._look_at = None
        self._damage_mark = None
        self._damage_seen = self._input_refused = False
        self._selected_name_id = None
        self._selected_guid = None
        self._aim_code = None
        self.last_plate = None
        self.killed_name_id = None
        self._xp_start = None
        self._target_level = None
        self._strides = 0
        self._approach_s = 0.0
        self.sidesteps = self._still_steps = self._unanswered = self._draw_outs = 0
        self._realigned = False
        self._sidestep_s = SIDESTEP_S
        h = humaniser(self.hid)
        self._side = -1 if h is not None and h.rng.random() < 0.5 else 1
        self._ahead = False
        self._blind_melee = False
        self._error_count = None
        self._damage_at = time.monotonic()
        self.last_hp = None
        self.detail = ""
        self._last_use = {}
        self._saved_at = None
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
        if in_combat and hp is not None and hp < self.heal_below:
            after = self._heal_first(v)
            if self._input_refused:
                return Fought.REFUSED
            if after is None:
                return Fought.BLIND
            if after.get("vitals.dead") is True or after.get("vitals.ghost") is True:
                self.detail = "the character died"
                return Fought.DIED
            v = after
            in_combat = v.get("vitals.combat") is True

        # Already engaged with something alive: that is the fight, and shopping for a
        # better one just adds a second attacker. Only if it is the one fighting us,
        # though: the client selects units on its own, and a Defias Thug standing at full
        # health, neither biting nor near, stayed selected for 40 s while another killed
        # the character, every fight spent trying to prove the bystander's plate (run
        # 20260924T064025-090aa8).
        bystander = (v.get("target.attacking_me") is False and v.get("target.in_melee") is False)
        engaged = (in_combat and v.get("target.has") is True
                   and v.get("target.hp") is not None and v["target.hp"] > DEAD_HP
                   and not bystander)
        # Already selected and alive, and the unit we came for: that is the fight. Whoever
        # selected it - the tutor, a previous look - re-acquiring could only swap it for
        # another of the same name, or for something else entirely.
        chosen = (not engaged and name_id is not None and v.get("target.has") is True
                  and v.get("target.name_id") == name_id
                  and isinstance(v.get("target.hp"), (int, float)) and v["target.hp"] > DEAD_HP
                  and not (in_combat and bystander))
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
            self._selected_guid = v.get("target.guid")
            self._damage_mark = v.get("target.hp")
            self.selected_plate = None
        if not self.engage(v) and not self._fight_blind(v):
            if self._aim_code is not FaceCode.NOT_VISIBLE:
                return self._aim_failure()
            # Kept from before this fight and not on screen: nothing says it is ahead or
            # near, so choose again from what is. Measured: one stale far selection
            # absorbed twenty-eight fights while the hunt walked between spots.
            #
            # And a selection made moments ago whose plate cannot be proved - a unit of the
            # same name in front of it answering every hover - is chosen again once, by a
            # click that proves itself; any unit of the wanted name will do for a kill
            # (seven such fights gave up in run 20260924T002817-cee9c2).
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
            hp = v.get("target.hp")
            # The client can move the selection on at the kill itself: a Timber Wolf at 20%
            # gave way to another unit at full health on the tick its experience arrived,
            # the kill went unlooted, and the quest's meat with it (run
            # 20260923T233909-8b1484). Nothing but a kill grants experience in a fight. The
            # next unit can share the name: a Kobold Worker at 19% became another at full
            # health far off, and the fight chased that one until its plate was lost (run
            # 20260924T013702-7f5692).
            risen = (isinstance(hp, (int, float)) and isinstance(self.last_hp, (int, float))
                     and hp - self.last_hp >= REPLACED_HP_RISE)
            guid = v.get("target.guid")
            if self._selected_guid is None:
                self._selected_guid = guid
            other = (guid is not None and self._selected_guid is not None
                     and guid != self._selected_guid)
            if risen or other or (self._selected_name_id is not None
                                  and v.get("target.name_id") != self._selected_name_id):
                if self._gained(v) or self._grey_gone() or self._experience_follows():
                    self.killed_name_id = self._selected_name_id
                    return Fought.KILLED
                self.detail = "selected target changed during fight"
                return Fought.LOST
            if hp is not None:
                self.last_hp = hp
            if isinstance(v.get("target.level"), int):
                self._target_level = v["target.level"]
            if hp == DEAD_HP:
                return self._settle(v)

            # Walking and swinging are the same loop, not one after the other.
            #
            # Closing used to be a gate: walk until the target takes damage, *then* start
            # the rotation. Damage comes from swinging, swinging is the rotation, and the
            # rotation was behind the gate — so a live run reported
            # `unreachable pressed [] closed 8` eight times over. It had walked at the
            # kobold and never once pressed anything at it.
            self._hold_clocks_while_casting(v)
            if self._note_damage(v) or self._note_swing(v):
                self._strides = 0              # progress: the approach budget starts again
                self._approach_s = 0.0

            self._last_near = v.get("target.in_melee") is True
            profile = self.profile or for_class(v.get("char.class_id"), v.get("char.race_id"))
            if self._ranged_ready(profile, v):
                # A caster with the mana casts from where it stands (V164). The client says
                # what is wrong with a cast - too far, not facing, out of sight - and each
                # is answered with the least movement that cures it, never during a cast.
                self._from_range = True
                error = self._new_error(v)
                casting = v.get("bars.casting") is True
                if error == "not_facing" and not casting:
                    if not self.engage(v):
                        return self._aim_failure()
                elif error == "out_of_range" and not casting:
                    if self._ranged_steps >= MAX_RANGED_STEPS:
                        self.detail = (f"stepped in {self._ranged_steps} times and the spell "
                                       "still does not reach; cannot reach it")
                        return Fought.UNREACHABLE
                    self._ranged_steps += 1
                    if not self._range_step(v):
                        return Fought.REFUSED if self._input_refused else self._aim_failure()
                elif error == "no_line_of_sight" and not casting:
                    self._sidestep("los")
                    if self._input_refused:
                        return Fought.REFUSED
                self._rotate(v)
                if self._input_refused:
                    return Fought.REFUSED
                time.sleep(pace(self.hid, 0.2))
                continue
            self._from_range = False

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
            if wrong_way and not self._blind_melee and self._aim_code is FaceCode.FACED:
                # "Facing the wrong way" is the client saying the unit is in reach and
                # behind, while its plate stands on the centre line. Two causes: a unit
                # directly behind the character projects there as surely as one ahead -
                # trusting the plate, the fight walked away from a Mangy Wolf until it killed
                # the character (run 20260924T035309-97796e) - or the camera no longer looks
                # where the character faces, so turning round by the keys flips the wolf from
                # one "centred, wrong way" to the next, 16 s without a hit (run ...0436).
                # First make the character face where the camera looks; if the client still
                # says "wrong way", the unit is behind, and it turns round.
                if self.realign is not None and not self._realigned:
                    self._realigned = True
                    event("engage.realign")
                    if self.realign() is False:
                        self._input_refused = True
                        self.detail = "camera realign refused"
                        return Fought.REFUSED
                elif not self._turn_round():
                    return Fought.REFUSED
                else:
                    self._realigned = False
                self._reach_at = time.monotonic()
                self._aim_code = None              # the next aim proves the plate afresh
                wrong_way = False
            if self._blind_melee and v.get("target.in_melee") is not True:
                self._blind_melee = False          # it left reach: aim by its plate again
            if self._blind_melee:
                # The client says where it is: a swing at something behind is "facing the
                # wrong way", and melee reaches the whole front half. A quarter turn, always
                # the same way, not round: a Mangy Wolf at the character's side stayed at
                # its side through eight half turns, forty seconds at 5% health, until the
                # character died (run 20260924T082110-0f56c6). Four quarters face anything.
                if wrong_way and not self._turn_quarter():
                    return Fought.REFUSED
            elif in_reach:
                # Stand and swing. Turn back only on evidence the swings are not landing.
                if (wrong_way or stalled) and not self.engage(v) and not self._fight_blind(v):
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
            time.sleep(pace(self.hid, 0.2))

        self.detail = f"{timeout_s:.0f}s and it is still standing"
        return Fought.TIMEOUT

    # -- pieces --------------------------------------------------------------

    @traced("target.acquire")
    def acquire(self, name_id: int | None, *, defend: bool = False) -> Fought | None:
        """Select something worth fighting. `None` means it worked.

        Nameplates first, because a plate means the client is drawing the unit near enough
        to fight, and `Tab` does not care how far away or how occluded its pick is. With
        no wanted plate in view the character looks round in turns of about a quarter
        before `Tab`; not in self-defence, where whatever is hitting us is chosen by `Tab`
        and found by the facing search.
        """
        self.selected_plate = None
        self._ahead = False
        self._mark_offset = None
        self._targeting().cancel_pending_spell()
        h = humaniser(self.hid)
        turn = getattr(self.hid, "TURN_RIGHT", "d")
        if h is not None and h.rng.random() < 0.5:
            turn = getattr(self.hid, "TURN_LEFT", "a")
        swept, look = 0.0, 0
        turned_round = False
        while True:
            picked = self._pick_plate(name_id, defend)
            if picked is not False:
                return picked
            if defend:
                # Tab picks in front of the character, and an attacker behind it is out of
                # reach: run 20260923T175710-b5044f pressed Tab three times, found nothing,
                # and was hit from behind. Turn round once and look again.
                chosen = self.select(name_id, defend=True)
                if chosen is not Fought.NO_TARGET or turned_round:
                    return chosen
                turned_round = True
                seconds = math.pi / TURN_RATE_SEED
                event("acquire.turn_round", data={"key": turn, "seconds": round(seconds, 3)})
                if not self.hid.hold(turn, seconds, exact=True):
                    self.detail = "scan input refused"
                    return Fought.REFUSED
                if self._targeting().wait_for_paint().code is PaintCode.BLIND:
                    self.detail = "radio lost while turning round"
                    return Fought.BLIND
                continue
            if swept >= SCAN_SWEEP_DEG:
                return self.select(name_id, defend=defend)
            look += 1
            seconds = (SCAN_TURN_S if h is None
                       else math.radians(h.rng.uniform(*SCAN_TURN_DEG)) / TURN_RATE_SEED)
            event("acquire.scan", data={"look": look, "key": turn, "seconds": round(seconds, 3)})
            # Exact: the turn is drawn already, and coverage is counted from it.
            if not self.hid.hold(turn, seconds, exact=True):
                self.detail = "scan input refused"
                return Fought.REFUSED
            swept += math.degrees(held(self.hid, seconds) * TURN_RATE_SEED)
            if self._targeting().wait_for_paint().code is PaintCode.BLIND:
                self.detail = "radio lost while looking round"
                return Fought.BLIND

    def _pick_plate(self, name_id: int | None, defend: bool) -> Fought | bool | None:
        """Select a plate in the current view: `None` selected, `False` none acceptable.

        Defending, whatever is attacking us is chosen before anything of the wanted name
        that is not: a bystander of the quest's own kind is still a fight, but not while
        something else is killing the character."""
        frame = self.read_frame()
        if frame is None:
            return False
        candidates = self._candidates(frame)
        for attackers_only in ((True, False) if defend else (False,)):
            for plate in candidates:
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
                      "point": list(point), "attackers_only": attackers_only})
                if not self.hid.click(*point):
                    self.detail = "selection input refused"
                    return Fought.REFUSED
                paint = self._targeting().wait_for_paint()
                if paint.code is not PaintCode.FRESH:
                    self.detail = paint.detail
                    return Fought.BLIND
                if self._acceptable(name_id, defend=defend, values=paint.after,
                                    attackers_only=attackers_only) is True:
                    self.selected_plate = self.last_plate = plate
                    return None
        return False

    def _candidates(self, frame) -> list[Plate]:
        """Plates worth clicking: alone first, then central. An ordering, not an ID.

        Isolation leads because a pull in the middle of a camp is what killed this
        character twice, and a plate with no neighbour is the best available evidence that
        a mob has none either.
        """
        # Red too: aggressive units - the Mangy Wolves and Defias round Goldshire - have
        # red plates, and a search without it could only find them by Tab.
        plates = find_plates(frame, colours=PROPOSAL_COLOURS)
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
                    values: dict | None = None, attackers_only: bool = False) -> bool | None:
        """Is what we just selected worth fighting? `None` if nothing is readable."""
        v = values if values is not None else self.read()
        self._observe(v)
        event("selection.expected", data={"wanted_name_id": name_id, "defend": defend,
                                          "attackers_only": attackers_only})
        if v is None:
            return None
        if v.get("target.has") is not True:
            return False
        hp = v.get("target.hp")
        if hp is not None and hp <= DEAD_HP:
            return False                       # a corpse is selectable and not a fight
        if attackers_only and v.get("target.attacking_me") is not True:
            return False
        if (name_id is None or v.get("target.name_id") == name_id
                or (defend and v.get("target.attacking_me") is True)):
            self._selected_name_id = v.get("target.name_id")
            self._selected_guid = v.get("target.guid")
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
        h = humaniser(self.hid)
        for _ in range(MAX_SELECTS if h is None else h.rng.randint(*SELECTS_DRAWN)):
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
            if defend and v.get("target.attacking_me") is not True:
                continue                       # defending: what hits us, not a bystander
            self._selected_name_id = v.get("target.name_id")
            self._selected_guid = v.get("target.guid")
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
            search_s=FACE_SEARCH_MAX_S if fighting else 0.0,
            stop=self._hurt if fighting else None,
            deadline_s=FIGHT_FACE_S if fighting else None)
        if result.code is FaceCode.INTERRUPTED and fighting:
            # Hurt while looking for it: the heal needs no facing, so it comes first, and
            # then the look again (session 126: three gnolls, twelve seconds of looking).
            event("engage.heal_first", detail=result.detail)
            self._heal_first(self.read())
            if self._input_refused:
                return False
            result = self._targeting().face_selected(
                expected_name_id=self._selected_name_id, hint=self.last_plate,
                search_s=FACE_SEARCH_MAX_S, deadline_s=FIGHT_FACE_S)
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

    def _hold_clocks_while_casting(self, values: dict) -> None:
        """A cast stops the swings, so no hit or swing can arrive while one runs: the
        clocks that wait for that evidence stand still for it.

        Otherwise every heal reads as a lost target. Each Holy Light, four seconds under
        pushback, aged the last hit past `REAIM_AFTER_S` and `REACH_HOLD_S`; the fight
        then stepped at a wolf already in melee, levelled the camera and turned, eleven
        seconds without a swing after one heal, and ran out its 45 s with the wolf at
        30% (run 20260924T052148-85c63f).
        """
        now = time.monotonic()
        if self._look_at is not None and values.get("bars.casting") is True:
            held = now - self._look_at
            self._damage_at += held
            self._last_aim_at += held
            if self._reach_at is not None:
                self._reach_at += held
        self._look_at = now

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
            self._unanswered = 0
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
            self._unanswered = 0
        return new

    def _close(self, values: dict, near: bool, *, deadline: float | None = None) -> bool:
        """Walk at the faced target until a swing can reach it. `True` once one has.

        Far off, one continuous walk steered on the plate; near, a short step and a look.
        Either stops on the first resolved swing or damage, on a facing error, or when the
        selection is no longer this fight's living target.
        """
        began = time.monotonic()
        try:
            return self._approach(near, deadline, values)
        finally:
            self._approach_s += time.monotonic() - began

    def _approach(self, near: bool, deadline: float | None, values: dict | None = None) -> bool:
        if near:
            before = self._position(values)
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
            last = None
            while time.monotonic() < wait_until:
                time.sleep(min(pace(self.hid, CLOSE_LOOK_S),
                               max(0.0, wait_until - time.monotonic())))
                v = last = self.read()
                if v is None or not self._same_fight(v):
                    return False
                if self._note_damage(v) or self._note_swing(v):
                    return True
            if (last is not None and last.get("target.in_melee") is True
                    and last.get("target.attacking_me") is True):
                self._unanswered += 1
                if self._unanswered >= UNANSWERED_STEPS:
                    self._unanswered = 0
                    self._draw_out()
                    return False
            after = self._position(last)
            if before is None or after is None:
                return False
            if distance_yards(before, after, self.bounds) >= STEP_STILL_YARDS:
                self._still_steps = 0
                return False
            self._still_steps += 1
            if self._still_steps >= STILL_STEPS:
                self._still_steps = 0
                self._sidestep("step")
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
        steer_at = started + pace(self.hid, CLOSE_STEER_S)
        near_at = None
        trail: deque = deque()
        blocked = False
        try:
            while time.monotonic() < stop_at:
                time.sleep(min(pace(self.hid, CLOSE_LOOK_S),
                               max(0.0, stop_at - time.monotonic())))
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
                if self._blocked(trail, v):
                    blocked = True
                    break
                if time.monotonic() >= steer_at:
                    steer_at = time.monotonic() + pace(self.hid, CLOSE_STEER_S)
                    seen = targeting.track_selected(plate, window_dy=CLOSE_TRACK_DY)
                    if seen is not None:
                        plate, offset = seen
                        self.last_plate = plate
                        if abs(offset) > CLOSE_STEER_TOLERANCE and not targeting.turn_toward(offset):
                            self._input_refused = True
                            self.detail = "turn input refused"
                            return False
        finally:
            self.hid.key_up("w")
        if blocked:
            self._sidestep("walk")
        return False

    def _position(self, values: dict | None) -> tuple[float, float] | None:
        """Where the character is, as a map fraction; `None` unread, or with no zone box."""
        if self.bounds is None or not values:
            return None
        mx, my = values.get("pos.mx"), values.get("pos.my")
        return None if mx is None or my is None else (mx, my)

    def _blocked(self, trail: deque, values: dict) -> bool:
        """Forward held `BLOCKED_AFTER_S` with less than `BLOCKED_YARDS` to show for it."""
        here = self._position(values)
        if here is None:
            return False
        now = time.monotonic()
        trail.append((now, here))
        while len(trail) > 1 and now - trail[1][0] >= BLOCKED_AFTER_S:
            trail.popleft()
        return (now - trail[0][0] >= BLOCKED_AFTER_S
                and distance_yards(trail[0][1], here, self.bounds) < BLOCKED_YARDS)

    def _draw_out(self) -> None:
        """Move off from an attacker that bites and cannot be hit, for it to follow out:
        back off first, then turn round and run clear, alternately."""
        self._draw_outs += 1
        if self._draw_outs % 2 == 1:
            event("approach.draw_out", data={"key": "s", "seconds": DRAW_OUT_S,
                                              "closed": self.closed})
            if not self.hid.hold("s", DRAW_OUT_S):
                self._input_refused = True
                self.detail = "back-off input refused"
            return
        event("approach.run_clear", data={"key": "w", "seconds": RUN_CLEAR_S,
                                           "closed": self.closed})
        if not self._turn_round():
            return
        if not self.hid.hold("w", RUN_CLEAR_S):
            self._input_refused = True
            self.detail = "run-clear input refused"
            return
        self._aim_code = None                   # the next look faces it afresh

    def _sidestep(self, mode: str) -> None:
        """Strafe off a blocked line to the unit; the next approach faces it again."""
        key = (getattr(self.hid, "STRAFE_RIGHT", "e") if self._side > 0
               else getattr(self.hid, "STRAFE_LEFT", "q"))
        seconds = self._sidestep_s
        self.sidesteps += 1
        event("approach.sidestep", data={"key": key, "seconds": round(seconds, 3),
                                          "mode": mode, "closed": self.closed})
        before = self._position(self.read())
        if not self.hid.hold(key, seconds):
            self._input_refused = True
            self.detail = "sidestep input refused"
            return
        self._side = -self._side
        self._sidestep_s = min(MAX_SIDESTEP_S, 2 * self._sidestep_s)
        after = self._position(self.read())
        if (before is not None and after is not None
                and distance_yards(before, after, self.bounds) < STEP_STILL_YARDS):
            event("approach.back_off", data={"key": "s", "seconds": BACK_OFF_S})
            if not self.hid.hold("s", BACK_OFF_S):
                self._input_refused = True
                self.detail = "back-off input refused"

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

    def _ranged_ready(self, profile: CombatProfile, values: dict) -> bool:
        """A caster (`CombatProfile.caster`) with a spell cast from range that it can use now:
        the client says the slot is usable, which a spell is not without its mana. Out of
        mana, a caster fights with its staff until the mana comes back (V164)."""
        if not profile.caster:
            return False
        usable = values.get("bars.usable")
        for attack in profile.by_role(Role.ATTACK):
            if not ranged(attack):
                continue
            if usable is not None:
                if usable & (1 << (attack.slot - 1)):
                    return True
            elif self._mana_left_after(attack, values) >= 0:
                return True
        return False

    def _range_step(self, values: dict) -> bool:
        """One step toward a unit a spell does not reach yet, facing it first (V164)."""
        if not self.engage(values):
            return False
        event("approach.request", data={"key": "w", "mode": "range_step",
                                        "duration_s": RANGED_STEP_S, "steps": self._ranged_steps})
        if not self.hid.hold("w", RANGED_STEP_S):
            self._input_refused = True
            self.detail = "approach input refused"
            return False
        self.closed += 1
        return True

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
        if toggle is None or not self._toggle_needed(toggle, v) or self._ranged_ready(profile, v):
            return True                        # a caster with the mana opens with a spell
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

    def _fight_blind(self, values: dict) -> bool:
        """Fight an attacker in melee whose plate could not be proved. `True` if taken up.

        A plate is proved by hovering the body beneath it, and two of a kind side by side
        answer for each other: two Defias Thugs, one hover landing on the other, three
        fights in a row gave up "not visible" while the pair beat the character to death
        (run 20260924T002817-cee9c2). Something hitting us in melee is within reach; the
        swing is on, and "facing the wrong way" is the only bearing needed.
        """
        # Or proved and never settled on the centre line: a Mangy Wolf in melee drifted
        # right faster than the pulses turned, and eight turns left its plate 0.18 of the
        # width off centre - well inside the front half a swing reaches - and the fight was
        # given up at full health while the wolf bit (run 20260924T033806-a3254d).
        if (self._aim_code not in (FaceCode.NOT_VISIBLE, FaceCode.UNSETTLED)
                or values.get("vitals.combat") is not True
                or values.get("target.attacking_me") is not True
                or values.get("target.in_melee") is not True):
            return False
        event("engage.blind_melee", data={"name_id": values.get("target.name_id")})
        self._blind_melee = True
        self._last_aim_at = time.monotonic()
        return self._ensure_attacking()

    def _turn_quarter(self) -> bool:
        turn = getattr(self.hid, "TURN_RIGHT", "d")
        seconds = (math.pi / 2) / TURN_RATE_SEED
        event("engage.turn_quarter", data={"key": turn, "seconds": round(seconds, 3)})
        if not self.hid.hold(turn, seconds, exact=True):
            self._input_refused = True
            self.detail = "turn input refused"
            return False
        return True

    def _turn_round(self) -> bool:
        turn = getattr(self.hid, "TURN_RIGHT", "d")
        seconds = math.pi / TURN_RATE_SEED
        event("engage.turn_round", data={"key": turn, "seconds": round(seconds, 3)})
        if not self.hid.hold(turn, seconds, exact=True):
            self._input_refused = True
            self.detail = "turn input refused"
            return False
        return True

    def _aim_failure(self) -> Fought:
        if self._input_refused:
            return Fought.REFUSED
        return {FaceCode.REFUSED: Fought.REFUSED, FaceCode.BLIND: Fought.BLIND,
                FaceCode.INTERRUPTED: Fought.INTERRUPTED,
                FaceCode.NO_TARGET: Fought.LOST, FaceCode.WRONG_TARGET: Fought.LOST,
                FaceCode.WRONG_KIND: Fought.LOST}.get(self._aim_code, Fought.NOT_VISIBLE)

    def _heal_first(self, values: dict) -> dict | None:
        """A fight already under way below the heal's line heals before it looks for the
        attacker: a heal needs no target and no facing. At 17% the selection and facing of
        a fresh attacker took five seconds with nothing pressed, and the heal came at 5%
        (run 20260924T132256-fc8503). The save, then the heal, as the rotation has them.
        """
        deadline = time.monotonic() + HEAL_FIRST_S
        v, idle_since = values, None
        while time.monotonic() < deadline:
            if (v is None or v.get("vitals.combat") is not True
                    or v.get("vitals.dead") is True or v.get("vitals.ghost") is True):
                return v
            hp = v.get("vitals.hp")
            saved = (self._saved_at is not None
                     and time.monotonic() - self._saved_at < SAVE_HEAL_WINDOW_S)
            if hp is None or (hp >= self.heal_below and not saved and self._pending_heal is None):
                return v
            busy = (v.get("bars.casting") is True or (v.get("bars.gcd") or 0.0) > 0.0
                    or self._pending_heal is not None)
            pressed = len(self.pressed)
            self._rotate(v, survival_only=True)
            if self._input_refused:
                return v
            if len(self.pressed) > pressed or busy:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > HEAL_FIRST_IDLE_S:
                return v                    # nothing to press: no heal ready, no mana
            time.sleep(pace(self.hid, 0.15))
            v = self.read()
            self._observe(v)
        return v

    def _rotate(self, values: dict, *, survival_only: bool = False) -> None:
        """Press the highest-priority row the client says is ready.

        Roles, not classes. There is no `if paladin` here and there is not going to be:
        the engine asks for a row with `role=heal` and presses it if the bars say it is
        ready, so a warrior is the same list with one fewer row and nothing changes.
        `survival_only` stops after the heal and its guards: no buff, no swing.
        """
        self._sample_race(values)
        if not self._press_answered(values):
            return
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

        hp = values.get("vitals.hp")
        in_combat = values.get("vitals.combat") is True

        # 0. A last resort (Lay on Hands: full health, all the mana, an hour's cooldown),
        #    for a fight about to be lost, where a heal would not finish in time.
        for last in profile.by_role(Role.LAST_RESORT):
            if in_combat and hp is not None and hp < LAST_RESORT_BELOW and pressable(last):
                self._press(last)
                return

        # 1. Stay alive. A heal is a global cooldown not spent swinging, so the line is
        #    low — but standing there at 20% because healing is "not the rotation" is how
        #    a character ends up running back from the graveyard.
        heal = profile.first(Role.HEAL)
        giving_up = self.heals_ignored >= HEAL_GIVE_UP and self.heals_landed == 0
        saved = (self._saved_at is not None
                 and time.monotonic() - self._saved_at < SAVE_HEAL_WINDOW_S)
        if (heal is not None and pressable(heal) and not giving_up
                # One at a time. `_watch_heal` is what decides whether the last one
                # landed, and pressing again before it answers is how a live fight got
                # `pressed [2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3]` - fifteen
                # Holy Lights, none of which healed anything, on a 2.5 second cast.
                and self._pending_heal is None
                and in_combat and hp is not None
                and (hp < self.heal_below or (saved and hp < SAVE_HEAL_BELOW))
                and self._has_mana_for(heal, values)
                and (saved or not self._finishes_first(values))):
            # Clear the way for it first, where the bar can: immune (Divine Protection),
            # or the attacker stunned (Hammer of Justice). Pushback is what left a level 6
            # paladin's Holy Lights unfinished for 22 s against one wolf. Under a save the
            # way is clear already.
            for guard in () if saved else (*profile.by_role(Role.SAVE),
                                           *profile.by_role(Role.STUN)):
                if (pressable(guard) and self._has_mana_for(guard, values)
                        and (guard.role is Role.SAVE
                             or values.get("target.attacking_me") is True)):
                    if self._press(guard) and guard.role is Role.SAVE:
                        self._saved_at = time.monotonic()
                    return
            if self._press(heal):
                self._pending_heal = (hp, time.monotonic())
            return
        if survival_only:
            return

        # 2. Keep the buffs up, and only when one is actually lapsing: `bars.ready` says a
        #    seal is pressable on every single tick, so without the interval the
        #    character stands there re-sealing and never swings. An aura, once pressed,
        #    lasts until a death; a blessing, its ten minutes, across fights.
        now = time.monotonic()
        reserve = self._mana_reserve(profile) if in_combat else 0

        def affordable(a: Ability) -> bool:
            return self._mana_left_after(a, values) >= reserve

        for buff in (*profile.by_role(Role.AURA), *profile.by_role(Role.BUFF)):
            last = self._lasting.get(buff.name) if buff.lasting else self._last_use.get(buff.slot)
            if pressable(buff) and affordable(buff) and (last is None or now - last >= buff.every_s):
                if self._press(buff) and buff.lasting:
                    self._lasting[buff.name] = now
                return

        # 3. Swing. A toggle is pressed at most once and only before anything has landed,
        #    because pressing melee auto-attack while already swinging **stops** it. A
        #    caster with the mana casts instead: its staff is for when the mana is gone.
        casting_instead = self._ranged_ready(profile, values)
        for attack in profile.by_role(Role.ATTACK):
            if not pressable(attack) or not affordable(attack):
                continue
            if attack.toggle and (casting_instead or not self._toggle_needed(attack, values)):
                continue
            if self._press(attack):
                if attack.toggle:
                    self._toggled = True
                if attack.spends:
                    # Judgement released the seal: seal again on the next look.
                    for buff in profile.by_role(Role.BUFF):
                        if not buff.lasting:
                            self._last_use.pop(buff.slot, None)
            return

    def _sample_race(self, values: dict) -> None:
        """One look for `_finishes_first`. A new selection starts the samples again."""
        guid = values.get("target.guid")
        if self._race and self._race[-1][4] != guid:
            self._race = []
        self._race.append((time.monotonic(), values.get("vitals.hp"), values.get("target.hp"),
                           values.get("bars.casting") is True, guid))

    def _finishes_first(self, values: dict) -> bool:
        """Will the target die well before we do, at the rates this fight has shown?

        Our loss is read over the last `FINISH_WINDOW_S`, whatever we were doing. The
        target's is read only across looks we were not casting, because a cast stops
        the swings and counting it would make every fight with a heal in it look
        unwinnable. The samples are this selection's (`_sample_race`).
        """
        now = time.monotonic()
        hp, target = values.get("vitals.hp"), values.get("target.hp")
        if hp is None or target is None or hp < FINISH_FLOOR:
            return False

        dealt = swinging = 0.0
        for (t0, _, h0, casting, _), (t1, _, h1, _, _) in zip(self._race, self._race[1:], strict=False):
            if casting or h0 is None or h1 is None:
                continue
            swinging += t1 - t0
            dealt += max(0.0, h0 - h1)
        recent = [(t, h) for t, h, *_ in self._race if h is not None and now - t <= FINISH_WINDOW_S]
        if swinging < FINISH_EVIDENCE_S or dealt <= 0.0 or len(recent) < 2:
            return False
        span = recent[-1][0] - recent[0][0]
        lost = recent[0][1] - recent[-1][1]
        if span < FINISH_EVIDENCE_S or lost <= 0.0:
            return False
        to_kill = target / (dealt / swinging)
        to_die = hp / (lost / span)
        finishing = to_kill < FINISH_MARGIN * to_die
        if finishing:
            event("heal.held", data={"hp": hp, "target_hp": target,
                                     "to_kill_s": round(to_kill, 1), "to_die_s": round(to_die, 1)})
        return finishing

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
            time.sleep(pace(self.hid, 0.4))
            v = self.read()
            self._observe(v)
            if v is None:
                return False
            hp = v.get("vitals.hp")
            if hp is not None and hp > before + 0.02:
                return True
        return False

    def buffs_lost(self) -> None:
        """A death took every buff: auras and blessings are pressed again at the next fight."""
        self._lasting.clear()
        self._pending_press = None

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
        now = time.monotonic()
        if not ability.toggle:
            self._pending_press = (ability, now, dict(self._last_use), dict(self._lasting),
                                   self._saved_at)
        self._last_use[ability.slot] = now
        self.pressed.append(ability.slot)
        return True

    def _press_answered(self, values: dict) -> bool:
        """Settle the last press. False while the client may still answer it.

        A press the client acts on starts a cast, the global cooldown or the slot's own
        cooldown within a look or two. One it drops - pressed under a stun, say - leaves
        none of them, and counting it anyway cost a level 9 paladin its seal for 25 s
        against a Defias Bandit: Snap Kick's stun swallowed the press, the seal was stamped
        as up, and Judgement stayed unusable until the stamp ran out; the heal it pressed
        under the next stun held off the heal row for its whole 2.5 s watch while the
        character sealed at 12% and died (run 20260924T140621-fc3531). So an unanswered
        press undoes all it stamped - its clocks, a save's window, a heal's watch - and
        the rotation chooses again.
        """
        if self._pending_press is None:
            return True
        ability, when, last_use, lasting, saved_at = self._pending_press
        ready = values.get("bars.ready")
        gcd = values.get("bars.gcd")
        if (values.get("bars.casting") is True or (gcd is not None and gcd > 0.0)
                or (ready is not None and not ready & (1 << (ability.slot - 1)))):
            self._pending_press = None
            self._dropped = (0, 0)
            return True
        age = time.monotonic() - when
        if age < PRESS_ANSWER_S:
            return False
        self._pending_press = None
        if age > PRESS_TELL_S:
            return True                        # its global cooldown would be over by now
        slot, times = self._dropped
        times = times + 1 if slot == ability.slot else 1
        event("ability.unanswered", data={"slot": ability.slot, "role": ability.role.value,
                                          "times": times})
        if times >= PRESS_GIVE_UP:
            self._dropped = (0, 0)
            return True                        # counted after all, as before
        self._dropped = (ability.slot, times)
        self._last_use, self._lasting, self._saved_at = last_use, lasting, saved_at
        if ability.role is Role.HEAL:
            self._pending_heal = None
        return True

    @staticmethod
    def _mana_reserve(profile) -> int:
        """Mana kept back in a fight for one heal and the save before it.

        A level 10 paladin re-sealed after every Judgement (40 mana a seal), came out of
        its fight at 18% - 54 of 300 mana, short of Holy Light's 60 - and died under its
        own Divine Protection with no heal to cast (session 88, run 20260924T225929-5a1ebe).
        A seal or a strike that would leave less than this waits; damage is lost, not the
        character. No heal, no reserve: a warrior spends its rage as before.
        """
        heal = profile.first(Role.HEAL)
        if heal is None or not heal.mana:
            return 0
        save = profile.first(Role.SAVE)
        return heal.mana + (save.mana if save is not None else 0)

    @staticmethod
    def _mana_left_after(ability: Ability, values: dict) -> float:
        """Mana after pressing `ability`; plenty when the pool or its cost is unknown."""
        frac, pool = values.get("vitals.power"), values.get("vitals.power_max")
        if not ability.mana or frac is None or not pool:
            return math.inf
        return frac * pool - ability.mana

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
        A grey target grants none, and its vanish below half health is taken as the kill.
        A caller that needs certainty still counts `quests.o0_have`.
        """
        event("fight.last_health", data={"target_hp": self.last_hp,
                                         "target_level": self._target_level})
        if (self.last_hp == 0.0 or self._gained(values) or self._grey_gone()
                or self._experience_follows()):
            self.killed_name_id = self._selected_name_id
            return Fought.KILLED
        self.detail = "target disappeared without observed death"
        return Fought.LOST

    def _grey_gone(self) -> bool:
        """The fought unit was grey to the character and last seen below half health."""
        mine = self._xp_start[0] if self._xp_start else None
        return (isinstance(self._target_level, int) and isinstance(mine, int)
                and self._target_level <= grey_level(mine)
                and isinstance(self.last_hp, (int, float)) and self.last_hp < GREY_KILL_HP)

    def _experience_follows(self) -> bool:
        """Watch briefly for the experience of a kill; it can lag the selection by a paint."""
        for _ in range(SETTLE_LOOKS):
            time.sleep(pace(self.hid, SETTLE_LOOK_S))
            later = self.read()
            self._observe(later)
            if self._gained(later):
                return True
        return False

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

    def _hurt(self, values: dict) -> str | None:
        """Below the heal line in a fight: why a search should stop for the heal."""
        hp = values.get("vitals.hp")
        if (values.get("vitals.combat") is True and isinstance(hp, (int, float))
                and not isinstance(hp, bool) and hp < self.heal_below):
            return f"{hp:.0%} health in a fight: the heal first"
        return None

    def _observe(self, values: dict | None) -> None:
        hp = None if values is None else values.get("vitals.hp")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool):
            self._low_hp = hp if self._low_hp is None else min(self._low_hp, hp)
        event("combat.observed", code="blind" if values is None else "readable",
              data={} if values is None else {key: values.get(key) for key in (
                  "vitals.hp", "vitals.power", "vitals.combat", "vitals.dead", "vitals.ghost",
                  "target.has", "target.name_id", "target.hp", "target.in_melee",
                  "target.attacking_me", "bars.casting", "bars.ready", "bars.usable")})
