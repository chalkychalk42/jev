"""Observe, choose one action, execute, measure; teach only from joined outcomes.

The guide still owns the objective and the supervisor still owns cancellation. This
loop gives the visual tutor and evaluated student the decisions *inside* that objective.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass

from jev.learn.episode import SkillOutcome
from jev.play.actions import action_dict, parse_action
from jev.play.observation import capability, judge, measured_effects
from jev.run.supervisor import Result


@dataclass(frozen=True)
class PlayConfig:
    mode: str = "teach"
    teacher_timeout_s: float = 30
    outcome_wait_s: float = 5
    max_actions: int = 64
    max_no_effect: int = 8
    poll_s: float = 0.1

    def __post_init__(self):
        if self.mode not in {"teach", "adaptive"}:
            raise ValueError("playing mode must be teach or adaptive")
        if any(not math.isfinite(v) or v <= 0 for v in (
                self.teacher_timeout_s, self.outcome_wait_s, self.poll_s,
                self.max_actions, self.max_no_effect)):
            raise ValueError("playing limits must be finite and positive")


def _await_checked(coro, checkpoint, *, poll_s=0.05):
    """Run in the sole body worker, canceling AND reaping the model on interruption."""
    async def run():
        task = asyncio.create_task(coro)
        try:
            while not task.done():
                checkpoint()
                await asyncio.wait({task}, timeout=poll_s)
            checkpoint()
            return await task
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return asyncio.run(run())


def expected_for(action: dict, requested: str | None, bucket: str) -> str:
    """A teacher cannot turn a failed attack into success by predicting 'observed'."""
    kind = action["kind"]
    if kind in {"observe", "pointer"}:
        allowed, default = {"observed"}, "observed"
    elif kind == "camera":
        allowed, default = {"scene_changed"}, "scene_changed"
    elif kind == "key":
        control = action["control"]
        if control in {"move_forward", "move_backward", "strafe_left", "strafe_right", "jump"}:
            allowed, default = {"closer", "moved", "arrived", "target_hp_decreased"}, (
                "closer" if bucket == "approach" else "moved")
        elif control in {"turn_left", "turn_right"}:
            allowed, default = {"scene_changed", "target_hp_decreased"}, "scene_changed"
        elif control in {"target_next", "target_previous", "target_self"}:
            allowed, default = {"selected"}, "selected"
        elif control == "attack_target":
            allowed, default = {"target_hp_decreased", "target_dead"}, "target_hp_decreased"
        elif control == "escape":
            allowed, default = {"target_cleared", "ui_closed"}, "ui_closed"
        else:
            allowed, default = {"scene_changed", "ui_opened", "ui_closed"}, "scene_changed"
    elif kind == "action_slot":
        allowed = {"target_hp_decreased", "target_dead", "healed", "power_restored", "scene_changed"}
        default = "target_hp_decreased"
    elif kind == "click":
        if action["intent"] == "select":
            allowed, default = {"selected"}, "selected"
        elif action["intent"] == "interact":
            allowed = {"ui_opened", "loot_received", "target_hp_decreased", "target_dead"}
            default = "ui_opened"
        else:
            allowed = {"ui_opened", "ui_closed", "quest_accepted", "quest_cleared", "quest_progress"}
            default = "ui_closed"
    else:
        defaults = {"TRAVEL_TO": "arrived", "ACCEPT_QUEST": "quest_accepted",
                    "TURNIN_QUEST": "quest_cleared", "GRIND_UNTIL": "quest_progress",
                    "COMBAT_PROFILE": "target_dead", "LOOT": "loot_received",
                    "EAT_DRINK": "healed", "VENDOR_REPAIR": "repaired",
                    "BAG_MAKE_SPACE": "bags_freed", "BUY_AMMO_REAGENT_FOOD": "supplies_replenished",
                    "RELEASE_SPIRIT": "released", "CORPSE_RUN": "recovered"}
        default = defaults.get(action["name"], "observed")
        allowed = {default, "power_restored"} if action["name"] == "EAT_DRINK" else {default}
    return requested if requested in allowed else default


def finished(first: dict, current: dict, *, observed_effects=()) -> bool:
    """The guide predicate ends a teaching episode; a tutor cannot announce completion."""
    context, v = first["context"], current["values"]
    skill = context["skill"]
    if skill == "ABORT_WAIT":
        return first["values"].get("ui.modal") is True and v.get("ui.modal") is False
    effects, _ = measured_effects(first, current)
    expected = {"TRAVEL_TO": "arrived", "ACCEPT_QUEST": "quest_accepted",
                "TURNIN_QUEST": "quest_cleared", "RELEASE_SPIRIT": "released",
                "CORPSE_RUN": "recovered", "VENDOR_REPAIR": "repaired",
                "BAG_MAKE_SPACE": "bags_freed", "BUY_AMMO_REAGENT_FOOD": "supplies_replenished"}
    if skill in expected:
        if skill == "VENDOR_REPAIR" and v.get("bags.durability_min") != 1:
            return False
        if skill == "BAG_MAKE_SPACE" and (v.get("bags.free") is None or v["bags.free"] < 6):
            return False
        return expected[skill] in effects
    if skill == "GRIND_UNTIL":
        level = context.get("until_level")
        if isinstance(level, int) and isinstance(v.get("char.level"), int):
            return v["char.level"] >= level
        return any(q.get("quest_id") == context.get("quest_id") and q.get("complete") is True
                   for q in current.get("state", {}).get("quests") or [])
    if skill == "COMBAT_PROFILE":
        return ("target_dead" in effects or "target_dead" in observed_effects) and v.get("vitals.combat") is False
    if skill == "LOOT":
        return "loot_received" in effects and v.get("ui.loot") is False
    if skill == "EAT_DRINK":
        from jev.perceive.radio_frame import POWER_BY_ID

        power_type = current.get("state", {}).get("vitals", {}).get("power_type")
        wire_type = POWER_BY_ID.get(v.get("vitals.power_type"))
        if wire_type is not None:
            power_type = wire_type
        if power_type not in set(POWER_BY_ID.values()):
            return False
        mana = power_type == "mana"
        return (v.get("vitals.hp") is not None and v["vitals.hp"] >= 0.9
                and (not mana or (v.get("vitals.power") is not None and v["vitals.power"] >= 0.75)))
    return False


class PlayController:
    def __init__(self, *, observer, executor, teacher, learner, journal, controls: dict,
                 controls_fingerprint: str, knowledge_fingerprint: str,
                 config: PlayConfig | None = None, say=print):
        self.observer, self.executor, self.teacher = observer, executor, teacher
        self.learner, self.journal, self.controls = learner, journal, controls
        self.controls_fingerprint = controls_fingerprint
        self.knowledge_fingerprint = knowledge_fingerprint
        self.config, self.say = config or PlayConfig(), say
        self.recent = deque(maxlen=12)
        self.learning_error = None

    def _observe(self, arm, checkpoint, *, retain=True):
        checkpoint()
        result = self.observer.observe(arm, retain=retain)
        self.executor.note_observation(result.data["values"])
        if retain:
            self.journal.observation(result.data)
        return result

    def _record(self, row):
        self.journal.append("actions", {"event": "result", **row})
        if self.learner is not None:
            try:
                self.learner.record(row)
            except Exception as exc:
                # Evidence is already in the run. An optional trainer failure removes
                # student authority, never silently loses the trace or crashes input.
                self.learning_error = f"{type(exc).__name__}: {exc}"
                self.say(f"motor learning unavailable: {self.learning_error}")

    def _prediction(self, observation, bucket, decision_id):
        if self.learner is None or self.learning_error:
            return None
        try:
            return self.learner.predict(
                observation, bucket, decision_id=decision_id,
                controls_fingerprint=self.controls_fingerprint,
                knowledge_fingerprint=self.knowledge_fingerprint,
                allow_student=self.config.mode == "adaptive")
        except Exception as exc:
            self.learning_error = f"{type(exc).__name__}: {exc}"
            return None

    def run(self, arm, checkpoint) -> Result:
        first = current = None
        result = Result(SkillOutcome.PREEMPTED, "teaching episode interrupted", "preempted")
        episode_id = arm.arm_id or uuid.uuid4().hex
        count = 0
        goal_verified = False
        observed_effects = set()
        started = time.time()
        try:
            first = current = self._observe(arm, checkpoint)
            no_effect = 0
            while count < self.config.max_actions:
                checkpoint()
                if finished(first.data, current.data):
                    goal_verified = True
                    result = Result(SkillOutcome.SUCCEEDED, "objective verified from observations", "done")
                    return result
                bucket = capability(current.data)
                decision_id = uuid.uuid4().hex
                prediction = self._prediction(current.data, bucket, decision_id)
                student = (prediction is not None and prediction.action is not None
                           and prediction.mode in {"canary", "active"}
                           and self.config.mode == "adaptive")
                if student:
                    # A cached final view from the previous action can already have
                    # changed. Infer again on the actual pre-input picture; novelty
                    # returns ownership to Jev before any student input is sent.
                    current = self._observe(arm, checkpoint)
                    bucket = capability(current.data)
                    prediction = self._prediction(current.data, bucket, decision_id)
                    student = bool(prediction and prediction.action and prediction.can_execute)
                author, model, reply = "student" if student else "teacher", None, None
                chosen_at = time.time()
                self.journal.append("actions", {"event": "request", "decision_id": decision_id,
                                                "episode_id": episode_id, "t": chosen_at,
                                                "observation_id": current.id, "author": author})
                if student:
                    action = parse_action(prediction.action)
                    expected = getattr(prediction, "expected_effect", None)
                    model = prediction.model
                else:
                    reply = _await_checked(self.teacher.decide(
                        current.data, current.png, controls=self.controls,
                        recent=list(self.recent), timeout_s=self.config.teacher_timeout_s), checkpoint)
                    reply_doc = asdict(reply)
                    reply_doc["action"] = action_dict(reply.action) if reply.action else None
                    self.journal.append("teacher", {"decision_id": decision_id,
                                                     "episode_id": episode_id, "t": time.time(),
                                                     **reply_doc})
                    if not reply.ok:
                        result = Result(SkillOutcome.ABORTED,
                                        f"visual teacher {reply.status}: {reply.detail}", "teacher_unavailable")
                        return result
                    action, expected, model = reply.action, reply.expected_effect, reply.actual_model
                doc = action_dict(action)
                expected = expected_for(doc, expected, bucket)
                # Models can take seconds. Fresh values govern execution, and both the
                # requested and actual starting views remain in the evidence.
                before = current if student else self._observe(arm, checkpoint)
                if before.data["context"]["step_id"] != current.data["context"]["step_id"]:
                    result = Result(SkillOutcome.PREEMPTED, "guide objective changed", "preempted")
                    return result
                requested = current
                row = {"schema": 1, "run_id": self.journal.run_id,
                       "episode_id": episode_id, "decision_id": decision_id,
                       "observation_id": requested.id, "request_observation_id": requested.id,
                       "t": chosen_at, "capability": bucket, "author": author, "model": model,
                       "before": requested.data, "execution_before": before.data,
                       "action": doc, "expected_effect": expected,
                       "controls_fingerprint": self.controls_fingerprint,
                       "knowledge_fingerprint": self.knowledge_fingerprint,
                       "synthetic": before.data.get("synthetic", False),
                       "cost": {"teacher_calls": len(reply.calls) if reply else 0,
                                "input_tokens": reply.tokens_in if reply else 0,
                                "output_tokens": reply.tokens_out if reply else 0},
                       "shadow": ({"model": prediction.model, "action": prediction.action,
                                   "confidence": prediction.confidence,
                                   "expected_effect": prediction.expected_effect}
                                  if prediction and not student else None)}
                self.journal.append("actions", {"event": "accepted", **row})
                delivery = after = None
                nonfatal_loot = False
                try:
                    from jev.play.executor import GuardState

                    guard = GuardState.from_observation(requested.data)
                    delivery = self.executor.execute(action, expected=guard)
                    delegated = delivery.metadata.get("skill_result", {})
                    nonfatal_loot = (
                        arm.decision.skill == "LOOT" and doc.get("name") == "LOOT"
                        and delivery.code == "delegated" and isinstance(delegated, dict)
                        and delegated.get("outcome") == SkillOutcome.SUCCEEDED
                        and delegated.get("code") == "nothing")
                    performed = delivery.delivered or delivery.code in {"observed", "delegated"}
                    due = time.monotonic() + self.config.outcome_wait_s
                    while True:
                        after = self._observe(arm, checkpoint, retain=False)
                        outcome = judge(before.data, after.data, expected_effect=expected,
                                        delivered=performed,
                                        detail="" if performed else delivery.detail, action=doc)
                        if (outcome["success"] or outcome["fatal"] or not performed
                                or (nonfatal_loot and outcome["verified"]
                                    and after.data["values"].get("ui.loot") is False)):
                            break
                        if time.monotonic() >= due:
                            break
                        time.sleep(self.config.poll_s)
                finally:
                    if after is not None:
                        after = self.observer.retain(after)
                        self.journal.observation(after.data)
                        current = after
                    outcome = judge(before.data, after.data if after else None,
                                    expected_effect=expected,
                                    delivered=bool(delivery and (delivery.delivered or delivery.code in
                                                                {"observed", "delegated"})),
                                    detail=(delivery.detail if delivery and delivery.code not in
                                            {"observed", "delegated", "delivered"} else
                                            "execution interrupted" if not delivery else ""), action=doc)
                    row.update(after=after.data if after else None, outcome=outcome,
                               elapsed_s=time.time() - chosen_at,
                               delivery=asdict(delivery) if delivery else None)
                    self._record(row)
                count += 1
                self.recent.append({"decision_id": decision_id, "capability": bucket,
                                    "author": author, "action": doc, "expected_effect": expected,
                                    "outcome": outcome,
                                    "delivery": asdict(delivery),
                                    "rationale": reply.rationale if reply else "evaluated student"})
                self.say(f"play {author} {bucket}: {doc['kind']} -> {outcome['reason']}")
                if outcome["fatal"]:
                    result = Result(SkillOutcome.PREEMPTED, "death observed", "died")
                    return result
                if outcome["verified"]:
                    observed_effects.update(outcome["effects"])
                if finished(first.data, current.data, observed_effects=observed_effects):
                    goal_verified = True
                    result = Result(SkillOutcome.SUCCEEDED, "objective verified from observations", "done")
                    return result
                # An existing bounded loot attempt may legitimately find no observable
                # take. Preserve that non-fatal composition result without teaching a
                # successful click, asserting an empty corpse, or awarding task reward.
                if (nonfatal_loot and outcome["verified"]
                        and current.data["values"].get("ui.loot") is False):
                    result = Result(SkillOutcome.SUCCEEDED,
                                    "bounded loot attempt finished without an observed take", "nothing")
                    return result
                useful = (outcome["success"] and expected not in {"observed", "scene_changed", "moved"})
                no_effect = 0 if useful else no_effect + 1
                if no_effect >= self.config.max_no_effect:
                    result = Result(SkillOutcome.ABORTED,
                                    "bounded teaching episode made no verified useful progress", "teaching_stalled")
                    return result
            result = Result(SkillOutcome.TIMED_OUT, "teaching action budget exhausted", "teaching_stalled")
            return result
        finally:
            final = {"episode_id": episode_id, "t": time.time(), "elapsed_s": time.time() - started,
                     "actions": count, "outcome": result.outcome.value,
                     "code": result.code, "detail": result.detail,
                     "first_observation_id": first.id if first else None,
                     "last_observation_id": current.id if current else None}
            if first and current:
                effects, progress = measured_effects(first.data, current.data)
                if goal_verified:
                    progress = max(progress, 1.0)  # an independently verified guide/service predicate
                final.update(effects=effects, progress=progress,
                             verified=goal_verified,
                             success=result.outcome is SkillOutcome.SUCCEEDED,
                             synthetic=first.data.get("synthetic", False))
            self.journal.append("episodes", final)
            if self.learner is not None and hasattr(self.learner, "finish_episode"):
                try:
                    self.learner.finish_episode(episode_id, run_id=self.journal.run_id, outcome=final)
                except Exception as exc:
                    self.learning_error = f"{type(exc).__name__}: {exc}"
