"""`situation_key` — the canonical bucket for "this circumstance".

One key, four jobs (`ARCHITECTURE.md` §2):

  * **teacher dedup** — three clients stuck on one step is one question, not three
  * **counterfactual bandit** — group differing choices on one situation, compare outcomes
  * **agreement metric** — policy prediction vs teacher choice, same bucket
  * **answer cache** — a recent answer for this bucket is reusable without asking

Because those four want the same thing, the binning has one job: **two states share a key
exactly when the same answer is correct for both.** Too fine and nothing ever matches, so
every question is asked afresh and nothing can be compared. Too coarse and distinct
situations collide, so the cache returns a confidently wrong answer and the agreement
metric flatters itself.

What is deliberately *not* in the key: exact position, exact health, target identity,
wall-clock time. None of them change what to do; all of them would shatter the buckets.

Bins are versioned separately from the corpus. Changing them invalidates the *cache* — old
rows keep their old key and stay comparable among themselves, which is why the version is
in the key rather than alongside it.
"""

from __future__ import annotations

from jev.world.state_v1 import State

SITUATION_VERSION = 1


def _age(age_s: float | None) -> str:
    """How long this step has been open, relative to a step going normally."""
    if age_s is None:
        return "?"
    if age_s < 60:
        return "fresh"
    if age_s < 180:
        return "slow"
    if age_s < 420:
        return "stuck"
    return "hopeless"


def _deaths(n: int) -> str:
    return str(n) if n < 3 else "3+"


def _progress(p: float | None) -> str:
    if p is None:
        return "?"
    if p <= 0.0:
        return "none"
    return "done" if p >= 1.0 else "part"


def _health(s: State) -> str:
    """Bands, not numbers — the bands are where the answer actually changes."""
    if s.vitals.ghost is True:
        return "ghost"
    if s.vitals.dead is True:
        return "dead"
    hp = s.vitals.hp
    if hp is None:
        return "?"
    if hp < 0.20:
        return "critical"
    if hp < 0.55:
        return "hurt"
    return "ok"


def _bags(free: int | None) -> str:
    if free is None:
        return "?"
    if free == 0:
        return "full"
    return "tight" if free <= 2 else "ok"


def _durability(d: float | None) -> str:
    if d is None:
        return "?"
    if d <= 0.05:
        return "broken"
    return "low" if d < 0.35 else "ok"


def _tri(v: bool | None, yes: str, no: str) -> str:
    """Tri-state renders as three values, never two. Unknown is its own situation —
    a decision made blind is not the same decision made informed."""
    return "?" if v is None else (yes if v else no)


def situation_key(state: State) -> str:
    """A readable, stable bucket for this state.

    Readable on purpose: it appears in logs, in the dashboard and in teacher prompts, and
    an opaque hash there costs more debugging time than it saves bytes. Use `digest()`
    where a fixed-width index key is wanted.
    """
    g = state.guide
    parts = [
        f"v{SITUATION_VERSION}",
        g.graph_id or "-",
        g.step_id or "-",
        f"age:{_age(g.age_s)}",
        f"d:{_deaths(g.deaths_on_step)}",
        f"route:{_tri(g.on_route, 'on', 'off')}",
        f"prog:{_progress(g.progress)}",
        f"hp:{_health(state)}",
        f"cmb:{_tri(state.vitals.combat, 'y', 'n')}",
        f"bag:{_bags(state.bags.free)}",
        f"dur:{_durability(state.bags.durability_min)}",
        # Whether we can see is part of the situation: the right move with a working
        # radio is often not the right move blind.
        f"sense:{'ok' if state.sense.addon_ok else 'blind'}",
    ]
    return "|".join(parts)


def digest(key: str, length: int = 12) -> str:
    """Short fixed-width form, for indexes and filenames. Not a security boundary."""
    import hashlib

    return hashlib.blake2b(key.encode("utf-8"), digest_size=length // 2).hexdigest()


def with_key(state: State) -> State:
    """Return `state` carrying its own key, so downstream never recomputes binning rules
    that may have been versioned since the row was written."""
    return state.model_copy(update={"situation_key": situation_key(state)})
