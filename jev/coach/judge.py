"""What the coach asks Jev (`jev.coach.model`), and how an answer becomes what is done.

Two kinds of decision are put to it, each only where there is a real choice:

  * **What to arm next** (`arm`): when the character is free to act and more than one of
    the scripted coach's own plans would do now - the guide's step, a service it is due
    (repair, bags, supplies, training, a home, a flight master), a meal, or grinding where
    it stands when the step is going badly - Jev picks which (PLAN §9: "Jev only arms
    skills and moves the playhead"). A preempt, a fight, and a single plan are never asked:
    the floor's answer stands.
  * **A learned choice** (`pick`): where the hunt stands next, the heal line a fight holds,
    who takes the attempt after a failed routine (`jev.learn.choices`). Jev picks among the
    options with each one's record in front of it, so the evidence the character has
    gathered is its memory; the record goes on being kept whoever picked.

Jev decides a whole character in the hive's comparison, or none of it: the question is
whether a character it coaches levels faster, and a coin per decision cannot answer that.
What it saw, what it said and what came of it are written for the outcome to grade.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from jev.coach.model import CoachModel
from jev.guide.graph import Node
from jev.world.state_v1 import State, StepKind

# Neutral on purpose: "the guide step is the default" had Jev pass by nearly every meal,
# repair and restock in its first hour (the hive, 28 September), at 40% health too. The
# objective is the hive's reward, said as it is scored (V312): levels a played hour, a death
# charged two minutes more (JevHive `docs/plans/reward.md`), not "fastest while dying as
# little as possible", which weighed the two as Jev pleased.
ARM = ("A World of Warcraft character is free to act. Pick what it does next to gain the most "
       "levels per played hour, each death counting as two minutes more: fighting hurt, with "
       "broken gear or without food and drink costs deaths, and every walk to a service costs "
       "minutes. XP/h and deaths/h, where shown, were measured by other characters doing it at "
       "this level.")
PICKS = {
    "hunt.station": ("Where should the character look for its quarry next? Each station's "
                     "record says how often it held the quarry and how long a visit took, its "
                     "walk included; the best finds the most a second."),
    "gather.station": ("Where should the character look for the object it gathers next? Each "
                       "station's record says how often it held one and how long a visit took; "
                       "the best finds the most a second."),
    "fight.heal_cycle": ("At what share of health should the character heal itself in this "
                         "fight? Each line's record says how often its fights killed and how "
                         "long each took to the next fight, rest and deaths included; the best "
                         "costs the fewest seconds a kill."),
    "recover.after_failure": ("A routine has just failed at its objective. Who takes the one "
                              "more attempt before the step is given up: the routine again or "
                              "the tutor? The best is done soonest."),
}
FIGHT = ("A World of Warcraft character is in a fight. Pick the attack to press next, of those "
         "ready now, to kill the target soonest without dying.")
# A fight's answer is taken at the next ready moment only while it is this fresh: the question
# is put as the last press begins, and a cast or a global cooldown later the fight may be
# another.
FIGHT_FRESH_S = 2.0
# The same question, asked again within this long with the same options, keeps its answer:
# the coach is asked each time a skill ends, and one ending a second after another has
# seen nothing new.
ARM_TTL_S = 30.0
STATION_TTL_S = 60.0


def _r(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def situation(state: State, node: Node | None = None) -> dict[str, Any]:
    """What Jev sees of the character: short keys, rounded numbers, nothing it cannot use
    (the decision catalog's conventions, and PLAN §9.3: a trimmed state, the step)."""
    c, v, b, g = state.char, state.vitals, state.bags, state.guide
    me = {"cls": c.cls, "race": c.race, "lvl": c.level, "xp": _r(c.xp_pct), "hp": _r(v.hp),
          "pow": _r(v.power), "pt": v.power_type.value if v.power_type is not None else None,
          "combat": v.combat, "zone": state.pos.zone}
    bags = {"free": b.free, "dur": _r(b.durability_min), "money": b.money_copper,
            "food": b.food_count, "drink": b.drink_count}
    step = None
    if node is not None:
        step = {"kind": node.kind.value, "title": node.title, "zone": node.zone,
                "band": list(node.level) if node.level else None,
                "progress": _r(g.progress), "age_s": _r(g.age_s, 0),
                "attempts": g.attempts, "deaths": g.deaths_on_step, "on_route": g.on_route}
    target = None
    if state.target.has is True:
        t = state.target
        target = {"n": t.name, "lvl": t.level, "hp": _r(t.hp),
                  "reaction": t.reaction.value if t.reaction is not None else None,
                  "dist": _r(t.dist, 1)}
    out = {"me": {k: val for k, val in me.items() if val is not None},
           "bags": {k: val for k, val in bags.items() if val is not None}}
    if step is not None:
        out["step"] = {k: val for k, val in step.items() if val is not None}
    if target is not None:
        out["tgt"] = {k: val for k, val in target.items() if val is not None}
    return out


def jamming(state: State, node: Node | None) -> bool:
    """The step is going badly: tried and failed, died on, or stalled past half its time."""
    g = state.guide
    if g.attempts or g.deaths_on_step:
        return True
    return (node is not None and g.age_s is not None
            and g.age_s > 0.5 * (node.timeout_s or 300.0))


def _pct(value) -> str:
    return "?" if value is None else f"{float(value):.0%}"


def evidence(rule: str, state: State | None) -> str:
    """The numbers behind an option: what the character has that the option answers."""
    if state is None:
        return ""
    v, b, g = state.vitals, state.bags, state.guide
    if rule.startswith("recover."):
        return f"health {_pct(v.hp)}, power {_pct(v.power)}"
    if rule in ("service.broken", "service.durability", "service.repair_near"):
        return f"worst gear at {_pct(b.durability_min)}, {b.money_copper or 0} copper"
    if rule == "service.bags_full":
        return f"{b.free} bag slots free"
    if rule == "service.supplies":
        return f"food {b.food_count}, drink {b.drink_count}, {b.money_copper or 0} copper"
    if rule == "service.pet":
        pet = state.pet
        return (f"pet out {pet.has}, dead {pet.dead}, happiness {pet.happiness}, "
                f"its food {pet.food_count}, power {_pct(v.power)}")
    if rule.startswith("service."):
        return f"{b.money_copper or 0} copper"
    if rule.startswith(("guide.", "fallback.")):
        return (f"on the step {g.age_s or 0:.0f} s, {g.attempts} failed attempts, "
                f"{g.deaths_on_step} deaths")
    return ""


def measured(plan, state: State | None, node: Node | None, values) -> str:
    """What the hive measured the plan's work to be worth at the character's level (V312):
    a step's quest, a rib, or grinding where it stands; empty without values or a number."""
    if values is None or state is None:
        return ""
    level, cls = state.char.level, state.char.cls
    rule = plan.rule.removeprefix("jev:")
    value = None
    if rule.startswith("guide.") and node is not None:
        if node.quest_id is not None:
            value = values.quest(node.quest_id, level, cls)
        elif node.kind in (StepKind.GRIND, StepKind.DING_GATE):
            value = values.grind(node.id, level, cls) or values.grinds(level, cls)
    elif rule.startswith("fallback.") or (plan.decision.skill == "GRIND_UNTIL"
                                          and not rule.startswith("guide.")):
        value = values.grinds(level, cls)
    return value.text() if value is not None else ""


def describe(plan, state: State | None = None, value: str = "") -> str:
    """One option as Jev reads it: what the plan does, why the coach has it, the numbers, and
    what its work was measured to be worth (`measured`)."""
    d = plan.decision
    what = d.skill or d.intent.value
    numbers = evidence(plan.rule, state)
    text = f"{what}: {d.why}{f' ({numbers})' if numbers else ''}"[:200]
    return f"{text}; {value}" if value else text


class Judge:
    """Jev's seat in one character's coach: `arm` for what to do next, `pick` for the
    learned choices; `state` reads the character now (for `pick`, whose callers hold none)."""

    def __init__(self, model: CoachModel, *, state: Callable[[], State | None] | None = None,
                 where: Callable[[], tuple[float, float] | None] | None = None,
                 record=None, clock: Callable[[], float] = time.monotonic, say=None,
                 values=None):
        self.model, self.state, self.clock, self.say = model, state, clock, say
        # What the hive measured each quest and grind to be worth (`jev.learn.values`, V312),
        # read once a session; `None` leaves the options as they were.
        self.values = values
        self.where = where                    # the character's world position, in yards
        self.record = record                  # where this character's calls are written
        self._last: tuple[tuple, float, str] | None = None
        # The station each hunt objective was last told to begin at, and when: a hunt begun
        # again within `STATION_TTL_S` begins there without asking (`jev.learn.choices`).
        self.stations: dict[tuple[str, str], tuple[str, float]] = {}
        self.asked = self.taken = 0

    def arm(self, state: State, node: Node | None, candidates: list) -> Any | None:
        """The plan Jev picks among `candidates` (the coach's own `Plan`s, each named by its
        rule), or `None` to leave the floor's."""
        by_rule = {}
        for plan in candidates:
            by_rule.setdefault(plan.rule, plan)
        if len(by_rule) < 2:
            return None
        key = (state.guide.step_id, tuple(sorted(by_rule)))
        now = self.clock()
        if self._last is not None and self._last[0] == key and now - self._last[1] < ARM_TTL_S:
            return by_rule.get(self._last[2])
        self.asked += 1
        answer = self.model.choose("coach.arm", situation(state, node), ARM,
                                   {rule: describe(plan, state,
                                                   measured(plan, state, node, self.values))
                                    for rule, plan in by_rule.items()},
                                   record=self.record, note={"floor": candidates[0].rule})
        if not answer.ok:
            return None
        self._last = (key, now, answer.choice)
        self.taken += 1
        return by_rule[answer.choice]

    def origin(self) -> tuple[float, float] | None:
        """Where the character stands, in world yards: how far each station is from it."""
        if self.where is not None:
            return self.where()
        state = self.state() if self.state is not None else None
        world = state.pos.world if state is not None else None
        return (world[0], world[1]) if world else None

    def pick(self, point: str, objective: str, options: dict[str, str]) -> str | None:
        """One option of a learned choice (`options`: name -> its record), or `None`."""
        if len(options) < 2:
            return None
        state = None
        if self.state is not None:
            try:
                state = self.state()
            except Exception:
                state = None
        seen = situation(state) if state is not None else {}
        seen["choice"] = {"point": point, "objective": objective}
        instructions = PICKS.get(point, f"Pick the best option for {point}.")
        self.asked += 1
        answer = self.model.choose(point, seen, instructions, options, record=self.record)
        if not answer.ok:
            return None
        self.taken += 1
        return answer.choice


def candidates(state: State, node: Node | None, context, floor) -> list:
    """Every plan the scripted coach would stand behind now, the floor's first: its services
    and meal (all that apply, not only the first), the guide's step, and grinding where the
    character stands when the step is going badly or there is none. Empty when the floor is
    a preempt or a fight: those are never put to anyone."""
    from jev.coach import policy

    if floor.rule.startswith(("preempt.", "fight.", "sense.", "guide.finished")):
        return []
    plans = [floor]
    due = policy.services(state, context=context)
    plans += due
    meal = policy._recover(state, context)
    if meal is not None:
        plans.append(meal)
    step = policy._guide(state, node)
    if step is not None:
        plans.append(step)
    if node is None or jamming(state, node):
        plans.append(policy._fallback(state))
    # While a service is due a hunt refuses to begin (`LiveBody._service_needed`) and a walk
    # is cancelled (`Supervisor`: "service needed"), so neither is a choice then: offered one,
    # Jev took it, the body refused, and the next decision was the same - a blood elf's hunt
    # began and ended 296 times in six minutes, "bags are nearly full" (V303), and walks were
    # cancelled every second or two on five bots after it (the coach, 28 September). A quest
    # taken or handed in where the character stands still is.
    # Nor while the step waits (`policy.step_wait`, V334): its grind, or a grind where the
    # character stands, is the walk that was refused.
    if due or floor.rule.startswith(("wait.step", "wait.sick")):
        plans = [p for p in plans if p is floor
                 or p.decision.skill not in ("GRIND_UNTIL", "TRAVEL_TO")]

    # Nor while a broken weapon waits for its repair (`policy.broken_wait`, V393): a grind is
    # the pull it must not take.
    if floor.rule.startswith("wait.broken"):
        plans = [p for p in plans if p is floor
                 or p.decision.skill not in ("GRIND_UNTIL", "TRAVEL_TO")]
    return plans


def fight_situation(values: dict) -> dict[str, Any]:
    """What Jev sees of a fight: the character, its target and how many are on it."""
    me = {"lvl": values.get("char.level"), "hp": _r(values.get("vitals.hp")),
          "pow": _r(values.get("vitals.power")), "casting": values.get("bars.casting")}
    target = {"lvl": values.get("target.level"), "hp": _r(values.get("target.hp")),
              "melee": values.get("target.in_melee"),
              "on_me": values.get("target.attacking_me")}
    return {"me": {k: v for k, v in me.items() if v is not None},
            "tgt": {k: v for k, v in target.items() if v is not None},
            "attackers": values.get("combat.attackers")}


class CombatJudge:
    """Jev's seat in a fight: which of the attacks ready now is pressed (the decision
    catalog's `combat.pve` next action). A decision a fight waits on is a swing not taken, so
    the question is put in the background as the last press begins, and the answer is taken
    at the next ready moment while it is fresh and still one of the choices; otherwise the
    bar's own order presses, as before. One question at a time."""

    def __init__(self, model: CoachModel, *, record=None,
                 clock: Callable[[], float] = time.monotonic):
        self.model, self.record, self.clock = model, record, clock
        self._lock = threading.Lock()
        self._pending = False
        self._answer: tuple[str, float] | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-fight")
        self.asked = self.taken = 0

    def take(self, options: Sequence[str]) -> str | None:
        """Jev's answer, once, if it is fresh and one of `options`."""
        with self._lock:
            answer, self._answer = self._answer, None
        if answer is None:
            return None
        choice, at = answer
        if self.clock() - at > FIGHT_FRESH_S or choice not in options:
            return None
        self.taken += 1
        return choice

    def ask(self, values: dict, options: dict[str, str]) -> None:
        """Put the next choice (`options`: name -> what it is) to Jev in the background."""
        if len(options) < 2:
            return
        with self._lock:
            if self._pending:
                return
            self._pending = True
        state = fight_situation(values)

        def work():
            try:
                answer = self.model.choose("fight.attack", state, FIGHT, options,
                                           record=self.record)
                if answer.ok:
                    with self._lock:
                        self._answer = (answer.choice, self.clock())
            finally:
                with self._lock:
                    self._pending = False

        self.asked += 1
        self._pool.submit(work)

    def close(self) -> None:
        self._pool.shutdown(wait=False)
