"""Catalog skills composed from the body that already passed the live slice.

This module selects no guide step and writes no parallel decision stream. It executes
the runtime's arm, using the existing planner, locator, quest UI, Fight, Loot and Rest.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from jev.clients.advance import AdvanceQuestFrame, Goal
from jev.clients.camera import Camera
from jev.clients.choose import ChooseListLine
from jev.clients.fight import HEAL_POINT, Fight, Kinds, Paying, Quarry
from jev.clients.gather import Gather, Gathered
from jev.clients.hearth import Hearth, Hearthed
from jev.clients.interact import Interact
from jev.clients.interact import Result as Interacted
from jev.clients.loot import Loot, Looted
from jev.clients.recover import Recover, Recovered
from jev.clients.repair import Repair
from jev.clients.rest import SLOT_KEYS as REST_KEYS
from jev.clients.rest import Rest
from jev.clients.spellbook import Spellbook
from jev.clients.talents import TALENTS_SCHEMA, TalentDesk, build_for
from jev.clients.targeting import FaceCode, Targeting
from jev.clients.taxi import TaxiDesk
from jev.clients.trainer import TrainerDesk
from jev.clients.vendor import Vended, Vendor
from jev.clients.windows import close_observed
from jev.coach.policy import BAGS_LOW, Context, service
from jev.coach.schema import Intent
from jev.guide.coords import map_to_world, world_to_map
from jev.guide.graph import Graph, ObjectiveTarget
from jev.guide.objectives import QUEST_ABSENT, progress, select_objective, target_progress
from jev.guide.path import PathStatus
from jev.guide.route_memory import CAMP_REFUSED, CAMP_YARDS, RECORD_FLUSH_S
from jev.guide.spawns import around as spawn_around
from jev.guide.spawns import lookup as spawn_points
from jev.learn.choices import Choice, Stations, objective_key
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed
from jev.perceive.radio_frame import CLASS_BY_ID, RACE_BY_ID, UI_ERROR_KEYS, list_lines, name_id
from jev.run.client import FLOOR_SWITCH_YARDS, FOCUS_QUICK_S, Client, surfaces_under
from jev.run.evidence import event
from jev.run.hunt import DEFAULT_HUNT_YARDS, PACK_YARDS, Hunt, Hunted, Place, spawn_tour
from jev.run.hunt import stations as hunt_stations
from jev.run.supervisor import BodyFailure, Cancelled, FocusLost, Result, Unsupported
from jev.world import graveyards, hostiles
from jev.world.combat import (
    HEAL_OUT_OF_COMBAT,
    Role,
    drink_to,
    for_class,
    grey_level,
    is_caster,
    rest_mana,
)
from jev.world.combat import from_bar as profile_from_bar
from jev.world.gear import keep as gear_keep
from jev.world.gear import load_worn, save_worn
from jev.world.gear import upgrades as gear_upgrades
from jev.world.home import load_home, save_home
from jev.world.quarry import NEAR_YARDS, Held
from jev.world.quarry import held as quarry_held
from jev.world.state_v1 import PowerType, State, StepKind
from jev.world.taxi import Node as TaxiNode
from jev.world.taxi import flight as flight_plan
from jev.world.taxi import load_nodes, save_node, visited
from jev.world.training import placements as spell_placements
from jev.world.training import spell as spell_facts
from jev.world.training import trainer_due, training_cost, unpressed
from jev.world.vendor import (
    Supply,
    bag_prices,
    bag_slots,
    consumable_role,
    consumables,
    flightmasters,
    innkeepers,
    junk_prices,
    load_merchant_failures,
    merchants,
    note_merchant,
    supplies_for,
    surplus_prices,
)

# Dying again this soon after getting up at the body means the body lies where something
# this character cannot beat still stands: the next recovery gets up at the graveyard's
# Spirit Healer instead, and goes home by hearthstone.
DEATH_TRAP_S = 180.0
# Killed by a unit this many levels above the character, it gets up at the Spirit Healer and
# goes home, not beside its body among them (V197): a level 3 mage stranded among level 5-6
# Mangy Wolves in west Elwynn got up at its body and died five times in one session.
OUTCLASSED_BY = 3
# Getting up at the Spirit Healer brings resurrection sickness: three quarters of every
# stat gone, for a minute a level above ten (ten at most). Walking out under it, a level 13
# paladin met a Dust Devil 90 s after getting up and died (session 150). It is waited out
# where the hearthstone took the character, inside the corpse run's own 420 s (V189).
SICKNESS_FROM_LEVEL = 10
SICKNESS_WAIT_MAX_S = 300.0
# Where a ghost gets up: this far short of the body, on the graveyard's side. A body can be
# reclaimed from inside the server's 39-yard radius and the character stands up where the
# ghost stood; at the body itself the Mangy Wolves round it killed a character at half
# health three times running, the Spirit Healer answering nothing each time (run
# 20260924T041014-a9781c). 25, not 32: on Sentinel Hill's slope a ghost stopped 32 yards
# short (34 by the walk's own slack) was out of the radius with the height, and walked there
# again and again, "still a ghost" (session 178). After each such get-up that did not come,
# the next stops half as far short (V213).
TRAP_RECLAIM_YARDS = 25.0
# A ghost this near its body on the map and still a ghost is on another floor than the body
# (V289): on the hill over Shadowthread Cave, 110 yards above a body in it, every plan from
# there started on the cave's floor under the ghost and arrived at once (the hive, 28 Sep).
OVER_BODY_YARDS = 5.0
# The server's reach for a reclaim: 39 yards, measured with the height (mangos-tbc
# `CORPSE_RECLAIM_RADIUS`, `HandleReclaimCorpseOpcode`). A ghost farther than that on the map
# alone cannot get up at its body whatever it presses.
RECLAIM_REACH_YARDS = 39.0
# Corpse runs that end still a ghost at the same body before the ghost gets up at the Spirit
# Healer instead (V301), and how near another body lies to be the same place. Two drowned orcs'
# bodies lay on the seabed off Ratchet, 60 to 70 yards under the ghosts walking the water over
# them: every walk arrived and every reclaim was refused. Ghosts since 08:05 and 08:20 on 28
# Sep, their 29 and 34 corpse runs of about 150 s in the hive's two hours to 11:11 all ended
# still a ghost.
UNREACHED_RUNS = 2
SAME_BODY_YARDS = 5.0
# How far round a body or a meal the spawns of units that attack on sight are looked for
# (`jev.world.hostiles`, V247), and the least room from them a get-up spot must have: under
# it, the body lies in a camp and the ghost gets up at the Spirit Healer. At the 34 bodies of
# sessions 195-219, the best spot 25 yards out had 20 yards or more in 28, and under 18 only
# in Fargodeep's kobold camp, three.
HOSTILE_LOOK_YARDS = 70.0
# The hearthstone's cooldown (an hour on 2.4.3), and how long a press that did not move the
# character leaves it before trying again: 10 of the mage's 14 presses in sessions 205-217 met
# a stone still cooling, about 20 s each, and a wedge pressed it walk after walk (V253).
HEARTH_COOLDOWN_S = 3600.0
HEARTH_RETRY_S = 600.0
CAMP_ROOM_YARDS = 18.0
# A unit not found where the walk to it ended, this near in x and y and standing this high
# over the lowest floor there, is on a floor above: walked to once more from below (V235).
UNDER_UNIT_YARDS = 12.0
UPPER_FLOOR_YARDS = 4.0
# Broken gear this far from the nearest repairer goes home by hearthstone first.
BROKEN_DURABILITY = 0.05
HEARTH_TO_REPAIR_YARDS = 150.0
# Where to hover for a unit too close and tall for its nameplate to show, as fractions of
# the client: down the middle first. The Spirit Healer stands over a fresh ghost and fills
# the centre of the screen, with its plate drawn behind the strip at the top.
HOVER_POINTS = ((0.5, 0.35), (0.5, 0.45), (0.5, 0.25), (0.45, 0.35), (0.55, 0.35),
                (0.5, 0.55), (0.4, 0.3), (0.6, 0.3))
# A right-click from out of reach answers "You are too far away!": step toward the unit,
# which the hover found in front of the character, and click again.
HOVER_STEPS = 5
HOVER_STEP_S = 0.35
# The answer to a click - a popup, or the error - arrives after the server's reply, which
# is later than the first fresh paint (run 20260923T183537-ee5ef3 read silence and stopped).
HOVER_ANSWER_S = 1.2
# An exploration objective is credited by the server once the character is inside its
# trigger; the quest log shows it a paint or two later.
EXPLORE_CREDIT_S = 5.0
EXPLORE_POLL_S = 0.25
# A merchant whose body cannot be clicked is passed over for the next nearest one that
# sells what is wanted: Dermot Johns stands behind his wagon, every point between his ring
# and his plate was wagon, and Godric Rothgar stood in plain view beside it (run
# 20260924T011327-6e5f4b stopped on its first full bags).
MERCHANT_TRIES = 5
# Merchants are ranked by their walk, not their distance. A corner costs more than its
# length: doorways and stairs are where the follower sticks. Goldshire's warlock trainer
# sells too, in the inn's cellar: the nearest in a straight line from the south, and the
# way back out took 155 turns and 18 stuck events (session 90). Planned from there, the
# cellar costs 818 yards and the smith at the forge 571. The `MERCHANT_PLANS` nearest in a
# straight line are planned (a few ms). No narrower window: from the vineyards the two
# nearest were up a tower (41 and 53 corners, 982 and 1,209 yards), a 100-yard window left
# Goldshire's out, and both climbs failed (session 101). A merchant no route reaches costs
# `UNPLANNED_FACTOR` times its straight distance.
CORNER_YARDS = 15.0
MERCHANT_PLANS = 12
UNPLANNED_FACTOR = 3.0
# The merchants are planned one at a time, nearest in a straight line first, and no longer
# than this; a walk is never shorter than its straight line, so planning stops at the first
# merchant that could not beat the best walk planned (V309). Planning all twelve before a step
# was walked, each incomplete plan asked again at every start height, stood the hive's
# services still until they timed out: in the runs begun 12:50-15:00 on 28 Sep, 136 of 181
# service timeouts came after 300 s and more with nothing recorded where the ranking plans,
# and each timed-out bag service stopped its session, 52 of them from 13:00 to 14:59.
RANKING_BUDGET_S = 20.0
# Each failure since a merchant's last sale adds this to its walk. Ranked behind every
# merchant that never failed, one missed hover at each nearby merchant (fifteen in Elwynn
# and Westfall, 26 September) sent a level 3 mage from Northshire to Ben Trias in Stormwind,
# 1,030 yards, for water, past Brother Danil twenty yards away.
FAILED_MERCHANT_YARDS = 250.0
# A repairer that fails is followed only by one standing this near the first chosen (V203):
# Northshire's three smiths are twenty yards apart, but from Sentinel Hill the next after
# William MacGregor was the Defias Profiteer in Moonbrook, 625 yards among level 15-17
# Defias, walked for as soon as MacGregor's walk ended in the hearthstone (session 160).
REPAIRER_NEIGHBOUR_YARDS = 60.0
# Food and drink are not walked for from farther than this (V205): resting without them is
# slower, the walk is the danger. With no Darnassian Bleu sold in Northshire, the level 4
# mage's restock walked for Goldshire's barkeep, 675 yards through the Defias road, as the
# walk for Ben Trias had through Elwynn's wolves the hour before (26 September).
SUPPLY_WALK_MAX_YARDS = 400.0
# Binding the hearthstone (`LiveBody.bindable`): an inn this near the guide's current step,
# while home is farther than `HOME_FAR_YARDS` from it or unknown. Goldshire's inn is 590
# yards from Northshire's quests, which bind nowhere, and 360 from Fargodeep Mine's.
INN_NEAR_YARDS = 500.0
HOME_FAR_YARDS = 900.0
# An inn this close to the remembered home is home already.
SAME_INN_YARDS = 40.0
# The innkeeper's line for it, the same on all 58 of this server's innkeepers who have one.
BIND_LINE = "Make this inn your home."
# How long the confirmation takes to appear, and to go once accepted.
POPUP_S = 4.0
# A flight master this near the character, its node not yet remembered, is visited
# (`LiveBody.discoverable`): a node can only be flown to once it has been.
DISCOVER_YARDS = 150.0
# How long a flight may take: the take-off, then ten yards a second at the least.
FLIGHT_BASE_S = 90.0
FLIGHT_YARDS_PER_S = 10.0
# Free slots a bag service asks the merchant for: every stack it may sell (`Vendor._sell`
# stops when all of them are gone).
SELL_ALL = 999
# What the bags' goods must fetch for a walk to a merchant while a slot is still free (V250):
# the mage's bag trips in sessions 205-213 fetched 0 copper six times and 4-19 seven times,
# each about 55 s and 240 yards when it arrived, and 19 of 42 were cut off by a fight on
# the way. With no slot free the walk is made whatever they fetch.
SALE_WORTH_COPPER = 30
# A bag is bought on the bag service's walk while the belt holds fewer slots than this and the
# purse keeps this much over the bag's price and the trainer's due (V260): the level 9 mage's
# 16-slot backpack held eight slots of quest items it could neither sell nor use, and its bag
# walks cost 9.6 minutes an hour (sessions 222-228), with 725 copper in the purse and a 6-slot
# pouch at 500 at Eastvale's general goods.
BAG_ROOM_SLOTS = 28
BAG_SPARE_COPPER = 100
# Every spawn point of a quest's world object, twice round: taken crates respawn.
GATHER_LAPS = 2
# Walks in a row that ended with the character wedged before it goes home by hearthstone.
WEDGED_WALKS = 2
# A walk that failed and brought the character less than this much nearer counts as wedged
# too: somewhere it can move about but not leave. Only a walk meant to go twice as far: a
# short one's failure is never much headway, and two at a merchant's counter are no reason
# to be sent home.
NO_HEADWAY_YARDS = 10.0
# Where to eat: this far from every spawn point of the step's own creatures, found on rings
# round the character. A level 6 paladin eating in the middle of the wolf camp was bitten at
# 26% health and fought a 45 s stalemate of heals; another was at 30% when two more came
# (runs 20260924T053651-ac99b2, ...050644-f9f9fa). Past an aggro reach, a meal is a meal.
REST_CLEAR_YARDS = 25.0
REST_RINGS = (10.0, 20.0, 30.0, 45.0, 60.0)
REST_BEARINGS = 16
# A step back, about two yards, before a second look at a spawn point that showed nothing.
GATHER_STEP_BACK_S = 0.6
MERCHANT_UNREACHABLE = frozenset({"not_visible", "no_target", "no_window", "approach_failed"})
# After buying spells, the spellbook census is rebuilt under its new revision (about 2.5 s
# at ten paints a second) before anything is put on the bar from it.
CENSUS_S = 8.0
# How long to read every paint for a whole bar and spellbook census when one is missing.
CENSUS_LOOK_S = 4.0
# Skills that start with no time for putting spells on the bar: a fight, a death, a wait.
UNHURRIED_EXCEPT = frozenset({"COMBAT_PROFILE", "LOOT", "FACE_TARGET", "RELEASE_SPIRIT",
                              "CORPSE_RUN", "ABORT_WAIT", "IDLE", "TRAIN_CLASS"})
CLASS_IDS = {name: class_id for class_id, name in CLASS_BY_ID.items()}
RACE_IDS = {name: race_id for race_id, name in RACE_BY_ID.items()}


def _counted(value) -> tuple[int | None, int | None, bool | None]:
    """An objective's progress as a hunt counts it: (have, need, complete), a positive
    complete flag with no counter (1, 1)."""
    if value.complete is True and value.have is None:
        return 1, 1, True  # a positive complete flag, including objectives with no counter
    return value.have, value.need, value.complete


def _tour(points, start) -> list[tuple[float, float, float]]:
    """Every point, from the one nearest `start`, always on to the nearest one left."""
    left, tour = [tuple(p) for p in points], []
    here = tuple(start[:2])
    while left:
        point = min(left, key=lambda p: math.dist(here, p[:2]))
        left.remove(point)
        tour.append(point)
        here = point[:2]
    return tour



# After a meal a caster conjures when the bags hold fewer than this of what a conjure makes,
# this many casts at most, each waited out (V166). Conjure Water makes two a cast at rank 1.
CONJURE_BELOW = 4
CONJURE_CASTS = 3
CONJURE_CAST_WAIT_S = 5.0
# A census of the bags takes a look a slot; once stocked, the next is not before this.
CONJURE_EVERY_S = 90.0
# The spell ids unknown to the catalog the kit line names (V361); the evidence has them all.
KIT_IDS_SAID = 8
# A caster's hunt stands this far short of each station: inside Fireball's 35 yards and
# Frostbolt's 30, outside most mobs' notice (V167).
CASTER_STANDOFF_YARDS = 18.0
# A reading this recent stands for the state a service check needs (V338): the hunt's own,
# taken a moment before it asks.
RECENT_READ_S = 0.5
# Where a step that waits on a death camp is waited out with no rib to go to (V334): this far
# beyond the camp's reach, on the nearest of these bearings round it that lies in no camp.
CAMP_CLEAR_YARDS = 20.0
CAMP_CLEAR_BEARINGS = 16
# A dry rib's wider prey is at most this many levels above the character (V337), as a rib's
# creatures are (`graph.RIB_LEVELS_ABOVE`, V323).
WIDEN_LEVELS_ABOVE = 1

class LiveBody:
    # Purse saves, one at a time (V328); a body made without `__init__` shares this one.
    _purse_lock = threading.Lock()
    # Every entry has an executor. The verifier receives exactly this capability set.
    HANDLERS: ClassVar[dict[str, str]] = {
        "TRAVEL_TO": "_travel", "ACCEPT_QUEST": "_quest", "TURNIN_QUEST": "_quest",
        "GRIND_UNTIL": "_hunt", "COMBAT_PROFILE": "_fight",
        "LOOT": "_loot", "EAT_DRINK": "_rest", "VENDOR_REPAIR": "_repair",
        "BAG_MAKE_SPACE": "_vendor", "BUY_AMMO_REAGENT_FOOD": "_vendor",
        "RELEASE_SPIRIT": "_release", "CORPSE_RUN": "_recover",
        "IDLE": "_wait", "ABORT_WAIT": "_wait", "FACE_TARGET": "_face",
        "TRAIN_CLASS": "_train",
        "BIND_HEARTH": "_bind",
        "DISCOVER_FLIGHT": "_discover",
    }
    available = frozenset(HANDLERS)

    def __init__(self, client: Client, graph: Graph, *, travel_timeout: float = 180,
                 hunt_timeout: float = 600, say: Callable[[str], None] = print,
                 record_frame: Callable[..., dict] | None = None,
                 hunt_spawns: dict | None = None, gear_memory=None, merchant_memory=None,
                 home_memory=None, taxi_memory=None, purse_memory=None):
        if client.bounds is None or client.travel is None:
            raise ValueError("body needs the composed planner and follower")
        self._purse_lock = threading.Lock()
        self.client, self.graph = client, graph
        self.travel_timeout, self.hunt_timeout, self.say = travel_timeout, hunt_timeout, say
        # Where each hunt's target spawns (`jev.guide.spawns`); empty walks rings.
        self.hunt_spawns = hunt_spawns or {}
        # What each choice has paid off before, and this run's log of them
        # (`jev.learn.choices`); without a memory, tours keep their own order.
        self.choice_memory = None
        # Jev, when it coaches this character (`jev.coach.judge.Judge`): it picks in the
        # learned choices below, with each option's record in front of it.
        self.judge = None
        self.choice_log = None
        self.choice_rng = None               # the draws' source; `None` seeds itself
        # What this character has been given to wear, slot by slot (`jev.world.gear`).
        self.gear_memory = gear_memory
        # Which merchants could not be reached or clicked (`jev.world.vendor`).
        self.merchant_memory = merchant_memory
        # Where the hearthstone takes this character (`jev.world.home`).
        self.home_memory = home_memory
        # The flight nodes this character has visited (`jev.world.taxi`).
        self.taxi_memory = taxi_memory
        # What the purse could not pay for, kept between sessions (`Context.purse`, V206).
        self.purse_memory = purse_memory
        self._side: str | None = None
        self._flying = False
        self._gear_checked: object = object()     # the bags' revision last looked through
        self._conjure_checked: object = object()  # and for what the conjures make (V166)
        self._conjure_next = 0.0                  # when the next census may be taken
        self._conjured_last: frozenset[str] = frozenset()   # while the bar is unread (V243)
        self._placing_checked: object = object()  # the bar and spellbook last planned from
        self._kit_said: frozenset | None = None   # the spellbook last reported on (V361)
        self.travelling = False
        self.policy_context = Context()
        self.checkpoint: Callable[[], None] = lambda: None
        self.arm: Armed | None = None
        ox, oy = client.origin
        w, h = client.size
        # Reader checkpoints stop loops which aren't currently sending keys as well.
        self.targeting = Targeting(client.hid, self._read, read_frame=self._frame,
                                    window_origin=client.origin, record_frame=record_frame)
        self.interact = Interact(hid=client.hid, bounds=client.bounds, read=self._read,
                                 read_frame=self._frame, read_pos=self._position,
                                 window_centre=(ox + w // 2, oy + h // 2),
                                 window_origin=client.origin, approach=self._approach,
                                 targeting=self.targeting)
        self.advance = AdvanceQuestFrame(hid=client.hid, read=self._read,
                                        quest_ids=self._quest_ids,
                                        window_origin=client.origin, window_size=client.size)
        self.chooser = ChooseListLine(hid=client.hid, read=self._reading,
                                      window_origin=client.origin, window_size=client.size)
        self.fight = Fight(hid=client.hid, read=self._read, read_frame=self._frame,
                           window_origin=client.origin, window_centre_x=w // 2,
                           targeting=self.targeting, bounds=client.bounds)
        self.rest = Rest(hid=client.hid, read=self._read, use_item=self._use_consumable)
        self.loot = Loot(hid=client.hid, read=self._read, read_frame=self._frame,
                         window_origin=client.origin, targeting=self.targeting)
        self.gather = Gather(hid=client.hid, read=self._read, targeting=self.targeting,
                             window_origin=client.origin, window_size=client.size)
        self.repair = Repair(hid=client.hid, read=self._read, visit=self._visit_repairer,
                             window_origin=client.origin, window_size=client.size)
        self.recover = Recover(hid=client.hid, read=self._read, walk_to=self._corpse_walk,
                               interact=self._talk_to, choose=self.chooser.run,
                               window_origin=client.origin, window_size=client.size)
        self.hearth = Hearth(hid=client.hid, read=self._read,
                             window_origin=client.origin, window_size=client.size)
        self._revived_at: float | None = None
        # The body no corpse run got up at, as (map, world x, world y), and how many runs in a
        # row ended still a ghost there; and where the ghost last appeared, beside its Spirit
        # Healer, for a session that begins as a ghost. Both in the purse file (V301).
        self._unreached: tuple[tuple[int, float, float], int] | None = None
        self._graveyard: tuple[int, float, float] | None = None
        # Where the character fell, as (map, world x, world y), kept the moment a death is
        # read (`observe`), in the purse file; and where it last stood alive, kept there as a
        # session ends (V328). A ghost the game paints no body for walks to the first.
        self._fell: tuple[int, float, float] | None = None
        self._alive_at: tuple[int, float, float] | None = None
        self._hearth_ready_at: float | None = None      # wall time, in the purse file (V253)
        self._talents_failed: int | None = None           # the level a visit failed at (V261)
        self._reclaim_yards = TRAP_RECLAIM_YARDS    # how far short of the body a ghost gets up
        self._wedged = 0
        self._walks: dict[int, float] = {}          # the last ranking's planned walks (V309)
        self.camera = Camera(hid=client.hid, window_origin=client.origin, window_size=client.size)
        self.interact.level = self.fight.level = self.loot.level = self.camera.ensure_level
        self.fight.realign = self.camera.face
        client.travel.read_pos = self._position

    def has_focus(self) -> bool:
        """Observe ownership even between skills, when the supervisor is watching."""
        ready = self.client.hid.ready()
        if not ready:
            self.camera.invalidate("client lost focus")
        return ready

    def reconnect(self, checkpoint, *, env_file=None) -> Result:
        from jev.run.watchdog import reconnect_client

        self.camera.invalidate("session reconnect")
        return reconnect_client(self.client, checkpoint, env_file=env_file)

    def _read(self):
        self.checkpoint()
        return self.client.read()

    def _reading(self):
        self.checkpoint()
        return self.client.reading()

    def _frame(self):
        self.checkpoint()
        return self.client.frame()

    def _position(self):
        self.checkpoint()
        return self.client.position()

    def _quest_ids(self):
        self.checkpoint()
        return self.client.quest_ids(tries=1)

    def execute(self, arm: Armed, state: State, checkpoint: Callable[[], None]) -> Result:
        self.arm = arm
        faction = getattr(state.char, "faction", None) if state is not None else None
        self._side = getattr(faction, "value", faction) or self._side
        def focused_checkpoint():
            checkpoint()
            if not self.has_focus():
                raise FocusLost("client lost focus; release before refocusing")
        self.checkpoint = focused_checkpoint
        self.client.hid.checkpoint = focused_checkpoint
        handler = self.HANDLERS.get(arm.decision.skill)
        if handler is None:
            return Result(SkillOutcome.ABORTED, f"no executor for {arm.decision.skill}", "unsupported")
        error = self._parameters()
        if error:
            return Result(SkillOutcome.ABORTED, error, "unsupported")
        if (not self.has_focus()
                and not self.client.focused(FOCUS_QUICK_S, checkpoint=checkpoint)):
            return Result(SkillOutcome.ABORTED, "client refused focus after backoff", "refused")
        self.checkpoint()
        self.ready_camera(state)
        # Every fight a skill starts - a hunt's, a tutor's delegated one - uses what the bar
        # holds now; and a skill with time to spare first puts on the bar what is missing.
        if arm.decision.skill not in UNHURRIED_EXCEPT:
            self._place_spells()
        self._bar_profile()
        return getattr(self, handler)(state)

    def ready_camera(self, state: State | None) -> None:
        """Level the camera while nothing is hitting the character.

        Every look needs it, and it is five seconds of mouse-look. Left to the first look,
        it landed in run 20260923T173347-590b06's first fight - a panic fight at 12% health,
        which was over before the drag was. A skill whose look finds it done skips it; one
        that runs in combat with it undone still levels first, as before.
        """
        if state is not None and state.vitals.combat is False and not self.camera.calibrated:
            self.camera.ensure_level()

    def _parameters(self) -> str | None:
        """Execute only requests this composition can honour, without silently retargeting."""
        return self.validate(self.arm.decision, self.arm.step_id)

    def validate(self, decision, step_id: str | None) -> str | None:
        node = self.graph.get(step_id or "")
        if decision.intent in (Intent.SKIP, Intent.ESCALATE):
            return f"body cannot execute intent {decision.intent}"
        kinds = {"ACCEPT_QUEST": {StepKind.QUEST_ACCEPT},
                 "TURNIN_QUEST": {StepKind.QUEST_TURNIN},
                 "GRIND_UNTIL": {StepKind.GRIND, StepKind.QUEST_OBJECTIVE, StepKind.DING_GATE}}
        if decision.skill in kinds and (node is None or node.kind not in kinds[decision.skill]):
            return f"{decision.skill} does not match the armed guide step"
        expected = {"zone": node.zone, "step_id": node.id} if node else {}
        if node and node.coord_zone_id is not None:
            expected["coord_zone_id"] = node.coord_zone_id
            if self.client.bounds.area_id != node.coord_zone_id:
                return "body and guide use different map coordinate frames"
        if decision.skill == "TRAVEL_TO" and node and node.pos:
            expected.update(x=node.pos[0], y=node.pos[1], r=node.r)
        if decision.skill == "VENDOR_REPAIR":
            expected["service"] = "repair"
        if decision.skill == "BAG_MAKE_SPACE":
            expected["service"] = "bags"
        if decision.skill == "BUY_AMMO_REAGENT_FOOD":
            expected["service"] = "supplies"
        if decision.skill == "TRAIN_CLASS":
            expected["service"] = "train"
        if decision.skill == "BIND_HEARTH":
            expected["service"] = "bind"
        if decision.skill == "DISCOVER_FLIGHT":
            expected["service"] = "discover"
        for key, value in decision.params.items():
            if key == "profile" and decision.skill == "COMBAT_PROFILE" and value in ("default", "panic"):
                continue  # Fight's measured health branch owns panic within the rotation.
            if (key == "until_level" and decision.skill == "GRIND_UNTIL" and node
                    and node.kind in (StepKind.GRIND, StepKind.DING_GATE)
                    and type(value) is int and value > 0):
                continue
            if key not in expected or value != expected[key]:
                return f"unsupported {decision.skill} parameter: {key}={value!r}"
        return None

    def release(self) -> None:
        self.client.hid.release_all()
        if self.client.hid.keys_down() or self.client.hid.held_buttons:
            raise RuntimeError("input release was refused; held inputs remain")
        self.client.hid.checkpoint = None

    @staticmethod
    def _result(outcome, detail="") -> Result:
        ok = outcome.opened if isinstance(outcome, Interacted) else outcome.ok
        status = (SkillOutcome.PREEMPTED if outcome.value in
                  ("bags_full", "service_needed", "died", "blind", "interrupted") else
                  SkillOutcome.SUCCEEDED if ok else
                  SkillOutcome.TIMED_OUT if outcome.value == "timeout" else SkillOutcome.ABORTED)
        return Result(status, detail, outcome.value)

    def _node(self):
        return self.graph.get(self.arm.step_id) if self.arm else None

    def _approach(self, world, stop_short: float = 0.0) -> bool:
        self.checkpoint()
        v = self._read()
        if v is None:
            raise Cancelled("cannot observe travel readiness")
        ghost = v.get("vitals.ghost") is True
        if not ghost:
            if v.get("vitals.dead") is True:
                raise Cancelled("dead before travel")
            # Not while combat is paused after fights that never engaged: this walk is the
            # way out of reach (V212). In session 176 the pause was met here sixteen times.
            leaving = (v.get("vitals.combat") is True
                       and self.policy_context.fight_paused(time.time()))
            if v.get("vitals.combat") is True and not leaving:
                raise Cancelled("combat before travel")
            hp = v.get("vitals.hp")
            if hp is None:
                raise Cancelled("health unread before travel")
            # Nor does that walk wait for a meal no fight allows (V218): session 188 asked
            # for one six times running, "in combat; not a moment to eat", standing still.
            if hp < HEAL_OUT_OF_COMBAT and not leaving and not self.fight.top_up():
                rested = self.rest.until(0.9)
                if not rested.ok:
                    raise BodyFailure(self._result(rested, f"not fit to travel: {self.rest.detail}"))
        if not ghost and not self._flying:
            self._fly_toward(world)
        self.travelling = True
        try:
            arrived = self._walk(world, stop_short)
            # Wedged indoors: out the way it came in, then the plan again from there (V230).
            back_out = getattr(self.client, "back_out", None)
            if (not arrived and back_out is not None
                    and (self._read() or {}).get("pos.indoors") is True and back_out()):
                self.say("  backed out: outdoors, walking on")
                arrived = self._walk(world, stop_short)
        finally:
            self.travelling = False
        self._note_wedged(arrived)
        return arrived

    def _walk(self, world, stop_short: float = 0.0) -> bool:
        return (self.client.approach(world, timeout_s=self.travel_timeout, stop_short=stop_short)
                if stop_short else self.client.approach(world, timeout_s=self.travel_timeout))

    def _fly_toward(self, world) -> bool:
        """Fly the long part of a walk, when a remembered node lands near its end and a
        flight master stands near its start (`jev.world.taxi.flight`). Anything short of
        landing leaves the character to walk from wherever it is."""
        here = self._position()
        if here is None:
            return False
        at = map_to_world(*here, self.client.bounds)
        plan = flight_plan(at, world[:2], load_nodes(self.taxi_memory),
                           flightmasters(self.client.bounds.map_id, self._side))
        if plan is None:
            return False
        master, node = plan
        self._flying = True
        try:
            if not self._approach(master.world) or not self._open_flightmaster(master):
                return False
            desk = TaxiDesk(self.client.hid, self._read, self.client.origin, self.client.size)
            try:
                seconds = FLIGHT_BASE_S + math.dist(master.world[:2], node.world[:2]) / FLIGHT_YARDS_PER_S
                flew = desk.fly(node.name_id, flight_s=seconds)
                if desk.here is not None:
                    save_node(self.taxi_memory, TaxiNode(desk.here, master.name, master.world))
            finally:
                desk.close()
            self.say(f"  flight from {master.name} to {node.flightmaster}: {flew.value}"
                     + (f" ({desk.detail})" if desk.detail else ""))
            return flew.ok
        finally:
            self._flying = False

    def _open_flightmaster(self, master) -> bool:
        """Talk to a flight master until its map is open: by its gossip line, or directly."""
        point = world_to_map(*master.world[:2], self.client.bounds)
        opened = self.interact.open_on(master.name, node_world=master.world, node_map=point)
        if opened is Interacted.TAXI:
            return True
        if opened is not Interacted.GOSSIP or not master.gossip or not self.chooser.run(master.gossip).ok:
            values = self._read()
            if values and values.get("ui.modal") is not True:
                close_observed(self.client.hid, self._read, values=values)
            return False
        deadline = time.monotonic() + POPUP_S
        while time.monotonic() < deadline:
            values = self._read()
            if values and values.get("ui.taxi") is True:
                return True
            time.sleep(0.1)
        return False

    def _undiscovered_master(self):
        here = self._position()
        if here is None:
            return None
        at = map_to_world(*here, self.client.bounds)
        known = load_nodes(self.taxi_memory)
        near = [(math.dist(m.world[:2], at), m)
                for m in flightmasters(self.client.bounds.map_id, self._side)
                if not visited(m.world, known)]
        near = [(d, m) for d, m in near if d <= DISCOVER_YARDS]
        return min(near, key=lambda pair: pair[0])[1] if near else None

    def discoverable(self, state: State) -> bool:
        """A flight master near the character whose node it has not visited."""
        try:
            faction = getattr(state.char, "faction", None)
            self._side = getattr(faction, "value", faction) or self._side
            return self._undiscovered_master() is not None
        except Exception:
            return False

    def _discover(self, state) -> Result:
        """Talk to the flight master: its map names the node here, which is remembered."""
        master = self._undiscovered_master()
        if master is None:
            return Result(SkillOutcome.ABORTED, "no unvisited flight master near", "nothing")
        if not self._open_flightmaster(master):
            return Result(SkillOutcome.ABORTED, f"{master.name}: the map did not open", "no_map")
        desk = TaxiDesk(self.client.hid, self._read, self.client.origin, self.client.size)
        try:
            desk.read_map()
        finally:
            desk.close()
        if desk.here is None:
            return Result(SkillOutcome.ABORTED, f"{master.name}: the map named no node here",
                          "no_node")
        save_node(self.taxi_memory, TaxiNode(desk.here, master.name, master.world))
        return Result(SkillOutcome.SUCCEEDED, f"{master.name}'s node remembered", "done")

    def _note_wedged(self, arrived: bool) -> None:
        """Home by hearthstone after `WEDGED_WALKS` walks in a row found the character
        wedged: every unstick heading tried and none moved it. Inside Northshire Abbey,
        against a barrel below Brother Neals' stairs, walk after walk ended "could not free
        the character" (run 20260924T074713-f215ef). A stone on cooldown does nothing.

        Or walks that failed without getting anywhere (`NO_HEADWAY_YARDS`): upstairs in the
        Lion's Pride Inn, walked on plans for the hall below, the character moved about the
        landing for two sessions and was never once wedged by the unstick's measure
        (sessions 110 and 111). Not a walk the caller or the desk cut short."""
        last = getattr(self.client, "last_travel", None)
        headway = getattr(self.client, "last_headway", None)
        distance = getattr(self.client, "last_distance", None)
        outcome = getattr(getattr(last, "outcome", None), "value", None)
        wedged = (not arrived and last is not None
                  and ("could not free the character" in (getattr(last, "detail", "") or "")
                       or (outcome not in ("aborted", "refused", "lost")
                           and isinstance(headway, float) and headway < NO_HEADWAY_YARDS
                           and isinstance(distance, float)
                           and distance >= 2 * NO_HEADWAY_YARDS)))
        self._wedged = self._wedged + 1 if wedged else 0
        if self._wedged >= WEDGED_WALKS:
            self._wedged = 0
            home = self._go_home()
            self.say(f"  wedged {WEDGED_WALKS} walks running: hearthstone {home.value} "
                     f"{self.hearth.detail}".rstrip())

    def _travel(self, state) -> Result:
        node = self._node()
        if node is None or node.world is None or node.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "step has no supported map destination", "unsupported")
        ok = self._approach(node.world)
        step = self.arm.step_id if self.arm is not None else None
        level = (self._read() or {}).get("char.level") if not ok else None
        note = self._walk_note(level if isinstance(level, int) else None) if not ok else None
        if note is not None and note[1] is not None:
            # Refused through a death camp: the step waits for the camp to end (V334).
            self.policy_context.step_waits(step, note[1], "its walk is refused through a "
                                           "death camp", time.time())
        elif note is not None and note[0] is False:
            # No route planned, not a yard walked: the step waits before it is armed again
            # (V335). Bot 480's walks from Dolanaar, TRAVEL_TO among them, were refused for 20
            # minutes on end (29 Sep 04:31-04:56).
            self._stuck(step, "no route to the step could be planned")
        elif ok or (note is not None and note[0]):
            self.policy_context.step_moved(step)
        return Result(SkillOutcome.SUCCEEDED if ok else SkillOutcome.ABORTED,
                      "", "arrived" if ok else "unreachable")

    def _quest(self, state) -> Result:
        node = self._node()
        if node is None or node.quest_id is None or node.npc_id is None or node.world is None:
            return Result(SkillOutcome.ABORTED, "quest has no placed NPC", "unsupported")
        if node.target_kind not in ("creature", "gameobject") or not node.target_name:
            return Result(SkillOutcome.ABORTED, "quest target needs a supported identity", "unsupported")
        if node.target_kind == "gameobject":
            # A wanted poster or a body: stood at, found by the name its tooltip gives.
            self._approach(node.world)
            if not self.gather.open(name_id(node.target_name)):
                return Result(SkillOutcome.ABORTED, f"{node.target_name}: {self.gather.detail}",
                              "not_visible")
            reading = self._reading()
            values = reading.values if reading is not None and reading.values else {}
            opened = Interacted.QUEST if values.get("ui.quest_frame") is True else Interacted.GOSSIP
        else:
            opened = self._open_on(node.target_name, node.world, node.pos)
        if not opened.opened:
            return self._result(opened, self.interact.detail)
        reading = self._reading()
        is_list = (reading is not None and reading.values
                   and reading.values.get("ui.advance_x") is None and list_lines(reading))
        if opened is Interacted.GOSSIP or is_list:
            chose = self.chooser.run(node.title)
            if not chose.ok:
                return self._result(chose, self.chooser.detail)
        with self.client._capturing:
            self.client.log.reset()
        goal = Goal.HELD if node.kind is StepKind.QUEST_ACCEPT else Goal.CLEARED
        outcome = self.advance.run(node.quest_id, goal)
        return self._result(outcome, self.advance.detail)

    def _quest_progress(self):
        node = self._node()
        self._quest_ids()
        with self.client._capturing:
            log = self.client.log.complete
        value = progress(log, node.quest_id if node else None)
        if (node and not node.objective_targets and value.first_incomplete is not None
                and value.first_incomplete > 0):
            raise Unsupported("next objective needs its own generated target; this graph places only the first")
        return value

    def _progress(self):
        return _counted(self._quest_progress())[:2]

    def _log_progress(self):
        """`_quest_progress` from the log as the last reading left it, without reading again
        (V338)."""
        node = self._node()
        with self.client._capturing:
            log = self.client.log.complete
        value = progress(log, node.quest_id if node else None)
        if (node and not node.objective_targets and value.first_incomplete is not None
                and value.first_incomplete > 0):
            raise Unsupported("next objective needs its own generated target; this graph places only the first")
        return value

    def _hunt(self, state) -> Result:
        node = self._node()
        if (node is None or node.world is None
                or node.kind not in (StepKind.QUEST_OBJECTIVE, StepKind.GRIND, StepKind.DING_GATE)
                or node.map_id != self.client.bounds.map_id):
            return Result(SkillOutcome.ABORTED, "no supported objective destination", "unsupported")
        destination = node
        progress_reader = self._progress
        def complete_reader():
            return self._quest_progress().complete
        # The counter in the log the hunt's own reading fed, read no more (V338).
        def observe(values):
            return _counted(self._log_progress())
        if node.kind is StepKind.QUEST_OBJECTIVE and node.objective_targets:
            self._quest_ids()
            with self.client._capturing:
                log = self.client.log.complete
            selection = select_objective(node, log)
            if selection.complete is True:
                return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
            if selection.target is None:
                # A quest not in the log is the route's to resolve (its accept was passed
                # over, or never reached), not a configuration fault that stops the run:
                # session 67 stopped on quest 16's objective with its accept passed over.
                # Nor is an objective of a quest in the log that cannot be worked: a delivery
                # whose item is gone, or counters full whose completion never shows. Each is a
                # failed attempt, failed over at the step's attempts (V385): stopped, the next
                # session met the same step and stopped again, every fifteen minutes for good
                # (Scalding Mornbrew cools in five minutes; nine dwarves and gnomes stood on its
                # delivery for days, 6 Oct).
                code = ("blind" if log is None else
                        "quest_absent" if selection.reason == QUEST_ABSENT else "unworkable")
                return Result(SkillOutcome.PREEMPTED if log is None else SkillOutcome.ABORTED,
                              selection.reason or "objective unavailable", code)
            destination = selection.target
            def selected_progress():
                self._quest_ids()
                with self.client._capturing:
                    return target_progress(self.client.log.complete, node.quest_id, destination)
            def progress_reader():
                value = selected_progress()
                return (1, 1) if value.complete is True and value.have is None else (value.have, value.need)
            def complete_reader():
                return selected_progress().complete
            def observe(values):
                with self.client._capturing:
                    return _counted(target_progress(self.client.log.complete, node.quest_id,
                                                    destination))
        if isinstance(destination, ObjectiveTarget) and destination.kind == "explore":
            return self._explore(destination, complete_reader)
        if (isinstance(destination, ObjectiveTarget) and destination.kind == "loot"
                and destination.target_kind == "gameobject"):
            return self._gather(node, destination, progress_reader, complete_reader)
        if (destination.target_kind != "creature" or not destination.target_name
                or destination.world is None or destination.map_id != self.client.bounds.map_id):
            return Result(SkillOutcome.ABORTED, "objective needs a supported creature target; objects need their own locator", "unsupported")
        if node.kind in (StepKind.GRIND, StepKind.DING_GATE):
            target = self.arm.decision.params.get("until_level")
            if not isinstance(target, int):
                return Result(SkillOutcome.ABORTED, "grind arm has no level predicate", "unsupported")
            def progress_reader():
                values = self._read()
                return (values.get("char.level") if values else None), target
            def complete_reader():
                level, needed = progress_reader()
                return None if level is None else level >= needed
            def observe(values):
                level = (values or {}).get("char.level")
                return level, target, None if level is None else level >= target
        wanted = name_id(destination.target_name)
        values = self._read() or {}
        profile = for_class(values.get("char.class_id"), values.get("char.race_id"))
        # A shooter stands off as a caster does (V358): Auto Shot reaches 35 yards, and a
        # hunter walked into melee pressed it 0.06 times a kill.
        caster = profile.caster or profile.shooter
        level = values.get("char.level") if isinstance(values.get("char.level"), int) else None
        hunt = Hunt(fight=self.fight, rest=self.rest, read=self._read,
                    approach=self._approach, progress=progress_reader, loot=self.loot, say=self.say,
                    is_complete=complete_reader, service_needed=self._service_needed,
                    stations=self._stations("hunt.station", objective_key(wanted, node.id)),
                    standoff_yards=CASTER_STANDOFF_YARDS if caster else 0.0,
                    conjure=self._conjure,
                    camp_until=lambda station: self._camp_end(station, level),
                    walk_note=lambda: self._walk_note(level), observe=observe,
                    place=self._hunt_place((self.arm.step_id if self.arm is not None else None,
                                            objective_key(wanted, node.id))),
                    where=self._world_position)
        yards = destination.hunt_yards or DEFAULT_HUNT_YARDS
        grind = node.kind in (StepKind.GRIND, StepKind.DING_GATE)
        # Every held quest's creatures round the hunt are its quarry too (V363).
        held = self._held(destination.world, yards, wanted, level, grind)

        def quarry(own):
            return Quarry(own, held.names, held.high) if held else own
        if node.kind is StepKind.GRIND:
            # A rib found dry fights any kind round it worth fighting (V337).
            def wider():
                kinds = self._rib_kinds(node, destination.world, yards, wanted, level)
                return Paying(quarry(kinds)) if kinds is not None else None
            hunt.widen = wider
        spawns = spawn_points(self.hunt_spawns, node.id, getattr(destination, "target_id", None))
        # A grind's pulls are for experience: none of a level grey to the character (V344).
        pull = Paying(quarry(wanted)) if grind else quarry(wanted)
        reach = yards + (NEAR_YARDS if held else 0.0) + PACK_YARDS
        # The other quests' stations, nearest the hunt's centre first: after the step's own
        # objective, before a grind's own creature (V363).
        also = sorted(held.points, key=lambda p: math.dist(p[:2], destination.world[:2]))
        outcome = hunt.run(destination.world, yards, pull, timeout_s=self.hunt_timeout,
                           spawns=spawns,
                           others=self._hostiles(destination.world, reach)
                           if spawns or also else (),
                           also=tuple(also), also_first=grind)
        step = self.arm.step_id if self.arm is not None else None
        if outcome is Hunted.CAMP and getattr(hunt, "until", None) is not None:
            # Not armed again until the camp ends (V334): bot 224's grind was armed again 0.5 s
            # after each hunt whose every walk the camp refused. Waited out on another rib
            # (`ClientRuntime._wait_elsewhere`), or, with none, out of the camp's reach.
            self.policy_context.step_waits(step, hunt.until, "its stations lie in or behind "
                                           "a death camp", time.time())
            self._out_of_camp(level)
        elif outcome is Hunted.UNREACHABLE and getattr(hunt, "stuck", False):
            self._stuck(step, "no route to any station of its hunt could be planned")
        elif getattr(hunt, "arrived", 0):
            self.policy_context.step_moved(step)
        return self._result(outcome, hunt.detail)

    def _held(self, world, yards: float, own: int | None, level: int | None,
              grind: bool) -> Held:
        """What a hunt of `yards` round `world` fights for the quests in the log besides its own
        creature `own` (`jev.world.quarry.held`, V363), said once a hunt; nothing with the log
        unread or the world snapshot unreadable."""
        bounds = self.client.bounds
        if world is None or bounds is None:
            return Held()
        with self.client._capturing:
            log = self.client.log.complete
        found = quarry_held(log, bounds.map_id, world, yards, level=level, grind=grind, own=own)
        if found:
            event("hunt.quests", data={"quests": list(found.quests), "names": sorted(found.names),
                                       "stations": len(found.points), "high": found.high})
            self.say(f"  hunting for {len(found.quests)} more held quest(s) too: "
                     f"{len(found.names)} kind(s), {len(found.points)} spawn(s) "
                     f"(quests {', '.join(str(q) for q in found.quests)})")
        return found

    def _hunt_place(self, key) -> Place:
        """Where the last hunt of this step and objective got to (`Place`, V343), kept while
        the same one is armed again; another step's hunt, or the first after a death, begins
        with a new one."""
        kept = getattr(self, "_place", None)
        if kept is None or kept[0] != key:
            kept = self._place = (key, Place())
        return kept[1]

    def _world_position(self) -> tuple[float, float] | None:
        """Where the character stands, in world yards on the guide's map; `None` unread."""
        here, bounds = self._position(), self.client.bounds
        if here is None or bounds is None or None in here:
            return None
        return map_to_world(*here, bounds)

    def rib_camped(self, node, level: int | None) -> bool:
        """Does every station a hunt of the rib `node` would stand at lie in a death camp
        counting at `level` (`Hunt._out_of_camps`, V334)? No, with none known."""
        if node is None or node.world is None or self.client.bounds is None \
                or node.map_id != self.client.bounds.map_id:
            return False
        spawns = spawn_points(self.hunt_spawns, node.id, None)
        tour = (spawn_tour(spawns) if spawns
                else hunt_stations(node.world, node.hunt_yards or DEFAULT_HUNT_YARDS))
        return bool(tour) and all(self._camp_end(p, level) is not None for p in tour)

    def _out_of_camp(self, level: int | None) -> bool:
        """Walk clear of the death camp the character stands in, if it stands in one: to the
        nearest point `CAMP_CLEAR_YARDS` beyond its reach that lies in no camp, where a step
        that waits on it is waited out with nothing else to do (V334). `True` when it walked
        there."""
        memory, bounds = getattr(self.client, "route_memory", None), self.client.bounds
        here = self._position()
        if memory is None or bounds is None or here is None:
            return False
        at = map_to_world(*here, bounds)
        camp = memory.camp_at(bounds.map_id, at[:2], time.time(), level) if at else None
        if camp is None:
            return False
        reach = CAMP_YARDS + CAMP_CLEAR_YARDS
        ring = [(camp.x + reach * math.cos(a), camp.y + reach * math.sin(a))
                for a in (2 * math.pi * k / CAMP_CLEAR_BEARINGS for k in range(CAMP_CLEAR_BEARINGS))]
        clear = [p for p in ring if self._camp_end(p, level) is None]
        if not clear:
            return False
        spot = min(clear, key=lambda p: math.dist(p, at[:2]))
        self.say(f"  out of the death camp's reach to wait, {math.dist(spot, at[:2]):.0f} yards")
        return self._approach((spot[0], spot[1], self._height_at(at)))

    def _height_at(self, at) -> float:
        """The height tracked where the character stands, else 0 (the planner looks 200 yards
        up and down for a floor, `jevpath_lazy`)."""
        ground = getattr(self.client, "_ground", None)
        if ground is not None and math.dist(ground[:2], at[:2]) <= 15.0:
            return ground[2]
        return 0.0

    def _stuck(self, step: str | None, why: str) -> None:
        """A try at the step planned no route: it waits before it is armed again (V335)."""
        wait = self.policy_context.step_stuck(step, why, time.time())
        if wait:
            self.say(f"  {why}: the step waits {wait:.0f}s")

    def _rib_kinds(self, node, world, yards: float, own: int | None,
                   level: int | None) -> Kinds | None:
        """What a dry grind rib fights (`Hunt.widen`, V337): its own kind, and any kind of
        normal rank spawned within its disk and a pack's reach that attacks this character on
        sight and whose levels lie within the rib's (`jev.world.hostiles.kinds`), the unit
        taken at a level none grey to the character and at most `WIDEN_LEVELS_ABOVE` above
        it. `None` with the level, the side or the rib's levels unknown, or nothing more."""
        band = getattr(node, "level", None)
        if not isinstance(level, int) or not band or world is None or self.client.bounds is None:
            return None
        low, high = max(band[0], grey_level(level) + 1), min(band[1], level + WIDEN_LEVELS_ABOVE)
        names = hostiles.kinds(self.client.bounds.map_id, world[0], world[1], yards + PACK_YARDS,
                               side=self._side, low=band[0], high=band[1]) - {own}
        if low > high or not names:
            return None
        return Kinds(own=own, names=names, low=low, high=high)

    def _camp_end(self, world, level: int | None) -> float | None:
        """When the death camp the world point `world` lies in ends, as wall time: its nearest
        camp death counting at `level` (`RouteMemory.camp_at`, V307); `None` for none, or with
        no route memory (V334)."""
        memory, bounds = getattr(self.client, "route_memory", None), self.client.bounds
        if memory is None or bounds is None or world is None:
            return None
        camp = memory.camp_at(bounds.map_id, tuple(world[:2]), time.time(), level)
        return camp.camp_until if camp is not None else None

    def _walk_note(self, level: int | None) -> tuple[bool, float | None] | None:
        """How the last walk that did not arrive went (`Hunt.walk_note`, V334): whether its
        route was planned, and when the death camp it was refused through ends; `None` from
        a client that does not say."""
        if not hasattr(self.client, "last_plan"):
            return None
        plan = self.client.last_plan
        if plan is None:
            return False, None
        if plan.status is PathStatus.NOPATH and plan.detail == CAMP_REFUSED:
            camp = getattr(plan, "camp", None)
            end = self._camp_end(camp, level) if camp is not None else None
            return False, end
        return plan.usable, None

    def _explore(self, target: ObjectiveTarget, complete: Callable[[], bool | None]) -> Result:
        """Walk to an exploration trigger's point and wait for the quest's own credit.

        The point is the trigger's centre, and the two on this route are spheres of ten and
        thirty yards, so arriving within travel's few yards is inside. Only the quest's
        positive complete flag is success: arriving is not exploring.
        """
        if target.world is None or target.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "exploration point is not on this map", "unsupported")
        arrived = self._approach(target.world)
        deadline = time.monotonic() + EXPLORE_CREDIT_S
        while complete() is not True:
            if time.monotonic() >= deadline:
                if not arrived:
                    return Result(SkillOutcome.ABORTED, "exploration point unreachable", "unreachable")
                return Result(SkillOutcome.ABORTED, "at the exploration point with no credit", "nothing")
            time.sleep(EXPLORE_POLL_S)
        return Result(SkillOutcome.SUCCEEDED, "exploration credited", "done")

    def _gather(self, node, target: ObjectiveTarget, progress, complete) -> Result:
        """Walk a quest object's spawn points, nearest first, taking it wherever it stands."""
        points = spawn_points(self.hunt_spawns, node.id, target.target_id)
        if not points and target.world is not None:
            points = (tuple(target.world),)
        if not points or not target.target_name or target.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "object has no placed spawn points", "unsupported")
        wanted = name_id(target.target_name)
        deadline = time.monotonic() + self.hunt_timeout
        here = self._position()
        start = map_to_world(*here, self.client.bounds) if here is not None else points[0][:2]
        tour = _tour(points, start)
        chooser = (self._stations("gather.station", f"object:{target.target_id}")
                   if len({tuple(p) for p in tour}) > 1 else None)
        laps = [chooser.order(tour) if chooser is not None else tour for _ in range(GATHER_LAPS)]
        for point in (p for lap in laps for p in lap):
            if complete() is True:
                return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
            if time.monotonic() > deadline:
                return Result(SkillOutcome.TIMED_OUT,
                              f"{self.hunt_timeout:.0f}s and the objective is not done", "timeout")
            if chooser is not None:
                chooser.arrive(point)
            self._approach(point)
            got = self.gather.pick(wanted, progress)
            if got is Gathered.NOT_HERE and self.client.hid.hold("s", GATHER_STEP_BACK_S):
                # Stood on the spawn point, the character itself hides what lies underfoot.
                got = self.gather.pick(wanted, progress)
            if chooser is not None:
                chooser.leave(got is Gathered.TOOK)
            self.say(f"    gather: {got.value} - {self.gather.detail}")
            if not got.ok:
                return self._result(got, self.gather.detail)
        if complete() is True:
            return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
        return Result(SkillOutcome.ABORTED, "every spawn point walked and the objective is short",
                      "nothing")

    def learn(self, memory, log=None) -> None:
        """Choices learned from their outcomes (`jev.learn.choices`, DECISIONS V158): where
        hunts and gathers stand next, and the heal line each fight holds."""
        self.choice_memory, self.choice_log = memory, log
        # Jev sets a fight's heal line as it begins; the line for a pack, picked in the fight,
        # is not a choice to wait on. A line's try is a fight's cycle (V311).
        self.fight.choices = Choice(memory, HEAL_POINT, log=log, rng=self.choice_rng,
                                    judge=self.judge, judged=frozenset({"all"}),
                                    unit="a cycle")

    def settle_learning(self) -> None:
        """The session ends: what is learned only when the next thing starts is closed now
        (the heal line's open cycle, V311)."""
        self.fight.settle()

    def here_world(self) -> tuple[float, float] | None:
        """Where the character stands, in world yards: how far Jev is told each station is
        (`jev.coach.judge.Judge`)."""
        state = self.client.state()
        if state is None or state.pos.mx is None or state.pos.my is None:
            return None
        return tuple(map_to_world(state.pos.mx, state.pos.my, self.client.bounds)[:2])

    def _stations(self, point: str, objective: str):
        """A chooser of stations for `point`, learning under `objective`; `None` without a
        memory (`jev.learn.choices.Stations`)."""
        if self.choice_memory is None:
            return None
        return Stations(self.choice_memory, point, objective, log=self.choice_log,
                        rng=self.choice_rng, judge=self.judge)

    def _service_needed(self) -> str | None:
        self.checkpoint()
        # The state of the reading the hunt has just taken, not another (V338): a state
        # built afresh at each look was a capture more between a fight and the next pull.
        recent = getattr(self.client, "recent_state", None)
        state = recent(RECENT_READ_S) if callable(recent) else None
        if state is None:
            state = self.client.state()
        if state is None:
            return None
        # On the armed step, as the policy sees it: a merchant or repairer found out of
        # reach is blocked for the step (V175, V185), and the client's own state names no
        # step. Session 156's repair walk timed out, and the grind stopped for "durability is
        # low" seventeen times, armed again each time by a policy that knew the repair was
        # out of reach (V194).
        if self.arm is not None and self.arm.step_id:
            state = state.model_copy(update={"guide": state.guide.model_copy(
                update={"step_id": self.arm.step_id})})
        plan = service(state, context=self.policy_context)
        return plan.decision.why if plan else None

    def _objective_name(self) -> int | Paying | None:
        """The creature the armed guide step wants, when it names one, as a pull for experience
        on a grind (`Paying`); else None."""
        node = self._node()
        if node is None:
            return None
        target = node
        if node.objective_targets:
            self._quest_ids()
            with self.client._capturing:
                log = self.client.log.complete
            selection = select_objective(node, log)
            target = selection.target
        if target is None or target.target_kind != "creature" or not target.target_name:
            return None
        wanted = name_id(target.target_name)
        # A grind's are for experience (V344); a quest's count at any level.
        return Paying(wanted) if node.kind in (StepKind.GRIND, StepKind.DING_GATE) else wanted

    def _fight(self, state) -> Result:
        # Inside an objective the fight is for its creature, not the nearest plate: the
        # first live run's nearest plate was a rabbit. Self-defence still takes whatever
        # is attacking us (Fight loosens the name in combat, never drops it).
        outcome = self.fight.run(self._objective_name())
        if outcome.ok:
            looted = self.loot.run(progress=self._progress, anchor=self.fight.last_plate,
                                   name_id=self.fight.killed_name_id,
                                   far=getattr(self.fight, "ended_far", False))
            # A corpse not found costs its loot, not the kill: the selection can move on at
            # the kill and leave nothing to hover (run 20260924T033806-a3254d, three times).
            if not looted.ok and looted is not Looted.NO_CORPSE:
                return self._result(looted, f"post-kill loot: {self.loot.detail}")
            # Said, so whoever delegated the fight knows the corpse is done with: the tutor
            # went on clicking corpses this had already emptied.
            return self._result(outcome, f"{self.fight.detail}; corpse looted: "
                                         f"{looted.value} - {self.loot.detail}")
        return self._result(outcome, self.fight.detail)

    def _face(self, state) -> Result:
        """Turn toward the selected living unit: the shared facing primitive, nothing more."""
        result = self.targeting.face_selected()
        status = (SkillOutcome.SUCCEEDED if result.faced else
                  SkillOutcome.PREEMPTED if result.code in (FaceCode.BLIND, FaceCode.INTERRUPTED)
                  else SkillOutcome.ABORTED)
        return Result(status, result.detail, result.code.value)

    def _loot(self, state) -> Result:
        # Where and what the most recent fight killed, when it killed something.
        killed = self.fight.killed_name_id
        anchor = self.fight.last_plate if killed is not None else None
        return self._result(self.loot.run(progress=self._progress, anchor=anchor,
                                          name_id=killed), self.loot.detail)

    def _rest(self, state) -> Result:
        self._clear_of_spawns()
        self._wear_upgrades()
        self._place_spells()
        self._spend_talents()
        self._bar_profile()
        # The policy's line (`jev.coach.policy._recover`): below it, drink; a caster sooner
        # and fuller (V164), or its rest would stop short and be armed again at once.
        caster = is_caster(state.char.cls)
        line = (self.measured_mana_line() if caster else None) or rest_mana(caster)
        if (state.vitals.power_type is PowerType.MANA and state.vitals.power is not None
                and state.vitals.power < line):
            hurt = state.vitals.hp is not None and state.vitals.hp < HEAL_OUT_OF_COMBAT
            rested = self._result(self.rest.until_both(0.9, drink_to(caster)) if caster and hurt
                                  else self.rest.until(drink_to(caster), role=Role.DRINK),
                                  self.rest.detail)
            self._conjure()
            self.fight.buff_up()
            return rested
        if self.fight.top_up():
            return Result(SkillOutcome.SUCCEEDED, "health topped up", "healthy")
        rested = self._result(self.rest.until(0.9), self.rest.detail)
        self._conjure()
        self.fight.buff_up()
        return rested

    def _spend_talents(self) -> None:
        """Talent points unspent go to the class's build, at a meal (V261): the paladin
        reached 15.87 with six unspent. A visit that fails is not made again this level."""
        values = self._read() or {}
        level = values.get("char.level")
        if (not values.get("char.talent_points") or (values.get("schema") or 0) < TALENTS_SCHEMA
                or values.get("vitals.combat") is not False or level == self._talents_failed):
            return
        build = build_for(values.get("char.class_id"))
        if not build:
            return
        desk = TalentDesk(self.client.hid, self._read, self.client.origin, self.client.size)
        outcome = desk.run(build)
        if not outcome.ok:
            self._talents_failed = level
        self.say(f"  talents: {outcome.value}, {desk.spent} spent"
                 + (f" [{', '.join(desk.learned)}]" if desk.learned else "")
                 + (f" ({desk.detail})" if desk.detail else ""))

    def _use_consumable(self, role: Role) -> bool:
        """Eat or drink the best of `role` the bags hold, for a meal whose bar slot is empty:
        a caster's conjured water, when the water it started with has run out (V166)."""
        values = self._read() or {}
        kind = "drink" if role is Role.DRINK else "food"
        user = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                      self.client.size)
        used = user.use_item(consumables(kind, values.get("char.level")))
        self.say(f"    {kind} from the bags: {used}" if used is not None
                 else f"    no {kind} in the bags - {user.detail}")
        return used is not None

    def _conjure(self) -> None:
        """After a meal, out of combat: a caster makes its water and food when the bags hold
        fewer than `CONJURE_BELOW` of what its conjures make, `CONJURE_CASTS` casts each at
        most (V166). Looked at only when the bags have changed since."""
        values = self._read()
        profile = self.fight.profile or (for_class(values.get("char.class_id"),
                                                   values.get("char.race_id"))
                                         if values else None)
        rows = [r for r in (profile.by_role(Role.CONJURE) if profile else ()) if r.creates]
        if not rows or not values or values.get("vitals.combat") is not False:
            return
        revision = values.get("inventory.revision")
        if (revision is None or revision == self._conjure_checked
                or time.monotonic() < self._conjure_next):
            return
        counter = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                         self.client.size)
        slots = counter.census()
        if not slots:
            return
        short = False                 # a cast skipped for want of mana: look again soon
        for row in rows:
            have = sum(count for item, count in slots.values() if item == row.creates)
            if have >= CONJURE_BELOW:
                continue
            cast = 0
            for _ in range(CONJURE_CASTS):
                values = self._read()
                if not values or values.get("vitals.combat") is not False:
                    return
                if (values.get("vitals.power") or 0) * (values.get("vitals.power_max") or 0) \
                        < row.mana:
                    short = True
                    break
                event("conjure.request", data={"slot": row.slot, "spell": row.spell_id,
                                               "creates": row.creates, "have": have})
                self.client.hid.tap(REST_KEYS.get(row.slot, str(row.slot)))
                self._await_cast()
                cast += 1
            self.say(f"    conjured {row.name} {cast} times: had {have}")
        after = self._read()
        self._conjure_checked = None if short else (after.get("inventory.revision") if after
                                                    else None)
        self._conjure_next = 0.0 if short else time.monotonic() + CONJURE_EVERY_S

    def _await_cast(self, timeout_s: float = CONJURE_CAST_WAIT_S) -> None:
        """Until a cast begun ends: it starts within a look, then runs its cast time."""
        began = time.monotonic()
        seen = False
        while time.monotonic() - began < timeout_s:
            values = self._read() or {}
            casting = values.get("bars.casting") is True
            seen = seen or casting
            if (seen and not casting) or (not seen and time.monotonic() - began > 1.0):
                return
            time.sleep(0.1)

    def _wear_upgrades(self) -> None:
        """Put on anything in the bags better than what this character wears (`jev.world.gear`).

        Before a meal: out of combat, standing still, no shop open. Only when the bags have
        changed since the last look, since a census of them takes a few seconds."""
        values = self._read()
        if not values or values.get("vitals.combat") is not False:
            return
        revision = values.get("inventory.revision")
        if revision is None or revision == self._gear_checked:
            return
        wearer = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                        self.client.size)
        items = wearer.bag_items()
        if items is None:
            return
        self._gear_checked = revision
        chosen = gear_upgrades(items, load_worn(self.gear_memory),
                               class_id=values.get("char.class_id"),
                               race_id=values.get("char.race_id"), level=values.get("char.level"))
        if not chosen:
            return
        worn = set(wearer.equip_items({p.item_id for p in chosen}))
        put_on = [p for p in chosen if p.item_id in worn]
        save_worn(self.gear_memory, put_on)
        self.say(f"  put on {len(put_on)} of {len(chosen)} upgrades: "
                 + ", ".join(f"{p.slot} {p.item_id}" for p in put_on)
                 + (f" ({wearer.detail})" if wearer.detail else ""))
        after = self._read()
        if after and after.get("inventory.revision") is not None:
            self._gear_checked = after.get("inventory.revision")

    def _census(self, *, seconds: float = CENSUS_LOOK_S):
        """The bar and the spellbook, whole: read every paint until both censuses are.

        Read as they come, a census can stay partial for minutes: the strip paints one
        entry per paint, and a reader at a steady fraction of the paint rate sees the same
        few bar slots over and over (session 56 put nothing on the bar for that). At twenty
        reads a second every paint is seen: the bar in 1.2 s, a spellbook of 17 in 1.7 s.
        The last whole ones are kept, so this reads only when one is missing.
        """
        census = getattr(self.client, "spells", None)
        if census is None:
            return None, None
        deadline = time.monotonic() + seconds
        while True:
            with self.client._capturing:
                bar, known = census.bar, census.known
            if (bar is not None and known is not None) or time.monotonic() >= deadline:
                return bar, known
            self._read()
            time.sleep(0.05)

    def _bar_profile(self) -> None:
        """Fight with what the bar holds: its census's spells by role, over the class's
        starting profile. Without a whole census the starting profile stands."""
        values = self._read()
        if values is None or getattr(self.client, "spells", None) is None:
            return
        base = for_class(values.get("char.class_id"), values.get("char.race_id"))
        bar, _ = self._census()
        self.fight.profile = profile_from_bar(bar, base) if bar else None

    def _trainer(self, state: State | None = None):
        """The class trainer worth a visit now (`jev.world.training.trainer_due`), or None."""
        look = self._training_look(state)
        return trainer_due(money=look[1], **look[0]) if look is not None else None

    def training_reserve(self, state: State | None = None) -> int:
        """What the purse keeps for the class trainer (`jev.world.training.training_cost`,
        V215): 0 without a census, a position or anything to learn."""
        try:
            look = self._training_look(state)
            return training_cost(**look[0]) if look is not None else 0
        except Exception:
            return 0

    def _training_look(self, state: State | None = None) -> tuple[dict, int | None] | None:
        """What the trainer rules ask, and the purse.

        From `state` when given: the policy asks from the supervisor's thread, where the
        body's own readers (and the running skill's checkpoint) are not to be touched.
        Without it, from a look of the body's own, in the skill's thread."""
        census = getattr(self.client, "spells", None)
        if census is None or self.client.bounds is None:
            return None
        if state is None:
            values, here = self._read(), self._position()
            if values is None or here is None:
                return None
            level, class_id, race_id, money = (values.get(k) for k in (
                "char.level", "char.class_id", "char.race_id", "bags.money_copper"))
        else:
            if state.pos.mx is None or state.pos.my is None:
                return None
            here = (state.pos.mx, state.pos.my)
            level, money = state.char.level, state.bags.money_copper
            class_id = CLASS_IDS.get(state.char.cls)
            race_id = RACE_IDS.get(state.char.race)
        with self.client._capturing:
            known, bar = census.known, census.bar
        return (dict(class_id=class_id, race_id=race_id, level=level, known=known, bar=bar,
                     map_id=self.client.bounds.map_id,
                     here=map_to_world(*here, self.client.bounds)[:2]), money)

    def trainable(self, state: State) -> bool:
        """Whether a class trainer has something to teach that the purse can pay for."""
        try:
            return self._trainer(state) is not None
        except Exception:
            return False

    @property
    def policy_context(self) -> Context:
        return self._policy_context

    @policy_context.setter
    def policy_context(self, context: Context) -> None:
        # The supervisor hands the body the runtime's context; training is asked of it.
        self._policy_context = context
        if self.purse_memory is not None:
            with contextlib.suppress(OSError, ValueError):
                raw = json.loads(Path(self.purse_memory).read_text())
                context.restore_purse(raw)
                # What the bar conjured when the last session ended stands until the bar is
                # read (V244): each session begins with its bar unread, and session 217's
                # first act was a restock of the food and water the mage conjures.
                kept = raw.get("conjured") if isinstance(raw, dict) else None
                if isinstance(kept, list):
                    self._conjured_last = frozenset(k for k in kept if k in ("food", "drink"))
                revived = raw.get("revived_at") if isinstance(raw, dict) else None
                if isinstance(revived, (int, float)) and not isinstance(revived, bool):
                    self._revived_at = float(revived)
                ready = raw.get("hearth_ready_at") if isinstance(raw, dict) else None
                if isinstance(ready, (int, float)) and not isinstance(ready, bool):
                    self._hearth_ready_at = float(ready)
                # A body the last session could not get up at is the same body now, and its
                # Spirit Healer where the ghost appeared (V301): a session in the hive lasted
                # about five corpse runs, and one that begins as a ghost never saw the graveyard.
                unreached = _numbers(raw.get("unreached_body") if isinstance(raw, dict) else None, 4)
                if unreached is not None:
                    self._unreached = ((int(unreached[0]), *unreached[1:3]), int(unreached[3]))
                graveyard = _numbers(raw.get("graveyard") if isinstance(raw, dict) else None, 3)
                if graveyard is not None:
                    self._graveyard = (int(graveyard[0]), graveyard[1], graveyard[2])
                # Where it fell and where it last stood alive, for a session begun dead or a
                # ghost (V328).
                for name, attr in (("fell", "_fell"), ("alive_at", "_alive_at")):
                    kept = _numbers(raw.get(name) if isinstance(raw, dict) else None, 3)
                    if kept is not None:
                        setattr(self, attr, (int(kept[0]), kept[1], kept[2]))
            context.saved = self._save_purse
        context.trainable = self.trainable
        context.camped = self.rib_camped
        context.reserve = self.training_reserve
        context.bindable = self.bindable
        context.discoverable = self.discoverable
        context.conjures = self.conjured_roles
        context.mana_line = self.measured_mana_line

    def measured_mana_line(self) -> float | None:
        """A caster's mana line from its kills (`Fight.mana_line`, V170), when there is one."""
        fight = getattr(self, "fight", None)
        return fight.mana_line() if hasattr(fight, "mana_line") else None

    def conjured_roles(self) -> frozenset[str]:
        """What this character's bar makes for itself: "drink", "food" (V166). While the
        bar's census is being read again (after training, the profile is none for a while)
        the last answer stands (V243): the level 8 mage's profile went blank after Frostbolt
        was placed, a restock of food and water it conjures was asked for, and the walk to
        the Westbrook Garrison's quartermaster wedged it inside for a session (215)."""
        profile = self.fight.profile
        if profile is None:
            return self._conjured_last
        roles: set[str] = set()
        for row in profile.by_role(Role.CONJURE):
            kind = consumable_role(row.creates)
            roles |= {"food", "drink"} if kind == "both" else {kind} if kind else set()
        if frozenset(roles) != self._conjured_last:
            self._conjured_last = frozenset(roles)
            self._save_purse()
        return self._conjured_last

    def _train(self, state) -> Result:
        trainer = self._trainer()
        if trainer is None:
            return Result(SkillOutcome.ABORTED, "no trainer has anything to teach", "nothing")

        def visit():
            return self._open_trainer(trainer)

        census = self.client.spells
        with self.client._capturing:
            before = census.book_revision
            known, bar = census.known, census.bar
        values = self._read() or {}
        # What is worth buying there, best first, by the list the strip paints (V237).
        desk = TrainerDesk(self.client.hid, self._read, visit, self.client.origin,
                           self.client.size, trainer=trainer, known=known, bar=bar,
                           race_id=values.get("char.race_id"))
        outcome = desk.run(timeout_s=self.travel_timeout + 180)
        # One visit a level, however it went: a spell a trainer lists and will not teach
        # (a talent's rank) would otherwise bring the character back at every look. A
        # visit cut short by a fight never gets here, and is asked for again after it.
        self.policy_context.train_failed(state.char.level if state is not None else None)
        names = [f"{f.name} {f.rank}" if f.rank else f.name
                 for f in map(spell_facts, desk.learned) if f is not None]
        self.say(f"  {trainer.name}: {outcome.value}, {desk.bought} bought for "
                 f"{desk.spent} copper" + (f" [{', '.join(names)}]" if names else "")
                 + (f" ({desk.detail})" if desk.detail else ""))
        if desk.bought:
            self._await_census(before)
        placed = self._place_spells(force=True)
        detail = f"{desk.bought} spells bought at {trainer.name}; {placed}"
        return self._result(outcome, detail)

    def _save_purse(self) -> None:
        if self.purse_memory is None or getattr(self, "_policy_context", None) is None:
            return
        from jev.persist import atomic_json

        # One save at a time, the latest last: the supervisor's thread keeps where the character
        # fell (`observe`) while the worker keeps the rest (V328).
        with self._purse_lock, contextlib.suppress(OSError):
            unreached = (None if self._unreached is None
                         else [*self._unreached[0], self._unreached[1]])
            fell, alive = getattr(self, "_fell", None), getattr(self, "_alive_at", None)
            atomic_json(Path(self.purse_memory), {"format": 1, **self.policy_context.purse(),
                                                  "conjured": sorted(self._conjured_last),
                                                  "revived_at": self._revived_at,
                                                  "hearth_ready_at": self._hearth_ready_at,
                                                  "unreached_body": unreached,
                                                  "graveyard": (list(self._graveyard)
                                                                if self._graveyard else None),
                                                  "fell": list(fell) if fell else None,
                                                  "alive_at": list(alive) if alive else None})

    def _go_home(self):
        """Home by hearthstone; where it sets the character down is home from then on. Not
        pressed while it is still cooling from the last use this character made of it
        (V253)."""
        now = time.time()
        if self._hearth_ready_at is not None and now < self._hearth_ready_at:
            self.hearth.detail = (f"cooling down, {(self._hearth_ready_at - now) / 60:.0f} "
                                  "minutes left")
            return Hearthed.NOT_READY
        home = self.hearth.run()
        if home is Hearthed.HOME:
            self._hearth_ready_at = now + HEARTH_COOLDOWN_S
            self._save_purse()
        elif home is Hearthed.NOT_READY:
            self._hearth_ready_at = now + HEARTH_RETRY_S
            self._save_purse()
        here = self._position() if home.ok else None
        world = map_to_world(*here, self.client.bounds) if here is not None else None
        if world is not None:
            save_home(self.home_memory, (world[0], world[1], 0.0), name="hearthstone arrival")
        return home

    def _inn(self, state: State | None = None):
        """The innkeeper nearest the guide's current step, within `INN_NEAR_YARDS` of it."""
        node = self.graph.get(state.guide.step_id) if state is not None and state.guide.step_id \
            else self._node()
        if node is not None and node.world is not None and node.map_id == self.client.bounds.map_id:
            at = node.world[:2]
        else:
            here = self._position()
            if here is None:
                return None
            at = map_to_world(*here, self.client.bounds)
        side = getattr(state.char, "faction", None) if state is not None else None
        side = getattr(side, "value", side)
        near = [(math.dist(i.world[:2], at), i)
                for i in innkeepers(self.client.bounds.map_id, side)]
        near = [(d, i) for d, i in near if d <= INN_NEAR_YARDS]
        return min(near, key=lambda pair: pair[0])[1] if near else None

    def bindable(self, state: State) -> bool:
        """An inn near the guide's work while home is far from it, or unknown.

        A character at level 1 with no home remembered stands where its hearthstone is
        bound: every new character's is, to its starting area. That place is remembered as
        home instead (V176): with home unknown, a level-1 mage left Northshire for
        Goldshire's inn at T-0 and died twice on the way."""
        try:
            inn = self._inn(state)
            if inn is None:
                return False
            home = load_home(self.home_memory)
            if (home is None and state.char.level == 1 and state.pos.mx is not None
                    and state.pos.my is not None and self.client.bounds is not None):
                here = map_to_world(state.pos.mx, state.pos.my, self.client.bounds)
                if here is not None:
                    save_home(self.home_memory, (here[0], here[1], 0.0),
                              name="where the character began")
                    return False
            if home is None:
                return True
            node = self.graph.get(state.guide.step_id or "")
            at = node.world[:2] if node is not None and node.world is not None else inn.world[:2]
            return (math.dist(home[:2], inn.world[:2]) > SAME_INN_YARDS
                    and math.dist(home[:2], at) > HOME_FAR_YARDS)
        except Exception:
            return False

    def _bind(self, state) -> Result:
        """Walk to the innkeeper, choose "Make this inn your home.", accept, remember it."""
        inn = self._inn(state)
        if inn is None:
            return Result(SkillOutcome.ABORTED, "no inn near the guide's work", "nothing")
        point = world_to_map(*inn.world[:2], self.client.bounds)
        opened = self.interact.open_on(inn.name, node_world=inn.world, node_map=point)
        if not opened.opened:
            return self._result(opened, f"{inn.name}: {self.interact.detail or opened.value}")
        try:
            if opened is not Interacted.GOSSIP:
                return Result(SkillOutcome.ABORTED, f"{inn.name} opened {opened.value}, not a gossip",
                              "no_gossip")
            chose = self.chooser.run(BIND_LINE)
            if not chose.ok:
                return Result(SkillOutcome.ABORTED, f"{inn.name}: {self.chooser.detail or chose.value}",
                              "no_bind_line")
            if not self._accept_popup():
                return Result(SkillOutcome.ABORTED, f"{inn.name}: no confirmation to accept",
                              "no_confirmation")
        finally:
            values = self._read()
            if values and values.get("ui.modal") is not True:
                close_observed(self.client.hid, self._read, values=values)
        save_home(self.home_memory, inn.world, name=inn.name)
        return Result(SkillOutcome.SUCCEEDED, f"{inn.name}'s inn is home", "done")

    def _accept_popup(self) -> bool:
        """Press the confirmation's first button (Accept) and see the confirmation go."""
        deadline = time.monotonic() + POPUP_S
        while time.monotonic() < deadline:
            values = self._read()
            if (values and values.get("ui.modal") is True
                    and values.get("ui.advance_x") is not None and values.get("ui.advance_y") is not None):
                ox, oy = self.client.origin
                w, h = self.client.size
                self.client.hid.click(ox + round(values["ui.advance_x"] * w),
                                      oy + round(values["ui.advance_y"] * h))
                gone = time.monotonic() + POPUP_S
                while time.monotonic() < gone:
                    after = self._read()
                    if after and after.get("ui.modal") is False:
                        return True
                    time.sleep(0.1)
                return False
            time.sleep(0.1)
        return False

    def _open_on(self, name: str, world, point):
        """Open a unit by name at its node (`Interact.open_on`), and once more from the floor
        below when it stands on one above and was not found where the walk ended (V235).
        The mage's walk to Zaldimar Wefhellt, upstairs in the Lion's Pride Inn, "arrived" in
        the hall under him: two plates tried, neither his, and training waited a session
        (session 197). Positions are x and y alone, so an arrival under a unit looks like one
        beside it; the next plan starts on the floor the character is on."""
        opened = self.interact.open_on(name, node_world=world, node_map=point)
        if opened in (Interacted.NOT_VISIBLE, Interacted.NO_TARGET) and self._under(world):
            self.say(f"  {name} was not found here: walking up to the floor above again")
            opened = self.interact.open_on(name, node_world=world, node_map=point)
        return opened

    def _under(self, world) -> bool:
        """The character stands under `world`, a floor or more below it: then the tracked
        height is put on the lowest floor here, where the next plan starts. With fewer than
        two floors read under the character, the floors are the unit's own (V263): the
        navmesh stops short of walls and furniture, and in the Lion's Pride Inn's hall the
        mage stood under Zaldimar Wefhellt where only the roof was read (session 238)."""
        query, bounds = getattr(self.client, "query", None), self.client.bounds
        here = self._position()
        if query is None or bounds is None or here is None or world is None or len(world) < 3:
            return False
        hx, hy = map_to_world(*here, bounds)[:2]
        if math.dist((hx, hy), world[:2]) > UNDER_UNIT_YARDS:
            return False
        floors = surfaces_under(query, bounds.map_id, hx, hy)
        if len(floors) < 2:
            floors = surfaces_under(query, bounds.map_id, world[0], world[1])
        if len(floors) < 2 or world[2] - floors[0] < UPPER_FLOOR_YARDS:
            return False
        self.client._ground = (hx, hy, floors[0])
        return True

    def _open_trainer(self, trainer) -> bool:
        point = world_to_map(*trainer.world[:2], self.client.bounds)
        opened = self._open_on(trainer.name, trainer.world, point)
        if not opened.opened:
            raise BodyFailure(self._result(opened, f"{trainer.name}: "
                                                   f"{self.interact.detail or opened.value}"))
        # Most trainers open on a gossip first, with their training line among others
        # ("I wish to unlearn my talents."): chosen by its text, never its position.
        return opened is not Interacted.GOSSIP or bool(
            trainer.gossip and self.chooser.run(trainer.gossip).ok)

    def _await_census(self, before: int | None) -> None:
        """Wait for a whole spellbook census under a newer revision than `before`: the
        purchases' spells are in it."""
        census = self.client.spells
        deadline = time.monotonic() + CENSUS_S
        while time.monotonic() < deadline:
            self._read()
            with self.client._capturing:
                if census.book_revision != before and census.bar is not None:
                    return
            time.sleep(0.05)

    def _place_spells(self, *, force: bool = False) -> str:
        """Put what the spellbook has and the bar lacks on the bar
        (`jev.world.training.placements`): a new rank over the old, a new spell on a free
        slot. Out of combat, and only when the bar or the spellbook changed since the last
        plan, unless `force`."""
        census = getattr(self.client, "spells", None)
        if census is None:
            return "no spells placed"
        values = self._read()
        if (values is None or values.get("vitals.combat") is not False
                or values.get("vitals.dead") is not False or values.get("vitals.ghost") is not False):
            return "no spells placed"
        bar, known = self._census()
        mark = (values.get("bars.revision"), values.get("spells.revision"))
        if bar is None or known is None or (not force and mark == self._placing_checked):
            return "no spells placed"
        self._placing_checked = mark
        self._say_kit(known)
        plan = spell_placements(bar, known)
        if not plan:
            return "nothing to place"
        book = Spellbook(self.client.hid, self._read, self.client.origin, self.client.size)
        outcome = book.place(plan)
        with self.client._capturing:
            census.forget_bar()
        done = ", ".join(f"{p.spell_id}->{p.slot}" for p in book.placed)
        self.say(f"  spells on the bar: {outcome.value} {len(book.placed)}/{len(plan)}"
                 + (f" [{done}]" if done else "") + (f" ({book.detail})" if book.detail else ""))
        return f"placed {len(book.placed)} of {len(plan)} spells ({outcome.value})"

    def _say_kit(self, known) -> None:
        """One line when the spellbook is first read, and again when it changes: the spells
        it holds that nothing presses (`jev.world.training.unpressed`, V361). A spell nobody
        classified was a spell nobody pressed, and nothing said so."""
        book = frozenset(known)
        if book == self._kit_said:
            return
        self._kit_said = book
        names, unknown = unpressed(book)
        if not names and not unknown:
            return
        event("kit.unpressed", data={"spells": names, "unknown_ids": unknown})
        self.say("  kit: not pressed - " + (", ".join(names) or "none")
                 + (f"; {len(unknown)} not in the catalog {unknown[:KIT_IDS_SAID]}"
                    if unknown else ""))

    def _clear_of_spawns(self) -> None:
        """Walk out of reach of the step's own spawn points, and of every unit's near that
        attacks on sight (V247), before a meal, where a way out is known: nearest point
        first, on rings round the character. All ten attacks on the mage resting or getting
        up began within 20 yards of such a spawn (sessions 195-219)."""
        node = self._node()
        here = self._position()
        if here is None:
            return
        world = map_to_world(*here, self.client.bounds)
        spawns = (*(spawn_around(self.hunt_spawns, node.id) if node is not None else ()),
                  *self._hostiles(world))
        if not spawns:
            return
        spot = rest_spot(world, spawns)
        if spot is None:
            return
        self.say(f"  resting out of the camp's reach, {math.dist(spot[:2], map_to_world(*here, self.client.bounds)):.0f} yards off")
        self._approach(spot)

    def _repair(self, state) -> Result:
        # Broken gear is no armour and no weapon: a level 6 paladin at full health lost the
        # first fight on its 310-yard walk to a repairer through wolf country (run
        # 20260924T042040-e86c88). Far from one, home by hearthstone first: a new
        # character's stone is bound beside its starting area's armourer.
        durability = state.bags.durability_min
        distance = self._repairer_yards()
        if (durability is not None and durability <= BROKEN_DURABILITY
                and distance is not None and distance > HEARTH_TO_REPAIR_YARDS
                and self._home_repairs(distance)):
            home = self._go_home()
            self.say(f"  broken gear and the nearest repairer {distance:.0f} yards off: "
                     f"hearthstone {home.value} {self.hearth.detail}".rstrip())
        repaired = self.repair.run()
        if repaired.value == "done":
            self.policy_context.repaired()
        return self._result(repaired, self.repair.detail)

    def _home_near_work(self, state) -> bool:
        """Home is near the guide's work: within `HOME_FAR_YARDS` of the step's node (V279).
        Up at the Spirit Healer, home by hearthstone is the way back to it only then: with its
        stone bound at Sentinel Hill and its grind on Elwynn's Prowlers (V276), the level 12
        mage would have hearthed 3,500 yards from its work after a death at a camp. A home or
        a step not known is taken to be near, as a new character's is."""
        home = load_home(self.home_memory)
        if home is None:
            return True
        step = getattr(getattr(state, "guide", None), "step_id", None)
        node = self.graph.get(step) if step else self._node()
        if node is None or node.world is None:
            return True
        return math.dist(home[:2], node.world[:2]) <= HOME_FAR_YARDS

    def _home_repairs(self, distance: float) -> bool:
        """Home by hearthstone is the way to a repairer (V278): one stands by home, and the walk
        back from home is shorter than the walk to the nearest repairer and back. A stone bound
        where the guide worked before is not: the level 12 mage, back on Elwynn's Prowlers with
        its stone bound at Sentinel Hill, hearthed 3,500 yards from a repairer 232 yards off, and
        walked it all back (session 266). A home not known is taken to stand by an armourer, as
        a new character's does."""
        home = load_home(self.home_memory)
        here = self._position()
        if home is None or here is None:
            return True
        by_home = self._repairer_yards(at=home[:2])
        world = map_to_world(*here, self.client.bounds)
        return (by_home is not None and by_home <= HEARTH_TO_REPAIR_YARDS
                and math.dist(home[:2], world[:2]) < 2 * distance)

    def _repairer_yards(self, at: tuple[float, float] | None = None) -> float | None:
        if at is None:
            here = self._position()
            if here is None:
                return None
            at = map_to_world(*here, self.client.bounds)
        placed = [m.world for m in self._repairers()] or [
            n.world for n in self.graph.nodes if n.kind is StepKind.REPAIR
            and n.world is not None and n.map_id == self.client.bounds.map_id]
        return min((math.dist(w[:2], at[:2]) for w in placed), default=None)

    def _vendor(self, state) -> Result:
        values, here = self._read(), self._position()
        if values is None or here is None:
            return Result(SkillOutcome.PREEMPTED, "vendor position or inventory unread", "blind")
        supplies = ()
        if self.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD":
            supplies = tuple(s for s in supplies_for(values.get("char.class_id"), values.get("char.race_id"))
                             if getattr(state.bags, f"{s.role.lower()}_count", None) == 0
                             and getattr(state.bags, f"{s.role.lower()}_id", None) == s.item_id)
            if not supplies:
                return Result(SkillOutcome.ABORTED, "no confirmed empty supported food/drink slot", "unsupported")
        eligible, min_free = junk_prices(), 1 if supplies else 6
        bag = ()
        if self.arm.decision.skill == "BAG_MAKE_SPACE":
            # A bag lying in the bags is the cheapest room there is: no merchant needed.
            equipper = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                              self.client.size)
            if equipper.equip_bags(bag_slots()):
                after = self._read()
                if after and (after.get("bags.free") or 0) > 0:
                    return Result(SkillOutcome.SUCCEEDED, "equipped a bag from the bags", "done")
            # Gear not worth wearing, and goods no quest needs, go with the grey: all of it,
            # since the character is at a merchant anyway and training wants the silver.
            items = equipper.bag_items()
            if items is not None:
                keep = gear_keep(items, load_worn(self.gear_memory),
                                 class_id=values.get("char.class_id"),
                                 race_id=values.get("char.race_id"))
                surplus = surplus_prices()
                eligible = {**eligible, **{i: surplus[i] for i in set(items)
                                           if i in surplus and i not in keep}}
                min_free = SELL_ALL
                # What fills the bags, for a service that finds nothing to sell: the mage's
                # bags stayed full through sessions 208-211, "no_junk" each visit.
                event("bags.census", data={"items": sorted(items), "keep": sorted(keep),
                                           "eligible": sorted(eligible)})
                counted = getattr(equipper, "last_census", None) or {}
                worth = sum(eligible.get(item, 0) * count for item, count in counted.values())
                free = values.get("bags.free")
                # A bag bought needs a slot to land in: one free, or one a sale empties.
                bag = (self._bag_to_buy(values)
                       if (isinstance(free, int) and free > 0) or worth > 0 else ())
                if (not bag and counted and isinstance(free, int) and free > 0
                        and worth < SALE_WORTH_COPPER):
                    return Result(SkillOutcome.ABORTED,
                                  f"the bags' goods fetch {worth} copper, not worth the walk "
                                  f"with {free} slot{'s' if free > 1 else ''} free", "no_junk")
        wanted = {s.item_id for s in (*supplies, *bag)}
        candidates = self._in_zone(m for m in merchants(self.client.bounds.map_id)
                                   if not wanted or wanted & m.items)
        if not candidates:
            # None in the zone sells what is wanted: as a merchant too far off (V205), nothing
            # to walk to on this step, and no fault of the session's (V292). A human warlock's
            # starting food, the Forest Mushroom Cap, is sold nowhere in Elwynn, and every
            # session stopped on it at once, 2515 times in a quarter of an hour (the hive).
            # Nor on any step after it this session (V302).
            if supplies:
                self.policy_context.supplies_out_of_reach("no_supplier")
            return Result(SkillOutcome.ABORTED, "no generated supplier in the measured zone",
                          "no_supplier" if supplies else "unsupported")
        world = map_to_world(*here, self.client.bounds)
        ranked = self._ranked(candidates, world)
        # A purchase keeps what the trainer is owed (V215).
        reserve = self.training_reserve() if supplies else 0
        if supplies:
            walk = self._walks.get(ranked[0].entry)          # planned once, by the ranking
            if walk is None:
                walk = self._walk_yards(ranked[0].world, math.dist(ranked[0].world[:2], world))
            if walk > SUPPLY_WALK_MAX_YARDS and not self._stranded(state, values):
                self.policy_context.supplies_out_of_reach("too_far")     # this session (V302)
                return Result(SkillOutcome.ABORTED,
                              f"the nearest merchant with them, {ranked[0].name}, is a "
                              f"{walk:.0f}-yard walk", "too_far")
            if walk > SUPPLY_WALK_MAX_YARDS:
                # Out of all it eats and drinks with its gear broken, the cap does not hold
                # (V302): home by hearthstone first when a merchant by home sells them, as for
                # a repair (V278), else the walk. With neither, the hive's characters died 40
                # times an hour of play, against 11 with broken gear alone and 4 with food and
                # whole gear, most of them at the graveyards of V300 (28 Sep, 09:11-11:11).
                if self._home_supplies(wanted, walk, world):
                    home = self._go_home()
                    self.say(f"  out of food and drink with broken gear, the nearest merchant "
                             f"with them {walk:.0f} yards off: hearthstone {home.value} "
                             f"{self.hearth.detail}".rstrip())
                    moved = self._position() if home.ok else None
                    if moved is not None:
                        world = map_to_world(*moved, self.client.bounds)
                        by_home = self._in_zone(m for m in merchants(self.client.bounds.map_id)
                                                if wanted & m.items)
                        ranked = self._ranked(by_home, world) if by_home else ranked
                else:
                    self.say(f"  out of food and drink with broken gear: walking {walk:.0f} "
                             f"yards to {ranked[0].name}")
        for merchant in ranked:
            def visit(merchant=merchant):
                return self._open_merchant(merchant.name, merchant.world,
                                           world_to_map(*merchant.world[:2], self.client.bounds))
            vendor = Vendor(self.client.hid, self._read, visit, self.client.origin, self.client.size,
                            eligible=eligible)
            try:
                outcome = vendor.run(expected_name=merchant.name,
                                     supplies=tuple(s for s in (*supplies, *bag)
                                                    if s.item_id in merchant.items),
                                     min_free=min_free,
                                     reserve_copper=(self.training_reserve() if bag else reserve),
                                     timeout_s=self.travel_timeout + 120)
            except BodyFailure as failure:
                if failure.result.code in MERCHANT_UNREACHABLE:
                    note_merchant(self.merchant_memory, merchant.entry, failed=True)
                if merchant is ranked[-1] or failure.result.code not in MERCHANT_UNREACHABLE:
                    raise
                self.say(f"  {failure.result.detail}; trying the next merchant")
                continue
            if outcome.ok:
                note_merchant(self.merchant_memory, merchant.entry, failed=False)
                if supplies:
                    self.policy_context.restocked()
                elif self.arm.decision.skill == "BAG_MAKE_SPACE":
                    # Sold, and the bags still nearly full: what is left does not sell, and
                    # the service is not asked again until something new is in them (V250).
                    # Five of the mage's visits in 205-217 were armed again at the counter.
                    free = (self._read() or {}).get("bags.free")
                    if isinstance(free, int) and free <= BAGS_LOW:
                        self.policy_context.bags_failed(free)
            if outcome is Vended.TOO_POOR and supplies:
                self.policy_context.supplies_need(vendor.needed_copper)
            if bag and vendor.bought_units:
                # The bag bought goes on the belt at once, where it is room.
                belt = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                              self.client.size)
                worn = belt.equip_bags(bag_slots())
                self.say(f"  bought a bag ({bag[0].item_id}): {'on' if worn else 'not on'} the belt")
            return self._result(outcome, vendor.detail or
                                f"sold {vendor.sold_stacks} stacks; bought {vendor.bought_units} units")
        raise AssertionError("unreachable: the last merchant returns or raises")

    def _stranded(self, state, values: dict) -> bool:
        """Out of all it eats and drinks that it does not conjure, with its gear broken: no cap
        on the walk to a merchant with them (V302)."""
        conjured = self.conjured_roles()
        roles = [s for s in supplies_for(values.get("char.class_id"), values.get("char.race_id"))
                 if s.role.lower() not in conjured]
        durability = state.bags.durability_min
        return (bool(roles) and durability is not None and durability <= BROKEN_DURABILITY
                and all(getattr(state.bags, f"{s.role.lower()}_count", None) == 0 for s in roles))

    def _home_supplies(self, wanted: set[int], walk: float, world) -> bool:
        """Home by hearthstone is the way to a merchant with `wanted` (V302), as it is to a
        repairer (V278): one stands within the walk's cap of home, and the walk back from home
        is shorter than the walk to the nearest merchant with them and back. A home not known
        is no such way."""
        home = load_home(self.home_memory)
        if home is None:
            return False
        by_home = [m for m in merchants(self.client.bounds.map_id) if wanted & m.items
                   and math.dist(m.world[:2], home[:2]) <= SUPPLY_WALK_MAX_YARDS]
        return bool(by_home) and math.dist(home[:2], world[:2]) < 2 * walk

    def _bag_to_buy(self, values: dict) -> tuple:
        """The general bag to buy on this walk, as a supply of one, or none: the belt short of
        `BAG_ROOM_SLOTS`, a merchant in the zone selling one, and the purse able to spare the
        cheapest a slot over the trainer's due and `BAG_SPARE_COPPER` (V260)."""
        total, money = values.get("inventory.total"), values.get("bags.money_copper")
        if not isinstance(total, int) or total >= BAG_ROOM_SLOTS or not isinstance(money, int):
            return ()
        slots, prices = bag_slots(), bag_prices()
        sold = {item for m in self._in_zone(merchants(self.client.bounds.map_id))
                for item in m.items if item in slots and item in prices}
        if not sold:
            return ()
        best = min(sold, key=lambda item: (prices[item] / slots[item], -slots[item], item))
        if money < prices[best] + self.training_reserve() + BAG_SPARE_COPPER:
            return ()
        return (Supply(item_id=best, name=f"bag {best}", role="bag", desired=1),)

    def _in_zone(self, candidates) -> list:
        """The merchants standing inside the measured zone's map box, or inside the box of the
        zone the character stands in, another on the same map (V281): at Sentinel Hill on the
        Elwynn guide the mage's repair went for Frederick Stover in Stormwind, 1,900 yards and
        no complete plan (session 266), and at 14 it will stand in Elwynn on the Westfall
        guide, Goldshire's smiths out of its box."""
        boxes = [self.client.bounds]
        here = self._zone_here()
        if (here is not None and here != self.client.bounds
                and here.map_id == self.client.bounds.map_id):
            boxes.append(here)
        return [m for m in candidates if any(
            (point := world_to_map(*m.world[:2], box)) is not None
            and all(0 <= value <= 1 for value in point) for box in boxes)]

    def _zone_here(self):
        """The map box of the zone the strip says the character stands in, or `None`."""
        zones = getattr(self.client, "coordinate_zones", None)
        if not zones:
            return None
        values = self._read() or {}
        return zones.get(values.get("pos.zone_id"))

    def _ranked(self, candidates, world) -> list:
        """The first `MERCHANT_TRIES` merchants to try from `world`: the shortest walk,
        each failure since the last sale counted in yards (V199). Planned one at a time,
        nearest in a straight line first, each plan after a checkpoint, until the next could
        not beat the best walk planned, `MERCHANT_PLANS` are planned or `RANKING_BUDGET_S` is
        spent (V309); the merchants not planned follow in straight-line order. The walks
        planned are kept for the caller (`_walks`)."""
        failed = load_merchant_failures(self.merchant_memory)

        def cost(merchant, yards: float) -> float:
            return yards + FAILED_MERCHANT_YARDS * failed.get(merchant.entry, 0)

        order = sorted(candidates, key=lambda m: cost(m, math.dist(m.world[:2], world)))
        self._walks = {}
        best, deadline = math.inf, time.monotonic() + RANKING_BUDGET_S
        for merchant in order[:MERCHANT_PLANS]:
            straight = math.dist(merchant.world[:2], world)
            if cost(merchant, straight) >= best or time.monotonic() >= deadline:
                break
            self.checkpoint()
            self._walks[merchant.entry] = self._walk_yards(merchant.world, straight)
            best = min(best, cost(merchant, self._walks[merchant.entry]))
        planned = sorted((m for m in order if m.entry in self._walks),
                         key=lambda m: cost(m, self._walks[m.entry]))
        return (planned + [m for m in order if m.entry not in self._walks])[:MERCHANT_TRIES]

    def _repairers(self) -> list:
        """The merchants in the zone that mend gear (the catalog's repair flag)."""
        return self._in_zone(m for m in merchants(self.client.bounds.map_id) if m.repairs)

    def _walk_yards(self, world, straight: float) -> float:
        """What walking to `world` costs, in yards: its plan's length and corners."""
        plan_to = getattr(self.client, "plan_to", None)
        planned = plan_to(world) if plan_to is not None else None
        if planned is None:
            return straight                      # no planner to ask: distance as before
        if not planned.usable:
            return straight * UNPLANNED_FACTOR
        return planned.length_yards() + CORNER_YARDS * max(0, len(planned.points) - 2)

    def _open_merchant(self, name, world, point) -> bool:
        opened = self.interact.open_on(name, node_world=world, node_map=point)
        if not opened.opened:
            # Preserve the measured nested failure. Collapsing this to False made a
            # rejected visible ring indistinguishable from a failed merchant journey.
            raise BodyFailure(self._result(opened,
                                           f"{name}: {self.interact.detail or opened.value}"))
        if opened is Interacted.GOSSIP:
            values = self._read()
            identity = values.get("merchant.gossip_name_id") if values else None
            if identity is None or not self.chooser.run_id(identity).ok:
                return False
            values = self._read()
            return bool(values and values.get("ui.vendor") is True)
        return opened is Interacted.VENDOR

    def _visit_repairer(self) -> bool:
        """Open a repairer's window: every one in the zone, ranked as merchants are, the
        next tried when one cannot be clicked (V201). The guide's REPAIR nodes named one
        smith a place - Janos Hammerknuckle in Northshire, whose awning took every probe
        (26 September), with Dermot Johns and Godric Rothgar in twenty yards - and from
        Stormwind the nearest was 1,281 yards off, a walk the repair ran out of time on."""
        here = self._position()
        if here is None:
            return False
        world = map_to_world(*here, self.client.bounds)
        repairers = self._repairers()
        if repairers:
            ranked = self._ranked(repairers, world)
            ranked = [m for m in ranked if math.dist(m.world[:2], ranked[0].world[:2])
                      <= REPAIRER_NEIGHBOUR_YARDS]
            for merchant in ranked:
                try:
                    opened = self._open_merchant(
                        merchant.name, merchant.world,
                        world_to_map(*merchant.world[:2], self.client.bounds))
                except BodyFailure as failure:
                    if failure.result.code in MERCHANT_UNREACHABLE:
                        note_merchant(self.merchant_memory, merchant.entry, failed=True)
                    if merchant is ranked[-1] or failure.result.code not in MERCHANT_UNREACHABLE:
                        raise
                    self.say(f"  {failure.result.detail}; trying the next repairer")
                    continue
                if opened:
                    note_merchant(self.merchant_memory, merchant.entry, failed=False)
                return opened
        candidates = [n for n in self.graph.nodes if n.kind is StepKind.REPAIR
                      and n.world is not None and n.map_id == self.client.bounds.map_id
                      and n.target_kind == "creature" and n.target_name]
        if not candidates:
            return False
        node = min(candidates, key=lambda n: math.dist(n.world[:2], world))
        return self._open_merchant(node.target_name, node.world, node.pos)

    def _corpse_walk(self, point) -> bool:
        wx, wy = map_to_world(*point, self.client.bounds)
        z = self._body_height((wx, wy))
        if z is None:
            return False
        return self._approach((wx, wy, z))

    def _body_height(self, world) -> float | None:
        """The height a walk to a body plans to: the nearest step's. The body's own is not
        read."""
        placed = [n for n in self.graph.nodes if n.world is not None
                  and n.map_id == self.client.bounds.map_id]
        if not placed:
            return None
        return min(placed, key=lambda n: math.dist(n.world[:2], world[:2])).world[2]

    def _floor_at(self, world, z: float) -> float | None:
        """The floor a plan to `world` at height `z` ends on: the planner's nearest."""
        query = getattr(self.client, "query", None)
        if query is None:
            return None
        snapped = query.path(self.client.bounds.map_id, (world[0], world[1], z),
                             (world[0], world[1], z))
        if snapped.status in (PathStatus.COMPLETE, PathStatus.PARTIAL) and snapped.points:
            return snapped.points[0][2]
        return None

    def _other_floor(self, body, spot) -> tuple[float, float] | None:
        """The floors of a get-up spot and its body, when the spot's is another: further from
        the body's than the spot is short of it, and more (V289). A walk plans to the spot at
        the body's height, which the planner puts on the floor nearest it; over a body in a
        cave that was the hill, 110 yards up."""
        z = self._body_height(body)
        if z is None:
            return None
        floors = self._floor_at(body, z), self._floor_at(spot, z)
        if None in floors:
            return None
        apart = math.dist((*body[:2], floors[0]), (*spot[:2], floors[1]))
        return floors if apart > self._reclaim_yards + FLOOR_SWITCH_YARDS else None

    def _short_of_body(self, point) -> bool:
        """Walk to within `TRAP_RECLAIM_YARDS` of the body, from the graveyard's side.

        The graveyard is where this process saw the ghost appear; a session that starts
        as a ghost never saw that, and the ghost's own position is the same side of the
        body until it walks."""
        origin = self.recover.graveyard or self._position()
        if origin is None:
            return self._corpse_walk(point)
        body = map_to_world(*point, self.client.bounds)
        start = map_to_world(*origin, self.client.bounds)
        short = self._short_spot(body, start)
        spawns = self._camp_spawns(body)
        clear = reclaim_spot(body[:2], short, spawns, self._reclaim_yards)
        if clear == short and math.dist(body[:2], start[:2]) <= self._reclaim_yards:
            # Inside the body's reach already and clear of the camp: up where it stands. Inside
            # a camp's reach it walks to the spot with the most room first (V300): at Raven Hill
            # the level 7 human's body lay 3 yards from the Spirit Healer, in the reach of the
            # level 23-25s round it.
            return True
        if clear != short:
            self.say(f"  getting up out of the camp's reach, "
                     f"{min(math.dist(clear, sp[:2]) - _extra(sp) for sp in spawns):.0f} yards "
                     "from its nearest spawn's reach")
        floors = self._other_floor(body, clear)
        if floors is not None:
            self.say(f"  the get-up spot is on another floor than the body ({floors[1]:.0f} "
                     f"against {floors[0]:.0f}): to the body itself")
            return self._corpse_walk(point)
        return self._corpse_walk(world_to_map(*clear, self.client.bounds))

    def _short_spot(self, body, start) -> tuple[float, float]:
        """Where a ghost from `start` gets up by its body at `body` (world yards), before the
        camp round it is looked at: `_reclaim_yards` short of it on `start`'s side, or `start`
        itself when that is nearer."""
        apart = math.dist(body[:2], start[:2])
        if apart <= self._reclaim_yards:
            return (start[0], start[1])
        share = self._reclaim_yards / apart
        return (body[0] + (start[0] - body[0]) * share, body[1] + (start[1] - body[1]) * share)

    def _hostiles(self, world, radius: float = HOSTILE_LOOK_YARDS):
        """The spawns round `world` of units that attack this character on sight and are
        worth experience at its level (`jev.world.hostiles`, V247)."""
        level = (self._read() or {}).get("char.level")
        return hostiles.near(self.client.bounds.map_id, world[0], world[1], radius,
                             side=self._side, level=level if isinstance(level, int) else None)

    def _camp_spawns(self, body) -> tuple:
        """What a ghost keeps clear of when it gets up: the step's own spawns and every
        hostile one round the body. On a travel or quest step the step has none, and the
        mage got up among the Mangy Wolves that had killed it (session 219)."""
        node = self._node()
        return (*(spawn_around(self.hunt_spawns, node.id) if node is not None else ()),
                *self._hostiles(body))

    def _body_room(self, state, point=None) -> float | None:
        """The most room from hostile spawns a ghost could get up with: at the graveyard's
        side of the body, where it stands inside the body's reach, or `_reclaim_yards` round
        the body (`_short_of_body`). The body is the map point `point`, else the one painted.
        `None` with no body known or nothing hostile near it."""
        pos = getattr(state, "pos", None)
        if point is None or None in point:
            point = (None, None) if pos is None else (pos.corpse_mx, pos.corpse_my)
        if None in point or pos is None:
            return None
        body = map_to_world(*point, self.client.bounds)
        spawns = self._camp_spawns(body)
        if not spawns:
            return None
        origin = self.recover.graveyard or ((pos.mx, pos.my) if pos.mx is not None
                                            and pos.my is not None else None)
        short = body[:2]
        if origin is not None:
            short = self._short_spot(body, map_to_world(*origin, self.client.bounds))
        spot = reclaim_spot(body[:2], short, spawns, self._reclaim_yards)
        return min(math.dist(spot, sp[:2]) - _extra(sp) for sp in spawns)

    def _healer_camp(self, body_room: float | None) -> float | None:
        """The room from hostile spawns at the Spirit Healer, where the ghost appeared, when its
        graveyard lies in a camp and the body's spot (`body_room`, `None` for nothing hostile
        round the body) has more room (V300); else `None`, as with no graveyard seen or
        nothing hostile round it."""
        if self.recover.graveyard is None:
            return None
        if self._death_camp(self.recover.graveyard):
            return -math.inf                 # where the character keeps dying (V307)
        at = map_to_world(*self.recover.graveyard, self.client.bounds)
        spawns = self._camp_spawns(at) if at is not None else ()
        if not spawns:
            return None
        room = min(math.dist(at[:2], sp[:2]) - _extra(sp) for sp in spawns)
        if room >= CAMP_ROOM_YARDS or (body_room is not None and body_room <= room):
            return None
        return room

    def _death_camp(self, point) -> bool:
        """Whether the map point `point` lies in a death camp that counts at the character's
        level (`RouteMemory.camp_at`, V307); no, with nothing read or no route memory."""
        memory, bounds = getattr(self.client, "route_memory", None), self.client.bounds
        if memory is None or bounds is None or point is None or None in point:
            return False
        at = map_to_world(*point, bounds)
        if at is None:
            return False
        level = (self._read() or {}).get("char.level")
        return memory.camp_at(bounds.map_id, at, time.time(),
                              level if isinstance(level, int) else None) is not None

    def _talk_to(self, name: str):
        """Right-click a named unit: by its nameplate, or where a fresh hover finds it."""
        opened = self.interact.open_on(name)
        if opened in (Interacted.NOT_VISIBLE, Interacted.NO_TARGET):
            return "hovered" if self._hover_interact(name) else opened
        return opened

    def _hover_interact(self, name: str) -> bool:
        """Right-click where a fresh hover says `name` is, for a unit whose nameplate does
        not show: run 20260923T182125-9c54ea's ghost stood under the Spirit Healer, its plate
        behind the strip, and the plate-first interaction found none. Out of reach (run
        ...182544-7dad55: "You are too far away!"), it steps closer and clicks again.

        Silence is out of reach too. The server drops a right-click from beyond five yards
        without a word (`INTERACTION_DISTANCE`, CMaNGOS `GetNPCIfCanInteractWith`): six
        hover-proved clicks on the Spirit Healer in run 20260924T045140-ec8686 opened
        nothing and raised no error, so only a window opening is an answer."""
        wanted = name_id(name)
        (ox, oy), (w, h) = self.client.origin, self.client.size
        too_far = UI_ERROR_KEYS.index("out_of_range")
        for _ in range(HOVER_STEPS + 1):
            point = None
            for fx, fy in HOVER_POINTS:
                candidate = (ox + round(fx * w), oy + round(fy * h))
                after = self.targeting.probe(candidate, require_target=False).after or {}
                if after.get("cursor.has") is True and after.get("cursor.name_id") == wanted:
                    point, errors = candidate, after.get("ui.error_count")
                    break
            if point is None:
                return False
            if self.client.hid.click(*point, right=True) is False:
                return False
            deadline = time.monotonic() + HOVER_ANSWER_S
            while time.monotonic() < deadline:
                answer = self._read() or {}
                if answer.get("ui.modal") is True or answer.get("ui.gossip") is True:
                    return True
                if (answer.get("ui.error_last") == too_far
                        and answer.get("ui.error_count") != errors):
                    break
                time.sleep(0.1)
            if not self.client.hid.hold("w", HOVER_STEP_S):
                return False
        return False

    def _recover(self, state) -> Result:
        self.fight.buffs_lost()
        self._kept_graveyard()
        # A body where the character keeps dying is not worth getting up at: run
        # 20260923T181209-bc03ba got up beside a level 6 wolf at half health and died,
        # four times. Up at the Spirit Healer instead, and home by hearthstone.
        # The clock is the wall's, kept in the purse file: session 219 began with the get-up
        # at the end of 218 forgotten, got up at the body again and died (V247).
        trapped = self._revived_at is not None and time.time() - self._revived_at < DEATH_TRAP_S
        stronger = self._killed_by_stronger(state)
        # A body with no spot in reach clear of the units that attack on sight lies in a
        # camp: 15 of the mage's 22 get-ups at the body died again, a median of 39 s later,
        # and 2 of its 12 at the Spirit Healer (sessions 195-219, V247).
        # Where the body lies: painted by the game, else what this process saw of it, the
        # server's in the hive, or where the character was seen to fall (V328).
        corpse = (state.pos.corpse_mx, state.pos.corpse_my) if getattr(state, "pos", None) else None
        if corpse is None or None in corpse:
            corpse = self.recover.corpse or self._known_body() or corpse
        room = self._body_room(state, corpse)
        camp = room is not None and room < CAMP_ROOM_YARDS
        # A body where the character has died twice in ten minutes lies in a death camp (V307),
        # whatever room the spawns leave round it: of 143 get-ups within 35 yards of the body's
        # spot in the hive's runs begun 11:50-13:08 on 28 Sep, 80 died again before the next.
        died_there = self._death_camp(corpse)
        if died_there:
            camp, room = True, -math.inf
        # But the Spirit Healer is a way out only from a graveyard with more room than the
        # body's spot (V300). In the hive's two hours to 11:11 on 28 Sep, 243 of 455 get-ups at
        # the Spirit Healer died within a minute, against 79 of 347 at the body, most to a unit
        # 3 or more levels up that attacked a median 2 s after the get-up: a level 5-6 unit that
        # wanders 15 yards spawns 16 yards from Brill's Spirit Healer, level 23-25s 29 yards
        # from Raven Hill's, and the level 2 undead and level 7 human who got up there had spots
        # by their bodies a median 32 and 37 yards clear of any unit's reach.
        healer_camp = self._healer_camp(room) if trapped or stronger or camp else None
        unreached = self._unreached_runs(corpse)
        if unreached >= UNREACHED_RUNS:
            # A body the ghost could not get up at, run after run, is out of its reach: under
            # water it cannot dive into, or ground it cannot walk to (V301). The Spirit Healer
            # is the one way up left, camp or not; up there, and on.
            up = self._spirit_healer()
            if up is Recovered.ALIVE:
                self._revived(None)
                self.say(f"  up at the Spirit Healer: {unreached} corpse runs in a row did not "
                         "get up at the body")
                self._wait_out_sickness()
                return self._result(up, "up at the Spirit Healer; the body is out of reach")
            self.say(f"  the Spirit Healer did not raise us ({up.value}); back to the body")
        elif healer_camp is not None:
            self.say("  the Spirit Healer is no way out: its graveyard lies in a death camp; up "
                     "at the body" if healer_camp == -math.inf else
                     f"  the Spirit Healer is no way out: its graveyard lies in a camp, "
                     f"{healer_camp:.0f} yards from a hostile spawn's reach at best; up at the body")
        elif trapped or stronger:
            up = self._spirit_healer()
            if up is Recovered.ALIVE:
                self._revived(None)
                if self._home_near_work(state):
                    home = self._go_home()
                    self.say(f"  up at the Spirit Healer; hearthstone: {home.value} "
                             f"{self.hearth.detail}")
                    detail = f"up at the Spirit Healer; hearthstone {home.value}"
                else:
                    self.say("  up at the Spirit Healer; home is far from the guide's work: "
                             "walking on")
                    detail = "up at the Spirit Healer; walking on"
                self._wait_out_sickness()
                return self._result(up, detail)
            self.say(f"  the Spirit Healer did not raise us ({up.value}); back to the body, "
                     f"to get up {TRAP_RECLAIM_YARDS:.0f} yards short of it")
        elif camp:
            # Up there, and on: the hearthstone is kept for a wedge.
            up = self._spirit_healer()
            if up is Recovered.ALIVE:
                self._revived(None)
                self.say("  up at the Spirit Healer: the body lies in a death camp" if died_there
                         else f"  up at the Spirit Healer: the body lies in a camp, "
                              f"{room:.0f} yards from a hostile spawn at best")
                self._wait_out_sickness()
                return self._result(up, "up at the Spirit Healer; the body lies in a camp")
            self.say(f"  the Spirit Healer did not raise us ({up.value}); back to the body")
        # Always short of the body. Whatever killed the character stands beside it, back at
        # its spawn: the first reclaim at the body itself, at half health, died again four
        # times in runs 20260924T045140-ec8686 and ...050644-f9f9fa, and a new session
        # never knows the last one's revive. Recover reads painted corpse coordinates.
        # And a ghost the walk left out of the body's reach stops pressing there (V301).
        walk, reach = self.recover.walk_to, getattr(self.recover, "reach", None)
        self.recover.walk_to, self.recover.reach = self._short_of_body, self._in_reclaim_reach
        try:
            outcome = self.recover.run(self.recover.corpse or self._known_body())
        finally:
            self.recover.walk_to, self.recover.reach = walk, reach
        self._keep_graveyard()                      # a body not yet released releases here
        if outcome is Recovered.NO_CORPSE:
            # Nothing knows where the body lies: up at the Spirit Healer, sickness and all,
            # rather than a corpse run aborted at once at every look (V328). 35 ghosts the
            # server had released to a capital's graveyard, their bodies off its map, aborted
            # 858,365 corpse runs on 29 Sep, hive-240 for 27 hours.
            detail = self.recover.detail
            up = self._spirit_healer()
            if up is Recovered.ALIVE:
                self._revived(None)
                self.say("  up at the Spirit Healer: nothing knows where the body lies")
                self._wait_out_sickness()
                return self._result(up, "up at the Spirit Healer; where the body lies is unknown")
            return self._result(outcome, f"{detail}; the Spirit Healer did not raise it "
                                         f"({up.value}: {self.recover.detail})")
        if outcome is Recovered.ALIVE:
            self._revived(time.time())
            self._reclaim_yards = TRAP_RECLAIM_YARDS
        elif outcome is Recovered.STILL_GHOST:
            # Out of the body's reach from there, it seems: half as far short next time.
            self._reclaim_yards = self._reclaim_yards / 2 if self._reclaim_yards > 4 else 0.0
            self._over_body()
            self._note_unreached(self.recover.corpse or corpse)
        return self._result(outcome, self.recover.detail)

    def _in_reclaim_reach(self, ghost, corpse) -> bool | None:
        """Whether a ghost at map point `ghost` stands within the server's reach of its body at
        `corpse` on the map alone (`RECLAIM_REACH_YARDS`); `None` when either is off the map."""
        here, there = (map_to_world(*ghost, self.client.bounds),
                       map_to_world(*corpse, self.client.bounds))
        if here is None or there is None:
            return None
        return math.dist(here[:2], there[:2]) <= RECLAIM_REACH_YARDS

    def _on_map(self, point) -> tuple[int, float, float] | None:
        """A map point as (map, world x, world y), as the purse file keeps it: the map frame is
        the guide's, and the next guide may have another."""
        if point is None or None in point or self.client.bounds is None:
            return None
        world = map_to_world(*point, self.client.bounds)
        return None if world is None else (self.client.bounds.map_id, world[0], world[1])

    def _unreached_runs(self, corpse) -> int:
        """The corpse runs in a row that ended still a ghost at the body at map point `corpse`
        (V301): none for another body, or none read."""
        at = self._on_map(corpse)
        if self._unreached is None or at is None:
            return 0
        kept, runs = self._unreached
        same = kept[0] == at[0] and math.dist(kept[1:], at[1:]) <= SAME_BODY_YARDS
        return runs if same else 0

    def _note_unreached(self, corpse) -> None:
        """One more corpse run that ended still a ghost at the body at map point `corpse`."""
        at = self._on_map(corpse)
        if at is None:
            return
        self._unreached = (at, self._unreached_runs(corpse) + 1)
        self._save_purse()

    def _keep_graveyard(self) -> None:
        """Where the ghost appeared, kept for a session that begins as a ghost (V301)."""
        at = self._on_map(getattr(self.recover, "graveyard", None))
        if at is not None and at != self._graveyard:
            self._graveyard = at
            self._save_purse()

    def _kept_graveyard(self) -> None:
        """A ghost that did not see where it appeared gets up there by the last session's
        sight of it (V301): without it the Spirit Healer is not looked for."""
        kept, bounds = self._graveyard, self.client.bounds
        if (self.recover.graveyard is None and kept is not None and bounds is not None
                and kept[0] == bounds.map_id):
            self.recover.graveyard = world_to_map(kept[1], kept[2], bounds)

    def _spirit_healer(self) -> Recovered:
        """Up at the Spirit Healer where the ghost appeared (`Recover.run_spirit_healer`): seen,
        kept (V301), or, for a ghost that has neither, the graveyard the server sends a ghost of
        its side to from where it stands (`_server_graveyard`). With none the healer refuses at
        once: the two orcs drowned off Ratchet had released before V301 kept a graveyard, and
        each second corpse run over their bodies went to a healer that answered "no graveyard
        seen" in half a millisecond, and back to the body (the hive, 28 Sep 12:17-12:19)."""
        if self.recover.graveyard is None:
            self.recover.graveyard = self._server_graveyard()
        return self.recover.run_spirit_healer()

    def _server_graveyard(self) -> tuple[float, float] | None:
        """As a map point, the graveyard nearest the character that the server sends a ghost of
        its side to, among those serving the zone it stands in (`jev.world.graveyards`)."""
        bounds, here = self.client.bounds, self._position()
        at = map_to_world(*here, bounds) if here is not None and bounds is not None else None
        if at is None:
            return None
        zone = self._zone_here()
        found = graveyards.nearest(bounds.map_id, at[0], at[1], side=self._side,
                                   zone=zone.area_id if zone is not None else None)
        return world_to_map(found[0], found[1], bounds) if found is not None else None

    def _over_body(self) -> None:
        """A ghost over its body on the map and still a ghost is on another floor than it
        (V289): the next walk is to the body itself, from the next floor under the ghost, the
        floor the plans here started on counted as tried."""
        body, here, bounds = self.recover.corpse, self._position(), self.client.bounds
        if body is None or here is None or bounds is None:
            return
        body_w, here_w = map_to_world(*body, bounds), map_to_world(*here, bounds)
        if math.dist(body_w[:2], here_w[:2]) > OVER_BODY_YARDS:
            return
        self._reclaim_yards = 0.0
        self.say("  over the body and still a ghost: on another floor than the body")
        z = self._body_height(body_w)
        if getattr(self.client, "_ground", None) is None and z is not None:
            floor = self._floor_at(here_w, z)
            if floor is not None:
                self.client._ground = (here_w[0], here_w[1], floor)
        self.client._next_floor(around=z)

    def _revived(self, at: float | None) -> None:
        """When the character last got up at its body (wall time), kept in the purse file
        for the next session's recovery. Any get-up leaves no body unreached and no graveyard
        to keep (V301)."""
        self._revived_at = at
        self._unreached = self._graveyard = self._fell = None
        self._save_purse()

    def _killed_by_stronger(self, state) -> bool:
        """The last fight's unit was `OUTCLASSED_BY` levels or more above the character."""
        killer = getattr(self.fight, "_target_level", None)
        mine = getattr(getattr(state, "char", None), "level", None)
        return isinstance(killer, int) and isinstance(mine, int) and killer - mine >= OUTCLASSED_BY

    def _wait_out_sickness(self) -> float:
        """Stand still while resurrection sickness lasts, up to `SICKNESS_WAIT_MAX_S`; an
        attack ends the wait, and the fight is the policy's. The seconds waited."""
        level = (self._read() or {}).get("char.level")
        if not isinstance(level, int) or level <= SICKNESS_FROM_LEVEL:
            return 0.0
        wait = min(60.0 * min(level - SICKNESS_FROM_LEVEL, 10), SICKNESS_WAIT_MAX_S)
        self.say(f"  resurrection sickness: waiting {wait / 60:.0f} min before going on")
        started = time.monotonic()
        while time.monotonic() - started < wait:
            self.checkpoint()
            if (self._read() or {}).get("vitals.combat") is True:
                break
            time.sleep(1.0)
        return time.monotonic() - started

    def _release(self, state) -> Result:
        # Released where it died: walks keep clear of the spot for a while
        # (`route_memory.DangerAvoidingQuery`), at about the level it died at; a second death
        # near it within ten minutes, or one in a camp still held, makes the place a death
        # camp, left once up (V307). The death is read before the release and kept after it,
        # in memory at once and saved by the memory's one writer (V327): kept first, the
        # release waited on the hive's shared save, and the server released the character
        # itself six minutes after it died.
        memory, here = getattr(self.client, "route_memory", None), self._position()
        values = self._read() or {}
        record = None
        if (memory is not None and here is not None and values.get("vitals.dead") is True
                and values.get("vitals.ghost") is not True):
            level, who = values.get("char.level"), values.get("char.key")
            record = (self.client.bounds.map_id, map_to_world(*here, self.client.bounds),
                      level if isinstance(level, int) else None,
                      who if isinstance(who, int) else None)
        released = self.recover.run(release_only=True)
        self._keep_graveyard()
        fell = self._on_map(here) if values.get("vitals.dead") is True else None
        if fell is not None and fell != self._fell:
            self._fell = fell                     # as `observe` keeps it, should it have missed it
            self._save_purse()
        if record is not None:
            self._keep_death(memory, *record)
        return self._result(released, self.recover.detail)

    def _keep_death(self, memory, map_id: int, at, level: int | None, who: int | None) -> None:
        """Keep a death in the route memory: in memory at once, so a death camp it makes or
        falls in is known now, and saved by the memory's writer (`save_soon`, V327). A camp is
        made of one character's deaths; one another made, that counts at this level, is left
        as well (review of 28 Sep). A memory with no writer saves as it keeps."""
        soon = getattr(memory, "save_soon", None)
        try:
            death = (memory.died(map_id, at, level=level, who=who, save=False)
                     if soon is not None else memory.died(map_id, at, level=level, who=who))
            if soon is not None:
                soon()
            if (death.camp(time.time())
                    or memory.camp_at(map_id, at, time.time(), level) is not None):
                self.policy_context.camp_left(map_id, at[0], at[1])
                self.say("  died in a death camp: left once up")
        except Exception as exc:                # the release is made: a record never undoes it
            self.say(f"  the death was not kept: {type(exc).__name__}: {exc}")

    def end_session(self, timeout: float = RECORD_FLUSH_S) -> None:
        """What a session's end keeps: the deaths still being saved, waited for at most
        `timeout` (V327); the process's writer goes on with any left, and its exit waits for
        them again (`route_memory.EXIT_FLUSH_S`). And where the character last stood alive,
        for a next session begun as a ghost the game paints no body for (V328)."""
        self._save_purse()
        flush = getattr(getattr(self.client, "route_memory", None), "flush", None)
        if flush is not None and flush(timeout) is False:
            self.say(f"  deaths still being saved after {timeout:.0f} s: the writer goes on")

    def observe(self, state) -> None:
        """Each state the supervisor reads (V328): where the character stands while alive,
        and where it fell the moment a death is read, kept in the purse file at once, whatever
        the worker is doing and whoever releases the spirit. A ghost first read with no fall
        kept fell where it last stood alive. Up again, nothing is kept of the body."""
        vitals, pos = getattr(state, "vitals", None), getattr(state, "pos", None)
        if vitals is None or pos is None:
            return
        here = self._on_map((pos.mx, pos.my))
        if vitals.dead is True or vitals.ghost is True:
            self._place = None                         # a death begins the hunt afresh (V343)
        if vitals.dead is False and vitals.ghost is False:
            if here is not None:
                self._alive_at = here
            self.recover.corpse = None
            if self._fell is not None:
                self._fell = None
                self._save_purse()
        elif vitals.dead is True and vitals.ghost is not True:
            fell = here or self._alive_at              # the body lies where the character does
            if fell is not None and fell != self._fell:
                self._fell = fell
                self._save_purse()
        elif vitals.ghost is True and self._fell is None and self._alive_at is not None:
            self._fell = self._alive_at
            self._save_purse()

    def _known_body(self) -> tuple[float, float] | None:
        """Where the body lies when the game paints none, as a map point (V328): the server's
        word where the client has one (the hive's `corpse_world`, never the live client's),
        else where the character was seen to fall (`observe`), on this map."""
        bounds = self.client.bounds
        if bounds is None:
            return None
        server = getattr(self.client, "corpse_world", None)
        found = None
        if callable(server):
            with contextlib.suppress(Exception):
                found = server()
        for kept in (found, self._fell):
            if kept is not None and kept[0] == bounds.map_id:
                return world_to_map(kept[1], kept[2], bounds)
        return None

    def _wait(self, state) -> Result:
        return Result(SkillOutcome.SUCCEEDED)


def reclaim_spot(body: tuple[float, float], short: tuple[float, float], spawns,
                 reach: float, clear: float = REST_CLEAR_YARDS,
                 bearings: int = REST_BEARINGS) -> tuple[float, float]:
    """Where a ghost gets up (V233): `short`, the graveyard's side of the body, when it is
    `clear` yards from every spawn of the step's creatures; else the point `reach` yards from
    the body with the most room from them, the graveyard's side on a tie. The mage got up
    beside its body at Fargodeep with half its health and mana, among the Kobold Tunnelers
    that had killed it, and died again twice (sessions 196-197)."""
    spawns = [(sp[0], sp[1], _extra(sp)) for sp in spawns]
    if not spawns:
        return short

    def room(point) -> float:
        return min(math.dist(point, sp[:2]) - sp[2] for sp in spawns)

    if room(short) >= clear:
        return short
    ring = [(body[0] + reach * math.cos(2 * math.pi * i / bearings),
             body[1] + reach * math.sin(2 * math.pi * i / bearings)) for i in range(bearings)]
    best = max([short, *ring], key=lambda p: (round(room(p), 1), -math.dist(p, short)))
    return best


def _numbers(value, count: int) -> list[float] | None:
    """`value` as `count` numbers, as the purse file keeps them; `None` for anything else."""
    if (isinstance(value, list) and len(value) == count
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        return [float(v) for v in value]
    return None


def _extra(spawn) -> float:
    """The yards a spawn's unit strays beyond the ordinary (`jev.world.hostiles`, V255);
    none for a point of the step's own spawns, which carry no such fourth figure."""
    return float(spawn[3]) if len(spawn) > 3 else 0.0


def rest_spot(here: tuple[float, float], spawns, clear: float = REST_CLEAR_YARDS,
              rings: tuple[float, ...] = REST_RINGS, bearings: int = REST_BEARINGS):
    """The nearest point at least `clear` yards from every spawn, or `None` when `here`
    already is one or no ring finds one. World yards; the height is the nearest spawn's."""
    def clear_of(point) -> bool:
        return all(math.dist(point, s[:2]) >= clear + _extra(s) for s in spawns)

    if clear_of(here[:2]):
        return None
    for reach in rings:
        found = [(here[0] + reach * math.cos(2 * math.pi * i / bearings),
                  here[1] + reach * math.sin(2 * math.pi * i / bearings))
                 for i in range(bearings)]
        found = [p for p in found if clear_of(p)]
        if found:
            # Of the ring's clear points, the one with the most room.
            best = max(found, key=lambda p: min(math.dist(p, s[:2]) - _extra(s) for s in spawns))
            z = min(spawns, key=lambda s: math.dist(best, s[:2]))[2]
            return (best[0], best[1], z)
    return None
