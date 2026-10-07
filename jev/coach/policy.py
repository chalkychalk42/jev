"""The scripted coach — the day-0 policy, and the reason a subscription is survivable.

This is the floor under everything. It is a costed priority list over world flags
(PLAN §8.2), it is free, it is always available, and it **always returns a valid plan**.

That last property is the whole point, and `ARCHITECTURE.md` §0 states it as the invariant
this project depends on:

    The system makes forward progress with zero teacher calls.

The teacher is an improvement engine, never a dependency. If Claude is rate-limited, down,
or simply not configured, the character keeps levelling — more slowly and more stupidly,
but it keeps levelling. Any design where that is not true has made a remote service part
of the hot loop of a game client, and it will spend its demo apologising.

Two tiers, borrowed from the plan:

  * **Hard preempts** — safety. These do not wait for anybody and cannot be overridden by
    a plan that arrived three seconds ago about a situation that has since changed.
  * **Soft priority** — what to do when nothing is on fire:
    `safety > corpse > service > eat > loot > combat > the armed guide skill > grind`

`decide()` returns `(Decision, confident)`. When it is not confident, the caller may
escalate — but escalation is an *upgrade*, never a prerequisite. The decision returned is
always usable on its own.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.coach.schema import Decision, Intent
from jev.guide.graph import Node
from jev.skills.catalog import NAMES
from jev.world.combat import HEAL_OUT_OF_COMBAT, is_caster, rest_mana
from jev.world.state_v1 import PowerType, State, StepKind
from jev.world.vendor import supply_prices

# Below this, a decision is worth a teacher call if one is affordable. Above it, asking
# would be spending a rate-limited resource on something already known.
CONFIDENT = 0.6

# Reflexes: the rules for a fight the character is already in, and for death. Their
# routine runs at once and is never put to a tutor (a thirty-second deadline is a death in
# a fight - run 20260923T172056-54f4d5), and losing one is a death, which the tracker
# counts against its step, not a failed attempt at that step.
REFLEX_RULES = ("fight.", "preempt.dead", "preempt.ghost", "preempt.critical")


def own_rule(rule: str) -> str:
    """The coach's rule behind an arm, whoever picked it: Jev's picks are the coach's own
    plans, armed as "jev:<rule>" (`jev.coach.judge`)."""
    return rule[4:] if rule.startswith("jev:") else rule


def reflex(rule: str) -> bool:
    return own_rule(rule).startswith(REFLEX_RULES)


# Services only a routine can do, never put to the tutor: training ends in drags from the
# spellbook onto the bar, which the tutor has no control for, and binding the hearthstone
# answers a confirmation the tutor has no row for either. A meal between fights is the
# routine's too: it was a third of the tutor's decisions in teach mode (38 of 112 over five
# sessions), its choices do not separate in any state a student sees (it ate at a median
# 57% health and walked about at 71-80%), and walking about took it off its route - session
# 93's walk back from a mine grew from 1,070 yards to 1,169 and its hand-in failed over.
ROUTINE_RULES = ("service.train", "service.bind", "service.discover", "recover.eat")


def routine_only(rule: str) -> bool:
    return own_rule(rule).startswith(ROUTINE_RULES)


# A repair, a restock or a bag service that could not be done on a step is not asked again on
# it (V175, V185, V309) until the playhead leaves the step, a service of its kind is done, or
# this long has passed, as a trainer not reached is asked again after half an hour (V254). The
# bar is kept in the purse file, and a grind rib is shared and revisited: held for the step
# alone, one timed-out sale on a rib barred every later visit to it, in every session, full
# bags and gear worn to nothing (review of 28 Sep). Neris's five sessions on one bag service
# (13:25-14:10 on 28 Sep) were five tries in 45 minutes, each ending its session; at most one
# a half hour, a failed one ending none (V309), costs a timeout.
UNREACHABLE_RETRY_S = 1800.0
# The services barred so, by skill, and the name of each one's bar in `Context`.
UNREACHABLE_KINDS = {"VENDOR_REPAIR": "repair", "BUY_AMMO_REAGENT_FOOD": "supplies",
                     "BAG_MAKE_SPACE": "bags"}
# A service that failed - aborted or timed out, not cut short - is armed again by no service
# rule within this many seconds (wall time), on any step: a floor under every bar above,
# which each keys by a step and so holds only while the step it names is the one the policy
# reads (V315). BIND_HEARTH was armed 40,963 times in the hive from 20:00 to 23:20 on 28 Sep,
# 815 times in one run of hive-477: its inn behind a death camp, the walk was refused at once
# (V307), the skill aborted in 0.25 s, and its bar, set on a step no rule reads, let it be
# armed again 0.5 s later. Not keyed by the step, which is what failed to agree.
SERVICE_RETRY_MIN_S = 60.0
# Each failure more in a row, since the skill last succeeded, doubles that wait, to at most
# `SERVICE_RETRY_MAX_S` (V322). A flat minute let a service no walk could cure be armed for as
# long as its need lasted: with no friendly plate drawn from 27 Sep 22:56 (V320) and the gear
# broken throughout, VENDOR_REPAIR was armed 57 times in sessions 387-426, 66 minutes, BAG_MAKE_
# SPACE 20 and BIND_HEARTH 21, none done; and the walks were where the mage died, 13 of its 29
# deaths in fights begun on them. Kept in the purse file, as a 15-minute session would forget it.
SERVICE_RETRY_MAX_S = 1800.0
# A guide step whose hunt found every station in or behind a death camp (V307) is not armed
# again until the first such camp ends (V334), as wall time, by step: the rest of the policy -
# a fight, a service, a meal - goes on, and the step's grind waits. Bot 224, its only road
# out of the Sepulcher through a camp of its own deaths, armed its grind again 0.5 s after
# each one failed, 4,879 walks refused in one session (29 Sep 15:10-15:15). No wait is kept
# longer than `STEP_WAIT_MAX_S` ahead, against a wall clock set back (the WSL clock, 28 Sep).
STEP_WAIT_MAX_S = 7200.0
# A guide step whose hunt or walk planned no route at all - the character never moved - waits
# `STEP_RETRY_MIN_S` before it is armed again, twice as long for each such try in a row, to at
# most `STEP_RETRY_MAX_S` (V335), as a failed service does (V322); a try that moves the
# character ends the run. After V325 made refusals instant, bot 224's hunt that planned none
# was armed again a median 0.6 s after the last ended, 204 times in a row, and bot 217's 124
# (29 Sep 15:10-15:15).
# The longest resurrection sickness lasts (level 20: ten minutes), so a sickness kept further
# ahead than this is a clock set back, and none (V379).
SICK_MAX_S = 600.0
# While sick, a repair is due from this worst durability (V379): the get-up took 25% from all.
SICK_REPAIR_BELOW = 0.9
STEP_RETRY_MIN_S = 60.0
STEP_RETRY_MAX_S = 1800.0
# A death skill that failed - aborted or timed out - is armed again at once the first time,
# then `DEATH_RETRY_MIN_S` after each failure more in a row, doubled each time, to at most
# `DEATH_RETRY_MAX_S`; the character up again, or the skill done, it starts afresh (V328).
# Nothing between the arms changes what a ghost knows: a ghost with no body known aborted
# CORPSE_RUN 858,365 times on 29 Sep, 35 characters, each about 1,797 times in a 15-minute
# session, hive-240 for 27 hours. A death waits on its own skill's bar alone.
DEATH_RETRY_MIN_S = 15.0
DEATH_RETRY_MAX_S = 300.0
DEATH_SKILLS = ("RELEASE_SPIRIT", "CORPSE_RUN")
# The repair reserve (V393): what the purse keeps for a repair out of every purchase - food and
# drink, ammunition, a bag - as it keeps the trainer's due (V215): about what a full repair of
# the kit its class wears costs from nothing, copper times the level squared by class (an
# item's price a point of durability grows with its item level, and so do its points). Priced
# on the DBCs (DurabilityCosts by item level and subclass, DurabilityQuality by quality), the
# kits worn by the hive's 478 characters of levels 1-20 (the character DB, 7 Oct 04:40) cost a
# median 141, 350 and 630 copper from nothing at levels 6-10, 11-15 and 16-20, the warriors',
# paladins', rogues' and hunters' 189, 407 and 892; a class's kit the level squared times its
# median below (30-59 characters each, the druids 12). 35.5% of the hive's deaths (7 Oct
# 01:25-03:50) were warriors', paladins', rogues' and hunters' with broken gear, broken about
# half their alive time with a median 16-30 copper in the purse.
REPAIR_RESERVE_PER_LEVEL2 = {"warrior": 3.0, "paladin": 2.6, "hunter": 0.8, "rogue": 2.5,
                             "priest": 1.6, "shaman": 2.6, "mage": 1.2, "warlock": 1.5,
                             "druid": 1.5}
REPAIR_RESERVE_UNREAD = 2.5          # a class not read: a rogue's, the melee kits' median
# Broken: the worst item at this or under. And under `REPAIR_NEAR_BELOW` the gear is mended
# wherever a repairer is a short detour (V393, `LiveBody.repairer_near`).
BROKEN = 0.05
REPAIR_NEAR_BELOW = 0.60
# A character whose weapon is broken takes no pull while its repair waits on a retry at most
# this far off (V393): it stands, fighting what attacks it, and the repair is armed when the
# retry comes. Further off - too poor with the bags sold, a repairer out of reach, a long
# back-off - it pulls as before: with no pull there is no loot, no copper and no repair
# (`Fight.run`'s broken-gear note).
BROKEN_STAND_S = 60.0


def repair_reserve(level: int | None, cls: str | None = None) -> int:
    """What the purse keeps for a repair at `level` (V393): the class's
    `REPAIR_RESERVE_PER_LEVEL2` times the level squared; nothing for a level not read."""
    if not isinstance(level, int) or isinstance(level, bool) or level < 1:
        return 0
    return round(REPAIR_RESERVE_PER_LEVEL2.get(cls or "", REPAIR_RESERVE_UNREAD) * level * level)


@dataclass(frozen=True)
class Plan:
    decision: Decision
    confident: bool
    rule: str          # which priority fired, so the log says why rather than what


@dataclass
class Context:
    """Measured service outcomes that remain true between observations."""

    repair_blocked: bool = False
    repair_money: int | None = None
    supplies_blocked: bool = False
    supplies_money: int | None = None
    # Called when a purse lesson is learned or cleared (`PURSE`), to keep it for the
    # character's next session (V206).
    saved: Callable[[], None] | None = None

    def repair_failed(self, money: int | None) -> None:
        self.repair_blocked, self.repair_money = True, money
        self._save()

    # When each service last failed, by skill (`SERVICE_RETRY_MIN_S`, V315), and how many
    # times in a row since it last succeeded (V322).
    service_failed_at: dict[str, float] = field(default_factory=dict)
    service_failures: dict[str, int] = field(default_factory=dict)

    # Resurrection sickness after a Spirit Healer get-up, as wall time (V379): the character
    # stood it out, up to five minutes (`SICKNESS_WAIT_MAX_S`), half of the hive's dead time (6 Oct
    # 13:00-17:00: 74.6 of 148.2 h). Sickness matters only to a fight: meanwhile the services
    # come due early (a repair the get-up's 25% made worth it, a sale, a trainer), hand-ins and
    # walks go on, and only the pulls of a grind or a kill objective wait for it to end.
    sick_until: float = 0.0

    def sick(self, now: float) -> float:
        """Seconds of resurrection sickness left at `now`; none past `SICK_MAX_S` ahead (a
        wall clock set back) and none once it has ended."""
        left = self.sick_until - now
        return left if 0.0 < left <= SICK_MAX_S else 0.0

    def service_failed(self, skill: str, now: float) -> None:
        self.service_failed_at[skill] = now
        self.service_failures[skill] = self.service_failures.get(skill, 0) + 1
        self._save()

    def service_wait(self, skill: str | None) -> float:
        """How long after its last failure `skill` waits: `SERVICE_RETRY_MIN_S`, doubled for
        each failure more in a row, at most `SERVICE_RETRY_MAX_S` (V322)."""
        failures = self.service_failures.get(skill or "", 0)
        return min(SERVICE_RETRY_MIN_S * 2 ** max(0, min(failures, 16) - 1), SERVICE_RETRY_MAX_S)

    def failed_lately(self, skill: str | None, now: float) -> bool:
        """Did `skill` fail within its wait (`service_wait`) of `now`? Either way round: a wall
        clock set back (the WSL clock, 28 Sep) does not hold a service off for good."""
        at = self.service_failed_at.get(skill or "")
        return at is not None and abs(now - at) < self.service_wait(skill)

    # Whether a rib lies wholly in a death camp counting at a level (`LiveBody.rib_camped`):
    # one that does is no rib to wait a step out on (`ClientRuntime._wait_elsewhere`, V334).
    camped: Callable[..., bool] | None = None
    # When each guide step may be armed again, as wall time (V334), and why it waits; and how
    # many tries at it in a row planned no route (V335).
    step_wait_until: dict[str, float] = field(default_factory=dict)
    step_wait_why: dict[str, str] = field(default_factory=dict)
    step_failures: dict[str, int] = field(default_factory=dict)

    def step_stuck(self, step_id: str | None, why: str, now: float) -> float:
        """A try at `step_id` planned no route (V335): it waits `STEP_RETRY_MIN_S`, doubled for
        each such try in a row, at most `STEP_RETRY_MAX_S`. The seconds it waits."""
        if not step_id:
            return 0.0
        failures = self.step_failures[step_id] = self.step_failures.get(step_id, 0) + 1
        wait = min(STEP_RETRY_MIN_S * 2 ** max(0, min(failures, 16) - 1), STEP_RETRY_MAX_S)
        self.step_waits(step_id, now + wait, why, now)
        self._save()
        return wait

    def step_moved(self, step_id: str | None) -> None:
        """A try at `step_id` walked somewhere: its run of tries that could not ends (V335)."""
        if step_id and self.step_failures.pop(step_id, None) is not None:
            self._save()

    def step_waits(self, step_id: str | None, until: float, why: str, now: float) -> None:
        """Hold `step_id` until `until` (wall time), the later of this and any wait it has."""
        if not step_id:
            return
        until = min(until, now + STEP_WAIT_MAX_S)
        if until <= self.step_wait_until.get(step_id, -math.inf):
            return
        self.step_wait_until = {k: v for k, v in self.step_wait_until.items()
                                if 0.0 < v - now <= STEP_WAIT_MAX_S}
        self.step_wait_why = {k: v for k, v in self.step_wait_why.items()
                              if k in self.step_wait_until}
        self.step_wait_until[step_id], self.step_wait_why[step_id] = until, why
        self._save()

    def step_waiting(self, step_id: str | None, now: float) -> float | None:
        """Seconds `step_id` still waits at `now`, or `None`; a wait that would end further
        off than `STEP_WAIT_MAX_S` is a clock set back, and none."""
        until = self.step_wait_until.get(step_id or "")
        if until is None:
            return None
        left = until - now
        return left if 0.0 < left <= STEP_WAIT_MAX_S else None
    # When each death skill last failed, and how many times in a row (V328).
    death_failed_at: dict[str, float] = field(default_factory=dict)
    death_failures: dict[str, int] = field(default_factory=dict)

    def death_failed(self, skill: str, now: float) -> None:
        self.death_failed_at[skill] = now
        self.death_failures[skill] = self.death_failures.get(skill, 0) + 1

    def death_done(self, skill: str | None = None) -> None:
        """A death skill done, or (no skill) the character up again: it starts afresh."""
        for kept in (self.death_failed_at, self.death_failures):
            if skill is None:
                kept.clear()
            else:
                kept.pop(skill, None)

    def death_wait(self, skill: str) -> float:
        """How long after its last failure a death skill waits (V328): none after the first
        in a row, then `DEATH_RETRY_MIN_S` doubled for each more, at most `DEATH_RETRY_MAX_S`."""
        failures = self.death_failures.get(skill, 0)
        if failures < 2:
            return 0.0
        return min(DEATH_RETRY_MIN_S * 2 ** min(failures - 2, 16), DEATH_RETRY_MAX_S)

    def death_waiting(self, skill: str, now: float) -> bool:
        """Is `skill` within its wait of its last failure? Either way round, as a service is
        (`failed_lately`): a wall clock set back holds no ghost for good."""
        at = self.death_failed_at.get(skill)
        return at is not None and abs(now - at) < self.death_wait(skill)

    fight_unengaged: int = 0
    fight_paused_until: float = 0.0

    def fight_ended(self, code: str | None, now: float) -> None:
        """Count fights that never engaged; `UNENGAGED_FIGHTS` in a row pause combat (V212)."""
        if code not in UNENGAGED_CODES:
            self.fight_unengaged = 0
            return
        self.fight_unengaged += 1
        if self.fight_unengaged >= UNENGAGED_FIGHTS:
            self.fight_unengaged = 0
            self.fight_paused_until = now + FIGHT_PAUSE_S

    def fight_paused(self, now: float) -> bool:
        return now < self.fight_paused_until

    def repaired(self) -> None:
        """A repair that landed: the purse pays for repairs again."""
        if self.repair_blocked or self.repair_free is not None:
            self.repair_blocked, self.repair_money, self.repair_free = False, None, None
            self._save()

    # A repairer out of reach is not walked to again on this step, as a merchant for
    # supplies is not (V175, V185): the walk out of Sentinel Hill's inn to William MacGregor
    # stuck in its doorway, and one failed repair stopped session 144.
    # Until the step is left, a repair is made or `UNREACHABLE_RETRY_S` (wall time) passes.
    repair_unreachable_step: str | None = None
    repair_unreachable_until: int | None = None

    def repair_unreachable(self, step_id: str | None, now: float | None = None) -> None:
        self._unreachable("repair", step_id, now)

    def _unreachable(self, kind: str, step_id: str | None, now: float | None) -> None:
        setattr(self, f"{kind}_unreachable_step", step_id)
        setattr(self, f"{kind}_unreachable_until",
                None if now is None else int(now + UNREACHABLE_RETRY_S))
        self._save()

    def _barred(self, kind: str, step_id: str | None, now: float | None) -> bool:
        """Is `kind`'s service barred on `step_id` at `now`? With no `now`, or no time kept
        with the bar, it stands until the step is left or a service of its kind is done."""
        until = getattr(self, f"{kind}_unreachable_until")
        return (step_id is not None and step_id == getattr(self, f"{kind}_unreachable_step")
                and (now is None or until is None or now < until))

    def _clear_unreachable(self, kind: str) -> bool:
        if getattr(self, f"{kind}_unreachable_step") is None:
            return False
        setattr(self, f"{kind}_unreachable_step", None)
        setattr(self, f"{kind}_unreachable_until", None)
        return True

    def served(self, skill: str) -> None:
        """A service done: a bar on its kind is lifted, a merchant or a smith reached, and its
        failures in a row are over (V322)."""
        kind = UNREACHABLE_KINDS.get(skill)
        cleared = kind is not None and self._clear_unreachable(kind)
        if self.service_failures.pop(skill, None) is not None:
            self.service_failed_at.pop(skill, None)
            cleared = True
        if cleared:
            self._save()

    def on_step(self, step_id: str | None) -> None:
        """The playhead's step: a bar on another step is lifted, as the playhead has left it."""
        if any([self._clear_unreachable(kind) for kind in UNREACHABLE_KINDS.values()
                if getattr(self, f"{kind}_unreachable_step") not in (None, step_id)]):
            self._save()

    # The free bag slots when a repair the purse could not pay was given up, its bags' goods
    # sold for it (`LiveBody._repair`, V393): fewer free since is more to sell.
    repair_free: int | None = None

    def repair_short(self, free: int | None) -> None:
        self.repair_free = free if isinstance(free, int) and not isinstance(free, bool) else None

    def can_repair(self, money: int | None, step_id: str | None = None,
                   now: float | None = None, free: int | None = None) -> bool:
        if self._barred("repair", step_id, now):
            return False
        if not self.repair_blocked:
            return True
        # A repair the purse could not pay, the bags' goods sold for it at the repairer
        # (`LiveBody._repair`), is asked again once the purse holds more than it did then or
        # the bags more to sell (fewer slots free), its failures in a row spacing the walks
        # (V322): the server mends item by item as the purse pays (`DurabilityRepairAll`), so
        # a little more mends a little more. Not at a doubled purse, or 100 copper more (V196,
        # amended by V393): 77 of the hive's 366 purse files held a repair so blocked on 7 Oct,
        # 58 failed with 10 copper or less, and of the 88 characters of levels 6-20 whose
        # weapon was broken, 23 could pay to mend it from the purse and 69 with their bags'
        # junk and surplus sold. An unknown purse and unknown bags establish nothing.
        grew = money is not None and self.repair_money is not None and money > self.repair_money
        filled = free is not None and self.repair_free is not None and free < self.repair_free
        return grew or filled

    def repair_retry_in(self, step_id: str | None, now: float) -> float | None:
        """Seconds until a repair held off by its failures in a row (V315, V322) or by a
        repairer out of reach on this step (V185) may be armed again; `None` when neither
        holds it (V393)."""
        waits = []
        at = self.service_failed_at.get("VENDOR_REPAIR")
        if at is not None and abs(now - at) < self.service_wait("VENDOR_REPAIR"):
            waits.append(max(0.0, at + self.service_wait("VENDOR_REPAIR") - now))
        if self._barred("repair", step_id, now):
            until = self.repair_unreachable_until
            waits.append(math.inf if until is None else max(0.0, until - now))
        return max(waits) if waits else None

    def supplies_failed(self, money: int | None) -> None:
        self.supplies_blocked, self.supplies_money = True, money
        self._save()

    def restocked(self) -> None:
        """Supplies bought: the purse pays for them again."""
        if self.supplies_blocked or self.supplies_needed is not None:
            self.supplies_blocked, self.supplies_money, self.supplies_needed = False, None, None
            self._save()

    # What the purchase that could not be paid needed, when the merchant said (V186): a
    # level 1 mage out of water, too poor to buy more, walked 100 to 150 yards to Northshire's
    # merchant and back after every kill, each copper looted making the purse "more" than
    # at the failure (the mage's check, 26 September).
    supplies_needed: int | None = None

    def supplies_need(self, copper: int | None) -> None:
        self.supplies_needed = copper
        self._save()

    # What a session learns of the purse is true of the next: each new session forgot it,
    # and the level 4 mage's first act every session was its hearthstone and a walk to a
    # smith it still could not pay, 45 copper against broken gear (26 September, V206). So is
    # a service that could not be done on a step (V309), as a trainer not reached is (V254):
    # Neris, a level 4 night elf, ended five sessions in a row from 13:25 to 14:10 on 28 Sep
    # on the same bag service on the same step, each timed out.
    PURSE = ("repair_blocked", "repair_money", "repair_free", "supplies_blocked",
             "supplies_money", "supplies_needed", "train_blocked_level", "train_blocked_until",
             "repair_unreachable_step", "supplies_unreachable_step", "bags_unreachable_step",
             "repair_unreachable_until", "supplies_unreachable_until", "bags_unreachable_until",
             "service_failed_at", "service_failures", "step_wait_until", "step_wait_why",
             "step_failures", "sick_until")

    def purse(self) -> dict:
        return {name: (dict(value) if isinstance(value := getattr(self, name), dict) else value)
                for name in self.PURSE}

    def restore_purse(self, raw: dict) -> None:
        for name in self.PURSE:
            value = raw.get(name)
            if name == "sick_until":
                self.sick_until = (float(value) if isinstance(value, (int, float))
                                   and not isinstance(value, bool) else 0.0)
            elif name == "step_wait_why":
                kept = value if isinstance(value, dict) else {}
                self.step_wait_why = {str(k): v for k, v in kept.items() if isinstance(v, str)}
            elif name.startswith(("service_", "step_")):
                kept = value if isinstance(value, dict) else {}
                number = int if name in ("service_failures", "step_failures") else float
                setattr(self, name, {str(k): number(v) for k, v in kept.items()
                                     if isinstance(v, (int, float)) and not isinstance(v, bool)})
            elif name.endswith("blocked"):
                setattr(self, name, value is True)
            elif name.endswith("_step"):
                setattr(self, name, value if isinstance(value, str) else None)
            else:
                setattr(self, name, value if isinstance(value, int) and not isinstance(value, bool)
                        else None)

    def _save(self) -> None:
        if self.saved is not None:
            self.saved()

    # A merchant out of reach (the walk or the talk timed out) is not walked to again on
    # this step, as a trainer is not (V175): Goldshire's innkeeper, upstairs of whom the
    # walk kept ending, stopped two sessions at T-0.
    supplies_unreachable_step: str | None = None
    supplies_unreachable_until: int | None = None

    def supplies_unreachable(self, step_id: str | None, now: float | None = None) -> None:
        self._unreachable("supplies", step_id, now)

    # A supply whose nearest merchant is beyond the walk's cap (V205), or which no merchant in
    # the zone sells (V292), is noted once a session and not asked for again in it (V302): the
    # bar on the step (V175) held for that step alone, and in the hive's two hours to 11:11 on
    # 28 Sep the purchase came back so 298 times, a level 8 draenei asking every few minutes,
    # at each new step, death and session, for Caregiver Breel 900 to 3,400 yards off. A
    # character out of all it eats and drinks with its gear broken (`stranded`) is asked past a
    # merchant too far: for it the cap does not hold.
    supplies_noted: str | None = None

    def supplies_out_of_reach(self, code: str) -> None:
        self.supplies_noted = code

    def can_restock(self, money: int | None, step_id: str | None = None, *,
                    stranded: bool = False, now: float | None = None) -> bool:
        if self._barred("supplies", step_id, now):
            return False
        if self.supplies_noted == "no_supplier" or (self.supplies_noted == "too_far"
                                                    and not stranded):
            return False
        if not self.supplies_blocked:
            return True
        if money is not None and self.supplies_needed is not None:
            return money >= self.supplies_needed
        return money is not None and self.supplies_money is not None and money > self.supplies_money

    bags_blocked: bool = False
    # The fewest free slots since a merchant visit found nothing to sell, and whether one
    # has freed since: a slot freed and filled again may hold something a merchant buys.
    bags_blocked_at: int = 0
    bags_freed: bool = False

    def bags_failed(self, free: int | None = None) -> None:
        self.bags_blocked, self.bags_blocked_at, self.bags_freed = True, free or 0, False

    # A bag service that could not be done on a step - timed out, or no merchant reached - is
    # not asked for again on it, as a repair or a restock is not (V175, V185, V309).
    bags_unreachable_step: str | None = None
    bags_unreachable_until: int | None = None

    def bags_unreachable(self, step_id: str | None, now: float | None = None) -> None:
        self._unreachable("bags", step_id, now)

    def can_make_space(self, free: int | None, step_id: str | None = None,
                       now: float | None = None) -> bool:
        if self._barred("bags", step_id, now):
            return False
        # Bags with nothing a merchant may buy: another visit changes nothing until something
        # new is in them, and a run asking again stops on it.
        if self.bags_blocked and free is not None:
            if free > BAGS_LOW or (self.bags_freed and free <= self.bags_blocked_at):
                self.bags_blocked = False
            elif free > self.bags_blocked_at:
                self.bags_freed = True
            elif not self.bags_freed:
                self.bags_blocked_at = free
        return not self.bags_blocked

    # Whether a class trainer has something to teach that the purse can pay for, from
    # the spellbook census and the trainer catalog (`LiveBody.trainable`). Absent, no
    # training is ever asked for.
    trainable: Callable[[State], bool] | None = None
    # What the purse keeps for the trainer while there are spells to learn in reach: the
    # least that makes a visit due (`LiveBody.training_reserve`, V215). A restock buys only
    # with what is above it. Absent, nothing is kept.
    reserve: Callable[[State], int] | None = None
    # What the character makes for itself ("drink", "food"): a caster's conjures, which
    # a restock need not buy (`LiveBody.conjured_roles`, V166).
    conjures: Callable[[], frozenset[str]] | None = None
    # A caster's measured mana line (`Fight.mana_line`, V170): `None` keeps the fixed one.
    mana_line: Callable[[], float | None] | None = None

    def conjured(self) -> frozenset[str]:
        try:
            return self.conjures() if self.conjures is not None else frozenset()
        except Exception:
            return frozenset()

    def kept(self, state: State) -> int:
        """What a purchase leaves in the purse: the trainer's due (V215) or the repair reserve
        (`repair_reserve`, V393), whichever is more - either may be the next service, and the
        same copper pays for it."""
        try:
            training = max(0, int(self.reserve(state))) if self.reserve is not None else 0
        except Exception:
            training = 0
        return max(training, repair_reserve(state.char.level, state.char.cls))

    # Whether a repairer stands a short detour off the character's way to the guide's step
    # (`LiveBody.repairer_near`, V393). Absent, none does.
    repairer_near: Callable[[State], bool] | None = None
    # Whether the character's weapon is broken: its weapon blows unusable with their cost paid,
    # as the strip's `bars.usable` says (`LiveBody.disarmed`, V393). Absent, it is not.
    disarmed: Callable[[State], bool] | None = None
    # What a purchase of ammunition costs, while a hunter's runs low (`LiveBody.ammo_low`,
    # V393); `None` when none is wanted. Absent, none ever is.
    ammo_low: Callable[[State], int | None] | None = None

    def _asked(self, name: str, state: State, default):
        hook = getattr(self, name)
        try:
            return hook(state) if hook is not None else default
        except Exception:
            return default
    train_blocked_level: int | None = None
    # A trainer not reached is asked for again from this wall time; a visit made, only at the
    # next level (`None`). Both kept in the purse file (V254): the level 7 mage's walk to its
    # trainer failed the same way in sessions 205, 206, 207 and 208, each session a new try.
    train_blocked_until: int | None = None

    def train_failed(self, level: int | None, *, retry_at: float | None = None) -> None:
        # One visit a level: the next level brings new spells, and a trainer that could not
        # be reached, or would not teach what it lists, may from there.
        self.train_blocked_level = level
        self.train_blocked_until = None if retry_at is None else int(retry_at)
        self._save()

    def can_train(self, state: State) -> bool:
        level = state.char.level
        if self.trainable is None or level is None:
            return False
        if level == self.train_blocked_level and (self.train_blocked_until is None
                                                  or state.t < self.train_blocked_until):
            return False
        return self.trainable(state)

    # Whether an inn stands near the guide's work while home is far or unknown
    # (`LiveBody.bindable`). Absent, the hearthstone is never bound.
    bindable: Callable[[State], bool] | None = None
    bind_blocked: tuple[str | None] | None = None      # (step,) once a bind failed there

    def bind_failed(self, step_id: str | None) -> None:
        # One try a step: the next step may be near another inn, or this one reachable.
        self.bind_blocked = (step_id,)

    def can_bind(self, state: State) -> bool:
        if self.bindable is None or self.bind_blocked == (state.guide.step_id,):
            return False
        return self.bindable(state)

    # Whether an unvisited flight master stands near the character (`LiveBody.discoverable`).
    discoverable: Callable[[State], bool] | None = None
    discover_blocked: tuple[str | None] | None = None  # (step,) once a visit failed there

    def discover_failed(self, step_id: str | None) -> None:
        self.discover_blocked = (step_id,)

    def can_discover(self, state: State) -> bool:
        if self.discoverable is None or self.discover_blocked == (state.guide.step_id,):
            return False
        return self.discoverable(state)

    # A death that made or fell in a death camp (V307), as (map, world x, world y): the runtime
    # leaves it for the grind of the character's level once it is up (`ClientRuntime`), and no
    # service is armed until the walk there is made (`leaving_until`, wall time). After each of
    # Merany's first three deaths at one spot by Raven Hill's graveyard, its walk to a repairer
    # went back through it (the hive, 28 Sep 12:15-12:23).
    death_camp: tuple[int, float, float] | None = None
    leaving_until: float | None = None
    # When that camp ends, as wall time (`Danger.camp_until`): the rib it lies on is barred until
    # then (V391); `None`, not said.
    death_camp_until: float | None = None

    def camp_left(self, map_id: int, x: float, y: float, until: float | None = None) -> None:
        self.death_camp, self.death_camp_until = (map_id, x, y), until

    def leaving(self, now: float) -> bool:
        return self.leaving_until is not None and now < self.leaving_until


def _d(intent: Intent, skill: str | None, why: str, confidence: float,
       abort_if: tuple[str, ...] = ("dead",), goal: str = "", **params) -> Decision:
    assert skill is None or skill in NAMES, f"{skill} is not in the catalog"
    return Decision(
        goal=goal or f"{intent.value}:{skill or 'none'}",
        intent=intent, skill=skill, params=dict(params),
        abort_if=list(abort_if), confidence=confidence, why=why,
    )


# --------------------------------------------------------------------------- preempts


def preempt(state: State, context: Context | None = None) -> Plan | None:
    """Safety. Ordered by how quickly ignoring it ends the run.

    Every test here is `is True`, never truthiness: unknown is not an emergency, and
    treating a blank reading as "dead" would have the character releasing its spirit every
    time a loading screen blanked the radio. A death skill that failed again waits its
    turn (`Context.death_waiting`, V328), the character standing still meanwhile.
    """
    v, f = state.vitals, state.flags

    if v.ghost is True:
        if context is not None and context.death_waiting("CORPSE_RUN", state.t):
            return Plan(_d(Intent.WAIT, "IDLE", "ghost; the corpse run failed again: waiting "
                           "before the next", 0.95, ("alive",)), True, "preempt.ghost.wait")
        return Plan(_d(Intent.SERVICE, "CORPSE_RUN", "ghost; walk back to the body", 0.95,
                       ("alive",)), True, "preempt.ghost")

    if v.dead is True:
        if context is not None and context.death_waiting("RELEASE_SPIRIT", state.t):
            return Plan(_d(Intent.WAIT, "IDLE", "dead; the release failed again: waiting "
                           "before the next", 0.95, ("ghost",)), True, "preempt.dead.wait")
        return Plan(_d(Intent.SERVICE, "RELEASE_SPIRIT", "dead; release", 0.95,
                       ("ghost",)), True, "preempt.dead")

    if state.ui.modal is True:
        # The world is obstructed. Moving while a dialog covers it is how a character
        # walks into a lake.
        return Plan(_d(Intent.WAIT, "ABORT_WAIT", "a dialog is covering the world", 0.9,
                       ("no_modal",)), True, "preempt.modal")

    if f.falling is True:
        return Plan(_d(Intent.WAIT, "IDLE", "falling; do not steer", 0.8,
                       ("landed",)), True, "preempt.falling")

    if v.hp is not None and v.hp < 0.20 and v.combat is True:
        return Plan(_d(Intent.SERVICE, "COMBAT_PROFILE", "critical in combat; panic branch",
                       0.8, ("dead", "hp>0.6"), profile="panic"), True, "preempt.critical")

    if state.ui.loot is True:
        # Short, and above combat: an open loot window blocks other interaction, so
        # leaving it open stalls everything behind it.
        return Plan(_d(Intent.SERVICE, "LOOT", "loot window is open", 0.9,
                       ("dead", "no_loot_window")), True, "preempt.loot")

    return None


# Free bag slots at which the merchant is visited. Not none: every fight on the way there
# drops loot, and with full bags session 81 left four kills unlooted on its way to one.
BAGS_LOW = 2
# Fights that ended without ever engaging - nothing faced, nothing found, nothing reached. After
# `UNENGAGED_FIGHTS` of them in a row, combat does not take the floor for `FIGHT_PAUSE_S`, and
# the step's walk carries the character out of reach of what it cannot reach (V212). A Defias
# Smuggler at 6% health threw knives from out of sight while the paladin turned and healed,
# 74 and 41 fights "not visible" in two sessions, 45 minutes (sessions 173-175).
UNENGAGED_CODES = frozenset({"not_visible", "no_target", "unreachable"})
UNENGAGED_FIGHTS = 3
FIGHT_PAUSE_S = 30.0

# --------------------------------------------------------------------------- soft tier


def _affordable(item_id: int, money: int | None) -> bool:
    """Is one purchase of this food or drink in the purse? A purse or price not known is not
    a reason to stay (V195). A level 2 mage with 10 copper and water at 25 walked from
    Northshire to Goldshire's merchants and on for it, 2,000 yards (the mage's third check)."""
    price = supply_prices().get(item_id)
    return money is None or not price or money >= price


def service(state: State, *, context: Context | None = None) -> Plan | None:
    """The service priority, shared by idle selection and long-skill handoff."""
    plans = services(state, context=context)
    return plans[0] if plans else None


def services(state: State, *, context: Context | None = None) -> list[Plan]:
    """Every service due now, the most urgent first (`service` is the first): what the coach's
    model chooses among (`jev.coach.judge`), where the floor takes only the first."""
    plans: list[Plan] = []

    def due(plan: Plan) -> None:
        # Nor one that failed within `SERVICE_RETRY_MIN_S`, whoever would pick it (V315).
        if context is not None and context.failed_lately(plan.decision.skill, state.t):
            return
        if all(p.rule != plan.rule for p in plans):
            plans.append(plan)

    if state.vitals.combat is not False:
        return plans
    # Out of a death camp first: the walk to a service went back through it (V307).
    if context is not None and context.leaving(state.t):
        return plans
    b = state.bags
    can_repair = context is None or context.can_repair(b.money_copper, state.guide.step_id,
                                                       state.t, b.free)
    can_sell = context is None or context.can_make_space(b.free, state.guide.step_id, state.t)

    # A walk to a merchant or a smith waits for a meal first, as training does (V259): of the
    # bag and repair walks begun below 60% health or 50% mana in sessions 205-228, 16 of 26
    # were attacked on the way and 7 ended in a death, against 1 death in 42 begun healthy.
    hurt = _recover(state, context) is not None

    # `is not None` throughout: unknown bags are not full bags, and a service loop on an
    # unread number is the failure the verifier's `no_service_loop` rule also guards.
    if (can_repair and not hurt and b.durability_min is not None
            and b.durability_min <= BROKEN):
        due(Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "equipment is broken", 0.85,
                       ("dead", "combat"), service="repair"), True, "service.broken"))

    # Sick after the Spirit Healer (V379): the minutes a pull must wait go to the services that
    # would come due anyway, a repair of the get-up's 25% first.
    sick = context is not None and context.sick(state.t) > 0.0
    if (sick and can_repair and b.durability_min is not None
            and b.durability_min < SICK_REPAIR_BELOW):
        due(Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "resurrection sickness: repairing meanwhile",
                       0.7, ("dead", "combat"), service="repair"), True, "service.sick_repair"))
    if sick and context.can_train(state):
        due(Plan(_d(Intent.SERVICE, "TRAIN_CLASS", "resurrection sickness: training meanwhile",
                       0.6, ("dead", "combat"), service="train"), True, "service.sick_train"))

    if can_sell and not hurt and b.free is not None and b.free <= BAGS_LOW:
        due(Plan(_d(Intent.SERVICE, "BAG_MAKE_SPACE", "bags are nearly full",
                       0.75, ("dead", "combat"), service="bags"), True, "service.bags_full"))

    # A spell the purse can pay for comes before a repair of gear not yet broken (V240): a
    # repair at a third of the durability buys no armour back, and the level 7 mage's
    # repairs took the copper Frostbolt waited for, 77 of 137 in session 208, while it died
    # to boars and bears it had no slow for.
    # Only while the repair cannot be had (V393): where it can, gear worn under 35% is mended
    # before the trainer is paid, as a weapon broken costs far more than a spell rank waited
    # for. In the hive's deaths of 7 Oct 01:25-03:50, melee characters died 60 times a hundred
    # kills with broken gear against 7.9 intact (warriors 98.5 against 5.4), and a rogue with a
    # broken weapon pressed Sinister Strike 0.5 times a minute against 13.4. Training comes
    # after, with what the repair left.
    if (context is not None and b.durability_min is not None and b.durability_min < 0.35
            and not can_repair
            and _recover(state, context) is None and context.can_train(state)):
        due(Plan(_d(Intent.SERVICE, "TRAIN_CLASS", "the class trainer has spells to teach",
                       0.6, ("dead", "combat"), service="train"), True, "service.train"))

    if can_repair and not hurt and b.durability_min is not None and b.durability_min < 0.35:
        due(Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "durability is low", 0.65,
                       ("dead", "combat"), service="repair"), True, "service.durability"))

    # Under 60%, mended wherever a repairer is a short detour (V393), as a flight master near
    # is visited: while the purse pays for it, and before the next deaths break it (a death
    # takes a tenth of every item's durability).
    if (can_repair and not hurt and context is not None and b.durability_min is not None
            and b.durability_min < REPAIR_NEAR_BELOW
            and context._asked("repairer_near", state, False)):
        due(Plan(_d(Intent.SERVICE, "VENDOR_REPAIR", "durability is under 60% and a repairer "
                    "is near", 0.6, ("dead", "combat"), service="repair"), True,
                 "service.repair_near"))

    conjured = context.conjured() if context is not None else frozenset()
    kept = [(item, count) for item, count, kind in ((b.food_id, b.food_count, "food"),
                                                    (b.drink_id, b.drink_count, "drink"))
            if item is not None and kind not in conjured]
    empty = [item for item, count in kept if count == 0]
    stranded = (bool(empty) and len(empty) == len(kept) and b.durability_min is not None
                and b.durability_min <= 0.05)
    if empty and (context is None or context.can_restock(b.money_copper, state.guide.step_id,
                                                         stranded=stranded, now=state.t)):
        # What is above the trainer's due (V215), asked only with something to buy: the
        # spellbook's census is shared with the capture thread.
        spare = (b.money_copper - context.kept(state)
                 if context is not None and b.money_copper is not None else b.money_copper)
        if any(_affordable(item, spare) for item in empty):
            due(Plan(_d(Intent.SERVICE, "BUY_AMMO_REAGENT_FOOD",
                           "confirmed food or drink is empty", 0.8, ("dead", "combat"),
                           service="supplies"), True, "service.supplies"))

    # A hunter's ammunition running low, bought with what is above the repair reserve (V393):
    # a shot is a hunter's weapon as its blade is, before the trainer's due. 22 of the hive's
    # 50 hunters carried none on 7 Oct (04:40): Jev never bought it.
    price = context._asked("ammo_low", state, None) if context is not None else None
    if (price is not None and not hurt
            and not context._barred("supplies", state.guide.step_id, state.t)
            and (b.money_copper is None
                 or b.money_copper - repair_reserve(state.char.level, state.char.cls)
                 >= price)):
        due(Plan(_d(Intent.SERVICE, "BUY_AMMO_REAGENT_FOOD", "ammunition is running low", 0.8,
                    ("dead", "combat"), service="supplies"), True, "service.ammo"))

    # Last: spells a trainer would teach now. A paladin that never trained fought to level
    # 8 on Seal of Righteousness and Holy Light rank 1, losing to two wolves at once. It can
    # wait for a meal: the walk to Goldshire's trainer is 640 yards of Elwynn.
    if context is not None and _recover(state, context) is None and context.can_train(state):
        due(Plan(_d(Intent.SERVICE, "TRAIN_CLASS", "the class trainer has spells to teach",
                       0.6, ("dead", "combat"), service="train"), True, "service.train"))

    # And a home near the work: a hearthstone bound to Northshire took a level 9 back
    # there four times in a day from Goldshire and Fargodeep.
    if context is not None and _recover(state, context) is None and context.can_bind(state):
        due(Plan(_d(Intent.SERVICE, "BIND_HEARTH", "home is far from the guide's work",
                       0.55, ("dead", "combat"), service="bind"), True, "service.bind"))

    # A flight master passed is a node to fly back to later: only a visited node can be.
    if context is not None and _recover(state, context) is None and context.can_discover(state):
        due(Plan(_d(Intent.SERVICE, "DISCOVER_FLIGHT", "an unvisited flight master is near",
                       0.5, ("dead", "combat"), service="discover"), True, "service.discover"))

    return plans


def _recover(state: State, context: Context | None = None) -> Plan | None:
    v = state.vitals
    if v.combat is True:
        return None
    hp, power = v.hp, v.power
    caster = is_caster(state.char.cls)
    measured = None
    if caster and context is not None and context.mana_line is not None:
        try:
            measured = context.mana_line()
        except Exception:
            measured = None
    line = measured if measured is not None else rest_mana(caster)
    low_mana = v.power_type is PowerType.MANA and power is not None and power < line
    if (hp is not None and hp < HEAL_OUT_OF_COMBAT) or low_mana:
        return Plan(_d(Intent.SERVICE, "EAT_DRINK", "out of combat and low; recover first",
                       0.8, ("dead", "combat")), True, "recover.eat")
    return None


def _fight(state: State, context: Context | None = None) -> Plan | None:
    if state.vitals.combat is not True:
        return None
    if context is not None and context.fight_paused(state.t):
        return None                     # walking out of reach of what cannot be fought (V212)
    # Fight already owns acquisition, closing, facing and the rotation. Splitting its
    # phases into separate arms would put the old duel-range heuristic back in charge
    # and interrupt a proven engagement whenever the target moves.
    return Plan(_d(Intent.SERVICE, "COMBAT_PROFILE", "in combat; defend with the full rotation",
                   0.85, ("dead", "no_combat"), profile="default"), True, "fight.rotation")


def _guide(state: State, node: Node | None) -> Plan | None:
    """Do what the step says. This is the happy path and should be almost every tick."""
    if node is None:
        return None

    skill = next((s for s in node.skills if s in NAMES and s != "TRAVEL_TO"), None)
    if skill is None and "TRAVEL_TO" in node.skills:
        skill = "TRAVEL_TO"
    if node.kind in (StepKind.QUEST_OBJECTIVE, StepKind.GRIND, StepKind.DING_GATE):
        skill = "GRIND_UNTIL"
    if skill is None:
        return None
    frame = ({"coord_zone_id": node.coord_zone_id} if node.coord_zone_id is not None
             else {"zone": node.zone})

    # Not yet in position: the step's own travel leg comes first.
    if (node.kind is not StepKind.QUEST_OBJECTIVE and node.pos is not None
            and state.pos.mx is not None and state.pos.my is not None):
        dx, dy = state.pos.mx - node.pos[0], state.pos.my - node.pos[1]
        if (dx * dx + dy * dy) ** 0.5 > node.r and "TRAVEL_TO" in node.skills:
            return Plan(
                _d(Intent.REJOIN, "TRAVEL_TO", f"not yet at {node.id}", 0.85,
                   ("dead", "stuck_s>8"), goal=f"travel:{node.id}",
                   **frame, x=node.pos[0], y=node.pos[1], r=node.r),
                True, "guide.travel")

    return Plan(
        _d(Intent.ADVANCE, skill, f"service step {node.id}", 0.8,
           ("dead", "stuck_s>8"), goal=f"advance:{node.id}",
           **frame, step_id=node.id),
        True, "guide.step")


def _fallback(state: State) -> Plan:
    """There is always an answer, even when nothing else applied.

    This is the floor of the floor. Grinding where we stand is rarely optimal and is never
    wrong enough to justify stopping — and stopping is what a system with no fallback does
    the first time its remote brain is unreachable.
    """
    return Plan(
        _d(Intent.GRIND_RIB, "GRIND_UNTIL", "no step and nothing urgent; grind here",
           0.35, ("dead", "stuck_s>8")),
        False, "fallback.grind")


# --------------------------------------------------------------------------- entry point


def _blind(state: State) -> bool:
    """Nothing trustworthy is being read right now."""
    return not state.sense.addon_ok and (state.sense.vision_conf or 0.0) < 0.5


def _derate(plan: Plan) -> Plan:
    """Mark a plan made without working senses as the guess it is.

    A step can still be attempted blind — the scripted tier has to return *something* —
    but claiming 0.8 confidence in a position nothing confirmed would hide the exact
    ticks worth spending a teacher call on, and would make `unresolved/h` report a
    perception outage as a quiet, confident run.
    """
    d = plan.decision
    return Plan(d.model_copy(update={"confidence": min(d.confidence, 0.4)}),
                False, plan.rule + "+blind")


def step_wait(state: State, context: Context | None) -> Plan | None:
    """The guide step waits (`Context.step_waits`, V334): nothing is armed for it, and the
    character stands where it is, a fight or a service still taken as they come."""
    if context is None or state.guide.step_id is None:
        return None
    left = context.step_waiting(state.guide.step_id, state.t)
    if left is None:
        return None
    why = context.step_wait_why.get(state.guide.step_id) or "its last try could not begin"
    return Plan(_d(Intent.WAIT, None, f"{state.guide.step_id} waits {left:.0f}s more: {why}",
                   1.0), True, "wait.step")


# The steps whose work is pulls: a grind's, and a quest objective's (V393, as V379 has them).
PULL_STEPS = (StepKind.GRIND, StepKind.DING_GATE, StepKind.QUEST_OBJECTIVE)


def broken_wait(state: State, node: Node | None, context: Context | None) -> Plan | None:
    """A character whose weapon is broken (`Context.disarmed`, V393) takes no pull while its
    repair waits on a retry at most `BROKEN_STAND_S` off: a grind's or an objective's step,
    or a grind where it stands, waits; a fight that comes to it is fought, and every other
    step - a hand-in, an accept, a walk - goes on. When the repair is due it is a service,
    armed before any step; when it waits longer, or is not to be had (too poor with the bags
    sold, a repairer out of reach), the step plays as before."""
    b = state.bags
    if (context is None or b.durability_min is None or b.durability_min > BROKEN
            or (node is not None and node.kind not in PULL_STEPS)
            or not context.can_repair(b.money_copper, free=b.free)     # too poor: pulls earn
            or not context._asked("disarmed", state, False)):
        return None
    left = context.repair_retry_in(state.guide.step_id, state.t)
    if left is None or left > BROKEN_STAND_S:
        return None
    return Plan(_d(Intent.WAIT, None, f"a broken weapon pulls nothing: the repair is tried "
                   f"again in {left:.0f}s", 1.0), True, "wait.broken")


def decide(state: State, node: Node | None = None, *, context: Context | None = None) -> Plan:
    """Always returns a usable plan. Never raises, never returns None.

    `confident=False` marks a tick worth a teacher call *if one is affordable*. It does
    not mark a tick that needs one — the decision handed back is valid either way, and
    that distinction is the difference between an improvement engine and a dependency.
    """
    # Safety first, and safety is not derated: a preempt fires on a positive observation
    # (`is True`), so if one matched, something was read.
    for plan in (preempt(state, context), _fight(state, context), service(state, context=context),
                 _recover(state, context)):
        if plan is not None:
            return plan
    # A broken weapon's pulls wait a moment for its repair (V393).
    waiting = broken_wait(state, node, context)
    if waiting is not None:
        return _derate(waiting) if _blind(state) else waiting

    plan = (step_wait(state, context) or sick_wait(state, node, context) or _guide(state, node)
            or _fallback(state))
    return _derate(plan) if _blind(state) else plan


def sick_wait(state: State, node: Node | None, context: Context | None) -> Plan | None:
    """Resurrection sickness (V379): a grind's or a kill objective's pulls wait for it to end;
    every other step (a hand-in, an accept, a walk) and every service goes on."""
    if context is None or node is None:
        return None
    left = context.sick(state.t)
    if left <= 0.0 or node.kind not in (StepKind.GRIND, StepKind.DING_GATE,
                                        StepKind.QUEST_OBJECTIVE):
        return None
    return Plan(_d(Intent.WAIT, None, f"resurrection sickness: no pulls for {left:.0f}s more",
                   1.0), True, "wait.sick")


def wants_teacher(plan: Plan) -> bool:
    """Would a teacher call improve this tick? Advisory, never blocking."""
    return not plan.confident or plan.decision.confidence < CONFIDENT
