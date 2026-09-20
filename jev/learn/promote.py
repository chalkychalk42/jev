"""Promotion — of a policy over a level band, and of a skill through its lifecycle.

Two ledgers, one rule each, and the rule in both cases is *measured, never asserted*.

**Per bracket, never global** (`ARCHITECTURE.md` §4). A policy that has learned 12-18 has
learned 12-18. Elwynn's answers are not Westfall's, and a global promotion hands the whole
farm to a model that was only ever tested on one zone — which is how a wean turns into an
outage. So the unit of promotion is the band, the heartbeat drops for that band alone, and
nothing one bracket does can change another's state.

A band promotes when policy/teacher agreement is at least 90% (Gate C) **and** deaths/h has
not risen. Agreement alone is not enough: a policy can agree with the teacher on every
question it is asked and still be killing the character between questions, and deaths/h is
the cheapest thing that notices.

Skill promotion is PLAN §10: `proposed` -> 2 clients x 3 runs -> `stable`, and a success
rate under 0.4 over 20 attempts -> `retired`. A retired skill is not retrievable without
the teacher in the loop — retirement is a statement that normal retrieval stops returning
it, not a delete, because the teacher may need to read the thing it is replacing.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jev.eval.counters import Check, Counters

# ----------------------------------------------------------------- policy promotion

AGREEMENT_GATE = 0.90
"""Gate C, verbatim: ">= 90% agreement with Jev" on one bracket (PLAN §3)."""

MIN_AGREEMENT_SAMPLES = 50
"""Teacher-decided ticks needed before an agreement figure means anything.

50 over 10: at ten samples one disagreement moves the number ten points, so the gate would
be decided by which tick the teacher happened to be asked about. At fifty the same
disagreement costs two points, which is noise rather than a verdict."""

DEATHS_SLACK = 0.5
"""How much deaths/h may rise and still count as "has not risen".

Deaths are integers over a window: one death in a two-hour measurement is 0.5/h, so this
is exactly one extra death over the span the freeze rule already demands. Zero slack would
make promotion hostage to a single unlucky pull; a wider slack would let the policy trade
the character's life for agreement."""

DEFAULT_HEARTBEAT_S = 20.0
"""The idle "still ok?" call, PLAN §9.1's 15-30 s, taken in the middle."""

HEARTBEAT_DROP = 10.0
"""Gate C: a promoted bracket runs with the heartbeat 10x slower — and only that bracket."""


@dataclass
class _Band:
    """One band's mutable state. `baseline` is the deaths/h promotion is measured against."""

    promoted: bool = False
    baseline_deaths_per_h: float | None = None
    agreement: float | None = None
    samples: int = 0
    deaths_per_h: float | None = None
    reason: str = "no measurement yet"
    promoted_at: float | None = None


@dataclass(frozen=True)
class BracketState:
    """What the ledger will say about one band, with the arithmetic that decided it."""

    bracket: str
    promoted: bool
    heartbeat_s: float
    agreement: float | None
    samples: int
    deaths_per_h: float | None
    baseline_deaths_per_h: float | None
    reason: str
    checks: tuple[Check, ...] = ()


class PromotionLedger:
    """Which bands the policy drives, and how often the teacher still checks in.

    Held as a ledger rather than recomputed from the board each time because promotion has
    memory: "deaths/h has not risen" is a comparison against what this band used to cost,
    and a stateless check has nothing to compare to.
    """

    def __init__(self, *, heartbeat_s: float = DEFAULT_HEARTBEAT_S) -> None:
        self.heartbeat_base_s = heartbeat_s
        self._bands: dict[str, _Band] = {}

    # ------------------------------------------------------------------ queries

    def is_promoted(self, bracket: str) -> bool:
        band = self._bands.get(bracket)
        return bool(band and band.promoted)

    def promoted_brackets(self) -> list[str]:
        return sorted(b for b, band in self._bands.items() if band.promoted)

    def heartbeat_s(self, bracket: str) -> float:
        """Seconds between idle teacher check-ins on this band, and on no other band.

        Promotion makes the interval ten times *longer*, which is what "the heartbeat
        drops 10x" means in calls per hour. The direction is worth stating because the
        units invert it and getting it backwards multiplies the teacher bill by a hundred.
        """
        base = self.heartbeat_base_s
        return base * HEARTBEAT_DROP if self.is_promoted(bracket) else base

    def state(self, bracket: str) -> BracketState:
        band = self._bands.get(bracket) or _Band()
        return BracketState(
            bracket=bracket,
            promoted=band.promoted,
            heartbeat_s=self.heartbeat_s(bracket),
            agreement=band.agreement,
            samples=band.samples,
            deaths_per_h=band.deaths_per_h,
            baseline_deaths_per_h=band.baseline_deaths_per_h,
            reason=band.reason,
        )

    # ------------------------------------------------------------------ updates

    def set_baseline(self, bracket: str, deaths_per_h: float) -> None:
        """Record what this band cost before the policy drove it."""
        self._bands.setdefault(bracket, _Band()).baseline_deaths_per_h = deaths_per_h

    def observe(
        self,
        bracket: str,
        *,
        agreement: float | None,
        samples: int,
        deaths_per_h: float | None,
    ) -> BracketState:
        """Feed one band one window's numbers and get its state back.

        Only this band is touched. Nothing here reads or writes another bracket's row, and
        that is the whole point of the module.
        """
        band = self._bands.setdefault(bracket, _Band())
        band.agreement, band.samples, band.deaths_per_h = agreement, samples, deaths_per_h

        if band.baseline_deaths_per_h is None:
            if deaths_per_h is None:
                band.reason = "no deaths/h measured yet"
                return self.state(bracket)
            # The first measurement becomes the baseline and cannot itself promote:
            # "has not risen" needs something to have risen from, and comparing a number
            # to itself promotes every band on its first quarter hour.
            band.baseline_deaths_per_h = deaths_per_h
            band.reason = "baseline recorded; promotion needs a second window"
            return self.state(bracket)

        deaths_ok = (
            deaths_per_h is not None
            and deaths_per_h <= band.baseline_deaths_per_h + DEATHS_SLACK
        )

        if band.promoted:
            # A promoted band is measured on deaths alone. Its agreement denominator has
            # been cut tenfold by the promotion itself, so re-applying the sample gate
            # here would demote every band that has ever been promoted, by construction.
            if not deaths_ok:
                self.demote(bracket, f"deaths/h rose to {deaths_per_h} from {band.baseline_deaths_per_h}")
            else:
                band.reason = "holding"
            return self.state(bracket)

        checks = (
            Check("samples", samples >= MIN_AGREEMENT_SAMPLES,
                  f"{samples} teacher-decided ticks, needs {MIN_AGREEMENT_SAMPLES}"),
            Check("agreement", agreement is not None and agreement >= AGREEMENT_GATE,
                  f"agreement {agreement}, needs >= {AGREEMENT_GATE}"),
            Check("deaths", deaths_ok,
                  f"deaths/h {deaths_per_h}, baseline {band.baseline_deaths_per_h}"
                  f" (+{DEATHS_SLACK} slack)"),
        )
        if all(c.passed for c in checks):
            band.promoted = True
            band.promoted_at = time.time()
            band.reason = "promoted"
        else:
            band.reason = "; ".join(c.detail for c in checks if not c.passed)

        return BracketState(
            bracket=bracket,
            promoted=band.promoted,
            heartbeat_s=self.heartbeat_s(bracket),
            agreement=agreement,
            samples=samples,
            deaths_per_h=deaths_per_h,
            baseline_deaths_per_h=band.baseline_deaths_per_h,
            reason=band.reason,
            checks=checks,
        )

    def observe_counters(self, c: Counters) -> BracketState | None:
        """Feed a band straight from the eval board.

        The board already computes agreement over teacher-decided ticks and deaths/h over
        an observed span; recomputing either here would give promotion its own second
        opinion about numbers the board is the source of.
        """
        if c.bracket is None:
            return None
        return self.observe(
            c.bracket,
            agreement=c.intent_agreement.rate,
            samples=c.intent_agreement.samples,
            deaths_per_h=c.deaths_per_h,
        )

    def demote(self, bracket: str, why: str) -> BracketState:
        """Hand the band back to the teacher. Cheap, reversible, and never global."""
        band = self._bands.setdefault(bracket, _Band())
        band.promoted = False
        band.promoted_at = None
        band.reason = f"demoted: {why}"
        return self.state(bracket)

    # -------------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            "heartbeat_base_s": self.heartbeat_base_s,
            "bands": {b: vars(band).copy() for b, band in self._bands.items()},
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> PromotionLedger:
        led = PromotionLedger(heartbeat_s=payload.get("heartbeat_base_s", DEFAULT_HEARTBEAT_S))
        for name, row in payload.get("bands", {}).items():
            led._bands[name] = _Band(**row)
        return led

    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    @staticmethod
    def load(path: str | pathlib.Path) -> PromotionLedger:
        return PromotionLedger.from_dict(
            json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        )


# ------------------------------------------------------------------ skill promotion


class SkillStatus(StrEnum):
    PROPOSED = "proposed"
    STABLE = "stable"
    RETIRED = "retired"


STABLE_CLIENTS = 2
STABLE_RUNS_PER_CLIENT = 3
"""PLAN §10's "2 clients x 3 runs", read as two clients that each succeeded on three
separate runs — not as six successes however they fall. The looser reading lets one lucky
client carry a skill to `stable`, and reproduction across clients is the entire point:
a skill that only works on one client's timing is exactly the skill this gate exists to
catch."""

RETIRE_WINDOW = 20
RETIRE_RATE = 0.4
"""PLAN §10: success rate under 0.4 over 20. Measured over the **last** twenty attempts,
not over all time, because a skill's success rate is a claim about the world as it is now
and the world is a server that gets patched."""


@dataclass
class SkillRecord:
    """One skill's evidence. Attempts are kept as outcomes, not as a running average, so
    the rolling window can be recomputed rather than trusted."""

    name: str
    status: SkillStatus = SkillStatus.PROPOSED
    recent: list[bool] = field(default_factory=list)
    successes: dict[str, list[str]] = field(default_factory=dict)  # client -> run ids
    attempts_total: int = 0
    successes_total: int = 0
    reason: str = "proposed"

    @property
    def success_rate(self) -> float | None:
        """Over the rolling window. `None` when nothing has been recorded — an unmeasured
        skill has an unknown rate, and a table that prints 0% for it retires new skills."""
        if not self.recent:
            return None
        return sum(1 for ok in self.recent if ok) / len(self.recent)

    @property
    def clients(self) -> int:
        return sum(1 for runs in self.successes.values() if len(runs) >= STABLE_RUNS_PER_CLIENT)

    def is_retrievable(self, *, teacher_in_loop: bool) -> bool:
        return self.status is not SkillStatus.RETIRED or teacher_in_loop


class SkillLedger:
    """The `skills` collection's write policy (PLAN §10), as code rather than as a habit."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}

    def propose(self, name: str) -> SkillRecord:
        """Register a draft. Drafts are retrievable — they have to be, or nothing ever gets
        the attempts that would promote them — but they are labelled `proposed` so a caller
        choosing between two skills can see which one has evidence behind it."""
        return self._skills.setdefault(name, SkillRecord(name=name))

    def record(self, name: str, *, client_id: str, run_id: str, ok: bool) -> SkillRecord:
        """One attempt. Returns the record, whose status may have just changed."""
        rec = self.propose(name)
        rec.attempts_total += 1
        rec.successes_total += 1 if ok else 0
        rec.recent.append(ok)
        del rec.recent[:-RETIRE_WINDOW]
        if ok:
            runs = rec.successes.setdefault(client_id, [])
            if run_id not in runs:
                runs.append(run_id)

        if rec.status is SkillStatus.RETIRED:
            # Retirement does not un-retire itself on a lucky run. Coming back requires the
            # teacher, because whatever retired it is a fact about the world that a single
            # success does not overturn.
            return rec

        rate = rec.success_rate
        if len(rec.recent) >= RETIRE_WINDOW and rate is not None and rate < RETIRE_RATE:
            rec.status = SkillStatus.RETIRED
            rec.reason = f"success {rate:.2f} over last {len(rec.recent)}, under {RETIRE_RATE}"
        elif rec.status is SkillStatus.PROPOSED and rec.clients >= STABLE_CLIENTS:
            rec.status = SkillStatus.STABLE
            rec.reason = f"{rec.clients} clients x {STABLE_RUNS_PER_CLIENT} runs"
        return rec

    def retrieve(self, name: str, *, teacher_in_loop: bool = False) -> SkillRecord | None:
        """Normal retrieval. A retired skill is not returned (PLAN §10), and the caller gets
        `None` rather than a retired record it might use by mistake."""
        rec = self._skills.get(name)
        if rec is None or not rec.is_retrievable(teacher_in_loop=teacher_in_loop):
            return None
        return rec

    def catalog(
        self, *, status: SkillStatus | None = None, teacher_in_loop: bool = False
    ) -> list[SkillRecord]:
        """Everything retrievable, newest evidence included, in a stable order."""
        out = [
            r for r in self._skills.values()
            if r.is_retrievable(teacher_in_loop=teacher_in_loop)
            and (status is None or r.status is status)
        ]
        return sorted(out, key=lambda r: r.name)

    def reinstate(self, name: str, *, teacher_in_loop: bool) -> SkillRecord:
        """Bring a retired skill back. Only with the teacher in the loop.

        The rule is not that a retired skill is unusable; it is that nothing *automatic*
        may pick one up again. Something has to have looked at why it failed.
        """
        if not teacher_in_loop:
            raise PermissionError(
                f"{name} is retired; reinstating it needs the teacher in the loop"
            )
        rec = self._skills[name]
        rec.status = SkillStatus.PROPOSED
        rec.recent.clear()
        rec.reason = "reinstated by the teacher"
        return rec

    # -------------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            name: {**vars(r), "status": r.status.value, "name": r.name}
            for name, r in self._skills.items()
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> SkillLedger:
        led = SkillLedger()
        for name, row in payload.items():
            row = dict(row)
            row["status"] = SkillStatus(row["status"])
            led._skills[name] = SkillRecord(**row)
        return led

    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    @staticmethod
    def load(path: str | pathlib.Path) -> SkillLedger:
        return SkillLedger.from_dict(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
