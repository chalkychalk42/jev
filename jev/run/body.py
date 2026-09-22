"""Catalog skills composed from the body that already passed the live slice.

This module selects no guide step and writes no parallel decision stream. It executes
the runtime's arm, using the existing planner, locator, quest UI, Fight, Loot and Rest.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import ClassVar

from jev.clients.advance import AdvanceQuestFrame, Goal
from jev.clients.camera import Camera
from jev.clients.choose import ChooseListLine
from jev.clients.fight import Fight
from jev.clients.interact import Interact
from jev.clients.interact import Result as Interacted
from jev.clients.loot import Loot
from jev.clients.recover import Recover
from jev.clients.repair import Repair
from jev.clients.rest import Rest
from jev.clients.targeting import Targeting
from jev.clients.vendor import Vendor
from jev.coach.policy import Context, service
from jev.coach.schema import Intent
from jev.guide.coords import map_to_world, world_to_map
from jev.guide.graph import Graph
from jev.guide.objectives import progress, select_objective, target_progress
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed
from jev.perceive.radio_frame import list_lines, name_id
from jev.run.client import FOCUS_QUICK_S, Client
from jev.run.hunt import DEFAULT_HUNT_YARDS, Hunt
from jev.run.supervisor import BodyFailure, Cancelled, FocusLost, Result, Unsupported
from jev.world.combat import HEAL_OUT_OF_COMBAT, Role
from jev.world.state_v1 import PowerType, State, StepKind
from jev.world.vendor import merchants, supplies_for


class LiveBody:
    # Every entry has an executor. The verifier receives exactly this capability set.
    HANDLERS: ClassVar[dict[str, str]] = {
        "TRAVEL_TO": "_travel", "ACCEPT_QUEST": "_quest", "TURNIN_QUEST": "_quest",
        "GRIND_UNTIL": "_hunt", "COMBAT_PROFILE": "_fight",
        "LOOT": "_loot", "EAT_DRINK": "_rest", "VENDOR_REPAIR": "_repair",
        "BAG_MAKE_SPACE": "_vendor", "BUY_AMMO_REAGENT_FOOD": "_vendor",
        "RELEASE_SPIRIT": "_release", "CORPSE_RUN": "_recover",
        "IDLE": "_wait", "ABORT_WAIT": "_wait",
    }
    available = frozenset(HANDLERS)

    def __init__(self, client: Client, graph: Graph, *, travel_timeout: float = 180,
                 hunt_timeout: float = 600, say: Callable[[str], None] = print,
                 record_frame: Callable[..., dict] | None = None):
        if client.bounds is None or client.travel is None:
            raise ValueError("body needs the composed planner and follower")
        self.client, self.graph = client, graph
        self.travel_timeout, self.hunt_timeout, self.say = travel_timeout, hunt_timeout, say
        self.travelling = False
        self.policy_context = Context()
        self.checkpoint: Callable[[], None] = lambda: None
        self.arm: Armed | None = None
        ox, oy = client.origin
        w, h = client.size
        # Reader checkpoints stop loops which aren't currently sending keys as well.
        self.targeting = Targeting(client.hid, self._read, read_frame=self._frame,
                                    window_origin=client.origin, record_frame=record_frame)
        self.interact = Interact(hid=client.hid, bounds=client.bounds, read=self._read,
                                 read_frame=self._frame, read_pos=self._position,
                                 window_centre=(ox + w // 2, oy + h // 2),
                                 window_origin=client.origin, approach=self._approach,
                                 targeting=self.targeting)
        self.advance = AdvanceQuestFrame(hid=client.hid, read=self._read,
                                        quest_ids=self._quest_ids,
                                        window_origin=client.origin, window_size=client.size)
        self.chooser = ChooseListLine(hid=client.hid, read=self._reading,
                                      window_origin=client.origin, window_size=client.size)
        self.fight = Fight(hid=client.hid, read=self._read, read_frame=self._frame,
                           window_origin=client.origin, window_centre_x=w // 2,
                           targeting=self.targeting)
        self.rest = Rest(hid=client.hid, read=self._read)
        self.loot = Loot(hid=client.hid, read=self._read, read_frame=self._frame,
                         window_origin=client.origin, targeting=self.targeting)
        self.repair = Repair(hid=client.hid, read=self._read, visit=self._visit_repairer,
                             window_origin=client.origin, window_size=client.size)
        self.recover = Recover(hid=client.hid, read=self._read, walk_to=self._corpse_walk,
                               window_origin=client.origin, window_size=client.size)
        self.camera = Camera(hid=client.hid, window_origin=client.origin, window_size=client.size)
        self.interact.level = self.fight.level = self.loot.level = self.camera.ensure_level
        client.travel.read_pos = self._position

    def has_focus(self) -> bool:
        """Observe ownership even between skills, when the supervisor is watching."""
        ready = self.client.hid.ready()
        if not ready:
            self.camera.invalidate("client lost focus")
        return ready

    def reconnect(self, checkpoint, *, env_file=None) -> Result:
        from jev.run.watchdog import reconnect_client

        self.camera.invalidate("session reconnect")
        return reconnect_client(self.client, checkpoint, env_file=env_file)

    def _read(self):
        self.checkpoint()
        return self.client.read()

    def _reading(self):
        self.checkpoint()
        return self.client.reading()

    def _frame(self):
        self.checkpoint()
        return self.client.frame()

    def _position(self):
        self.checkpoint()
        return self.client.position()

    def _quest_ids(self):
        self.checkpoint()
        return self.client.quest_ids(tries=1)

    def execute(self, arm: Armed, state: State, checkpoint: Callable[[], None]) -> Result:
        self.arm = arm
        def focused_checkpoint():
            checkpoint()
            if not self.has_focus():
                raise FocusLost("client lost focus; release before refocusing")
        self.checkpoint = focused_checkpoint
        self.client.hid.checkpoint = focused_checkpoint
        handler = self.HANDLERS.get(arm.decision.skill)
        if handler is None:
            return Result(SkillOutcome.ABORTED, f"no executor for {arm.decision.skill}", "unsupported")
        error = self._parameters()
        if error:
            return Result(SkillOutcome.ABORTED, error, "unsupported")
        if (not self.has_focus()
                and not self.client.focused(FOCUS_QUICK_S, checkpoint=checkpoint)):
            return Result(SkillOutcome.ABORTED, "client refused focus after backoff", "refused")
        self.checkpoint()
        return getattr(self, handler)(state)

    def _parameters(self) -> str | None:
        """Execute only requests this composition can honour, without silently retargeting."""
        return self.validate(self.arm.decision, self.arm.step_id)

    def validate(self, decision, step_id: str | None) -> str | None:
        node = self.graph.get(step_id or "")
        if decision.intent in (Intent.SKIP, Intent.ESCALATE):
            return f"body cannot execute intent {decision.intent}"
        kinds = {"ACCEPT_QUEST": {StepKind.QUEST_ACCEPT},
                 "TURNIN_QUEST": {StepKind.QUEST_TURNIN},
                 "GRIND_UNTIL": {StepKind.GRIND, StepKind.QUEST_OBJECTIVE}}
        if decision.skill in kinds and (node is None or node.kind not in kinds[decision.skill]):
            return f"{decision.skill} does not match the armed guide step"
        expected = {"zone": node.zone, "step_id": node.id} if node else {}
        if node and node.coord_zone_id is not None:
            expected["coord_zone_id"] = node.coord_zone_id
            if self.client.bounds.area_id != node.coord_zone_id:
                return "body and guide use different map coordinate frames"
        if decision.skill == "TRAVEL_TO" and node and node.pos:
            expected.update(x=node.pos[0], y=node.pos[1], r=node.r)
        if decision.skill == "VENDOR_REPAIR":
            expected["service"] = "repair"
        if decision.skill == "BAG_MAKE_SPACE":
            expected["service"] = "bags"
        if decision.skill == "BUY_AMMO_REAGENT_FOOD":
            expected["service"] = "supplies"
        for key, value in decision.params.items():
            if key == "profile" and decision.skill == "COMBAT_PROFILE" and value in ("default", "panic"):
                continue  # Fight's measured health branch owns panic within the rotation.
            if (key == "until_level" and decision.skill == "GRIND_UNTIL" and node
                    and node.kind is StepKind.GRIND and type(value) is int and value > 0):
                continue
            if key not in expected or value != expected[key]:
                return f"unsupported {decision.skill} parameter: {key}={value!r}"
        return None

    def release(self) -> None:
        self.client.hid.release_all()
        if self.client.hid.keys_down() or self.client.hid.held_buttons:
            raise RuntimeError("input release was refused; held inputs remain")
        self.client.hid.checkpoint = None

    @staticmethod
    def _result(outcome, detail="") -> Result:
        ok = outcome.opened if isinstance(outcome, Interacted) else outcome.ok
        status = (SkillOutcome.PREEMPTED if outcome.value in
                  ("bags_full", "service_needed", "died", "blind", "interrupted") else
                  SkillOutcome.SUCCEEDED if ok else
                  SkillOutcome.TIMED_OUT if outcome.value == "timeout" else SkillOutcome.ABORTED)
        return Result(status, detail, outcome.value)

    def _node(self):
        return self.graph.get(self.arm.step_id) if self.arm else None

    def _approach(self, world) -> bool:
        self.checkpoint()
        v = self._read()
        if v is None:
            raise Cancelled("cannot observe travel readiness")
        ghost = v.get("vitals.ghost") is True
        if not ghost:
            if v.get("vitals.dead") is True:
                raise Cancelled("dead before travel")
            if v.get("vitals.combat") is True:
                raise Cancelled("combat before travel")
            hp = v.get("vitals.hp")
            if hp is None:
                raise Cancelled("health unread before travel")
            if hp < HEAL_OUT_OF_COMBAT and not self.fight.top_up():
                rested = self.rest.until(0.9)
                if not rested.ok:
                    raise BodyFailure(self._result(rested, f"not fit to travel: {self.rest.detail}"))
        self.travelling = True
        try:
            return self.client.approach(world, timeout_s=self.travel_timeout)
        finally:
            self.travelling = False

    def _travel(self, state) -> Result:
        node = self._node()
        if node is None or node.world is None or node.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "step has no supported map destination", "unsupported")
        ok = self._approach(node.world)
        return Result(SkillOutcome.SUCCEEDED if ok else SkillOutcome.ABORTED,
                      "", "arrived" if ok else "unreachable")

    def _quest(self, state) -> Result:
        node = self._node()
        if node is None or node.quest_id is None or node.npc_id is None or node.world is None:
            return Result(SkillOutcome.ABORTED, "quest has no placed NPC", "unsupported")
        if node.target_kind != "creature" or not node.target_name:
            return Result(SkillOutcome.ABORTED, "quest target needs a supported creature identity", "unsupported")
        opened = self.interact.open_on(node.target_name, node_world=node.world, node_map=node.pos)
        if not opened.opened:
            return self._result(opened, self.interact.detail)
        reading = self._reading()
        is_list = (reading is not None and reading.values
                   and reading.values.get("ui.advance_x") is None and list_lines(reading))
        if opened is Interacted.GOSSIP or is_list:
            chose = self.chooser.run(node.title)
            if not chose.ok:
                return self._result(chose, self.chooser.detail)
        with self.client._capturing:
            self.client.log.reset()
        goal = Goal.HELD if node.kind is StepKind.QUEST_ACCEPT else Goal.CLEARED
        outcome = self.advance.run(node.quest_id, goal)
        return self._result(outcome, self.advance.detail)

    def _quest_progress(self):
        node = self._node()
        self._quest_ids()
        with self.client._capturing:
            log = self.client.log.complete
        value = progress(log, node.quest_id if node else None)
        if (node and not node.objective_targets and value.first_incomplete is not None
                and value.first_incomplete > 0):
            raise Unsupported("next objective needs its own generated target; this graph places only the first")
        return value

    def _progress(self):
        value = self._quest_progress()
        if value.complete is True and value.have is None:
            return 1, 1  # a positive complete flag, including objectives with no counter
        return value.have, value.need

    def _hunt(self, state) -> Result:
        node = self._node()
        if (node is None or node.world is None
                or node.kind not in (StepKind.QUEST_OBJECTIVE, StepKind.GRIND)
                or node.map_id != self.client.bounds.map_id):
            return Result(SkillOutcome.ABORTED, "no supported objective destination", "unsupported")
        destination = node
        progress_reader = self._progress
        def complete_reader():
            return self._quest_progress().complete
        if node.kind is StepKind.QUEST_OBJECTIVE and node.objective_targets:
            self._quest_ids()
            with self.client._capturing:
                log = self.client.log.complete
            selection = select_objective(node, log)
            if selection.complete is True:
                return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
            if selection.target is None:
                code = "blind" if log is None else "unsupported"
                return Result(SkillOutcome.PREEMPTED if log is None else SkillOutcome.ABORTED,
                              selection.reason or "objective unavailable", code)
            destination = selection.target
            def selected_progress():
                self._quest_ids()
                with self.client._capturing:
                    return target_progress(self.client.log.complete, node.quest_id, destination)
            def progress_reader():
                value = selected_progress()
                return (1, 1) if value.complete is True and value.have is None else (value.have, value.need)
            def complete_reader():
                return selected_progress().complete
        if (destination.target_kind != "creature" or not destination.target_name
                or destination.world is None or destination.map_id != self.client.bounds.map_id):
            return Result(SkillOutcome.ABORTED, "objective needs a supported creature target; objects need their own locator", "unsupported")
        if node.kind is StepKind.GRIND:
            target = self.arm.decision.params.get("until_level")
            if not isinstance(target, int):
                return Result(SkillOutcome.ABORTED, "grind arm has no level predicate", "unsupported")
            def progress_reader():
                values = self._read()
                return (values.get("char.level") if values else None), target
            def complete_reader():
                level, needed = progress_reader()
                return None if level is None else level >= needed
        hunt = Hunt(fight=self.fight, rest=self.rest, read=self._read,
                    approach=self._approach, progress=progress_reader, loot=self.loot, say=self.say,
                    is_complete=complete_reader, service_needed=self._service_needed)
        outcome = hunt.run(destination.world, destination.hunt_yards or DEFAULT_HUNT_YARDS,
                           name_id(destination.target_name),
                           timeout_s=self.hunt_timeout)
        return self._result(outcome, hunt.detail)

    def _service_needed(self) -> str | None:
        self.checkpoint()
        state = self.client.state()
        if state is None:
            return None
        plan = service(state, context=self.policy_context)
        return plan.decision.why if plan else None

    def _fight(self, state) -> Result:
        outcome = self.fight.run(None)
        if outcome.ok:
            looted = self.loot.run(progress=self._progress)
            if not looted.ok:
                return self._result(looted, f"post-kill loot: {self.loot.detail}")
        return self._result(outcome, self.fight.detail)

    def _loot(self, state) -> Result:
        return self._result(self.loot.run(progress=self._progress), self.loot.detail)

    def _rest(self, state) -> Result:
        if state.vitals.power_type is PowerType.MANA and state.vitals.power is not None and state.vitals.power < 0.35:
            return self._result(self.rest.until(0.75, role=Role.DRINK), self.rest.detail)
        if self.fight.top_up():
            return Result(SkillOutcome.SUCCEEDED, "health topped up", "healthy")
        return self._result(self.rest.until(0.9), self.rest.detail)

    def _repair(self, state) -> Result:
        return self._result(self.repair.run(), self.repair.detail)

    def _vendor(self, state) -> Result:
        values, here = self._read(), self._position()
        if values is None or here is None:
            return Result(SkillOutcome.PREEMPTED, "vendor position or inventory unread", "blind")
        supplies = ()
        if self.arm.decision.skill == "BUY_AMMO_REAGENT_FOOD":
            supplies = tuple(s for s in supplies_for(values.get("char.class_id"), values.get("char.race_id"))
                             if getattr(state.bags, f"{s.role.lower()}_count", None) == 0
                             and getattr(state.bags, f"{s.role.lower()}_id", None) == s.item_id)
            if not supplies:
                return Result(SkillOutcome.ABORTED, "no confirmed empty supported food/drink slot", "unsupported")
        wanted = {s.item_id for s in supplies}
        candidates = [m for m in merchants(self.client.bounds.map_id)
                      if (not wanted or wanted & m.items)
                      and (point := world_to_map(*m.world[:2], self.client.bounds)) is not None
                      and all(0 <= value <= 1 for value in point)]
        if not candidates:
            return Result(SkillOutcome.ABORTED, "no generated supplier in the measured zone", "unsupported")
        world = map_to_world(*here, self.client.bounds)
        merchant = min(candidates, key=lambda m: math.dist(m.world[:2], world))
        def visit():
            return self._open_merchant(merchant.name, merchant.world,
                                       world_to_map(*merchant.world[:2], self.client.bounds))
        vendor = Vendor(self.client.hid, self._read, visit, self.client.origin, self.client.size)
        outcome = vendor.run(expected_name=merchant.name,
                             supplies=tuple(s for s in supplies if s.item_id in merchant.items),
                             min_free=1 if supplies else 6,
                             timeout_s=self.travel_timeout + 120)
        return self._result(outcome, vendor.detail or
                            f"sold {vendor.sold_stacks} stacks; bought {vendor.bought_units} units")

    def _open_merchant(self, name, world, point) -> bool:
        opened = self.interact.open_on(name, node_world=world, node_map=point)
        if not opened.opened:
            # Preserve the measured nested failure. Collapsing this to False made a
            # rejected visible ring indistinguishable from a failed merchant journey.
            raise BodyFailure(self._result(opened,
                                           f"{name}: {self.interact.detail or opened.value}"))
        if opened is Interacted.GOSSIP:
            values = self._read()
            identity = values.get("merchant.gossip_name_id") if values else None
            if identity is None or not self.chooser.run_id(identity).ok:
                return False
            values = self._read()
            return bool(values and values.get("ui.vendor") is True)
        return opened is Interacted.VENDOR

    def _visit_repairer(self) -> bool:
        here = self._position()
        if here is None:
            return False
        world = map_to_world(*here, self.client.bounds)
        candidates = [n for n in self.graph.nodes if n.kind is StepKind.REPAIR
                      and n.world is not None and n.map_id == self.client.bounds.map_id
                      and n.target_kind == "creature" and n.target_name]
        if not candidates:
            return False
        node = min(candidates, key=lambda n: math.dist(n.world[:2], world))
        return self._open_merchant(node.target_name, node.world, node.pos)

    def _corpse_walk(self, point) -> bool:
        wx, wy = map_to_world(*point, self.client.bounds)
        placed = [n for n in self.graph.nodes if n.world is not None
                  and n.map_id == self.client.bounds.map_id]
        if not placed:
            return False
        z = min(placed, key=lambda n: math.dist(n.world[:2], (wx, wy))).world[2]
        return self._approach((wx, wy, z))

    def _recover(self, state) -> Result:
        # Recover reads painted corpse coordinates. No guessed quest-node corpse.
        return self._result(self.recover.run(self.recover.corpse), self.recover.detail)

    def _release(self, state) -> Result:
        return self._result(self.recover.run(release_only=True), self.recover.detail)

    def _wait(self, state) -> Result:
        return Result(SkillOutcome.SUCCEEDED)
