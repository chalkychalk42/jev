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

import time
from collections.abc import Callable
from typing import Any

from jev.coach.model import CoachModel
from jev.guide.graph import Node
from jev.world.state_v1 import State

ARM = ("A World of Warcraft character is free to act. Pick what it does next to gain levels "
       "fastest without dying. The guide step is the default; a service or a meal only when "
       "it pays for its walk.")
PICKS = {
    "hunt.station": ("Where should the character look for its quarry next? Each station's "
                     "record says how often it held the quarry and how long a visit took."),
    "gather.station": ("Where should the character look for the object it gathers next? Each "
                       "station's record says how often it held one and how long a visit took."),
    "fight.heal_below": ("At what share of health should the character heal itself in this "
                         "fight? Each line's record says how often its fights went well."),
    "recover.after_failure": ("A routine has just failed at its objective. Who takes the one "
                              "more attempt before the step is given up: the routine again or "
                              "the tutor?"),
}
# The same question, asked again within this long with the same options, keeps its answer:
# the coach is asked each time a skill ends, and one ending a second after another has
# seen nothing new.
ARM_TTL_S = 30.0


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


def describe(plan) -> str:
    """One option as Jev reads it: what the plan does and why the coach has it."""
    d = plan.decision
    what = d.skill or d.intent.value
    return f"{what}: {d.why}"[:160]


class Judge:
    """Jev's seat in one character's coach: `arm` for what to do next, `pick` for the
    learned choices; `state` reads the character now (for `pick`, whose callers hold none)."""

    def __init__(self, model: CoachModel, *, state: Callable[[], State | None] | None = None,
                 record=None, clock: Callable[[], float] = time.monotonic, say=None):
        self.model, self.state, self.clock, self.say = model, state, clock, say
        self.record = record                  # where this character's calls are written
        self._last: tuple[tuple, float, str] | None = None
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
                                   {rule: describe(plan) for rule, plan in by_rule.items()},
                                   record=self.record)
        if not answer.ok:
            return None
        self._last = (key, now, answer.choice)
        self.taken += 1
        return by_rule[answer.choice]

    def origin(self) -> tuple[float, float] | None:
        """Where the character stands, in world yards: how far each station is from it."""
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
    plans += policy.services(state, context=context)
    meal = policy._recover(state, context)
    if meal is not None:
        plans.append(meal)
    step = policy._guide(state, node)
    if step is not None:
        plans.append(step)
    if node is None or jamming(state, node):
        plans.append(policy._fallback(state))
    return plans
