"""Train the coach policy: `state -> intent (+ skill)`.

This is PLAN §12.1 step 4 and the thing `ARCHITECTURE.md` §0 is aiming at — the rung that
makes the teacher an improvement engine rather than a dependency. It is a few hundred to a
few thousand tabular rows with thirty-one columns, about half of them unknown on any given
tick, and it must produce a *calibrated* answer because the coach escalates when it is
uncertain. That shape picks the model.

Why a histogram gradient-boosted tree
-------------------------------------
`HistGradientBoostingClassifier`, over the two obvious alternatives:

* **Over a random forest or a plain `GradientBoostingClassifier`:** neither accepts NaN, so
  both force an imputer, and an imputer fills "nobody looked at the quest log" with the
  median quest log. That is `ARCHITECTURE.md` §6's "unknown is not a negative fact"
  violated inside the model, where no test can see it. This estimator splits on
  missingness natively and sends unknowns down their own branch, so an unobserved field
  stays unobserved all the way to the leaf. Roughly half our columns can be `None`, so this
  is not a detail.
* **Over a single decision tree:** a tree is more legible, which matters here, but its leaf
  probabilities are worthless — a pure leaf built from two rows reports 1.0. Confidence is
  load-bearing (the coach arms on it and escalates without it), so an averaged ensemble is
  worth the legibility.

Native categorical support is the third reason: thirteen categorical columns one-hot into
sixty-odd on a few hundred rows, and the split it wants is "this band or that band", not
"is this one bit set".

Honest cold start
-----------------
With no corpus the policy reports **no intent and zero confidence** — not a guess. It
returns `None` rather than, say, `wait`, because `wait` is an instruction: something in
flight, do not act. An untrained model emitting `wait` at low confidence is a model whose
caller has to know to distrust it. `None` is an abstention, and "absence of an answer is
not an answer" is exactly the invariant that keeps the scripted GOAP default driving until
there is a reason to hand over.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from jev.coach.schema import Intent
from jev.learn.dataset import (
    CATEGORICAL_INDICES,
    FEATURE_NAMES,
    Dataset,
    encode,
    encode_one,
    featurize,
)
from jev.world.state_v1 import State

POLICY_FORMAT = 1
"""Bump when the saved payload changes shape. A load of an older format fails loudly; a
policy that loads and then predicts from the wrong columns fails silently, for a night."""

EVIDENCE_HALF_LIFE = 200
"""Rows at which the policy reports half the confidence its probabilities claim.

200 over 20: twenty graded-good rows covers a handful of `situation_key` buckets, and a
policy allowed to be confident on twenty rows will be driving a character on two examples
of the situation it is in. 200 is roughly a night of a single client's escalations, which
is the smallest corpus anyone should hand a body to."""

UNSEEN_SITUATION_FACTOR = 0.5
"""Multiplier when the state's bucket was never in training.

A bucket the policy has not seen is the definition of the case the coach must escalate
(`ARCHITECTURE.md` §1). Halved rather than zeroed: zeroing would make every new step an
escalation forever, so the teacher bill could never fall on new content, which is the one
thing this whole rung exists to do."""

CONSTANT_HEAD_MAX = 0.5
"""Ceiling for a head trained on a corpus with only one answer in it.

Such a head is right about every row it has ever seen and knows nothing about when the
answer changes, because it has never seen it change. It is a default, not a decision, and
it is capped below any threshold a caller would arm on."""

SEED = 0
"""Fixed, because a nightly retrain that produces a different policy from the same corpus
cannot be attributed to anything."""


# ----------------------------------------------------------------------------- heads


def observed_columns(X: np.ndarray) -> np.ndarray:
    """Mask of columns with at least one observed value in this corpus.

    A column nothing ever observed cannot be split on — there is no threshold to put
    between zero values — and the estimator says so by refusing to bin it. Dropping it is
    not a workaround for that refusal, it is agreeing with it: a feature with no
    observations carries no signal about anything.

    Do not be tempted to fix this by imputing instead. Filling an unobserved column with a
    median invents the observation, which is the one thing `ARCHITECTURE.md` §6 forbids,
    and it does it in the one place no test can see.
    """
    return ~np.all(np.isnan(X), axis=0)


@dataclass
class _Head:
    """One classifier, its label set, and the columns it was actually fitted on.

    The column mask travels with the model because prediction must feed it exactly the
    columns training gave it. A model asked to predict from a wider matrix than it was fit
    on does not raise — it reads the wrong column and answers confidently.
    """

    classes: tuple[str, ...]
    model: Any | None
    n: int
    columns: np.ndarray | None = None

    def decide(self, x: np.ndarray) -> tuple[str | None, float]:
        if not self.classes:
            return None, 0.0
        if self.model is None:
            return self.classes[0], CONSTANT_HEAD_MAX
        if self.columns is not None:
            x = x[:, self.columns]
        proba = self.model.predict_proba(x)[0]
        i = int(np.argmax(proba))
        return str(self.model.classes_[i]), float(proba[i])


def _fit_head(X: np.ndarray, y: Sequence[str], *, seed: int) -> _Head:
    labels = tuple(sorted(set(y)))
    if len(labels) < 2 or len(y) < 2:
        # Not an error. A bracket where the graded-good answer was always `advance` is a
        # real and common corpus; it simply cannot teach anything about the alternative.
        return _Head(classes=labels, model=None, n=len(y))

    mask = observed_columns(X)
    if not mask.any():
        return _Head(classes=labels, model=None, n=len(y))
    kept = np.flatnonzero(mask).tolist()
    categorical = frozenset(CATEGORICAL_INDICES)

    clf = HistGradientBoostingClassifier(
        categorical_features=[i for i, col in enumerate(kept) if col in categorical],
        # Tuned for hundreds of rows, not thousands: the defaults (min_samples_leaf=20,
        # early stopping on an auto-held-out tenth) would spend most of a small corpus on
        # validation and then refuse to split on anything.
        min_samples_leaf=5,
        max_leaf_nodes=15,
        max_iter=200,
        learning_rate=0.1,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )
    clf.fit(X[:, mask], np.asarray(y, dtype=object))
    return _Head(classes=labels, model=clf, n=len(y), columns=mask)


# ------------------------------------------------------------------------ prediction


@dataclass(frozen=True)
class Prediction:
    """What the policy thinks, and how much of that is evidence.

    `confidence` is the intent confidence, because intent is what Gate C measures and what
    the coach escalates on. `skill_confidence` is reported separately rather than folded
    in: naming the right strategy and then arming the wrong program for it is a different
    failure with a different fix, and averaging them hides both.
    """

    intent: str | None
    skill: str | None
    confidence: float
    skill_confidence: float | None = None
    situation_key: str | None = None
    seen_situation: bool = False

    @property
    def is_abstention(self) -> bool:
        """No intent. Not a `wait`, not a guess — nothing, so the scripted default drives."""
        return self.intent is None


ABSTAIN = Prediction(intent=None, skill=None, confidence=0.0)


@dataclass
class Policy:
    """A trained coach. Names itself `policy:v3`, which is what `DecisionRow.model` records."""

    version: int = 1
    intent_head: _Head = field(default_factory=lambda: _Head((), None, 0))
    skill_head: _Head = field(default_factory=lambda: _Head((), None, 0))
    n_rows: int = 0
    seen_keys: frozenset[str] = frozenset()
    trained_at: float = 0.0
    authors: frozenset[str] = frozenset()
    brackets: tuple[str, ...] = ()
    sklearn_version: str = sklearn.__version__

    # ---------------------------------------------------------------- identity

    @property
    def model_name(self) -> str:
        return f"policy:v{self.version}"

    @property
    def is_cold(self) -> bool:
        return self.n_rows == 0 or not self.intent_head.classes

    # ---------------------------------------------------------------- inference

    def predict(self, state: State) -> tuple[str | None, str | None, float]:
        """`(intent, skill, confidence)`. The contract the coach calls on the hot path."""
        p = self.predict_full(state)
        return p.intent, p.skill, p.confidence

    def predict_full(self, state: State) -> Prediction:
        return self.predict_features(featurize(state), situation_key=state.situation_key)

    def shadow(self, state: State) -> Prediction:
        """The always-on shadow prediction (`ARCHITECTURE.md` §4 rule 2).

        Runs on every tick, including the ticks it is not driving, which is the only way
        Gate C's 90% is measurable at the end. It therefore never raises: an exception in a
        metric must not take down a live client, so anything unexpected becomes an
        abstention with zero confidence and the row still gets written.
        """
        try:
            return self.predict_full(state)
        except Exception:
            return ABSTAIN

    def predict_features(
        self, features: Mapping[str, Any], *, situation_key: str | None = None
    ) -> Prediction:
        """Predict from an already-built feature row. Used by offline evaluation, which has
        rows rather than states."""
        if self.is_cold:
            return Prediction(
                intent=None, skill=None, confidence=0.0, situation_key=situation_key
            )

        x = encode_one(features)
        seen = situation_key is not None and situation_key in self.seen_keys
        intent, p_intent = self.intent_head.decide(x)
        confidence = self._confidence(p_intent, seen)

        skill: str | None = None
        p_skill: float | None = None
        if intent is not None and intent != Intent.WAIT.value and self.skill_head.classes:
            skill, raw = self.skill_head.decide(x)
            p_skill = self._confidence(raw, seen)

        return Prediction(
            intent=intent,
            skill=skill,
            confidence=confidence,
            skill_confidence=p_skill,
            situation_key=situation_key,
            seen_situation=seen,
        )

    def _confidence(self, p: float, seen: bool) -> float:
        """Class probability, discounted by how much evidence stands behind it.

        A raw `predict_proba` on three hundred rows is a statement about three hundred
        rows, not about the world; handing it to a caller that arms skills on it is how a
        policy takes over a bracket it has not learned.
        """
        evidence = self.n_rows / (self.n_rows + EVIDENCE_HALF_LIFE)
        return float(p * evidence * (1.0 if seen else UNSEEN_SITUATION_FACTOR))

    # ---------------------------------------------------------------- persistence

    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        """Write to a file, or to `policy_v{version}.joblib` inside a directory."""
        p = pathlib.Path(path)
        if p.is_dir():
            p = p / f"policy_v{self.version}.joblib"
        p.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "format": POLICY_FORMAT,
                "version": self.version,
                "feature_names": FEATURE_NAMES,
                "intent_head": self.intent_head,
                "skill_head": self.skill_head,
                "n_rows": self.n_rows,
                "seen_keys": self.seen_keys,
                "trained_at": self.trained_at,
                "authors": self.authors,
                "brackets": self.brackets,
                "sklearn_version": self.sklearn_version,
            },
            p,
        )
        return p

    @staticmethod
    def load(path: str | pathlib.Path) -> Policy:
        """Load, refusing anything whose columns are not the ones the featuriser emits.

        A model saved against a different `FEATURE_NAMES` still predicts — from the wrong
        columns, confidently, with no error anywhere. That is the single most expensive
        silent failure available to this module, so it is checked on every load.
        """
        payload = joblib.load(pathlib.Path(path))
        if payload.get("format") != POLICY_FORMAT:
            raise ValueError(
                f"policy format {payload.get('format')} != {POLICY_FORMAT}; retrain it"
            )
        if tuple(payload.get("feature_names", ())) != FEATURE_NAMES:
            raise ValueError(
                "policy was trained on different features than this featuriser emits; "
                "retrain rather than predicting from mismatched columns"
            )
        return Policy(
            version=payload["version"],
            intent_head=payload["intent_head"],
            skill_head=payload["skill_head"],
            n_rows=payload["n_rows"],
            seen_keys=frozenset(payload["seen_keys"]),
            trained_at=payload["trained_at"],
            authors=frozenset(payload.get("authors", ())),
            brackets=tuple(payload.get("brackets", ())),
            sklearn_version=payload.get("sklearn_version", "?"),
        )

    def describe(self) -> str:
        """One block for the nightly log. Says what it learned from, not just how well."""
        if self.is_cold:
            return f"{self.model_name}  COLD  no training rows; every state abstains"
        return (
            f"{self.model_name}  rows={self.n_rows}  buckets={len(self.seen_keys)}\n"
            f"  intents={','.join(self.intent_head.classes)}\n"
            f"  skills={','.join(self.skill_head.classes) or '-'}\n"
            f"  authors={','.join(sorted(self.authors)) or '-'}"
            f"  brackets={','.join(self.brackets) or '-'}\n"
            f"  sklearn={self.sklearn_version}  trained_at={self.trained_at:.0f}"
        )


def cold_start(version: int = 1) -> Policy:
    """A policy with no corpus. Useful from day 0: the shadow column wants filling from the
    first run, and a policy that abstains honestly fills it without touching the character."""
    return Policy(version=version)


# -------------------------------------------------------------------------- training


def train(dataset: Dataset, *, version: int = 1, seed: int = SEED) -> Policy:
    """Fit both heads on graded-good rows.

    An empty dataset is not an error and does not raise: it returns the cold-start policy.
    The nightly job runs on nights where nothing was graded, and a learner that crashes on
    an empty corpus is a learner that stops the pipeline on its quietest night.
    """
    if len(dataset) == 0:
        return cold_start(version=version)

    X = dataset.X()
    intents = dataset.intents()

    skill_rows = [(i, s) for i, s in enumerate(dataset.skills()) if s]
    if skill_rows:
        idx = [i for i, _ in skill_rows]
        skill_head = _fit_head(X[idx], [s for _, s in skill_rows], seed=seed)
    else:
        skill_head = _Head((), None, 0)

    return Policy(
        version=version,
        intent_head=_fit_head(X, intents, seed=seed),
        skill_head=skill_head,
        n_rows=len(dataset),
        seen_keys=frozenset(dataset.situation_keys()),
        trained_at=time.time(),
        authors=frozenset(dataset.authors),
        brackets=tuple(dataset.brackets()),
    )


# ------------------------------------------------------------------------ evaluation


@dataclass(frozen=True)
class Evaluation:
    """How the policy does on rows it can be scored against.

    Scored on the corpus it was trained on unless a held-out set is passed, and it says so:
    a resubstitution number is an upper bound and the only honest way to print one is next
    to the word that names it.
    """

    n: int
    intent_accuracy: float | None
    skill_accuracy: float | None
    mean_confidence: float | None
    held_out: bool

    def __str__(self) -> str:
        kind = "held-out" if self.held_out else "resubstitution"
        acc = "-" if self.intent_accuracy is None else f"{self.intent_accuracy * 100:.1f}%"
        sk = "-" if self.skill_accuracy is None else f"{self.skill_accuracy * 100:.1f}%"
        mc = "-" if self.mean_confidence is None else f"{self.mean_confidence:.2f}"
        return f"n={self.n} ({kind})  intent {acc}  skill {sk}  mean confidence {mc}"


def evaluate(policy: Policy, dataset: Dataset, *, held_out: bool = False) -> Evaluation:
    if len(dataset) == 0 or policy.is_cold:
        return Evaluation(len(dataset), None, None, None, held_out)

    preds = [
        policy.predict_features(e.features, situation_key=e.situation_key)
        for e in dataset.examples
    ]
    hits = sum(1 for p, e in zip(preds, dataset.examples, strict=True) if p.intent == e.intent)
    scored = [(p, e) for p, e in zip(preds, dataset.examples, strict=True) if e.skill]
    skill_hits = sum(1 for p, e in scored if p.skill == e.skill)

    return Evaluation(
        n=len(dataset),
        intent_accuracy=hits / len(dataset),
        skill_accuracy=(skill_hits / len(scored)) if scored else None,
        mean_confidence=float(np.mean([p.confidence for p in preds])),
        held_out=held_out,
    )


def encode_dataset(dataset: Dataset) -> np.ndarray:
    """The feature matrix, for callers doing their own model work on the same columns."""
    return encode([e.features for e in dataset.examples])
