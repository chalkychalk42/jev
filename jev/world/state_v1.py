"""`state_v1` — the contract.

Perception, tracker, coach, teacher, verifier and every training row use this object.
It is the only shape that crosses a process boundary. Change it through `SCHEMA_VERSION`
and a migration, never by quietly adding a field with a default that lies.

Units, decided once so nobody has to ask again
----------------------------------------------
* fractions (hp, power, durability, progress)  -> 0.0 .. 1.0   NEVER 0..100
* distances                                    -> yards
* durations and timestamps                     -> seconds (float)
* money                                        -> copper (int), as the client counts it
* angles  -> radians, 0 = +X, increasing toward +Y. WoW's world axes are +X north and
             +Y west, so that reads as 0 = north increasing north -> west -> south -> east.
             Confirmed against this server's own source, mangos-tbc Object.cpp:
             GetNearPoint2dAt advances x += d*cos(a), y += d*sin(a), and GetAngle is
             atan2(dy, dx) with no negation. DO NOT negate dy.
* map position -> `mx`, `my` in 0..1, relative to the *current zone's* map. This is what
             GetPlayerMapPosition returns and the only position a 2.4.3 addon can know.
             A percentage means nothing without the zone it is a percentage of, so `zone_id`
             travels with it always.
* world position -> yards (x, y, z), DERIVED from (zone_id, mx, my) via the client's own
             WorldMapArea.dbc bounds. `None` until something derives it. Navmesh work needs
             it; the addon cannot supply it.
* screen coordinates -> pixels, origin top-left of the game window

Unknown is not a negative fact
------------------------------
`None` means "not observed". It is never rendered as False, 0 or "". A reader that cannot
see whether the player is mounted must say `None`, because "not mounted" is a claim and it
did not make one. See `ARCHITECTURE.md` §6.

Helpers that collapse the tri-state exist (`is_true`, `is_false`) and they are explicit at
the call site on purpose.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

# A tri-state observation: True, False, or None for "not observed".
Tri = bool | None

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]


def is_true(v: Tri) -> bool:
    """True only when positively observed. `None` is not a yes."""
    return v is True


def is_false(v: Tri) -> bool:
    """True only when positively observed to be absent. `None` is not a no."""
    return v is False


class Frozen(BaseModel):
    """Base for every state model: immutable, strict, no stray fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- enums


class PowerType(StrEnum):
    MANA = "mana"
    RAGE = "rage"
    ENERGY = "energy"
    FOCUS = "focus"
    NONE = "none"


class Reaction(StrEnum):
    HOSTILE = "hostile"
    NEUTRAL = "neutral"
    FRIENDLY = "friendly"


class Classification(StrEnum):
    NORMAL = "normal"
    ELITE = "elite"
    RARE = "rare"
    RARE_ELITE = "rare_elite"
    BOSS = "boss"


class StepKind(StrEnum):
    """GuideGraph node kinds (PLAN §7.2)."""

    TRAVEL = "travel"
    QUEST_ACCEPT = "quest_accept"
    QUEST_OBJECTIVE = "quest_objective"
    QUEST_TURNIN = "quest_turnin"
    GRIND = "grind"
    TRAIN = "train"
    VENDOR = "vendor"
    REPAIR = "repair"
    FLIGHT = "flight"
    BOAT = "boat"
    HEARTH = "hearth"
    BUY = "buy"
    SKIPPABLE_GATE = "skippable_gate"
    MONEY_GATE = "money_gate"
    DING_GATE = "ding_gate"


class ArmedBy(StrEnum):
    """Who armed the current skill. On every tick, or distillation is impossible.

    `ARCHITECTURE.md` §4 rule 1 — without this you cannot tell whose decision you are
    cloning, and the corpus is unusable no matter how large.
    """

    S1_PREEMPT = "s1_preempt"   # System 1 safety override, no deliberation
    TRACKER = "tracker"         # step predicate fired, mechanical
    POLICY = "policy"           # the distilled local coach
    TEACHER = "teacher"         # Claude or GLM
    HUMAN = "human"             # hotkey takeover; the strongest label we have


class Source(StrEnum):
    """Where this state came from. Everything downstream behaves identically for all."""

    FUSED = "fused"         # live: radio + vision merged
    RADIO = "radio"         # live: addon only
    VISION = "vision"       # live: pixels only, addon off or failed
    REPLAY = "replay"       # recorded trace
    SYNTHETIC = "synthetic" # simulator, no game


class SenseFault(StrEnum):
    """Why the radio is not trusted. Checksum and sequence answer different questions.

    A bad checksum is a misread. A frozen sequence is a live strip that stopped updating —
    a hung addon. Same `addon_ok=False`, different postmortem.
    """

    NONE = "none"
    CHECKSUM = "checksum"       # decoded, failed integrity — bad read
    STALE = "stale"             # sequence has not advanced — addon hung or client frozen
    NOT_FOUND = "not_found"     # strip is not on screen at all
    CALIBRATION = "calibration" # calibration row unreadable, colour transform unsolvable


# --------------------------------------------------------------------------- models


class Char(Frozen):
    name: str | None = None
    cls: str | None = None            # lowercase: mage, warrior, ...
    race: str | None = None
    faction: Literal["alliance", "horde"] | None = None
    level: int | None = Field(default=None, ge=1, le=70)
    xp_pct: Fraction | None = None    # progress through the current level


class Pos(Frozen):
    """Map-relative position. `zone_id` is mandatory context for `mx`/`my`."""

    zone_id: int | None = None        # WorldMapArea row; the tables both sides generate from
    zone: str | None = None           # human label, for prompts and logs only
    sub: str | None = None            # subzone text
    mx: Fraction | None = None        # 0..1 across the zone map
    my: Fraction | None = None
    facing: float | None = None       # radians; see module docstring, do not negate dy
    indoors: Tri = None
    world: tuple[float, float, float] | None = None  # yards; derived, see docstring


class Vitals(Frozen):
    hp: Fraction | None = None
    hp_max: int | None = None
    power: Fraction | None = None
    power_max: int | None = None
    power_type: PowerType | None = None
    combat: Tri = None
    dead: Tri = None
    ghost: Tri = None


class Flags(Frozen):
    """Tri-state, not a list of strings.

    PLAN §6 shows `"flags": ["MOUNTED?", "SWIMMING"]`. A list can only say "not present";
    it cannot say "nobody looked", and those are different facts (ARCHITECTURE.md §6). Use
    `present()` where a prompt wants the compact list form.
    """

    mounted: Tri = None
    swimming: Tri = None
    falling: Tri = None
    on_taxi: Tri = None
    resting: Tri = None
    stealthed: Tri = None
    afk: Tri = None

    def present(self) -> list[str]:
        """Only positively-observed flags, upper-cased. Unknowns are omitted, not denied."""
        return [k.upper() for k, v in self.model_dump().items() if v is True]

    def unknown(self) -> list[str]:
        """Flags nothing has looked at. Useful in a postmortem, and in the eval board."""
        return [k.upper() for k, v in self.model_dump().items() if v is None]


class Target(Frozen):
    """The selected unit.

    `has=False` must be a positive observation of nothing selected, which is harder than it
    sounds: with no target the target frame shows the world behind it, and a grassy or
    textured backdrop reads as a drawn health bar. The gate belongs on whatever measure
    separates a flat UI fill from world texture, calibrated against radio ground truth
    (ARCHITECTURE.md §5) rather than chosen.
    """

    has: Tri = None
    name: str | None = None           # the join key; `/target <exact name>` acquires exactly
    level: int | None = None
    hp: Fraction | None = None
    reaction: Reaction | None = None
    classification: Classification | None = None
    attacking_me: Tri = None
    in_melee: Tri = None              # CheckInteractDistance("target", 3) — ask the client
    dist: float | None = None         # yards
    screen_xy: tuple[float, float] | None = None
    tapped_by_other: Tri = None


class Bags(Frozen):
    free: int | None = Field(default=None, ge=0)
    durability_min: Fraction | None = None   # worst equipped slot
    money_copper: int | None = Field(default=None, ge=0)


class Ui(Frozen):
    """Windows. Vision owns these — the radio can report them but pixels decide (PLAN §5.3)."""

    loot: Tri = None
    gossip: Tri = None
    vendor: Tri = None
    quest_frame: Tri = None
    trainer: Tri = None
    mail: Tri = None
    modal: Tri = None                 # any blocking dialog, incl. the addon-blocked popup
    error: str | None = None          # last UI error text, if any


class Objective(Frozen):
    text: str
    have: int = 0
    need: int = 1

    @property
    def done(self) -> bool:
        return self.have >= self.need


class Quest(Frozen):
    quest_id: int | None = None
    title: str | None = None
    objectives: tuple[Objective, ...] = ()
    complete: Tri = None


class GuidePos(Frozen):
    """Where the playhead is. The coach services this; it does not search Azeroth."""

    graph_id: str | None = None
    step_id: str | None = None
    kind: StepKind | None = None
    age_s: float | None = None        # seconds on this step; drives timeout -> on_fail
    on_route: Tri = None
    progress: Fraction | None = None
    deaths_on_step: int = 0
    attempts: int = 0


class Sense(Frozen):
    """How much of this state to believe, and why."""

    addon_ok: bool = False
    fault: SenseFault = SenseFault.NONE
    seq: int | None = None            # radio sequence counter; frozen seq means stale
    vision_conf: Fraction | None = None
    source: Source = Source.FUSED


class Control(Frozen):
    armed_skill: str | None = None
    armed_by: ArmedBy | None = None
    armed_at: float | None = None     # timestamp the skill was armed
    s1_mode: str | None = None        # travel | combat | loot | recover | idle


class State(Frozen):
    """`state_v1`. One object, versioned, carried everywhere."""

    schema_version: int = SCHEMA_VERSION
    t: float                                   # seconds, wall clock
    client_id: str

    char: Char = Char()
    pos: Pos = Pos()
    vitals: Vitals = Vitals()
    flags: Flags = Flags()
    target: Target = Target()
    bags: Bags = Bags()
    ui: Ui = Ui()
    # `None` means the quest log was not read; `()` means it was read and is empty.
    # These are different facts and the difference is load-bearing: "the objective counter
    # is not ticking" is the first ambiguity `ARCHITECTURE.md` §1 names, and defaulting an
    # unread log to empty answers it wrongly and confidently.
    quests: tuple[Quest, ...] | None = None
    guide: GuidePos = GuidePos()
    sense: Sense = Sense()
    control: Control = Control()

    # Set by `jev.coach.situation`, not by perception. Carried on the state so every
    # stream (tick, decision, grade) can be joined without recomputing binning rules
    # that may have been versioned since. See ARCHITECTURE.md §2.
    situation_key: str | None = None

    def objective_counts(self) -> list[tuple[int, int]] | None:
        """Flattened (have, need) across every tracked quest, or `None` if unread.

        Returning `[]` for an unread log would let a caller conclude "no objectives" from
        "nobody looked", which is the mistake this schema exists to prevent.
        """
        if self.quests is None:
            return None
        return [(o.have, o.need) for q in self.quests for o in q.objectives]

    def quest_log(self) -> tuple[Quest, ...]:
        """The log as a sequence, treating unread as empty.

        For callers that genuinely cannot act on the distinction. Naming it explicitly
        means the choice to discard it is visible at the call site.
        """
        return self.quests or ()


def json_schema() -> dict:
    """The JSON Schema for `state_v1`, for the verifier and the teacher contract."""
    return State.model_json_schema()
