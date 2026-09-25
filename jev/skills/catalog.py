"""The skill catalog — what the coach is allowed to arm.

A skill is a temporally extended program, not a keypress (PLAN §8.1). The coach never
emits raw keys in the happy path; it arms one of these and System 1 runs it at 30 Hz.
That split is why everything above 2 Hz is allowed to be slow, wrong, or absent.

Every skill declares `success` and `timeout_s`. A skill with no success predicate cannot
be graded, cannot be promoted or retired, and cannot tell a stall from a slow success —
so it is not a skill, it is a hope.

The `pre`/`success` predicates are structured rather than string expressions, for the same
reason the graph's `on_fail` edges are: a typo in a string becomes a silent never-fires.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.world.state_v1 import State

Predicate = Callable[[State], bool]


class Stage(StrEnum):
    """Promotion state (PLAN §10). Measured, never asserted."""

    BUILTIN = "builtin"      # hand-written, always available
    PROPOSED = "proposed"    # drafted by the teacher, unproven
    STABLE = "stable"        # 2 clients x 3 runs succeeded
    RETIRED = "retired"      # success rate < 0.4 over 20; not retrievable without the teacher


def JUDGED_ELSEWHERE(_s: State) -> bool:
    """Success for a skill that does not judge itself.

    Nine skills in this catalog end on something only the tracker knows — arrival inside
    a node radius, a quest appearing in the log, a rib's own predicate. They were all
    written as `lambda s: False`, which is indistinguishable at runtime from "tried and
    failed": every one showed up as aborted, and PLAN §10's retirement rule would have
    retired the entire travel and questing half of the catalog for never succeeding at
    something it was never asked to decide.

    Identity matters, not the return value — callers compare against this function to
    know the answer is not theirs to give, and record `UNKNOWN` rather than a failure.
    Absence of an answer is not an answer, applied to skills.
    """
    return False


@dataclass(frozen=True)
class Skill:
    name: str
    summary: str
    timeout_s: float
    success: Predicate
    pre: Predicate = field(default=lambda _s: True)
    stage: Stage = Stage.BUILTIN
    interruptible: bool = True
    on_fail: str | None = None       # a skill name to try once before escalating
    # The budget is for the work in front of an NPC. A walk to get there is bounded by
    # travel's own timeouts and is not charged to it: a step counts as reached a hundred
    # yards out, and an accept that had to find its way out of Goldshire's inn cellar spent
    # its sixty seconds on the stairs (session 90).
    walk_free: bool = False


def _alive(s: State) -> bool:
    return s.vitals.dead is not True and s.vitals.ghost is not True


# The catalog. Order is PLAN §8.1's implementation order, which is also roughly the order
# a character needs them in.
_SKILLS: tuple[Skill, ...] = (
    Skill("IDLE", "do nothing, deliberately", 5.0,
          success=lambda s: True),

    Skill("FOLLOW_PATH", "walk a recorded polyline", 300.0,
          success=JUDGED_ELSEWHERE,
          pre=_alive, on_fail="STUCK_RECOVER"),

    Skill("TRAVEL_TO", "get to a point on the zone map", 600.0,
          success=JUDGED_ELSEWHERE,
          pre=_alive, on_fail="STUCK_RECOVER"),

    Skill("COMBAT_PROFILE", "run a named rotation until the target is dead", 120.0,
          success=lambda s: s.target.has is False or s.vitals.combat is False,
          pre=lambda s: _alive(s) and s.target.has is True),

    Skill("APPROACH_TARGET", "close to melee reach", 30.0,
          # Ask the client whether we are in reach. Inferring it from damage having
          # landed means the character must hit a mob to learn it can reach one.
          success=lambda s: s.target.in_melee is True,
          pre=lambda s: _alive(s) and s.target.has is True),

    Skill("FACE", "turn to face the target", 5.0,
          success=lambda s: True, pre=_alive),

    Skill("ACQUIRE_TARGET", "select something to fight", 10.0,
          # Mechanical, not a judgement: `/target <exact name>` when the step names its
          # mobs, nearest hostile nameplate otherwise. Being in combat with nothing
          # selected is a gap System 1 closes, and treating it as ambiguity sent 42% of a
          # simulated run to the teacher for "pick a target".
          success=lambda s: s.target.has is True,
          pre=lambda s: _alive(s) and s.target.has is not True),

    Skill("LOOT", "clear the loot window", 15.0,
          success=lambda s: s.ui.loot is False,
          pre=lambda s: _alive(s) and s.ui.loot is True),

    # A walk out of the camp's reach, gear and bar upkeep, then the rest's own 45 s: from
    # low mana on level-1 water that was more than 60 s (session 107).
    Skill("EAT_DRINK", "sit and recover to a working level", 120.0,
          success=lambda s: (s.vitals.hp or 0) > 0.85 and (s.vitals.power or 1) > 0.7,
          pre=lambda s: _alive(s) and s.vitals.combat is False),

    Skill("VENDOR_REPAIR", "sell greys, repair, make space", 180.0,
          success=lambda s: (s.bags.free is not None and s.bags.free >= 6
                             and s.bags.durability_min is not None
                             and s.bags.durability_min > 0.7),
          pre=_alive, on_fail="STUCK_RECOVER"),

    Skill("DISCOVER_FLIGHT", "visit a flight master so its node can be flown to", 240.0,
          # Talk to it and read its map; the node painted as here is remembered
          # (`LiveBody._discover`).
          success=JUDGED_ELSEWHERE, pre=_alive),

    Skill("BIND_HEARTH", "make the inn nearest the guide's work home", 420.0,
          # A walk to the innkeeper, a gossip line and a confirmation; the body judges it
          # by the confirmation closing (`LiveBody._bind`).
          success=JUDGED_ELSEWHERE, pre=_alive),

    Skill("TRAIN_CLASS", "learn available spells and put them on the bar", 600.0,
          # The walk is most of it: Goldshire's paladin trainer is 640 yards from
          # Northshire Abbey. A purchase is the money falling and a placement is the bar's
          # census holding the spell (`LiveBody._train`), so the body judges it.
          success=JUDGED_ELSEWHERE, pre=_alive),

    Skill("RELEASE_SPIRIT", "release to the graveyard", 30.0,
          success=lambda s: s.vitals.ghost is True,
          pre=lambda s: s.vitals.dead is True),

    Skill("CORPSE_RUN", "walk the ghost back to the body and resurrect", 420.0,
          success=lambda s: s.vitals.ghost is False and s.vitals.dead is False,
          pre=lambda s: s.vitals.ghost is True),

    Skill("HEARTH", "use the hearthstone", 60.0,
          success=JUDGED_ELSEWHERE, pre=lambda s: _alive(s) and s.vitals.combat is False),

    Skill("FLIGHT_PATH", "take a known flight", 600.0,
          success=lambda s: s.flags.on_taxi is False, pre=_alive),

    Skill("BOAT_OR_ZEP", "ride a boat or zeppelin to the next zone", 600.0,
          # Fragile by nature (PLAN §14): it ends on a zone change and a timer, and there
          # is nothing to read mid-crossing. The one rule that matters is not to walk off
          # the dock while waiting.
          success=JUDGED_ELSEWHERE, pre=lambda s: _alive(s) and s.vitals.combat is not True),

    Skill("ACCEPT_QUEST", "take the quest from the NPC in front of us", 60.0,
          success=JUDGED_ELSEWHERE, walk_free=True,
          pre=lambda s: _alive(s) and s.vitals.combat is not True),

    Skill("TURNIN_QUEST", "hand the quest back", 60.0,
          success=JUDGED_ELSEWHERE, walk_free=True,
          pre=lambda s: _alive(s) and s.vitals.combat is not True),

    Skill("GOSSIP_PICK", "choose a gossip option", 20.0,
          success=lambda s: s.ui.gossip is False,
          pre=lambda s: _alive(s) and s.ui.gossip is True),

    Skill("GRIND_UNTIL", "kill, loot and eat around a point until a condition holds", 900.0,
          success=JUDGED_ELSEWHERE,
          pre=_alive),

    Skill("STUCK_RECOVER", "jump, strafe, back up, repath", 45.0,
          success=lambda s: True, pre=_alive),

    Skill("BAG_MAKE_SPACE", "visit a merchant and sell confirmed unneeded junk", 360.0,
          success=JUDGED_ELSEWHERE, pre=_alive),

    Skill("BUY_AMMO_REAGENT_FOOD", "restock exact supported food and drink", 360.0,
          success=JUDGED_ELSEWHERE, pre=_alive),

    Skill("MOUNT_UP", "mount, if we have one and may use it", 15.0,
          success=lambda s: s.flags.mounted is True,
          pre=lambda s: _alive(s) and s.vitals.combat is not True
                        and (s.char.level or 0) >= 30),

    Skill("ABORT_WAIT", "stop and wait out something the client is doing", 30.0,
          success=lambda s: s.ui.modal is False, pre=lambda s: True),
)

BY_NAME: dict[str, Skill] = {s.name: s for s in _SKILLS}
NAMES: frozenset[str] = frozenset(BY_NAME)


def get(name: str) -> Skill | None:
    return BY_NAME.get(name)


def judges_itself(skill: Skill) -> bool:
    """Can this skill tell whether it worked? If not, its rate is not its own."""
    return skill.success is not JUDGED_ELSEWHERE


def retrievable(stages: tuple[Stage, ...] = (Stage.BUILTIN, Stage.STABLE, Stage.PROPOSED)
                ) -> frozenset[str]:
    """Skills the coach may retrieve.

    `RETIRED` is excluded deliberately and is not a default anywhere: a skill that failed
    sixty percent of the time is worse than no skill, because the coach will keep choosing
    it. It becomes reachable again only with the teacher in the loop (PLAN §10).
    """
    return frozenset(n for n, s in BY_NAME.items() if s.stage in stages)
