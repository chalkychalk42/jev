"""What the coach and the teacher are allowed to say.

The coach never emits raw keys. It **arms a skill** and moves the playhead; System 1 owns
the keyboard. That split is the reason anything above 2 Hz can be slow, wrong or missing
without the character standing still (PLAN §8.1).

Two reply shapes, because the teacher and the coach are asked different questions:

  * `Decision` — what to do *now*. The coach's whole vocabulary.
  * `TeacherReply` — one or more **durable artifacts**, optionally with a decision.

The ordering there is `DECISIONS.md` V11 and it is the reason the teacher is affordable. A
decision helps one client once; a skill or an `on_fail` edge helps every client forever. On
a rate-limited teacher that difference compounds, so the schema asks for the artifact first
and treats the immediate action as the fallback.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Intent(StrEnum):
    """The coach's entire decision space. Seven values, and most ticks need none of them.

    These are *strategies for the current step*, not events. Moving the playhead is the
    tracker's job and never the coach's — `ADVANCE` means "go at this step head-on", which
    is what to do when nothing suggests otherwise.
    """

    ADVANCE = "advance"       # pursue the current step directly — the default strategy
    SKIP = "skip"             # this step is not going to work; take the alternative edge
    REJOIN = "rejoin"         # we are off-route; get back to it
    GRIND_RIB = "grind_rib"   # leave the spine for a grind loop, then recheck
    SERVICE = "service"       # vendor, repair, train, bags, food
    ESCALATE = "escalate"     # I cannot settle this; ask the teacher
    WAIT = "wait"             # something is in flight; do not act


class ArtifactKind(StrEnum):
    SKILL_DRAFT = "skill_draft"
    GRAPH_PATCH = "graph_patch"
    ON_FAIL_EDGE = "on_fail_edge"
    COMBAT_PROFILE = "combat_profile"
    MARK_SKIPPABLE = "mark_skippable"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Decision(Strict):
    """One armed skill, with the conditions under which it must stop.

    `abort_if` is mandatory and non-empty. A skill with no abort condition is a skill that
    runs until something else kills it, and "something else" during a live run is usually
    the character's corpse.
    """

    goal: str = Field(description="human-readable target, e.g. 'advance:ally_human_012_hogger'")
    intent: Intent
    skill: str | None = Field(default=None, description="catalog name; None only for WAIT")
    params: dict[str, Any] = Field(default_factory=dict)
    abort_if: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    why: str = Field(max_length=280, description="one line; it goes in the postmortem")


class Artifact(Strict):
    """Something durable. This is what the teacher is for."""

    kind: ArtifactKind
    target: str = Field(description="step_id, skill name or profile id this applies to")
    payload: dict[str, Any]
    rationale: str = Field(max_length=500)


class TeacherReply(Strict):
    """Artifacts first, action second — deliberately, see the module docstring."""

    artifacts: list[Artifact] = Field(default_factory=list)
    decision: Decision | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    def is_empty(self) -> bool:
        """No artifact and no decision. That is an abstention, not an instruction to stop."""
        return not self.artifacts and self.decision is None


class Verdict(Strict):
    """Why a plan was accepted or refused. The reason is logged, so it must be specific."""

    ok: bool
    rule: str | None = None
    reason: str | None = None

    @staticmethod
    def accept() -> Verdict:
        return Verdict(ok=True)

    @staticmethod
    def refuse(rule: str, reason: str) -> Verdict:
        return Verdict(ok=False, rule=rule, reason=reason)


def teacher_json_schema() -> dict:
    return TeacherReply.model_json_schema()


Status = Literal["ok", "timeout", "transport", "abstained", "rejected"]
"""Why a call ended.

Four of these five are not decisions, and telling them apart is the whole point of having
the field. A timeout is the transport failing. An abstention is the model declining to
answer. A rejection is the verifier refusing a well-formed answer. Collapsing any of them
into "the model said stop" ends runs that were fine (`ARCHITECTURE.md` §6).
"""
