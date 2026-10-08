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
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

import numpy as np

from jev.clients.hid import held, humaniser, pace
from jev.clients.plates import Plates
from jev.clients.plates import show as show_plates
from jev.clients.targeting import (
    FACE_HINT_SEARCH_S,
    FACE_SEARCH_MAX_S,
    FaceCode,
    HoverCode,
    PaintCode,
    Targeting,
)
from jev.clients.travel import MIN_TRAVEL_FOR_HEADING, TURN_RATE_SEED
from jev.guide.coords import ZoneBounds, distance_yards
from jev.learn.choices import DEATH_S
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
    by_value,
    instant_blow,
    lingers,
    ranged,
    reach,
    repeats,
)
from jev.world.training import spell as spell_facts

# The radio fraction preserves zero exactly. Low health is still a living target.
DEAD_HP = 0.0
# Weapon blows that ask for nothing but a weapon in the hand and their cost - no stance or
# form, no dodge to answer, no stealth, no dagger - so one greyed out with its cost paid is a
# broken weapon (`Fight.disarmed`, V393): not Overpower, Backstab, Ambush or a druid's.
PLAIN_BLOWS = frozenset({"Sinister Strike", "Heroic Strike", "Raptor Strike", "Cleave", "Slam",
                         "Hemorrhage", "Mortal Strike"})


def _blow_name(ability: Ability) -> str:
    """An ability's spell name, by the catalog where it knows the spell."""
    facts = spell_facts(ability.spell_id) if ability.spell_id is not None else None
    return getattr(facts, "name", None) or ability.name


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
# A slot the client refused as "not ready" while the bar painted it ready is left this long,
# and the rotation presses another row meanwhile (V251): the mage pressed one spell 19 times
# in 19 s against a murloc at 23% (session 201) and 12 in 13 s against two wolves (214),
# "Spell is not ready yet" each time, and died both times.
NOT_READY_HOLD_S = 4.0
# An attack that waits for the next swing (Heroic Strike, Raptor Strike, Maul) is answered by
# nothing the client shows: no cast, no global cooldown, nothing spent until the swing. Counted
# unanswered, it was undone and pressed again, each press replacing the one queued: 1,205
# refused "interrupted" in a quarter of an hour of the hive (28 Sep, 12:00-12:15), and the
# rotation waited out each answer. It is counted as done and left for about a swing (V306).
NEXT_SWING_HOLD_S = 2.5
# After a press the client acted on, no other is made before this (V285): its global cooldown
# ends later than the bar paints it. Of the mage's 78 presses refused as not ready (sessions
# 246-283), 75 came within 2 s of the press before; at 1.7 s after one, 106 presses were
# answered and 30 refused, at 1.8 s 222 and 6, and from 1.9 s none was refused. Each refusal
# cost a look and left its row out for `NOT_READY_HOLD_S`: Fireball, then Arcane Missiles.
GCD_GUARD_S = 1.9

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
# A target below this health that has turned from the character (it no longer attacks it)
# while still in melee is running, and a runner comes back with its camp: it is stunned
# where it stands (V188).
RUNNER_HP = 0.3
# How long a fight begun below the heal's line spends on its save and heal before it
# looks for the attacker, and how long with nothing pressable before it gives that up.
HEAL_FIRST_S = 8.0
# In a fight, facing the target may take this long by the clock before the fight falls back
# to swinging by the client's own errors (`Targeting.face_selected`, `deadline_s`).
FIGHT_FACE_S = 3.0
# The heal lines a fight may hold (`Fight.heal_below`), learned from how fights went
# (`jev.learn.choices`). Of 999 fights where something attacked first, 42% of those that began
# below 60% health went badly against 4% above it (25 September).
HEAL_LINES = ("0.40", "0.50", "0.60")
# A line is learned by the seconds a kill costs (V311), "fight.heal_cycle": each fight's
# outcome is its cycle, from its start to the next fight's that came to blows - the fight,
# the rest after it, the loot, the walk to the next - won when it killed, with a death charged
# `DEATH_S` more and never a win. Before, a line won when its fight did not go badly
# ("fight.heal_below", V158), which never charged the rest a low line leaves: 0.50 had won 213
# of 221 fights in the live memory (28 Sep) with no second of its rests counted.
HEAL_POINT = "fight.heal_cycle"
# A cycle counts at most this long: past it the time is a walk to a town or to the next
# quest, whatever line the fight held. Of 405 cycles between the live bot's heal-line fights
# to 28 Sep, the median was 49 s, the 90th percentile 140 s and the 95th 227 s.
CYCLE_MAX_S = 300.0
# A caster whose spell does not reach yet (the client's "out of range") steps this long
# toward the unit, facing it first, at most this many times a fight (V164).
RANGED_STEP_S = 0.8
# A blind melee's step at an attacker the client says is too far away (V207): about three
# yards, less than the reach it is short of.
BLIND_STEP_S = 0.4
MAX_RANGED_STEPS = 8
# A shooter's repeating shot (Auto Shot, V358) with no damage to the unit for this long is
# taken to have stopped or never begun: pressed again, and after `SHOT_GIVE_UP` such silences
# a fight the shooter fights in melee, as before. Two shots of a 2.8-3.0 s bow and a margin.
SHOT_SILENT_S = 7.0
SHOT_GIVE_UP = 2
# A mark (Hunter's Mark, V404) goes on a unit with at least this share of its health left: on
# one nearly dead it is a global cooldown for nothing. It lasts its spell's two minutes.
MARK_ABOVE = 0.5
MARK_S = 120.0
# A slow (Concussive Shot, V404) is pressed at a unit coming for the character and not yet at
# hand, not again while it lasts (its spell's 4 s).
SLOW_S = 4.0
# A shooter at hand with a unit that is not attacking it - a pet holding it, a unit running or
# held - backs off out of its shot's dead zone (V404): about nine yards at the walk backwards,
# where Auto Shot reaches it again (it does not inside the melee reach and five yards, the
# bridge's `min_range`); at most this many times a fight, and not again within this long. A
# unit attacking it follows at its own run, faster than a walk backwards, and is fought at hand.
DEAD_ZONE_STEPS = 3
DEAD_ZONE_AGAIN_S = 4.0
# After a root at contact (Frost Nova) a caster backs off this long, still facing: about
# nine yards at the walk backwards, out of the held unit's reach (V169).
STEP_CLEAR_S = 2.0
# With more than one unit counted attacking it steps aside instead, this long at a run (V271):
# about ten yards square to the one it faces, clear of one in front and one behind. Backing
# straight off the one faced took the level 10 mage into the Prowler behind it, held and still
# in reach, and it died with the one it faced at 28% (session 247).
STEP_ASIDE_S = 1.5
# A caster that has spent this share of its mana on a unit whose health never moved is not
# hurting it (V273): a unit that cannot reach the caster evades, and takes nothing. The fight
# ends unreachable, and the unit is not taken again for `UNHURT_S`. Twice the level 10 mage
# spent its mana from full to nothing so, and died to the unit when it came (a Mangy Wolf behind
# a tree at Crystal Lake, session 242; a Murloc Lurker, session 250).
# The root at contact is held for more than one attacker counted, or for a fight going badly:
# below this share of health (V275). Pressed at every first contact, it saved single fights 2% of
# health (median 22% lost before it, 20% after) and cost them 4 s (12 s to 16 s), and was cooling
# when a second attacker came: fights with two or more went from 33% of health lost to 47%, and
# the level 10 mage's deaths were such fights (sessions 239-253).
ROOT_HP = 0.5
# A root is stepped clear of only once the client has answered its press (V282): its slot
# cooling, the global cooldown or its mana gone, looked for this often until `PRESS_ANSWER_S`.
# At 45% health against one Prowler the mage pressed Frost Nova five times in 11 s, "Spell is
# not ready yet" four times while the bar painted it ready, and backed off two seconds after
# each: the Prowler followed and bit, 48% to 29% of the mage's health (session 275). One in
# nine of the mage's roots went so (18 of 167, sessions 246-279), in runs within a fight.
ROOT_LOOK_S = 0.15
# With a second attacker counted, one is held out of the fight (Polymorph, V287) and the other
# fought alone, then it. Fights with two attackers or more cost the level 12-13 mage 0.35 of
# its health against 0.21 for one, and were most of its deaths: Prowler pairs and packs
# (sessions 246-285). Since V395 every hold (`cc`: Fear, Gouge, Hibernate, Entangling Roots)
# takes the healthiest attacker that is not selected, found by Tab in at most `HOLD_TABS`
# presses: the selected one carries the fight's damage over time and Fireball's burn, which
# break a Polymorph or a Fear at the next tick, and 29.6% of the hive's deaths (7 Oct
# 01:25-03:50) were fights with two attackers or more in which no warlock, rogue or warrior
# held anything (hive-557, a warlock at 17: a second hyena 6.6 s in, its Fear never on the
# bar). Not while the selected one is about to die (`HOLD_FINISH_HP`); and the selected one
# itself only untouched (`HOLD_FRESH_HP`) when Tab finds no other.
HOLD_FINISH_HP = 0.2
HOLD_FRESH_HP = 0.98
HOLD_TABS = 4
# How long a unit held stays out of the fight as far as the fight is concerned when its spell
# does not say (Polymorph's first rank), and the longest its cast is followed, pushback
# included.
HELD_S = 20.0
HOLD_CAST_S = 4.0
# A guard (V396: Psychic Scream, Evasion, Shield Block, Retaliation) is pressed with two
# attackers or more at hand, or with the character's health falling `GUARD_RACE` times as fast
# as the selected unit's over the race's window, and at least `GUARD_LOSS` of it gone there.
# In the hive's deaths with two attackers or more no warlock, warrior, rogue, shaman or druid
# pressed a control or defensive spell at all, and of the level 12+ bars no priest's held
# Psychic Scream (0 of 36) nor any rogue's Evasion (0 of 37). One guard at a time, for as long
# as it lasts (`GUARD_S` where the spell does not say).
GUARD_RACE = 1.5
GUARD_LOSS = 0.1
GUARD_S = 8.0
# A caster with a wand shoots it (V397) at a unit below this health, a cast's mana saved for
# the next, and whenever no spell of its can be cast for want of mana, where it swung a staff.
# Priests, mages and warlocks rest 11-17% of their played time in the hive (7 Oct).
WAND_FINISH_HP = 0.25
UNHURT_MANA = 0.35
UNHURT_S = 60.0
# A press whose ability's mana has at least this share gone since it was pressed was
# answered, whatever the bar painted (V176).
ANSWER_SPENT = 0.7
# A caster keeps its lasting buffs up between fights (V176), with at least this share of
# its mana left after each, and waits this long for the global cooldown between two.
BUFF_UP_RESERVE = 0.5
BUFF_UP_GCD_S = 1.6
# A caster's mana line, measured (V170): before a pull it wants this many times what a kill
# has cost it, the median of the last `MANA_KILLS` kills, within `MANA_LINE_RANGE`, once it
# has `MANA_KILLS_MIN` kills to go on.
MANA_MARGIN = 1.15
MANA_KILLS = 10
MANA_KILLS_MIN = 3
MANA_LINE_RANGE = (0.35, 0.85)
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


def _area(ability: Ability) -> bool:
    """Damage to every enemy round the character (V277): Arcane Explosion, Thunder Clap."""
    known = spell_facts(ability.spell_id)
    return known is not None and known.role == "area"


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
    HELD = "held"                    # the selected unit held out of it (Polymorph): the other next
    USED = "used"                    # a quest's item used on it, and the quest complete (V387)

    @property
    def ok(self) -> bool:
        return self is Fought.KILLED


# The classification a widened fight takes (`Kinds`, V337): `UnitClassification`'s "normal",
# as the addon paints it (no elite, rare, rare elite or boss). Reactions 1-3 are hostile.
NORMAL_RANK = 1
HOSTILE_REACTIONS = (1, 2, 3)


@dataclass(frozen=True)
class Kinds:
    """What a grind rib found dry fights (V337): its own kind as ever, and any of `names`, the
    kinds of normal rank that spawn round it and attack the character on sight, when the unit
    selected is hostile, of normal rank and of a level within `low`-`high` - the rib's levels,
    none grey to the character and none more than a level above it."""

    own: int | None
    names: frozenset[int]
    low: int
    high: int

    def named(self, name: int | None) -> bool:
        """Is `name` one of these kinds, by name alone (a hover's)?"""
        return name is not None and (name == self.own or name in self.names)

    def takes(self, values: dict) -> bool:
        """Is the selected unit one to fight: its own kind, or one of the others that is
        hostile, of normal rank and of a level within `low`-`high`?"""
        name = values.get("target.name_id")
        if name is not None and name == self.own:
            return True
        level = values.get("target.level")
        return (name in self.names and isinstance(level, int) and self.low <= level <= self.high
                and values.get("target.reaction") in HOSTILE_REACTIONS
                and values.get("target.classification") == NORMAL_RANK)


def pays(values: dict) -> bool:
    """Does a kill of the selected unit pay the character experience: its level above the
    character's grey level (`grey_level`, the server's rule), both as the strip paints them?
    Either unknown, it may (a skull's level is unknown, and pays)."""
    level, own = values.get("target.level"), values.get("char.level")
    return not (isinstance(level, int) and isinstance(own, int) and level <= grey_level(own))


def tagged(values: dict) -> bool:
    """Is the selected unit tagged by someone else and not attacking the character (V344)?
    Its kill and its loot are theirs; one attacking the character is its fight whoever
    tagged it. Unknown (a strip before schema 21) is not tagged."""
    return values.get("target.tapped") is True and values.get("target.attacking_me") is not True


@dataclass(frozen=True)
class Paying:
    """A pull for experience (V344): `wanted` (a name id, or `Kinds`) at a level that pays the
    character (`pays`). A grind's hunt asks for this; a quest's does not, a grey kill counting
    for a quest as any other. Self-defence takes what attacks the character at any level, as
    ever. Anything else asked of it is asked of `wanted` (`own`, `names`, `low`, `high`)."""

    wanted: object

    def named(self, name: int | None) -> bool:
        return _named(self.wanted, name)

    def takes(self, values: dict) -> bool:
        return _takes(self.wanted, values) and pays(values)

    @property
    def own(self):
        return getattr(self.wanted, "own", self.wanted)

    def __getattr__(self, name: str):
        if name.startswith("__") or name == "wanted":
            raise AttributeError(name)
        return getattr(self.wanted, name)


# What another held quest's creature may be when a hunt takes it (`Quarry`, V363): not friendly
# (1-2 hostile, 3 unfriendly, 4 neutral; a quest's boars and striders are often neutral).
QUEST_REACTIONS = (1, 2, 3, 4)


@dataclass(frozen=True)
class Quarry:
    """A hunt's pull for every quest held (V363): `wanted`, the step's own (a name id, or a dry
    rib's `Kinds`), as ever, and any of `names`, the creatures the log's other open kill and loot
    counters want round the hunt (`jev.world.quarry.held`), when the unit selected is not
    friendly, of normal rank and of a level at most `high`. A grind asks for it as a pull that
    pays (`Paying`, V344); a quest objective at any level its own creature, as before."""

    wanted: object
    names: frozenset[int]
    high: int | None = None

    @property
    def own(self):
        return getattr(self.wanted, "own", self.wanted)

    def named(self, name: int | None) -> bool:
        return name is not None and (name in self.names or _named(self.wanted, name))

    def takes(self, values: dict) -> bool:
        if _takes(self.wanted, values):
            return True
        level = values.get("target.level")
        return (values.get("target.name_id") in self.names and isinstance(level, int)
                and (self.high is None or level <= self.high)
                and values.get("target.reaction") in QUEST_REACTIONS
                and values.get("target.classification") == NORMAL_RANK)


def _named(wanted, name: int | None) -> bool:
    """Is `name` the unit `wanted` (a name id, `Kinds`, `Quarry` or `Paying`), by name alone?"""
    return (wanted.named(name) if isinstance(wanted, (Kinds, Paying, Quarry))
            else name == wanted)


def _takes(wanted, values: dict) -> bool:
    """Is the selected unit the one `wanted` (a name id, `Kinds`, `Quarry` or `Paying`)?"""
    return (wanted.takes(values) if isinstance(wanted, (Kinds, Paying, Quarry))
            else values.get("target.name_id") == wanted)


def _logged(wanted):
    """`wanted` as the evidence keeps it."""
    if isinstance(wanted, Paying):
        return _logged(wanted.wanted)
    if isinstance(wanted, Quarry):
        inner = _logged(wanted.wanted)
        return sorted({*(inner if isinstance(inner, list) else [inner]), *wanted.names} - {None})
    return sorted({wanted.own, *wanted.names} - {None}) if isinstance(wanted, Kinds) else wanted


def _jev_name(ability: Ability) -> str:
    """An attack as Jev names it: its spell's name, or its slot."""
    return ability.name or f"slot {ability.slot}"


def _jev_text(ability: Ability) -> str:
    """What Jev is told of an attack: its cost, its cast and whether it slows."""
    facts, known = reach(ability.spell_id), spell_facts(ability.spell_id)
    parts = [f"{ability.mana} power" if ability.mana else "no cost"]
    if facts is not None:
        parts.append("instant" if facts.instant else
                     f"{facts.cast_s:.1f} s {'channel' if facts.channel else 'cast'}")
        parts.append(f"{facts.max_yd:.0f} yd")
    if known is not None and known.slows:
        parts.append("slows the target")
    if _area(ability):
        parts.append("hits all round the character")
    return f"{_jev_name(ability)}: " + ", ".join(parts)


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
    # Jev, when it picks this character's attacks (`jev.coach.judge.CombatJudge`, V298).
    judge: object | None = None

    pressed: list[int] = field(default_factory=list, init=False)
    closed: int = field(default=0, init=False)
    # Something equipped is at zero durability. Advisory: reported so the caller can
    # decide to go and repair, never a reason to refuse the fight.
    broken: bool = field(default=False, init=False)
    # Nothing loaded to fire, as the body's last bag census counted (V402, `LiveBody._dry`): a
    # shooter's fight gives its shots up at once, as after `SHOT_GIVE_UP` silences. The server
    # refuses every shot "no ammo", and a dry hunter stood up to 14 s at range pressing Auto
    # Shot before it gave up and walked in: 24 of the hive's 42 hunters were dry on 7 Oct.
    dry: bool = field(default=False, init=False)
    # Whether the weapon is the broken thing, as last read (`disarmed`, V393): `None` until a
    # reading could tell.
    disarmed_seen: bool | None = field(default=None, init=False)
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
    _asides: int = field(default=0, init=False)          # steps aside after a root, for the side (V271)
    _power_start: float | None = field(default=None, init=False)   # the fight's first mana (V273)
    _unhurt: dict = field(default_factory=dict, init=False)        # guid -> when found unhurt
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
    _blind_cast: bool = field(default=False, init=False)
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
    # Slots the client refused as not ready, and until when they are left (V251).
    _held: dict[int, float] = field(default_factory=dict, init=False)
    # When the last press the client acted on was made, a toggle's aside (`GCD_GUARD_S`).
    _gcd_from: float | None = field(default=None, init=False)
    # Units held out of the fight (Polymorph), by guid, and until when (V287); whether this
    # fight has pressed its hold.
    _holding: dict[str, float] = field(default_factory=dict, init=False)
    _held_this_fight: bool = field(default=False, init=False)
    # An acquisition that takes a held unit, nothing else attacking (V395); until when the
    # last guard holds the crowd off (V396); whether the shot repeating is a caster's wand,
    # which any other press stops (V397).
    _waking: bool = field(default=False, init=False)
    _after_hold: bool = field(default=False, init=False)
    # The most attackers this fight counted, for a death's (V395).
    _most_attackers: int = field(default=0, init=False)
    _guarded_until: float = field(default=0.0, init=False)
    _wanding: bool = field(default=False, init=False)
    # (time, our health, target health, casting, target guid), this fight: who dies first.
    _race: list[tuple] = field(default_factory=list, init=False)
    # The last look the evidence clocks were advanced to (`_hold_clocks_while_casting`).
    _look_at: float | None = field(default=None, init=False)
    # The lowest health this fight saw, for how it went.
    _low_hp: float | None = field(default=None, init=False)
    # A caster's fight cast from range (V164): a kill lies out there (`ended_far`), and the
    # loot walks to it when the client says it is too far or the corpse is not found. Not
    # only a kill made with the unit out of melee: the client's melee is ten yards and a
    # corpse is looted from five, and a mage's wolves, charging in, died at six to ten
    # yards and went unlooted - no Tough Wolf Meat in four kills (the mage's second check,
    # V192).
    ended_far: bool = field(default=False, init=False)
    _from_range: bool = field(default=False, init=False)
    # What each recent kill cost, as a share of the mana pool, and the mana this fight saw
    # first and last (V170).
    mana_costs: deque = field(default_factory=lambda: deque(maxlen=MANA_KILLS), init=False)
    # The heal line drawn for a fight against two or more (V172), and whether it heals at all.
    _pack_line: str | None = field(default=None, init=False)
    # The last fight's cycle, open until the next fight that comes to blows or the session's
    # end (V311): its objective, line, whether it killed, whether it died, and its start.
    _cycle: tuple[str, str, bool, bool, float] | None = field(default=None, init=False)
    _heals: bool = field(default=True, init=False)
    _mana_seen: tuple[float | None, float | None] = field(default=(None, None), init=False)
    _last_near: bool = field(default=False, init=False)
    _ranged_steps: int = field(default=0, init=False)
    # A shooter's repeating shot (V358): when it was pressed and is taken to be repeating
    # (`None`: not shooting), how many times it went silent this fight, and whether the
    # fight has given shooting up.
    _shooting_at: float | None = field(default=None, init=False)
    _shots_silent: int = field(default=0, init=False)
    _no_shots: bool = field(default=False, init=False)
    # Damage over time put on a unit this fight, by (its guid, the spell's name), until when
    # it lasts (V360): not pressed on it again before then. A next-swing blow pressed and its
    # cost, until its swing (`NEXT_SWING_HOLD_S`): what else is pressed leaves that much.
    _dotted: dict[tuple, float] = field(default_factory=dict, init=False)
    _dot_guid: object = field(default=None, init=False)
    # A shooter's backing off out of its dead zone this fight (V404): how many times, and when.
    _dead_zone_steps: int = field(default=0, init=False)
    _dead_zone_at: float = field(default=-math.inf, init=False)
    _queued: dict[int, tuple[float, int]] = field(default_factory=dict, init=False)

    # -- the skill -----------------------------------------------------------

    @traced("fight")
    def run(self, name_id: int | None = None, *, timeout_s: float = 45.0) -> Fought:
        """Select, engage, and hold the rotation until something settles it.

        With `choices`, the fight's heal line is drawn from what each line has done, and
        its cycle is recorded for it when the next fight comes to blows (V311, `HEAL_POINT`).
        A fight that never came to blows teaches nothing and closes no cycle; one cut short
        is a cycle only when it was going badly (below `BAD_FIGHT_HP`)."""
        line = None
        # A class with no heal has no line to draw, and its fights say nothing of one.
        heals = self.profile is None or self.profile.first(Role.HEAL) is not None
        self._heals, self._pack_line = heals, None
        if self.choices is not None and heals:
            line = self.choices.pick("all", HEAL_LINES)
            self.heal_below = float(line)
        self._low_hp = None
        self._most_attackers = 0
        self._mana_seen = (None, None)
        self.ended_far = self._from_range = self._last_near = False
        self._ranged_steps = 0
        started = time.monotonic()
        result = None
        try:
            result = self._fight(name_id, timeout_s)
            self.ended_far = result is Fought.KILLED and self._from_range
            first, last = self._mana_seen
            if result is Fought.KILLED and first is not None and last is not None:
                self.mana_costs.append(max(0.0, first - last))
            return result
        finally:
            if result is Fought.DIED and self._most_attackers >= 2:
                # A death with two attackers or more counted, as its own operation for the
                # canary to count (V395, V396): 29.6% of the hive's deaths on 7 Oct.
                with operation("fight.outnumbered") as span:
                    span.finish(code="died", data={"attackers": self._most_attackers})
            low = self._low_hp
            went_badly = result is Fought.DIED or (low is not None and low < BAD_FIGHT_HP)
            came_to_blows = result in (Fought.KILLED, Fought.DIED, Fought.LOSING, Fought.TIMEOUT,
                                       Fought.UNREACHABLE, Fought.LOST)
            # Cut short (a death, a stop): known only when it was going badly. A fight that
            # became one against two or more teaches the pack's line, not the single's.
            if line is not None and (came_to_blows or (result is None and went_badly)):
                objective, option = (("pack", self._pack_line) if self._pack_line is not None
                                     else ("all", line))
                self._close_cycle(started)          # the last fight's ends where this began
                self._cycle = (objective, option, result is Fought.KILLED,
                               result is Fought.DIED, started)

    def _close_cycle(self, until: float) -> None:
        """Record the open cycle for its line (V311): its seconds to `until`, at most
        `CYCLE_MAX_S`, and `DEATH_S` more for a death; a win when it killed and lived. The
        line with the most kills a second of cycle is the one with the fewest seconds a kill:
        a rate, as the choices draw it, since the kills a cycle over its seconds is the long
        run's kills a second, where a mean of each cycle's seconds a kill would have none for
        a fight that did not kill."""
        cycle, self._cycle = self._cycle, None
        if cycle is None or self.choices is None:
            return
        objective, option, killed, died, since = cycle
        seconds = min(max(0.0, until - since), CYCLE_MAX_S) + (DEATH_S if died else 0.0)
        self.choices.outcome(objective, option, killed and not died, seconds)

    def settle(self) -> None:
        """The session ends: the open cycle is closed where it stands (V311)."""
        self._close_cycle(time.monotonic())

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
        self._held = {}
        self._held_this_fight = False
        self._race = []
        self._look_at = None
        self._damage_mark = None
        self._damage_seen = self._input_refused = False
        self._power_start = None
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
        self._blind_cast = False
        self._error_count = None
        self._damage_at = time.monotonic()
        self.last_hp = None
        self.detail = ""
        self._last_use = {}
        self._saved_at = None
        self._shooting_at, self._shots_silent, self._no_shots = None, 0, bool(self.dry)
        self._wanding = False
        self._dead_zone_steps, self._dead_zone_at = 0, -math.inf
        # The damage over time a fight just ended by a hold left on the unit fought is on it
        # still when the next fight takes it back (V395).
        self._dotted = self._dotted if self._after_hold else {}
        self._queued, self._dot_guid, self._after_hold = {}, None, False
        event("fight.request", data={"wanted_name_id": _logged(name_id), "timeout_s": timeout_s})

        v = self.read()
        if self._targeting().cancel_pending_spell(v):
            v = self.read()                  # its click would cast, not select
        self._observe(v)
        if v is None:
            return Fought.BLIND
        # Units to fight are found by their plates as friendly ones are, which a relaunched
        # client may not draw (V320): shown from the state this reading paints.
        show_plates(self.hid, self.read, Plates.ENEMY, values=v)
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
        self.disarmed(v)

        hp = v.get("vitals.hp")
        if not in_combat and hp is not None and hp < MIN_START_HP:
            self.detail = f"{hp:.0%} health; not starting a fight on that"
            return Fought.TOO_HURT
        if in_combat and hp is not None and hp < self._heal_line(v):
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
        # Nor is a friendly unit ever the fight (V293), in reach or not: up at the Spirit
        # Healer beside a Rotting Dead, a level 2 warlock's selection was the healer, and
        # every Shadow Bolt was refused "bad targets" while the dead killed it (the hive).
        friendly = isinstance(v.get("target.reaction"), int) and v["target.reaction"] >= 5
        # Nor a unit held out of the fight (V395): the selection a hold leaves is the held
        # unit, and the hive's mage Polymorphed one, began its next fight on it at once and
        # broke it with Fire Blast, its only two holds in 400 runs (7 Oct 02:00-05:00).
        held = self._held_now(v.get("target.guid"))
        engaged = (in_combat and v.get("target.has") is True
                   and v.get("target.hp") is not None and v["target.hp"] > DEAD_HP
                   and not bystander and not friendly and not tagged(v) and not held)
        # Already selected and alive, and the unit we came for: that is the fight. Whoever
        # selected it - the tutor, a previous look - re-acquiring could only swap it for
        # another of the same name, or for something else entirely.
        chosen = (not engaged and name_id is not None and v.get("target.has") is True
                  and _takes(name_id, v) and not tagged(v)
                  and isinstance(v.get("target.hp"), (int, float)) and v["target.hp"] > DEAD_HP
                  and not (in_combat and bystander) and not friendly and not held)
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
            acquired = self._acquire_or_wake(name_id, defend=in_combat)
            if acquired is not None:
                return acquired
        else:
            self._selected_name_id = v.get("target.name_id")
            self._selected_guid = v.get("target.guid")
            self._damage_mark = v.get("target.hp")
            self.selected_plate = None
        # Blind melee on what the client says now, not before the look: a Tab pick is not
        # yet in melee or attacking, and the look can take seconds (V191).
        if (not self.engage(v) and not self._fight_blind(fresh := self.read() or v)
                and not self._cast_blind(fresh)):
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
            acquired = self._acquire_or_wake(name_id, defend=in_combat)
            if acquired is not None:
                return acquired
            if not self.engage(v) and not self._cast_blind(self.read() or v):
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
            if profile.caster and self._unhurt_by(v):
                return Fought.UNREACHABLE
            self._watch_shots(profile, v)
            if self._ranged_ready(profile, v):
                # A caster with the mana casts from where it stands (V164). The client says
                # what is wrong with a cast - too far, not facing, out of sight - and each
                # is answered with the least movement that cures it, never during a cast.
                self._from_range = True
                error = self._new_error(v)
                casting = v.get("bars.casting") is True
                if error == "not_facing" and not casting:
                    near = v.get("target.in_melee") is True
                    if self._aim_code is FaceCode.FACED and not self._blind_cast:
                        # Its plate on the centre line and the spell "not in front": the unit
                        # stands behind the caster, between it and the camera. Facing by the
                        # plate never turned: a Defias Cutpurse behind the level 5 mage took
                        # it from 95% to dead in 44 s while Fireball was pressed 40 times,
                        # every one "Target needs to be in front of you" (V208).
                        if not self._face_behind():
                            return Fought.REFUSED
                    elif self._blind_cast and near:
                        # Found blind and in reach: a quarter turn, no search first (V256).
                        if not self._turn_quarter():
                            return Fought.REFUSED
                    elif not self.engage(v):
                        if (not self._blind_cast and not self._input_refused and near
                                and v.get("target.attacking_me") is True):
                            # In reach and hitting the caster, its plate lost beside or
                            # behind: turned for a quarter at a time, as blind melee is, not
                            # given up. A Riverpaw Outrunner beside the level 9 mage cost the
                            # search's 3 s, the fight's end and a fresh one, 8 s with nothing
                            # pressed, and the mage died with the gnoll at 30% (session 225).
                            event("engage.blind_cast", data={
                                "name_id": v.get("target.name_id"), "in_melee": True})
                            self._blind_cast = True
                        if not self._blind_cast or self._input_refused:
                            return Fought.REFUSED if self._input_refused else self._aim_failure()
                        if not self._turn_quarter():     # a blind cast turns as blind melee does
                            return Fought.REFUSED
                elif (error == "out_of_range" or self._beyond_reach(profile, v)) and not casting:
                    if self._ranged_steps >= MAX_RANGED_STEPS:
                        self.detail = (f"stepped in {self._ranged_steps} times and the spell "
                                       "still does not reach; cannot reach it")
                        return Fought.UNREACHABLE
                    self._ranged_steps += 1
                    if not self._range_step(v):
                        if not self._blind_cast or self._input_refused:
                            return Fought.REFUSED if self._input_refused else self._aim_failure()
                        if not self._blind_step():       # a Tab pick lies ahead
                            return Fought.REFUSED
                elif error == "no_line_of_sight" and not casting:
                    self._sidestep("los")
                    if self._input_refused:
                        return Fought.REFUSED
                elif (not casting and self._hold_wanted(profile, v)
                      and (held := self._hold(profile, v)) is not None):
                    return held
                elif (v.get("target.in_melee") is True and not casting and self._root_wanted(v)
                      and not self._holding_now(time.monotonic()) and self._root(profile, v)):
                    if self._input_refused:
                        return Fought.REFUSED
                    time.sleep(pace(self.hid, 0.2))
                    continue
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
            error = self._new_error(v)
            wrong_way = error == "not_facing"
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
                if not self._face_behind():
                    return Fought.REFUSED
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
                if error == "out_of_range":
                    # "Too far away" is the client saying it is not in reach after all: the
                    # strip's `in_melee` is the ten-yard duel check, and a Fleshripper hovering
                    # eight yards off hit from there while the swings at it failed, 45 s from
                    # 100% to 88%, and the character died (session 163). A step ahead, bounded
                    # as closing is; the next error says whether to turn (V207).
                    if self._strides >= MAX_CLOSE_BURSTS:
                        self.detail = (f"stepped in {self._strides} times blind and it is still "
                                       "too far away; cannot reach it")
                        return Fought.UNREACHABLE
                    self._strides += 1
                    if not self._blind_close():
                        return Fought.REFUSED
            elif in_reach:
                # Stand and swing. Turn back only on evidence the swings are not landing.
                if ((wrong_way or stalled) and not self.engage(v)
                        and not self._fight_blind(self.read() or v)):
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

            # A shooter at hand with a unit not attacking it - its pet holds it, it runs - backs
            # out of the dead zone to shoot (V404).
            if self._dead_zone_due(profile, v):
                self._leave_dead_zone()
                if self._input_refused:
                    return Fought.REFUSED
                time.sleep(pace(self.hid, 0.2))
                continue

            # A second attacker held off at hand too (V395): a rogue's Gouge, a druid's
            # Entangling Roots on one still coming.
            if (v.get("bars.casting") is not True and self._hold_wanted(profile, v)
                    and (held := self._hold(profile, v)) is not None):
                return held
            if self._input_refused:
                return Fought.REFUSED

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
                if chosen is not Fought.NO_TARGET:
                    return chosen
                if turned_round:
                    if (self.read() or {}).get("vitals.combat") is False:
                        # Out of combat by now: nothing is attacking, and anything taken is
                        # a new fight. All 31 times the fallback below fired in sessions
                        # 195-219 the fight had ended; ten of them pulled a unit minding its
                        # own business, a level 1 wolf and a cow among them, and one of those
                        # was the mage's death (session 211, V249).
                        self.detail = "nothing attacking any more"
                        return Fought.NO_TARGET
                    # Nothing attacking could be found either side, only what is in view:
                    # fight that, and with no plate in view whatever Tab picks. Something
                    # unfound hit the paladin by Jangolode Mine for 4-5% every four seconds;
                    # with nothing else taken it turned round 33 times in four minutes and
                    # then again all the next session, healing, fighting nothing, while Tab
                    # offered a Defias Smuggler (sessions 169-170, V209-V210). A fight moves
                    # the character: out of a hidden caster's sight, or into its partner's.
                    last = self._pick_plate(name_id, defend, any_plate=True)
                    if last is False:
                        event("acquire.anything", data={"wanted_name_id": _logged(name_id)})
                        last = self.select(None)
                    return last
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

    def _pick_plate(self, name_id: int | None, defend: bool,
                    any_plate: bool = False) -> Fought | bool | None:
        """Select a plate in the current view: `None` selected, `False` none acceptable.

        Defending, whatever is attacking us is chosen before anything of the wanted name
        that is not: a bystander of the quest's own kind is still a fight, but not while
        something else is killing the character."""
        frame = self.read_frame()
        if frame is None:
            return False
        candidates = self._candidates(frame)
        # Defending with no name wanted, only what attacks us: the second pass is for a
        # bystander of the quest's own kind, and without a name it took any plate. A Young
        # Goretusk minding its own business was chosen while something behind the paladin
        # took it from full health to dead (session 168, V209). Tab, attackers only in
        # self-defence, and the turn round find what is behind.
        passes = (((True, False) if name_id is not None or any_plate else (True,))
                  if defend else (False,))
        for attackers_only in passes:
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
                          data={"point": list(point), "wanted_name_id": _logged(name_id),
                                "name_id": (hover.after or {}).get("cursor.name_id")})
                    if hover.code in (HoverCode.REFUSED, HoverCode.BLIND):
                        self.detail = hover.detail
                        return Fought.REFUSED if hover.code is HoverCode.REFUSED else Fought.BLIND
                    after = hover.after or {}
                    if (after.get("cursor.has") is not True or after.get("cursor.dead") is True
                            or not _named(name_id, after.get("cursor.name_id"))):
                        continue
                event("selection.request", data={"method": "plate",
                                                 "wanted_name_id": _logged(name_id),
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
        event("selection.expected", data={"wanted_name_id": _logged(name_id), "defend": defend,
                                          "attackers_only": attackers_only})
        if v is None:
            return None
        if v.get("target.has") is not True:
            return False
        hp = v.get("target.hp")
        if hp is not None and hp <= DEAD_HP:
            return False                       # a corpse is selectable and not a fight
        guid = v.get("target.guid")
        if guid is not None and time.monotonic() - self._unhurt.get(guid, -math.inf) < UNHURT_S:
            return False                       # found unhurt a moment ago (V273)
        if self._held_now(guid) and not self._waking:
            # Held out of the fight a moment ago (V287), whether or not the strip calls it
            # attacking: the server keeps a sheep's or a feared unit's victim, and the hive's
            # mage took its sheep back at once (V395).
            return False
        if attackers_only and v.get("target.attacking_me") is not True:
            return False
        if tagged(v):
            return False                       # another's kill and loot (V344)
        if (defend and v.get("target.attacking_me") is not True
                and (v.get("combat.attackers") or 0) >= 1):
            # A bystander of the wanted kind is a fight, but not while something else is
            # attacking: a level 7 mage defending against a Young Forest Bear took an idle
            # Rockhide Boar, twice, and died both times (sessions 207-208, V249).
            return False
        if (name_id is None or _takes(name_id, v)
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
            event("selection.request", data={"method": "tab", "wanted_name_id": _logged(name_id),
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
            if tagged(v):
                # Tab takes a unit another has tagged, as the look's grey plate does not:
                # 1,273 kills that paid nothing in the hive's two hours (V344).
                continue
            if self._held_now(v.get("target.guid")) and not self._waking:
                continue                       # held out of the fight (V395)
            if (name_id is not None and not _takes(name_id, v)
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
        # A plate just clicked is looked for briefly, toward where it was (`FACE_HINT_SEARCH_S`).
        result = self._targeting().face_selected(
            expected_name_id=self._selected_name_id, hint=self.last_plate,
            search_s=(FACE_SEARCH_MAX_S if fighting
                      else FACE_HINT_SEARCH_S if self.last_plate is not None else 0.0),
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

    def _unhurt_by(self, values: dict) -> bool:
        """A caster's mana spent on a unit whose health has not moved (V273): the unit is
        marked not to be taken again for a while, and the fight says why it ends."""
        guid = values.get("target.guid")
        if (not self._damage_seen and guid is not None
                and time.monotonic() - self._unhurt.get(guid, -math.inf) < UNHURT_S):
            # Still selected from the fight it was found unhurt in: a fight begun on the kept
            # selection spent another 40% on the same Prowler (session 270).
            self.detail = "the unit took nothing a moment ago; not hurt"
            return True
        power = values.get("vitals.power")
        if not isinstance(power, (int, float)):
            return False
        if self._power_start is None:
            self._power_start = power
            return False
        spent = self._power_start - power
        if self._damage_seen or spent < UNHURT_MANA:
            return False
        if guid is not None:
            self._unhurt[guid] = time.monotonic()
        self.detail = f"{spent:.0%} of its mana spent and the unit's health never moved; not hurt"
        event("fight.unhurt", data={"spent": round(spent, 3), "guid": guid})
        return True

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

    def disarmed(self, values: dict | None) -> bool | None:
        """Is the weapon broken (V393)? With something at zero durability, a plain weapon blow
        on the bar (`PLAIN_BLOWS`) whose cost the power pays, and none such usable: the client
        greys it out, and the hive's strip does as the server says (`item_ok`). One usable
        says the weapon is whole and the broken thing armour; `None` where nothing tells - no
        such blow on the bar, a warrior's rage too low out of combat - and then the last
        reading that told stands, until the gear is mended. A rogue with a broken weapon
        pressed Sinister Strike 0.5 times a minute against 13.4, a warrior Heroic Strike,
        Hamstring and Rend 0.3 against 2.7 (the hive, 7 Oct 01:25-03:50)."""
        if not values:
            return self.disarmed_seen
        durability = values.get("bags.durability_min")
        if durability is None:
            return self.disarmed_seen
        if durability > 0.0:
            self.disarmed_seen = None
            return False
        usable, power, pool = (values.get("bars.usable"), values.get("vitals.power"),
                               values.get("vitals.power_max"))
        if (not isinstance(usable, int) or not isinstance(power, (int, float))
                or not isinstance(pool, (int, float)) or pool <= 0):
            return self.disarmed_seen
        profile = self.profile or for_class(values.get("char.class_id"), values.get("char.race_id"))
        # The weapons a class fights with (V402, V493): a melee class's main hand, which its
        # plain blows ask for; a shooter's ranged one, whose repeating shot (Auto Shot) the
        # client greys out when it is broken, and its main hand too, what it strikes with at
        # hand (Raptor Strike): a pet-less hunter of 6-10 has the unit at hand 84% of a fight,
        # and with the main hand broken and the bow whole lost 18 fights of 27 (7-8 Oct).
        shooter = profile.shooter and not profile.caster
        told = []
        for asks in ((repeats, lambda a: _blow_name(a) in PLAIN_BLOWS) if shooter
                     else (lambda a: _blow_name(a) in PLAIN_BLOWS,)):
            paid = [a for a in profile.abilities if asks(a) and power * pool >= a.mana]
            if paid:
                # A stance's page (a warrior's 73-84) is the main bar's twelve keys.
                told.append(not any(usable & (1 << ((a.slot - 1) % 12)) for a in paid))
        if told:
            self.disarmed_seen = any(told)
        return self.disarmed_seen

    def buff_up(self) -> int:
        """Out of combat, a caster's lasting buffs that are due (Frost Armor, Arcane
        Intellect), each only with `BUFF_UP_RESERVE` of the mana left after it (V176), and
        every class's aura not yet up (V404: a hunter's Aspect of the Hawk, a paladin's
        Devotion Aura), which a fight's first look pressed before at the cost of a global
        cooldown. How many were pressed."""
        pressed = 0
        profile = self.profile               # the bar's census: without it, nothing is read
        if profile is None:
            return 0
        v = self.read()
        if v is None or v.get("vitals.combat") is not False:
            return 0
        pool = v.get("vitals.power_max")
        rows = ((*profile.by_role(Role.AURA), *profile.by_role(Role.BUFF)) if profile.caster
                else profile.by_role(Role.AURA))
        for buff in rows:
            if not buff.lasting:
                continue
            last = self._lasting.get(buff.name)
            if last is not None and time.monotonic() - last < buff.every_s:
                continue
            v = self.read() or v
            if v.get("vitals.combat") is not False:
                break
            bit = 1 << (buff.slot - 1)
            usable, ready = v.get("bars.usable"), v.get("bars.ready")
            if (usable is not None and not usable & bit) or (ready is not None and not ready & bit):
                continue
            power = v.get("vitals.power")
            if (isinstance(power, (int, float)) and pool and buff.mana
                    and power - buff.mana / pool < BUFF_UP_RESERVE):
                continue
            if self._press(buff):
                self._lasting[buff.name] = time.monotonic()
                self._pending_press = None
                pressed += 1
                event("buff.up", data={"slot": buff.slot, "name": buff.name})
                time.sleep(pace(self.hid, BUFF_UP_GCD_S))
        return pressed

    def _ranged_ready(self, profile: CombatProfile, values: dict) -> bool:
        """A caster (`CombatProfile.caster`) with a spell cast from range that it can use now:
        the client says the slot is usable, which a spell is not without its mana. Out of
        mana, a caster fights with its staff until the mana comes back (V164).

        A shooter (`CombatProfile.shooter`, V358) the same with its repeating shot, while the
        unit is not at hand: Auto Shot reaches no unit in melee (the dead zone), and there it
        fights with its blade; and not once this fight has given shooting up (`_watch_shots`).
        """
        if not profile.caster:
            return profile.shooter and self._shot_ready(profile, values)
        usable = values.get("bars.usable")
        for attack in profile.by_role(Role.ATTACK):
            if not ranged(attack) or (repeats(attack) and self._no_shots):
                continue
            if usable is not None:
                if usable & (1 << (attack.slot - 1)):
                    return True
            elif self._mana_left_after(attack, values) >= 0:
                return True
        return False

    def _shot_ready(self, profile: CombatProfile, values: dict) -> bool:
        """A shooter's repeating shot usable at a unit not at hand (V358)."""
        if self._no_shots or values.get("target.in_melee") is True:
            return False
        usable = values.get("bars.usable")
        return any(usable is None or bool(usable & (1 << (a.slot - 1)))
                   for a in profile.by_role(Role.ATTACK) if repeats(a))

    def _watch_shots(self, profile: CombatProfile, values: dict) -> None:
        """Whether the repeating shot still repeats (V358). The server stops Auto Shot when
        the unit comes into the dead zone, and the client marks its slot out of range there:
        then it is pressed again once the unit is back out of it. A shot with no damage to
        the unit for `SHOT_SILENT_S` is taken as stopped (pressed again: on the client a
        press of a repeating shot stops it, so it is never pressed while taken to repeat),
        and after `SHOT_GIVE_UP` such silences the fight is fought in melee."""
        if self._shooting_at is None:
            return
        out = values.get("bars.out_range")
        slots = [a.slot for a in profile.by_role(Role.ATTACK) if repeats(a)]
        # A wand has no dead zone (V397): a caster's shot goes on at hand.
        if ((values.get("target.in_melee") is True and not profile.caster)
                or (isinstance(out, int) and any(out & (1 << (slot - 1)) for slot in slots))):
            self._shooting_at = None
            return
        now = time.monotonic()
        if now - max(self._shooting_at, self._damage_at) < SHOT_SILENT_S:
            return
        self._shooting_at = None
        self._shots_silent += 1
        self._no_shots = self._shots_silent >= SHOT_GIVE_UP
        event("fight.shots_silent", data={"times": self._shots_silent,
                                          "given_up": self._no_shots})

    def _dead_zone_due(self, profile: CombatProfile, values: dict) -> bool:
        """Whether a shooter (`CombatProfile.shooter`, no caster) backs out of its shot's dead
        zone now (V404): in a fight, the unit at hand (the strip's ten yards) and not attacking
        it - a pet holds it, it runs or is held - with no second attacker, its repeating shot
        usable and not given up this fight, at most `DEAD_ZONE_STEPS` times a fight and
        `DEAD_ZONE_AGAIN_S` apart. A unit attacking it follows faster than a walk backwards:
        that one is fought at hand (Raptor Strike, Wing Clip)."""
        now = time.monotonic()
        if (not profile.shooter or profile.caster or self._no_shots
                or values.get("vitals.combat") is not True or values.get("bars.casting") is True
                or values.get("target.in_melee") is not True
                or values.get("target.attacking_me") is not False
                or (values.get("combat.attackers") or 0) >= 2
                or self._dead_zone_steps >= DEAD_ZONE_STEPS
                or now - self._dead_zone_at < DEAD_ZONE_AGAIN_S):
            return False
        hp = values.get("target.hp")
        if not isinstance(hp, (int, float)) or hp <= DEAD_HP:
            return False
        usable = values.get("bars.usable")
        return any(usable is None or bool(usable & (1 << (a.slot - 1)))
                   for a in profile.by_role(Role.ATTACK) if repeats(a))

    def _leave_dead_zone(self) -> None:
        """Back off out of the dead zone, still facing the unit (V404), as a caster steps clear
        of a root (`STEP_CLEAR_S`); the repeating shot is pressed again from there."""
        self._dead_zone_steps += 1
        self._dead_zone_at = time.monotonic()
        event("fight.dead_zone", data={"key": "s", "seconds": STEP_CLEAR_S,
                                       "times": self._dead_zone_steps})
        if not self._back_off(STEP_CLEAR_S):
            self._input_refused = True
            self.detail = "dead-zone step input refused"
        self._shooting_at = None

    def _back_off(self, seconds: float) -> bool:
        """A walk backwards this long, still facing the unit: the client's S key."""
        return bool(self.hid.hold("s", seconds))

    def _dotted_now(self, attack: Ability, now: float) -> bool:
        """Damage over time of this attack's already on the selected unit (V360)."""
        return (lingers(attack)
                and self._dotted.get((self._dot_guid, attack.name), -math.inf) > now)

    @staticmethod
    def _caster_order(attacks: tuple[Ability, ...], values: dict) -> tuple[Ability, ...]:
        """A caster's attacks, best first (V165): at contact an instant (Fire Blast), which
        nothing hitting it can push back; before the unit has come for it, a spell that
        slows it (Frostbolt), for another cast before it arrives; else the bar's order."""
        near = values.get("target.in_melee") is True
        coming = values.get("target.attacking_me") is True

        def rank(attack: Ability) -> int:
            facts = reach(attack.spell_id)
            if near:
                return 0 if facts is not None and facts.instant else 1
            if not coming:
                known = spell_facts(attack.spell_id)
                return 0 if known is not None and known.slows else 1
            return 0

        # A stable sort: within a rank, the order the attacks came in, the bar's with those
        # of known damage by damage a second (`by_value`, V360).
        return tuple(sorted(attacks, key=rank))

    @staticmethod
    def _root_wanted(values: dict) -> bool:
        """A root at contact for more than one attacker, or below half health (V275)."""
        hp = values.get("vitals.hp")
        return ((values.get("combat.attackers") or 0) >= 2
                or (isinstance(hp, (int, float)) and hp < ROOT_HP))

    def _root(self, profile: CombatProfile, values: dict) -> bool:
        """At contact, hold what is round the caster (Frost Nova) and back off, still facing,
        to cast again out of its reach (V169); with more than one attacker counted, step aside,
        left and right in turn (V271). `True` if a root was pressed and the client answered it:
        one it did not is not stepped clear of, and its slot is left while the rotation goes
        on (V282)."""
        if not self._press_answered(values):
            return False
        usable, ready = values.get("bars.usable"), values.get("bars.ready")
        now = time.monotonic()
        if self._in_gcd(now):
            return False
        for row in profile.by_role(Role.ROOT):
            bit = 1 << (row.slot - 1)
            if ((usable is not None and not usable & bit) or (ready is not None and not ready & bit)
                    or self._held.get(row.slot, 0.0) > now):
                continue
            if self._mana_left_after(row, values) < 0 or not self._press(row):
                continue
            if not self._root_landed(row):
                return False
            many = (values.get("combat.attackers") or 0) >= 2
            self._asides += many
            key, seconds = (("q" if self._asides % 2 else "e", STEP_ASIDE_S) if many
                            else ("s", STEP_CLEAR_S))
            event("engage.root", data={"slot": row.slot, "step": key, "step_s": seconds})
            if not self.hid.hold(key, seconds):
                self._input_refused = True
                self.detail = "step-clear input refused"
            return True
        return False

    def _root_landed(self, row: Ability) -> bool:
        return self._landed(row, "root")

    def _landed(self, row: Ability, what: str) -> bool:
        """The client's answer to an instant's press - a root's, a hold's - looked for until
        `PRESS_ANSWER_S` (V282). Unanswered, the press is settled as dropped and the slot left
        for `NOT_READY_HOLD_S`. A strip with no bar to read cannot tell, and the press counts
        as landed, as before."""
        pressed = time.monotonic()
        while True:
            time.sleep(pace(self.hid, ROOT_LOOK_S))
            v = self.read()
            self._observe(v)
            if v is not None and v.get("bars.ready") is None:
                return True
            if v is not None and self._pending_press is not None and self._answer_in(v):
                self._press_answered(v)
                return True
            if time.monotonic() - pressed >= PRESS_ANSWER_S:
                break
        if v is not None:
            self._press_answered(v)
        self._pending_press = None
        self._held[row.slot] = time.monotonic() + NOT_READY_HOLD_S
        event(f"engage.{what}_unanswered", data={"slot": row.slot})
        return False

    def _holding_now(self, now: float) -> bool:
        """A unit held out of the fight (Polymorph) is still held: nothing round the caster is
        pressed, a root or damage to all, which would wake it (V287)."""
        return any(until > now for until in self._holding.values())

    def _held_now(self, guid) -> bool:
        """This unit is held out of the fight (V287, V395)."""
        return guid is not None and self._holding.get(guid, -math.inf) > time.monotonic()

    def _acquire_or_wake(self, name_id: int | None, *, defend: bool) -> Fought | None:
        """`acquire`, and with nothing else to fight in a fight while a unit is held, the held
        one (V395): the one it was held from died, and a sheep left alone mends its health."""
        acquired = self.acquire(name_id, defend=defend)
        if (acquired is not Fought.NO_TARGET or not defend
                or not self._holding_now(time.monotonic())):
            return acquired
        self._waking = True
        try:
            acquired = self.acquire(name_id, defend=defend)
        finally:
            self._waking = False
        if acquired is None:
            self._holding.pop(self._selected_guid, None)
            event("fight.wake", data={"guid": self._selected_guid})
        return acquired

    def _hold_wanted(self, profile: CombatProfile, values: dict) -> bool:
        """Hold an attacker out of the fight (V287, V395): a second attacker counted, the
        selected one attacking and not about to die (`HOLD_FINISH_HP`), a hold pressable now,
        none held already, one try a fight."""
        hp = values.get("target.hp")
        return (not self._held_this_fight
                and (values.get("combat.attackers") or 0) >= 2
                and values.get("target.attacking_me") is True
                and values.get("target.guid") is not None
                and isinstance(hp, (int, float)) and hp > HOLD_FINISH_HP
                and not self._holding_now(time.monotonic())
                and bool(self._hold_rows(profile, values)))

    def _hold_rows(self, profile: CombatProfile, values: dict) -> list[Ability]:
        """The holds the bar can press now: usable, ready, not left as refused, affordable."""
        usable, ready = values.get("bars.usable"), values.get("bars.ready")
        now = time.monotonic()
        rows = []
        for row in profile.by_role(Role.CC):
            bit = 1 << (row.slot - 1)
            if ((usable is not None and not usable & bit) or (ready is not None and not ready & bit)
                    or self._held.get(row.slot, 0.0) > now or self._mana_left_after(row, values) < 0):
                continue
            rows.append(row)
        return rows

    def _hold(self, profile: CombatProfile, values: dict) -> Fought | None:
        """Hold the healthiest attacker that is not selected (V395), found by Tab, faced and
        pressed, and end the fight once the hold has landed: the next fight takes the one
        attacking, the held one left (`_acceptable`) while it lasts. With none other found,
        the selected one itself, while nothing has touched it (V287's hold). `None` if
        nothing was held; the fight goes on with whatever attacker is selected then."""
        if not self._press_answered(values) or self._in_gcd(time.monotonic()):
            return None
        rows = self._hold_rows(profile, values)
        if not rows:
            return None
        self._held_this_fight = True
        fought = values.get("target.guid")
        # No swing at what is about to be held: a blow breaks a Gouge or a Polymorph.
        self._stop_swinging(profile, values)
        other = self._select_other(fought, values.get("combat.attackers") or 2)
        if self._input_refused:
            return None
        if other is not None:
            v = other
        else:
            v = self._back_to(fought)
            if v is None or v.get("target.guid") != fought:
                return self._lost_selection(v)
            if not self._untouched(v):
                return None                  # back on the unit fought: the fight goes on
        row = next((r for r in rows if self._holds(r, v)), None)
        if row is None or not self._face_for_hold(v):
            return self._adopt(v)
        before = v.get("vitals.power")
        if not self._press(row):
            return None
        reach_ = reach(row.spell_id)
        landed = (self._cast_through(row, before) if reach_ is not None and not reach_.instant
                  else self._landed(row, "hold"))
        guid = v.get("target.guid")
        if not landed:
            event("fight.hold_failed", data={"slot": row.slot, "guid": guid,
                                             "other": other is not None})
            return self._adopt(self.read() or v)
        self._holding[guid] = time.monotonic() + (row.holds_s or HELD_S)
        self._after_hold = True
        event("fight.hold", data={"slot": row.slot, "name": row.name,
                                  "name_id": v.get("target.name_id"), "guid": guid,
                                  "target_hp": v.get("target.hp"), "fought": fought,
                                  "fought_hp": values.get("target.hp"), "other": other is not None,
                                  "attackers": values.get("combat.attackers")})
        self.detail = (f"held {'another attacker' if other is not None else 'the selected unit'}"
                       f" ({row.name}) with more than one attacking; the one attacking next")
        return Fought.HELD

    def _select_other(self, fought, attackers: int) -> dict | None:
        """Tab to the healthiest attacker that is not `fought` and is not held (V395), in at
        most `HOLD_TABS` presses to see them and as many more to come back to the best; the
        reading with it selected, or `None` with none found (the selection where Tab left
        it). Tab takes what is in front, as the client's does: an attacker behind is not
        found, and the selected one is held instead if nothing has touched it yet."""
        found: dict = {}
        current = None
        for _ in range(HOLD_TABS):
            current = self._tab_once()
            if current is None:
                return None
            if self._holdable(current, fought):
                found[current.get("target.guid")] = current.get("target.hp") or 0.0
                if len(found) >= max(1, attackers - 1):
                    break
        if not found:
            return None
        best = max(found, key=lambda guid: found[guid])
        for _ in range(HOLD_TABS):
            if current is not None and current.get("target.guid") == best:
                return current
            current = self._tab_once()
            if current is None:
                return None
        return current if current is not None and self._holdable(current, fought) else None

    def _back_to(self, fought) -> dict | None:
        """Tab back to the unit fought, at most `HOLD_TABS` presses; the reading at the end."""
        v = self.read()
        self._observe(v)
        for _ in range(HOLD_TABS):
            if v is None or v.get("target.guid") == fought:
                return v
            v = self._tab_once()
        return v

    def _lost_selection(self, values: dict | None) -> Fought | None:
        """Tab left another unit selected: fought on when it attacks, else the fight ends and
        the next takes what attacks (V395) - never a bystander pulled."""
        if values is not None and values.get("target.attacking_me") is True:
            return self._adopt(values)
        if values is None or self._input_refused:
            return None
        self.detail = "the search for an attacker to hold left a bystander selected"
        return Fought.LOST

    def _tab_once(self) -> dict | None:
        """One Tab and the paint after it; `None` blind or refused."""
        event("selection.request", data={"method": "tab", "for": "hold"})
        if not self.hid.tap("tab"):
            self._input_refused = True
            self.detail = "selection input refused"
            return None
        paint = self._targeting().wait_for_paint()
        if paint.code is not PaintCode.FRESH:
            return None
        self._observe(paint.after)
        return paint.after

    def _holdable(self, values: dict, fought) -> bool:
        """An attacker other than `fought`, alive, not held already, not another's."""
        guid, hp = values.get("target.guid"), values.get("target.hp")
        return (values.get("target.has") is True and guid is not None and guid != fought
                and values.get("target.attacking_me") is True
                and isinstance(hp, (int, float)) and hp > DEAD_HP
                and not self._held_now(guid) and not tagged(values))

    def _untouched(self, values: dict) -> bool:
        """The selected unit as yet untouched (`HOLD_FRESH_HP`), nothing of ours on it."""
        hp, guid, now = values.get("target.hp"), values.get("target.guid"), time.monotonic()
        return (isinstance(hp, (int, float)) and hp >= HOLD_FRESH_HP
                and not any(g == guid and until > now for (g, _), until in self._dotted.items()))

    @staticmethod
    def _holds(row: Ability, values: dict) -> bool:
        """This hold takes this unit (V395): of a creature type it takes (Hibernate beasts and
        dragonkin, by name and level, `jev.world.creatures`); and a root, which pins a unit
        where it stands, only one not at hand, whose blows it keeps off."""
        from jev.world.creatures import takes

        if row.pins and values.get("target.in_melee") is not False:
            return False
        level = values.get("target.level")
        return takes(row.creatures, values.get("target.name_id"),
                     level if isinstance(level, int) else None)

    def _face_for_hold(self, values: dict) -> bool:
        """Face the unit to hold, as a fight faces an attacker, without the swing `engage`
        starts: the spell asks for it in front."""
        result = self._targeting().face_selected(
            expected_name_id=values.get("target.name_id"), hint=None,
            search_s=FACE_SEARCH_MAX_S, stop=None, deadline_s=FIGHT_FACE_S)
        event("engage.request", code=result.code.value, data={"for": "hold",
                                                              "offset": result.offset})
        return result.faced

    def _stop_swinging(self, profile: CombatProfile, values: dict) -> None:
        """Melee auto-attack off before the selection moves to the unit to hold (V395)."""
        toggle = next((a for a in profile.by_role(Role.ATTACK) if a.toggle), None)
        if toggle is not None and values.get("bars.attacking") is True and self._press(toggle):
            self._toggled = False

    def _adopt(self, values: dict) -> None:
        """No hold: the fight goes on with the attacker selected now, whichever it is (V395)."""
        self._selected_name_id = values.get("target.name_id")
        self._selected_guid = values.get("target.guid")
        self._damage_mark = self.last_hp = values.get("target.hp")
        self.selected_plate = None
        self._race = []
        return None

    def _cast_through(self, row: Ability, before: float | None) -> bool:
        """A cast pressed, followed to its end (V287): `True` once it has completed, its mana
        gone from `before` - a cast's mana goes as it lands - within `HOLD_CAST_S`, pushback
        included. One that never began is settled as dropped, as `_press_answered` settles."""
        pressed = time.monotonic()
        began = False
        v = None
        while time.monotonic() - pressed < HOLD_CAST_S:
            time.sleep(pace(self.hid, ROOT_LOOK_S))
            v = self.read()
            self._observe(v)
            if v is None:
                continue
            if v.get("vitals.dead") is True:
                return False
            if v.get("bars.casting") is True:
                if not began:
                    began = True
                    self._press_answered(v)          # its global cooldown from the press
                continue
            if not began and time.monotonic() - pressed >= PRESS_ANSWER_S:
                self._press_answered(v)              # never began: dropped
                return False
            if began:
                # Fear's cost is a share of the base mana the catalog does not say: any mana
                # gone is its landing (V395).
                power, pool = v.get("vitals.power"), v.get("vitals.power_max")
                if not (isinstance(before, (int, float)) and isinstance(power, (int, float))
                        and bool(pool)):
                    return False
                spent = (before - power) * pool
                return spent >= ANSWER_SPENT * row.mana if row.mana else spent > 0
        if v is not None and self._pending_press is not None:
            self._press_answered(v)
        return False

    @staticmethod
    def _beyond_reach(profile: CombatProfile, values: dict) -> bool:
        """The strip says none of the caster's ranged attacks reaches the unit (schema 17's
        `bars.out_range`): step in before pressing, not after the client's error (V171)."""
        out, reach_ = values.get("bars.out_range"), values.get("bars.in_range")
        if not isinstance(out, int) or not isinstance(reach_, int):
            return False
        slots = [1 << (a.slot - 1) for a in profile.by_role(Role.ATTACK) if ranged(a)]
        return bool(slots) and all(out & bit for bit in slots) and not any(
            reach_ & bit for bit in slots)

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

        Asked with a fresh reading (V191). A Fleshripper hovers over the character, its
        plate above the top of the screen: Tab picked it, the look for the plate ran out
        3.5 s later with it biting, and the reading from before the look - not yet in melee,
        not yet attacking - refused this: six fights in session 151 gave up "not visible".

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

    def _face_behind(self) -> bool:
        """Not in front, says the client, while the plate stands on the centre line: face
        where the camera looks, and if it still says so, turn round, the unit being behind.
        `False` if an input was refused."""
        if self.realign is not None and not self._realigned:
            self._realigned = True
            event("engage.realign")
            if self.realign() is False:
                self._input_refused = True
                self.detail = "camera realign refused"
                return False
        elif not self._turn_round():
            return False
        else:
            self._realigned = False
        self._reach_at = time.monotonic()
        self._aim_code = None                  # the next aim proves the plate afresh
        return True

    def _cast_blind(self, values: dict) -> bool:
        """Cast at a caster's Tab pick whose plate could not be proved. `True` if taken up.

        Tab picks only in front, and a spell needs only that and its range - which the strip
        says per slot (`bars.in_range`, V171) - not a plate on screen. On the mage's session
        of 26 September 06:24, all ten Tab picks among Northshire's wolves were given up
        "no plate proved" with nothing pressed. Out of reach, the fight steps forward
        (`_blind_step`); "facing the wrong way" is a quarter turn, as in blind melee (V204).
        Casters only: a melee character must see what it walks at.
        """
        profile = self.profile or for_class(values.get("char.class_id"), values.get("char.race_id"))
        hp = values.get("target.hp")
        if (self._aim_code is not FaceCode.NOT_VISIBLE or not self._ahead
                or not self._ranged_ready(profile, values)
                or values.get("target.has") is not True
                or (isinstance(hp, (int, float)) and hp <= DEAD_HP)):
            return False
        event("engage.blind_cast", data={"name_id": values.get("target.name_id")})
        self._blind_cast = True
        self._last_aim_at = time.monotonic()
        return True

    def _blind_step(self) -> bool:
        """One step ahead toward a blind cast's Tab pick that a spell does not reach yet."""
        event("approach.request", data={"key": "w", "mode": "blind_step",
                                        "duration_s": RANGED_STEP_S, "steps": self._ranged_steps})
        if not self.hid.hold("w", RANGED_STEP_S):
            self._input_refused = True
            self.detail = "approach input refused"
            return False
        self.closed += 1
        return True

    def _blind_close(self) -> bool:
        """A short step ahead at a blind melee's attacker the client says is too far (V207)."""
        event("approach.request", data={"key": "w", "mode": "blind_melee",
                                        "duration_s": BLIND_STEP_S, "strides": self._strides})
        if not self.hid.hold("w", BLIND_STEP_S):
            self._input_refused = True
            self.detail = "approach input refused"
            return False
        self.closed += 1
        return True

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
            if hp is None or (hp >= self._heal_line(v) and not saved
                              and self._pending_heal is None):
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
        raw_usable = usable                  # before out of range is taken off: the mana's

        profile = self.profile or for_class(values.get("char.class_id"),
                                            values.get("char.race_id"))
        self._watch_heal(values, ready)
        # An attack whose slot the client marks out of range is not pressed (V294), a ranged
        # one's dead zone beside the unit included: hunters pressed Auto Shot in melee 129
        # times an hour each, every press "too close" (the hive). Melee's toggle is left alone.
        out = values.get("bars.out_range")
        if isinstance(out, int) and out:
            for attack in profile.by_role(Role.ATTACK):
                if not attack.toggle:
                    usable &= ~(out & (1 << (attack.slot - 1)))

        looked = time.monotonic()

        def pressable(a: Ability) -> bool:
            bit = 1 << (a.slot - 1)
            return (bool(ready & bit) and bool(usable & bit)
                    and self._held.get(a.slot, 0.0) <= looked
                    and (a.toggle or repeats(a) or not self._in_gcd(looked)))

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
                and (hp < self._heal_line(values) or (saved and hp < SAVE_HEAL_BELOW))
                and self._has_mana_for(heal, values)
                and (saved or not self._finishes_first(values))):
            # Clear the way for it first, where the bar can: immune (Divine Protection),
            # or the attacker stunned (Hammer of Justice). Pushback is what left a level 6
            # paladin's Holy Lights unfinished for 22 s against one wolf. Under a save the
            # way is clear already.
            for guard in () if saved else (*profile.by_role(Role.SAVE),
                                           *profile.by_role(Role.STUN)):
                # A shield that lasts (Power Word: Shield, V361) is not pressed again while it
                # does, across fights: its Weakened Soul refuses it for 15 s.
                lasts = guard.role is Role.SAVE and guard.every_s > 0
                if (lasts and time.monotonic() - self._lasting.get(guard.name, -math.inf)
                        < guard.every_s):
                    continue
                if (pressable(guard) and self._has_mana_for(guard, values)
                        and (guard.role is Role.SAVE
                             or values.get("target.attacking_me") is True)):
                    if self._press(guard) and guard.role is Role.SAVE:
                        self._saved_at = time.monotonic()
                        if lasts:
                            self._lasting[guard.name] = self._saved_at
                    return
            if self._press(heal):
                self._pending_heal = (hp, time.monotonic())
            return

        # 1a. Hold a crowd's blows off (V396): Psychic Scream, Evasion, Retaliation, with two
        #     attackers or more at hand or a fight being lost. A scream clears the way for a
        #     heal as a save does.
        guard = self._guard_due(profile, values, pressable) if in_combat else None
        if guard is not None:
            if self._press(guard):
                now = time.monotonic()
                self._guarded_until = now + (guard.holds_s or GUARD_S)
                if guard.around and heal is not None:
                    self._saved_at = now
                event("fight.guard", data={"slot": guard.slot, "name": guard.name,
                                           "attackers": values.get("combat.attackers"),
                                           "hp": hp, "target_hp": values.get("target.hp")})
            return
        if survival_only:
            return

        # 1b. Stop a runner. Gnolls and Defias run at about a fifth of their health and come
        #     back with their camp: two of session 149's deaths were among four and five
        #     Riverpaw gnolls, after "Riverpaw Herbalist attempts to run away in fear!".
        target_hp = values.get("target.hp")
        if (in_combat and isinstance(target_hp, (int, float)) and 0 < target_hp < RUNNER_HP
                and values.get("target.attacking_me") is False
                and values.get("target.in_melee") is True):
            for stun in profile.by_role(Role.STUN):
                if pressable(stun) and self._has_mana_for(stun, values):
                    event("fight.runner", data={"target_hp": round(target_hp, 3)})
                    self._press(stun)
                    return

        # 2. Keep the buffs up, and only when one is actually lapsing: `bars.ready` says a
        #    seal is pressable on every single tick, so without the interval the
        #    character stands there re-sealing and never swings. An aura, once pressed,
        #    lasts until a death; a blessing, its ten minutes, across fights.
        now = time.monotonic()
        reserve = self._mana_reserve(profile) if in_combat else 0

        def affordable(a: Ability) -> bool:
            facts = reach(a.spell_id)
            keep = 0 if facts is not None and facts.next_swing else held_back()
            return self._mana_left_after(a, values) >= reserve + keep

        def held_back() -> int:
            return sum(cost for until, cost in self._queued.values() if until > now)

        for buff in (*profile.by_role(Role.AURA), *profile.by_role(Role.BUFF)):
            if profile.caster and buff.lasting and in_combat:
                continue                 # a caster's mana is its damage: buffed between fights
            last = self._lasting.get(buff.name) if buff.lasting else self._last_use.get(buff.slot)
            if pressable(buff) and affordable(buff) and (last is None or now - last >= buff.every_s):
                if self._press(buff) and buff.lasting:
                    self._lasting[buff.name] = now
                return

        # 2b. Open on the unit (V404): its mark (Hunter's Mark) once while it lasts, on a unit
        #     with most of its health left; and a slow (Concussive Shot) at one coming for the
        #     character and not yet at hand, not again while it lasts. Neither is pressed out of
        #     its reach (`bars.out_range`).
        self._dot_guid = values.get("target.guid")
        far = out if isinstance(out, int) else 0
        coming = (values.get("target.attacking_me") is True
                  and values.get("target.in_melee") is False)
        for row, wanted, lasts in (
                *((r, isinstance(target_hp, (int, float)) and target_hp >= MARK_ABOVE,
                   r.every_s or MARK_S) for r in profile.by_role(Role.MARK)),
                *((r, coming, r.holds_s or SLOW_S) for r in profile.by_role(Role.SLOW))):
            if (wanted and not far & (1 << (row.slot - 1))
                    and self._dotted.get((self._dot_guid, row.name), -math.inf) <= now
                    and pressable(row) and affordable(row) and self._press(row)):
                self._dotted[(self._dot_guid, row.name)] = now + lasts
                event("fight." + row.role.value, data={"slot": row.slot, "name": row.name,
                                                       "target_hp": target_hp})
                return

        # 3. Swing. A toggle is pressed at most once and only before anything has landed,
        #    because pressing melee auto-attack while already swinging **stops** it. A
        #    caster with the mana casts instead: its staff is for when the mana is gone.
        casting_instead = self._ranged_ready(profile, values)
        # Damage round the character reaches only what is at hand, and is worth its cost
        # against more than one: first then, and not pressed otherwise (V277).
        crowd = (values.get("target.in_melee") is True
                 and (values.get("combat.attackers") or 0) >= 2
                 and not self._holding_now(time.monotonic()))       # it would wake it (V287)
        areas = tuple(a for a in profile.by_role(Role.ATTACK) if _area(a))
        # By damage a second where the data says it, not by the bar's slot (V360): Smite in
        # slot 2 left Mind Blast pressed in 1% of the priests' kills.
        # A caster's instant blows keep their places: kept for contact (V165).
        attacks = by_value(tuple(a for a in profile.by_role(Role.ATTACK) if not _area(a)),
                           keep=instant_blow if casting_instead else None)
        # Damage over time already on this unit is not pressed again while it lasts, and a
        # next-swing blow pressed keeps its cost for its swing (V360).
        self._dot_guid = values.get("target.guid")
        attacks = tuple(a for a in attacks if not self._dotted_now(a, now))
        if casting_instead:
            attacks = self._caster_order(attacks, values)
        # A shooter's repeating shot first, once: it costs nothing and no global cooldown, and
        # pressed while it repeats it would stop (V358). Never in melee, where it cannot reach.
        shots = tuple(a for a in attacks if repeats(a))
        attacks = tuple(a for a in attacks if not repeats(a))
        if profile.caster and shots:
            # A caster's wand (V397): shot at a unit nearly dead, or with no spell's mana left,
            # and let repeat - any press stops it, as pressing it again does.
            if self._wand_due(values, attacks,
                              lambda a: bool(raw_usable & (1 << (a.slot - 1))) and affordable(a)):
                if self._shooting_at is not None:
                    return
                attacks = shots
        elif casting_instead and self._shooting_at is None:
            attacks = (*shots, *attacks)
        if crowd:
            attacks = (*areas, *attacks)
        # Jev's pick among the attacks ready now, asked as the last press began (V298); the
        # bar's order when it has none. Melee's toggle stays the bar's.
        if self.judge is not None:
            choices = {_jev_name(a): a for a in attacks
                       if not a.toggle and pressable(a) and affordable(a)}
            picked = self.judge.take(tuple(choices))
            if picked is not None:
                attacks = (choices[picked], *(a for a in attacks if a is not choices[picked]))
        for attack in attacks:
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
                if self.judge is not None:
                    self.judge.ask(values, {_jev_name(a): _jev_text(a) for a in attacks
                                            if not a.toggle and affordable(a)})
            return

    def _guard_due(self, profile: CombatProfile, values: dict, pressable) -> Ability | None:
        """The guard to press now (V396), or `None`: with the selected unit at hand, two
        attackers or more counted and none held, or the fight being lost (`_losing`); one
        guard at a time while it lasts; a scream round the character not while a unit is held,
        whom it would take too."""
        rows = profile.by_role(Role.GUARD)
        now = time.monotonic()
        if (not rows or values.get("target.in_melee") is not True
                or now < self._guarded_until):
            return None
        holding = self._holding_now(now)
        crowd = (values.get("combat.attackers") or 0) >= 2 and not holding
        if not crowd and not self._losing(values):
            return None
        for row in rows:
            if row.around and holding:
                continue
            if pressable(row) and self._mana_left_after(row, values) >= 0:
                return row
        return None

    def _losing(self, values: dict) -> bool:
        """The character's health falling `GUARD_RACE` times as fast as the selected unit's
        or more over the race's window (`FINISH_WINDOW_S`), with `GUARD_LOSS` of it gone in
        it (V396): this selection's samples (`_sample_race`)."""
        now = time.monotonic()
        recent = [(t, hp, target) for t, hp, target, _, _ in self._race
                  if now - t <= FINISH_WINDOW_S and hp is not None and target is not None]
        if len(recent) < 2 or recent[-1][0] - recent[0][0] < FINISH_EVIDENCE_S:
            return False
        ours = recent[0][1] - recent[-1][1]
        theirs = max(0.0, recent[0][2] - recent[-1][2])
        return ours >= GUARD_LOSS and ours >= GUARD_RACE * theirs

    def _wand_due(self, values: dict, spells: tuple[Ability, ...], castable) -> bool:
        """A caster's wand is shot now (V397): its unit below `WAND_FINISH_HP`, or none of its
        spells from range `castable` - the client's usable bit, out of range or not, and the
        mana above what a priest keeps for its heal; not once its shots have gone silent this
        fight (`_watch_shots`)."""
        if self._no_shots:
            return False
        hp = values.get("target.hp")
        finishing = isinstance(hp, (int, float)) and not isinstance(hp, bool) and hp < WAND_FINISH_HP
        return finishing or not any(castable(a) for a in spells if ranged(a))

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
        facts = reach(ability.spell_id)
        if lingers(ability) and facts is not None:
            # On this unit until it runs out (V360); taken off again if the press goes
            # unanswered (`_press_answered`).
            self._dotted[(self._dot_guid, ability.name)] = now + facts.dot_s
        if facts is not None and facts.next_swing:
            self._held[ability.slot] = now + NEXT_SWING_HOLD_S
            self._queued[ability.slot] = (now + NEXT_SWING_HOLD_S, ability.mana)
        elif repeats(ability):
            # No cast, no global cooldown and nothing spent: nothing for the client to answer
            # with, so no press waits on one. Taken as repeating (`_watch_shots`).
            self._shooting_at = now
            profile = self.profile
            self._wanding = profile is not None and profile.caster
        elif not ability.toggle:
            self._pending_press = (ability, now, dict(self._last_use), dict(self._lasting),
                                   self._saved_at, self._mana_seen[1])
        if self._wanding and not repeats(ability):
            # A spell stops a wand's shot, as the server breaks any repeating spell but Auto
            # Shot (V397): pressed again when next due.
            self._shooting_at, self._wanding = None, False
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
        ability, when, last_use, lasting, saved_at, _ = self._pending_press
        if self._answer_in(values):
            self._pending_press = None
            if not ability.toggle:
                self._gcd_from = when
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
        # With the client's last error, which says why when it said anything: a quarter of
        # the mage's presses went unanswered in sessions 201 and 214.
        error = values.get("ui.error_last")
        key = (UI_ERROR_KEYS[error] if isinstance(error, int) and 0 < error < len(UI_ERROR_KEYS)
               else None)
        event("ability.unanswered", data={
            "slot": ability.slot, "role": ability.role.value, "times": times, "error": key,
            "error_count": values.get("ui.error_count"), "in_melee": values.get("target.in_melee")})
        if key == "not_ready":
            self._held[ability.slot] = time.monotonic() + NOT_READY_HOLD_S
        if times >= PRESS_GIVE_UP:
            self._dropped = (0, 0)
            return True                        # counted after all, as before
        self._dropped = (ability.slot, times)
        self._last_use, self._lasting, self._saved_at = last_use, lasting, saved_at
        self._dotted.pop((self._dot_guid, ability.name), None)
        if ability.role is Role.HEAL:
            self._pending_heal = None
        return True

    def _in_gcd(self, now: float) -> bool:
        """Inside the global cooldown of the last press the client acted on (V285)."""
        return self._gcd_from is not None and now - self._gcd_from < GCD_GUARD_S

    def _answer_in(self, values: dict) -> bool:
        """The client acted on the pending press: a cast, the global cooldown, the slot's own
        cooldown, or the ability's mana gone."""
        ability, _, _, _, _, power_before = self._pending_press
        ready = values.get("bars.ready")
        gcd = values.get("bars.gcd")
        power, pool = values.get("vitals.power"), values.get("vitals.power_max")
        # Its mana gone is an answer too (V176): a mage's Frost Armor, cast, left the bar and
        # the global cooldown unpainted, was read as dropped and cast again, 60 of 165 mana.
        spent = (bool(ability.mana) and isinstance(power_before, (int, float))
                 and isinstance(power, (int, float)) and bool(pool)
                 and (power_before - power) * pool >= ANSWER_SPENT * ability.mana)
        return (values.get("bars.casting") is True or (gcd is not None and gcd > 0.0)
                or (ready is not None and not ready & (1 << (ability.slot - 1))) or spent)

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
                and not isinstance(hp, bool) and hp < self._heal_line(values)):
            return f"{hp:.0%} health in a fight: the heal first"
        return None

    def _heal_line(self, values: dict | None) -> float:
        """This look's heal line: the fight's, or once two or more are attacking (schema 17's
        `combat.attackers`), the pack's, drawn and learned apart (V172). Two and three
        attackers killed Testvvi at Jerod's Landing and Riverpaw: what holds against one
        wolf need not against a camp."""
        if not self._heals:
            return 0.0                   # no heal to stop anything for: a mage's search goes on
        attackers = (values or {}).get("combat.attackers")
        if (isinstance(attackers, int) and attackers >= 2 and self.choices is not None):
            if self._pack_line is None:
                self._pack_line = self.choices.pick("pack", HEAL_LINES)
            return float(self._pack_line)
        return self.heal_below

    def mana_line(self) -> float | None:
        """The mana a caster wants before a pull, from what its kills have cost (V170); `None`
        until it has `MANA_KILLS_MIN` kills to go on."""
        if len(self.mana_costs) < MANA_KILLS_MIN:
            return None
        low, high = MANA_LINE_RANGE
        return min(high, max(low, MANA_MARGIN * statistics.median(self.mana_costs)))

    def _observe(self, values: dict | None) -> None:
        attackers = None if values is None else values.get("combat.attackers")
        if isinstance(attackers, int) and not isinstance(attackers, bool):
            self._most_attackers = max(self._most_attackers, attackers)
        hp = None if values is None else values.get("vitals.hp")
        if isinstance(hp, (int, float)) and not isinstance(hp, bool):
            self._low_hp = hp if self._low_hp is None else min(self._low_hp, hp)
        power = None if values is None else values.get("vitals.power")
        if isinstance(power, (int, float)) and not isinstance(power, bool):
            first, _ = self._mana_seen
            self._mana_seen = (power if first is None else first, power)
        event("combat.observed", code="blind" if values is None else "readable",
              data={} if values is None else {key: values.get(key) for key in (
                  "vitals.hp", "vitals.power", "vitals.combat", "vitals.dead", "vitals.ghost",
                  "target.has", "target.name_id", "target.hp", "target.in_melee",
                  "target.attacking_me", "bars.casting", "bars.ready", "bars.usable",
                  "combat.attackers")})
