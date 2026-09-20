"""The prompt. Small on purpose, and bounded by construction.

PLAN §9.3 lists what goes in — identity, output schema, trimmed `state_v1`, the active step
plus the next two nodes, the top retrieved skills, the last few events, the last death
postmortem — and then one instruction: *no 40k-token Wowhead dump*. This module is that
instruction made mechanical. `build_prompt` cannot return a string over the ceiling; if the
context handed in is too big it sheds optional sections in a fixed order and, if even the
core will not fit, raises rather than quietly sending something enormous.

That guarantee is worth more than it looks. The probe in `client.py` measured 21k tokens of
CLI system prompt riding along with a 10-token question, on a window the whole farm shares.
The part we control is the part that has to stay honest.

`DECISIONS.md` V11 / `ARCHITECTURE.md` §3 decide the shape of the ask, not just its size:

    preferred:  skill draft | graph patch | on_fail edge | combat profile change
    fallback:   an immediate action, for this client, this once

so the contract section asks for the artifact first and names the action as the fallback.
A decision helps one client once; an `on_fail` edge helps every client forever, and on a
rate-limited teacher that difference is the whole economics.

Trimming is an allowlist, not a denylist
----------------------------------------
`trim_state` names the fields that go in rather than the fields that stay out. A denylist
means every field added to `state_v1` later silently joins every prompt, and the first
notice is the bill — which is exactly how a bounded prompt stops being bounded. An
allowlist fails the other way: a new field is invisible to the teacher until someone adds
it here, which is a missing sentence rather than a budget breach.

Dropped for cause: screen coordinates and map `mx`/`my` (System 1 navigates, the teacher
does not), world yards (derived, and the same), facing (radians are for the mover),
`hp_max`/`power_max` (the fraction carries the decision), timestamps and sequence counters
(the situation, not the clock), and the character's name.

Unknown stays unknown: fields nothing observed are omitted and the header says so, because
rendering `None` as `false` would invent an observation nobody made (`ARCHITECTURE.md` §6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jev.coach.schema import teacher_json_schema
from jev.coach.situation import situation_key as compute_situation_key
from jev.world.state_v1 import State

# ~2,000 tokens at the usual 4 chars/token. Measured, not guessed: identity, contract,
# schema and headers are ~4,100 chars before anything varies — the schema alone is ~2,400 —
# so 8,000 leaves ~3,900 for the half that changes, which fits all of PLAN §9.3's sections
# at their caps without shedding. 6,000 was tried first and was wrong: it left so little
# that every realistic prompt shed its retrieved skills, which is retrieval paid for and
# thrown away. Tightening much below this optimises the wrong number anyway — the CLI adds
# a ~21,000-token system-prompt floor (see `client.py`) that dwarfs our half.
# Chars rather than tokens because a tokenizer is a dependency and an approximation either
# way; the authoritative counts come back from the CLI and land in the DecisionRow.
PROMPT_CHAR_CEILING = 8000
CHARS_PER_TOKEN = 4

MAX_NEXT_NODES = 2        # PLAN §9.3: the active step and the next two
MAX_SKILLS = 5            # PLAN §9.3: top-5 retrieved
MAX_EVENTS = 3            # PLAN §9.3: last 3
MAX_POSTMORTEM_LINES = 10  # PLAN §9.3: 10 lines
MAX_POSTMORTEM_CHARS = 800

# A TBC quest has exactly four objective slots — the world DB stores them as
# ReqCreatureOrGOId1..4 (ARCHITECTURE.md §8) — so four is the schema's own limit rather
# than a guess. Three quests, not the whole log: the question is always about the active
# step, and five quests at four objectives each measured 1,900 chars, a quarter of the
# entire budget spent on quests that mostly do not bear on it.
MAX_QUESTS = 3
MAX_OBJECTIVES = 4
MAX_TEXT = 90             # one objective or event line; longer is a paragraph, not a fact


class PromptTooLarge(RuntimeError):
    """The core sections alone exceeded the ceiling.

    Raised rather than truncated: the core is identity, contract, schema and state, and a
    half-sent schema produces a reply that fails validation, which costs a call and teaches
    nobody anything. Hitting this means a caller passed something enormous as identity or
    as the question, and that is a bug to fix rather than a size to absorb.
    """


@dataclass(frozen=True)
class StepNode:
    """One GuideGraph node as the teacher needs to see it."""

    step_id: str
    kind: str | None = None
    title: str | None = None
    on_fail: str | None = None   # the edge taken when this step gives up — V11's target

    def line(self) -> str:
        bits = [self.step_id]
        if self.kind:
            bits.append(f"({self.kind})")
        if self.title:
            bits.append(_clip(self.title, MAX_TEXT))
        if self.on_fail:
            bits.append(f"on_fail->{self.on_fail}")
        return " ".join(bits)


@dataclass(frozen=True)
class SkillCard:
    """A retrieved skill. Success rate included because the teacher is being asked to pick
    between them, and a catalog without rates is a list of names with equal authority."""

    name: str
    success_rate: float | None = None
    one_line: str = ""

    def line(self) -> str:
        rate = "?" if self.success_rate is None else f"{self.success_rate:.2f}"
        return f"{self.name} rate={rate} {_clip(self.one_line, MAX_TEXT)}".rstrip()


@dataclass(frozen=True)
class PromptContext:
    """Everything around the state. All of it optional — the state and the schema are the
    only things a question cannot be asked without."""

    active_step: StepNode | None = None
    next_nodes: tuple[StepNode, ...] = ()
    skills: tuple[SkillCard, ...] = ()
    events: tuple[str, ...] = ()
    death_postmortem: str | None = None
    question: str | None = None
    catalog: tuple[str, ...] = ()


DEFAULT_QUESTION = (
    "No rule could settle this step. What is the durable fix, and what should this client "
    "do right now if there is none?"
)


def estimate_tokens(text: str) -> int:
    """A budget guard, not an accounting figure. Four chars per token is the English rule of
    thumb; the real counts come back from the CLI in `usage` and go in the DecisionRow."""
    return len(text) // CHARS_PER_TOKEN + 1


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _prune(value: Any) -> Any:
    """Drop unobserved leaves and the empty containers they leave behind.

    Omission, never `false`: the header tells the teacher that a missing field was not
    observed, which is the one rendering of `None` that does not invent a fact.
    """
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items() if v is not None}
        return {k: v for k, v in out.items() if v not in (None, {}, [], ())}
    if isinstance(value, list | tuple):
        return [_prune(v) for v in value if v is not None]
    if isinstance(value, float):
        # Two decimals: every fraction in state_v1 is a band in practice, and 0.8999999998
        # spends eight characters claiming a precision no reader of a health bar has.
        return round(value, 2)
    return value


def trim_state(state: State) -> dict[str, Any]:
    """`state_v1` reduced to what could change the answer. See the module docstring."""
    flags_true = state.flags.present()
    flags_unknown = state.flags.unknown()
    ui_open = [k.upper() for k, v in state.ui.model_dump().items() if v is True]

    quests = []
    for q in state.quest_log()[:MAX_QUESTS]:
        objectives = [
            f"{_clip(o.text, MAX_TEXT)} {o.have}/{o.need}" for o in q.objectives[:MAX_OBJECTIVES]
        ]
        quests.append(
            _prune({"title": _clip(q.title, MAX_TEXT) if q.title else None,
                    "id": q.quest_id, "complete": q.complete, "obj": objectives})
        )

    doc: dict[str, Any] = {
        "situation": state.situation_key or compute_situation_key(state),
        "char": {
            "class": state.char.cls,
            "race": state.char.race,
            "faction": state.char.faction,
            "level": state.char.level,
            "xp_pct": state.char.xp_pct,
        },
        "where": {"zone": state.pos.zone, "sub": state.pos.sub, "indoors": state.pos.indoors},
        "vitals": {
            "hp": state.vitals.hp,
            "power": state.vitals.power,
            "power_type": state.vitals.power_type,
            "combat": state.vitals.combat,
            "dead": state.vitals.dead,
            "ghost": state.vitals.ghost,
        },
        "flags": flags_true or None,
        "flags_unobserved": flags_unknown or None,
        "target": {
            "has": state.target.has,
            "name": state.target.name,
            "level": state.target.level,
            "hp": state.target.hp,
            "reaction": state.target.reaction,
            "class": state.target.classification,
            "attacking_me": state.target.attacking_me,
            "in_melee": state.target.in_melee,
            "dist_yd": state.target.dist,
            "tapped_by_other": state.target.tapped_by_other,
        },
        "bags": {
            "free": state.bags.free,
            "durability_min": state.bags.durability_min,
            "copper": state.bags.money_copper,
        },
        "ui_open": ui_open or None,
        "ui_error": state.ui.error,
        "quests": quests or None,
        "guide": {
            "graph": state.guide.graph_id,
            "step": state.guide.step_id,
            "kind": state.guide.kind,
            "age_s": state.guide.age_s,
            "on_route": state.guide.on_route,
            "progress": state.guide.progress,
            "deaths_on_step": state.guide.deaths_on_step or None,
            "attempts": state.guide.attempts or None,
        },
        # How much of the above to believe. A plan made blind is not the same plan made
        # informed, so the teacher is told which one it is being asked for.
        "sense": {
            "addon_ok": state.sense.addon_ok,
            "fault": state.sense.fault,
            "source": state.sense.source,
            "vision_conf": state.sense.vision_conf,
        },
        "armed": {
            "skill": state.control.armed_skill,
            "by": state.control.armed_by,
            "s1_mode": state.control.s1_mode,
        },
    }
    return _prune(doc)


IDENTITY = (
    "You are the teacher for an autonomous leveling agent on a private TBC (2.4.3) server.\n"
    "You are asked only about situations no rule could settle, you are answered by a queue "
    "shared with the whole farm, and you cost a rate-limited subscription window. Be brief."
)

CONTRACT = (
    "Reply with ONE JSON object matching SCHEMA. No prose, no markdown fence, no commentary.\n"
    "\n"
    "Prefer a DURABLE ARTIFACT over an action. A decision helps one client once; a skill "
    "draft, a graph patch or an on_fail edge helps every client forever, and calls are "
    "scarce. Put those in `artifacts`. Use `decision` only as the fallback for this client "
    "this once; emitting both is correct when the fix is durable but slow to land.\n"
    "`decision.abort_if` is mandatory and non-empty: a skill with no abort condition runs "
    "until something else stops it, and during a live run that is usually the corpse.\n"
    "A local rules verifier can refuse your decision; artifacts are kept regardless.\n"
    "\n"
    "If STATE does not support an answer, still answer: set confidence below 0.3 and say in "
    "`why` which observation is missing. Do not invent an observation STATE does not "
    "contain, and do not return an empty object."
)

STATE_HEADER = (
    "STATE is state_v1, trimmed. Fractions are 0..1. Distances are yards. A field that is "
    "absent was NOT OBSERVED - absence is not a 'no'. `situation` is the bucket this "
    "question is cached and deduplicated under; other clients in the same bucket get this "
    "same answer."
)


def build_prompt(
    state: State,
    ctx: PromptContext | None = None,
    *,
    ceiling: int = PROMPT_CHAR_CEILING,
) -> str:
    """Assemble the prompt, guaranteed under `ceiling` characters.

    Sections are shed worst-value-first when the budget is tight: the death postmortem goes
    before the events, the events before the retrieved skills, the skills before the next
    nodes. That order is the reverse of how much each one changes the answer — the next
    nodes are what an `on_fail` edge attaches to, so they are the last optional thing to go,
    while a postmortem is context for a death that has already been paid for.
    """
    ctx = ctx or PromptContext()

    core = [
        IDENTITY,
        CONTRACT,
        "SCHEMA: " + json.dumps(teacher_json_schema(), separators=(",", ":")),
        STATE_HEADER,
        "STATE: " + json.dumps(trim_state(state), separators=(",", ":"), default=str),
    ]
    question = "QUESTION: " + _clip(ctx.question or DEFAULT_QUESTION, 400)

    optional: dict[str, str] = {}
    if ctx.active_step is not None:
        optional["ACTIVE_STEP"] = "ACTIVE_STEP: " + ctx.active_step.line()
    if ctx.next_nodes:
        optional["NEXT"] = "NEXT: " + " | ".join(
            n.line() for n in ctx.next_nodes[:MAX_NEXT_NODES]
        )
    if ctx.skills:
        optional["SKILLS"] = "SKILLS:\n" + "\n".join(
            "  " + s.line() for s in ctx.skills[:MAX_SKILLS]
        )
    if ctx.catalog:
        # The verifier refuses any skill not in the catalog, so withholding the catalog
        # means paying for answers that are rejected on arrival.
        optional["CATALOG"] = "CATALOG: " + ", ".join(sorted(ctx.catalog))
    if ctx.events:
        optional["EVENTS"] = "EVENTS:\n" + "\n".join(
            "  " + _clip(e, MAX_TEXT) for e in ctx.events[-MAX_EVENTS:]
        )
    if ctx.death_postmortem:
        lines = ctx.death_postmortem.strip().splitlines()[:MAX_POSTMORTEM_LINES]
        optional["LAST_DEATH"] = _clip_block(
            "LAST_DEATH:\n" + "\n".join("  " + line.strip() for line in lines),
            MAX_POSTMORTEM_CHARS,
        )

    shed_order = ("LAST_DEATH", "EVENTS", "SKILLS", "CATALOG", "NEXT", "ACTIVE_STEP")
    while True:
        text = "\n\n".join([*core, *optional.values(), question])
        if len(text) <= ceiling:
            return text
        for name in shed_order:
            if name in optional:
                del optional[name]
                break
        else:
            raise PromptTooLarge(
                f"core sections are {len(text)} chars, over the {ceiling} ceiling; "
                "nothing optional is left to drop"
            )


def _clip_block(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
