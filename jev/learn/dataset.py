"""Build the training set from the three streams. `DECISIONS.md` V9 lives here.

**Label by outcome, not by authorship.** A decision becomes an example only when the sixty
seconds after it were graded good. That is what makes a wrong teacher call free
(`ARCHITECTURE.md` §2) and it is what lets the student exceed the teacher: where the
teacher was uncertain, different clients try different answers in the same bucket and the
grade picks the winner.

The author filter is a parameter and it defaults to **teacher, human, and graded-good
policy**. Including the policy's own good trajectories is not a convenience — it is the
DAgger correction required by `ARCHITECTURE.md` §4 rule 4. A policy trained only on states
the teacher was consulted about meets a different distribution of states the moment it
starts driving, and the states it then gets wrong are exactly the ones absent from its
training set. Excluding them is the classic distribution-shift bug, so it has to be the
thing you opt into, not the default.

What a row is
-------------
One graded decision, joined to the tick it applied to:

    decision --(decision_id)--> grade      keep only good=True
       |
       +--(run_id, client_id, tick_id)--> tick --> state --> features

Features: two disciplines, on purpose
-------------------------------------
**Coarse, bin-shared with `situation_key`.** The binning helpers are imported from
`jev.coach.situation` rather than re-derived, because these bins have to be the same bins:
the label was given on a bucket — a teacher answered *that circumstance* — so a feature
finer than the bucket invites the model to split on a distinction the label never depended
on. Re-implementing them here would let the two drift silently, which is the failure mode
`ARCHITECTURE.md` §6 names under "generate both sides from one source".

**Fine and continuous, deliberately finer than the key.** `situation_key` is coarse
because it is a *cache* key: two states share it when the same answer is correct for both,
and it throws away anything that would shatter the buckets. A classifier has no cache to
protect and can afford a real number. The clearest case is money — it is not in the key at
all, and "gold is 40 and the mount costs 90" is one of the three genuine ambiguities
`ARCHITECTURE.md` §1 names. A binned wallet cannot represent that question.

Unknown stays unknown. Tri-states become three-valued categories, never two; continuous
unknowns become NaN and are handed to a model that splits on missingness natively, rather
than being imputed to a number nobody observed.
"""

from __future__ import annotations

import math
import pathlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# Private on purpose in `situation`, imported on purpose here: the coarse features and the
# cache key must bin identically or the agreement metric compares two different questions.
from jev.coach.situation import _age, _bags, _deaths, _durability, _health, _progress, _tri
from jev.eval.counters import bracket_of
from jev.learn.episode import Stream, read
from jev.world.state_v1 import ArmedBy, Classification, Source, State, StepKind

# --------------------------------------------------------------------------- authors

TEACHER_AND_HUMAN: frozenset[str] = frozenset({ArmedBy.TEACHER.value, ArmedBy.HUMAN.value})
"""The cautious set: only what an authority chose. Trains a student that is capped by its
teacher and blind to the states it will actually meet once it drives."""

DEFAULT_AUTHORS: frozenset[str] = TEACHER_AND_HUMAN | {ArmedBy.POLICY.value}
"""Teacher, human, and the policy's own graded-good rows. The default, because the
alternative is a distribution-shift bug you do not find until handover."""


# -------------------------------------------------------------------------- features

CATEGORICAL: dict[str, tuple[str, ...]] = {
    # Every vocabulary here is produced by a `jev.coach.situation` binning helper, so the
    # classifier and the cache key agree by construction. Values are ordered by severity
    # where severity exists; the model is told these are categorical so the order is
    # documentation rather than signal.
    "step_kind": ("?", *(k.value for k in StepKind)),
    "age_band": ("?", "fresh", "slow", "stuck", "hopeless"),
    "deaths_band": ("0", "1", "2", "3+"),
    "route": ("?", "on", "off"),
    "progress_band": ("?", "none", "part", "done"),
    "health_band": ("?", "ok", "hurt", "critical", "dead", "ghost"),
    "combat": ("?", "n", "y"),
    "bag_band": ("?", "ok", "tight", "full"),
    "durability_band": ("?", "ok", "low", "broken"),
    "sense": ("blind", "ok"),
    # Beyond the key. Target *identity* shatters buckets and is excluded everywhere, but
    # target *classification* is a five-value set that changes the answer outright — "the
    # route says north and there is a level 30 elite standing on it" (ARCHITECTURE.md §1).
    "target": ("?", "n", "y"),
    "target_class": ("?", *(c.value for c in Classification)),
    "modal": ("?", "n", "y"),
    "mounted": ("?", "n", "y"),
}

CONTINUOUS: tuple[str, ...] = (
    "hp",                    # the bands say hurt; the number says how hurt
    "power",                 # not in the key at all: a mage at 5% mana cannot grind
    "age_s",
    "progress",
    "deaths_on_step",
    "attempts",
    "bags_free",
    "durability_min",
    "gold",                  # the mount gate. The key does not carry money at all
    "level",
    "xp_pct",                # how close the next ding is changes whether to push on
    "target_dist",
    "target_hp",
    "target_level_delta",    # target level minus ours: the elite-on-the-road question
    "objectives_done",
    "objectives_open",
    "vision_conf",
)

FEATURE_NAMES: tuple[str, ...] = (*CATEGORICAL, *CONTINUOUS)
CATEGORICAL_INDICES: tuple[int, ...] = tuple(range(len(CATEGORICAL)))
"""Categoricals first, so the index list a model needs is a prefix and stays correct when
a continuous feature is appended."""

EXCLUDED: dict[str, str] = {
    "control.armed_skill": "it is the label — feeding the armed skill predicts itself",
    "control.armed_by": "the author. V9 trains on outcomes, not on who chose",
    "control.armed_at": "a timestamp of the label",
    "shadow_intent": "the previous policy's own answer; training on it trains on itself",
    "shadow_skill": "same",
    "shadow_confidence": "same",
    "situation_key": "the bucket the label was given in; a per-bucket lookup, memorised",
    "guide.step_id": "identity, not circumstance. A model that memorises step ids cannot "
                     "transfer to the next zone, and per-bracket promotion assumes it can",
    "guide.graph_id": "same",
    "pos.mx/my": "exact position shatters buckets — the key excludes it for that reason",
    "target.name": "identity again; the classification is the part that changes the answer",
    "t": "wall clock. Nothing about the right move depends on it",
    "client_id": "which body it happened to; the policy is shared across all of them",
}
"""Fields that must never become features, each with the reason. Held as data rather than
as a comment so a test can assert the label is not among the inputs."""


def _f(v: float | int | None) -> float:
    """Unknown becomes NaN, never 0.0. A zero here is a claim that something was measured
    at zero, and the model is chosen so that NaN survives as its own branch."""
    return math.nan if v is None else float(v)


def featurize(state: State) -> dict[str, Any]:
    """One state to one feature row. Pure, and the only place features are defined."""
    g, v, b, tg = state.guide, state.vitals, state.bags, state.target
    # `None` is an unread log; `[]` is a log that was read and holds no objectives.
    # The schema used to conflate these and both had to be emitted as NaN, which blinded
    # the model to the first ambiguity ARCHITECTURE.md §1 names — "the objective counter
    # is not ticking". A read-but-empty log is now a real, learnable zero.
    counts = state.objective_counts()
    done = None if counts is None else sum(1 for have, need in counts if have >= need)
    open_ = None if counts is None else len(counts) - done

    return {
        "step_kind": g.kind.value if g.kind else "?",
        "age_band": _age(g.age_s),
        "deaths_band": _deaths(g.deaths_on_step),
        "route": _tri(g.on_route, "on", "off"),
        "progress_band": _progress(g.progress),
        "health_band": _health(state),
        "combat": _tri(v.combat, "y", "n"),
        "bag_band": _bags(b.free),
        "durability_band": _durability(b.durability_min),
        "sense": "ok" if state.sense.addon_ok else "blind",
        "target": _tri(tg.has, "y", "n"),
        "target_class": tg.classification.value if tg.classification else "?",
        "modal": _tri(state.ui.modal, "y", "n"),
        "mounted": _tri(state.flags.mounted, "y", "n"),
        "hp": _f(v.hp),
        "power": _f(v.power),
        "age_s": _f(g.age_s),
        "progress": _f(g.progress),
        "deaths_on_step": float(g.deaths_on_step),
        "attempts": float(g.attempts),
        "bags_free": _f(b.free),
        "durability_min": _f(b.durability_min),
        "gold": _f(None if b.money_copper is None else b.money_copper / 10_000),
        "level": _f(state.char.level),
        "xp_pct": _f(state.char.xp_pct),
        "target_dist": _f(tg.dist),
        "target_hp": _f(tg.hp),
        "target_level_delta": _f(
            None if (tg.level is None or state.char.level is None) else tg.level - state.char.level
        ),
        # An empty quest tuple cannot be told apart from an unread quest log in `state_v1`,
        # so it reads as unknown rather than as "no objectives". Reporting zero open
        # objectives for a log nobody looked at would invent the one observation the first
        # named ambiguity in ARCHITECTURE.md §1 turns on.
        "objectives_done": _f(done),
        "objectives_open": _f(open_),
        "vision_conf": _f(state.sense.vision_conf),
    }


def _code(feature: str, value: Any) -> float:
    """Category to ordinal code. A value this featuriser has never heard of is *unobserved*,
    not a new fact, so it takes the unknown slot rather than inventing a class."""
    vocab = CATEGORICAL[feature]
    text = str(value)
    if text in vocab:
        return float(vocab.index(text))
    return float(vocab.index("?")) if "?" in vocab else 0.0


def encode(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    """Feature dicts to the float matrix the model eats. Column order is `FEATURE_NAMES`."""
    out = np.empty((len(rows), len(FEATURE_NAMES)), dtype=np.float64)
    for i, row in enumerate(rows):
        for j, name in enumerate(FEATURE_NAMES):
            out[i, j] = (
                _code(name, row.get(name, "?")) if name in CATEGORICAL else _f(row.get(name))
            )
    return out


def encode_one(row: Mapping[str, Any]) -> np.ndarray:
    return encode([row])


# -------------------------------------------------------------------------- examples


@dataclass(frozen=True)
class Example:
    """One graded-good decision, ready to train on."""

    run_id: str
    decision_id: str
    client_id: str
    t: float
    situation_key: str
    author: str
    intent: str
    skill: str | None
    reward: float
    level: int | None
    bracket: str | None
    features: dict[str, Any]


@dataclass(frozen=True)
class Dropped:
    """Why rows did not become examples.

    Kept and reported rather than discarded quietly. A training set that shrinks from
    nine thousand rows to forty is a bug somewhere upstream, and a builder that returns
    forty rows with no explanation is a builder that hides it for a week.
    """

    no_grade: int = 0
    not_good: int = 0
    wrong_author: int = 0
    no_tick: int = 0
    unparsable_state: int = 0
    no_intent: int = 0
    synthetic: int = 0

    def total(self) -> int:
        return (
            self.no_grade + self.not_good + self.wrong_author
            + self.no_tick + self.unparsable_state + self.no_intent + self.synthetic
        )


@dataclass(frozen=True)
class Dataset:
    examples: tuple[Example, ...]
    authors: frozenset[str]
    dropped: Dropped

    def __len__(self) -> int:
        return len(self.examples)

    def X(self) -> np.ndarray:
        return encode([e.features for e in self.examples])

    def intents(self) -> list[str]:
        return [e.intent for e in self.examples]

    def skills(self) -> list[str | None]:
        return [e.skill for e in self.examples]

    def situation_keys(self) -> set[str]:
        return {e.situation_key for e in self.examples}

    def author_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.examples:
            out[e.author] = out.get(e.author, 0) + 1
        return out

    def for_bracket(self, bracket: str) -> Dataset:
        """One band's rows. Promotion is per bracket, so training and measuring are too."""
        return Dataset(
            examples=tuple(e for e in self.examples if e.bracket == bracket),
            authors=self.authors,
            dropped=self.dropped,
        )

    def brackets(self) -> list[str]:
        return sorted({e.bracket for e in self.examples if e.bracket})


def _normalise_authors(authors: Iterable[str | ArmedBy]) -> frozenset[str]:
    return frozenset(str(a) for a in authors)


def build_from_rows(
    ticks: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    grades: Sequence[Mapping[str, Any]],
    *,
    authors: Iterable[str | ArmedBy] = DEFAULT_AUTHORS,
    include_synthetic: bool = False,
) -> Dataset:
    """Join the three streams into examples. The outcome filter is not optional.

    `good` is never a parameter. The author set is, because who chose is a policy question;
    whether it worked is the whole basis of the corpus (`DECISIONS.md` V9) and an override
    for it would be a foot-gun that quietly caps the student at the teacher.
    """
    wanted = _normalise_authors(authors)
    by_tick = {
        (t.get("run_id"), t.get("client_id"), t.get("tick_id")): t
        for t in ticks
    }
    by_decision = {(g.get("run_id"), g.get("decision_id")): g for g in grades}

    examples: list[Example] = []
    no_grade = not_good = wrong_author = no_tick = unparsable = no_intent = synthetic = 0

    for d in decisions:
        author = str(d.get("author") or "")
        if author not in wanted:
            wrong_author += 1
            continue
        g = by_decision.get((d.get("run_id"), d.get("decision_id")))
        if g is None:
            # A decision with no grade is not an example. Absence of an outcome is not a
            # good outcome, and a live run always has a tail of ungraded decisions.
            no_grade += 1
            continue
        if not g.get("good") or d.get("status", "ok") != "ok" or d.get("intent") == "escalate":
            not_good += 1
            continue
        intent = d.get("intent")
        if not intent:
            no_intent += 1
            continue
        tick = by_tick.get((d.get("run_id"), d.get("client_id"), d.get("tick_id")))
        if tick is None:
            no_tick += 1
            continue
        state = _parse_state(tick.get("state"))
        if state is None:
            unparsable += 1
            continue
        if not include_synthetic and state.sense.source in (Source.SYNTHETIC, Source.REPLAY):
            synthetic += 1
            continue

        examples.append(
            Example(
                run_id=str(d.get("run_id")),
                decision_id=str(d.get("decision_id")),
                client_id=str(d.get("client_id")),
                t=float(d.get("t") or 0.0),
                situation_key=str(tick.get("situation_key") or d.get("situation_key") or ""),
                author=author,
                intent=str(intent),
                skill=d.get("skill"),
                reward=float(g.get("reward") or 0.0),
                level=state.char.level,
                bracket=bracket_of(state.char.level),
                features=featurize(state),
            )
        )

    return Dataset(
        examples=tuple(examples),
        authors=wanted,
        dropped=Dropped(
            no_grade=no_grade,
            not_good=not_good,
            wrong_author=wrong_author,
            no_tick=no_tick,
            unparsable_state=unparsable,
            no_intent=no_intent,
            synthetic=synthetic,
        ),
    )


def _parse_state(raw: Any) -> State | None:
    """A row this build cannot read is dropped and counted, never guessed at.

    `State` forbids extra fields, so a corpus written by a newer `state_v1` lands here. The
    count is the signal that a migration is owed; substituting defaults would train on
    states nobody was ever in.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return State.model_validate(raw)
    except Exception:
        return None


def load_streams(run_dir: str | pathlib.Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Read one run, preferring parquet where the converter has already been.

    The learner is the reader the nightly conversion exists for, so it looks for parquet
    first; the board reads the same runs live, where parquet does not exist yet. Both end
    up with rows of the same shape, which is the point of `parquet.read_parquet` being an
    exact inverse.
    """
    # Imported here, not at module scope: `jev.learn.parquet` needs pyarrow, and this
    # module is also what the live coach featurises through. A client that only wants to
    # predict must not have to install the whole `learn` extra to do it.
    from jev.learn.parquet import read_parquet

    d = pathlib.Path(run_dir)
    out: list[list[dict]] = []
    for stream in (Stream.TICKS, Stream.DECISIONS, Stream.GRADES):
        pq_path, jsonl_path = d / f"{stream.value}.parquet", d / f"{stream.value}.jsonl"
        if pq_path.exists() and (not jsonl_path.exists()
                                or pq_path.stat().st_mtime_ns >= jsonl_path.stat().st_mtime_ns):
            out.append(read_parquet(pq_path))
        elif jsonl_path.exists():
            out.append(read(jsonl_path))
        else:
            out.append([])
    return out[0], out[1], out[2]


def build(
    run_dirs: Iterable[str | pathlib.Path],
    *,
    authors: Iterable[str | ArmedBy] = DEFAULT_AUTHORS,
    include_synthetic: bool = False,
) -> Dataset:
    """Build across many runs. Rows are keyed by run, so mixing runs is safe."""
    ticks: list[dict] = []
    decisions: list[dict] = []
    grades: list[dict] = []
    for d in run_dirs:
        t, dec, g = load_streams(d)
        ticks += t
        decisions += dec
        grades += g
    return build_from_rows(ticks, decisions, grades, authors=authors,
                           include_synthetic=include_synthetic)
