"""Catalog skills composed from the body that already passed the live slice.

This module selects no guide step and writes no parallel decision stream. It executes
the runtime's arm, using the existing planner, locator, quest UI, Fight, Loot and Rest.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import ClassVar

from jev.clients.advance import AdvanceQuestFrame, Goal
from jev.clients.camera import Camera
from jev.clients.choose import ChooseListLine
from jev.clients.fight import Fight
from jev.clients.gather import Gather, Gathered
from jev.clients.hearth import Hearth
from jev.clients.interact import Interact
from jev.clients.interact import Result as Interacted
from jev.clients.loot import Loot, Looted
from jev.clients.recover import Recover, Recovered
from jev.clients.repair import Repair
from jev.clients.rest import Rest
from jev.clients.targeting import FaceCode, Targeting
from jev.clients.vendor import Vendor
from jev.coach.policy import Context, service
from jev.coach.schema import Intent
from jev.guide.coords import map_to_world, world_to_map
from jev.guide.graph import Graph, ObjectiveTarget
from jev.guide.objectives import progress, select_objective, target_progress
from jev.guide.spawns import lookup as spawn_points
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import Armed
from jev.perceive.radio_frame import UI_ERROR_KEYS, list_lines, name_id
from jev.run.client import FOCUS_QUICK_S, Client
from jev.run.hunt import DEFAULT_HUNT_YARDS, Hunt
from jev.run.supervisor import BodyFailure, Cancelled, FocusLost, Result, Unsupported
from jev.world.combat import HEAL_OUT_OF_COMBAT, Role
from jev.world.state_v1 import PowerType, State, StepKind
from jev.world.vendor import bag_slots, merchants, supplies_for

# Dying again this soon after getting up at the body means the body lies where something
# this character cannot beat still stands: the next recovery gets up at the graveyard's
# Spirit Healer instead, and goes home by hearthstone.
DEATH_TRAP_S = 180.0
# Where a ghost gets up: this far short of the body, on the graveyard's side. A body can be
# reclaimed from inside the server's 39-yard radius and the character stands up where the
# ghost stood; at the body itself the Mangy Wolves round it killed a character at half
# health three times running, the Spirit Healer answering nothing each time (run
# 20260924T041014-a9781c).
TRAP_RECLAIM_YARDS = 32.0
# Broken gear this far from the nearest repairer goes home by hearthstone first.
BROKEN_DURABILITY = 0.05
HEARTH_TO_REPAIR_YARDS = 150.0
# Where to hover for a unit too close and tall for its nameplate to show, as fractions of
# the client: down the middle first. The Spirit Healer stands over a fresh ghost and fills
# the centre of the screen, with its plate drawn behind the strip at the top.
HOVER_POINTS = ((0.5, 0.35), (0.5, 0.45), (0.5, 0.25), (0.45, 0.35), (0.55, 0.35),
                (0.5, 0.55), (0.4, 0.3), (0.6, 0.3))
# A right-click from out of reach answers "You are too far away!": step toward the unit,
# which the hover found in front of the character, and click again.
HOVER_STEPS = 5
HOVER_STEP_S = 0.35
# The answer to a click - a popup, or the error - arrives after the server's reply, which
# is later than the first fresh paint (run 20260923T183537-ee5ef3 read silence and stopped).
HOVER_ANSWER_S = 1.2
# An exploration objective is credited by the server once the character is inside its
# trigger; the quest log shows it a paint or two later.
EXPLORE_CREDIT_S = 5.0
EXPLORE_POLL_S = 0.25
# A merchant whose body cannot be clicked is passed over for the next nearest one that
# sells what is wanted: Dermot Johns stands behind his wagon, every point between his ring
# and his plate was wagon, and Godric Rothgar stood in plain view beside it (run
# 20260924T011327-6e5f4b stopped on its first full bags).
MERCHANT_TRIES = 3
# Every spawn point of a quest's world object, twice round: taken crates respawn.
GATHER_LAPS = 2
# A step back, about two yards, before a second look at a spawn point that showed nothing.
GATHER_STEP_BACK_S = 0.6
MERCHANT_UNREACHABLE = frozenset({"not_visible", "no_target", "no_window", "approach_failed"})


def _tour(points, start) -> list[tuple[float, float, float]]:
    """Every point, from the one nearest `start`, always on to the nearest one left."""
    left, tour = [tuple(p) for p in points], []
    here = tuple(start[:2])
    while left:
        point = min(left, key=lambda p: math.dist(here, p[:2]))
        left.remove(point)
        tour.append(point)
        here = point[:2]
    return tour


class LiveBody:
    # Every entry has an executor. The verifier receives exactly this capability set.
    HANDLERS: ClassVar[dict[str, str]] = {
        "TRAVEL_TO": "_travel", "ACCEPT_QUEST": "_quest", "TURNIN_QUEST": "_quest",
        "GRIND_UNTIL": "_hunt", "COMBAT_PROFILE": "_fight",
        "LOOT": "_loot", "EAT_DRINK": "_rest", "VENDOR_REPAIR": "_repair",
        "BAG_MAKE_SPACE": "_vendor", "BUY_AMMO_REAGENT_FOOD": "_vendor",
        "RELEASE_SPIRIT": "_release", "CORPSE_RUN": "_recover",
        "IDLE": "_wait", "ABORT_WAIT": "_wait", "FACE_TARGET": "_face",
    }
    available = frozenset(HANDLERS)

    def __init__(self, client: Client, graph: Graph, *, travel_timeout: float = 180,
                 hunt_timeout: float = 600, say: Callable[[str], None] = print,
                 record_frame: Callable[..., dict] | None = None,
                 hunt_spawns: dict | None = None):
        if client.bounds is None or client.travel is None:
            raise ValueError("body needs the composed planner and follower")
        self.client, self.graph = client, graph
        self.travel_timeout, self.hunt_timeout, self.say = travel_timeout, hunt_timeout, say
        # Where each hunt's target spawns (`jev.guide.spawns`); empty walks rings.
        self.hunt_spawns = hunt_spawns or {}
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
                           targeting=self.targeting, bounds=client.bounds)
        self.rest = Rest(hid=client.hid, read=self._read)
        self.loot = Loot(hid=client.hid, read=self._read, read_frame=self._frame,
                         window_origin=client.origin, targeting=self.targeting)
        self.gather = Gather(hid=client.hid, read=self._read, targeting=self.targeting,
                             window_origin=client.origin, window_size=client.size)
        self.repair = Repair(hid=client.hid, read=self._read, visit=self._visit_repairer,
                             window_origin=client.origin, window_size=client.size)
        self.recover = Recover(hid=client.hid, read=self._read, walk_to=self._corpse_walk,
                               interact=self._talk_to, choose=self.chooser.run,
                               window_origin=client.origin, window_size=client.size)
        self.hearth = Hearth(hid=client.hid, read=self._read,
                             window_origin=client.origin, window_size=client.size)
        self._revived_at: float | None = None
        self.camera = Camera(hid=client.hid, window_origin=client.origin, window_size=client.size)
        self.interact.level = self.fight.level = self.loot.level = self.camera.ensure_level
        self.fight.realign = self.camera.level
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
        self.ready_camera(state)
        return getattr(self, handler)(state)

    def ready_camera(self, state: State | None) -> None:
        """Level the camera while nothing is hitting the character.

        Every look needs it, and it is five seconds of mouse-look. Left to the first look,
        it landed in run 20260923T173347-590b06's first fight - a panic fight at 12% health,
        which was over before the drag was. A skill whose look finds it done skips it; one
        that runs in combat with it undone still levels first, as before.
        """
        if state is not None and state.vitals.combat is False and not self.camera.calibrated:
            self.camera.ensure_level()

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
        if node.target_kind not in ("creature", "gameobject") or not node.target_name:
            return Result(SkillOutcome.ABORTED, "quest target needs a supported identity", "unsupported")
        if node.target_kind == "gameobject":
            # A wanted poster or a body: stood at, found by the name its tooltip gives.
            self._approach(node.world)
            if not self.gather.open(name_id(node.target_name)):
                return Result(SkillOutcome.ABORTED, f"{node.target_name}: {self.gather.detail}",
                              "not_visible")
            reading = self._reading()
            values = reading.values if reading is not None and reading.values else {}
            opened = Interacted.QUEST if values.get("ui.quest_frame") is True else Interacted.GOSSIP
        else:
            opened = self.interact.open_on(node.target_name, node_world=node.world,
                                           node_map=node.pos)
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
        if isinstance(destination, ObjectiveTarget) and destination.kind == "explore":
            return self._explore(destination, complete_reader)
        if (isinstance(destination, ObjectiveTarget) and destination.kind == "loot"
                and destination.target_kind == "gameobject"):
            return self._gather(node, destination, progress_reader, complete_reader)
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
                           timeout_s=self.hunt_timeout,
                           spawns=spawn_points(self.hunt_spawns, node.id,
                                               getattr(destination, "target_id", None)))
        return self._result(outcome, hunt.detail)

    def _explore(self, target: ObjectiveTarget, complete: Callable[[], bool | None]) -> Result:
        """Walk to an exploration trigger's point and wait for the quest's own credit.

        The point is the trigger's centre, and the two on this route are spheres of ten and
        thirty yards, so arriving within travel's few yards is inside. Only the quest's
        positive complete flag is success: arriving is not exploring.
        """
        if target.world is None or target.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "exploration point is not on this map", "unsupported")
        arrived = self._approach(target.world)
        deadline = time.monotonic() + EXPLORE_CREDIT_S
        while complete() is not True:
            if time.monotonic() >= deadline:
                if not arrived:
                    return Result(SkillOutcome.ABORTED, "exploration point unreachable", "unreachable")
                return Result(SkillOutcome.ABORTED, "at the exploration point with no credit", "nothing")
            time.sleep(EXPLORE_POLL_S)
        return Result(SkillOutcome.SUCCEEDED, "exploration credited", "done")

    def _gather(self, node, target: ObjectiveTarget, progress, complete) -> Result:
        """Walk a quest object's spawn points, nearest first, taking it wherever it stands."""
        points = spawn_points(self.hunt_spawns, node.id, target.target_id)
        if not points and target.world is not None:
            points = (tuple(target.world),)
        if not points or not target.target_name or target.map_id != self.client.bounds.map_id:
            return Result(SkillOutcome.ABORTED, "object has no placed spawn points", "unsupported")
        wanted = name_id(target.target_name)
        deadline = time.monotonic() + self.hunt_timeout
        here = self._position()
        start = map_to_world(*here, self.client.bounds) if here is not None else points[0][:2]
        for point in _tour(points, start) * GATHER_LAPS:
            if complete() is True:
                return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
            if time.monotonic() > deadline:
                return Result(SkillOutcome.TIMED_OUT,
                              f"{self.hunt_timeout:.0f}s and the objective is not done", "timeout")
            self._approach(point)
            got = self.gather.pick(wanted, progress)
            if got is Gathered.NOT_HERE and self.client.hid.hold("s", GATHER_STEP_BACK_S):
                # Stood on the spawn point, the character itself hides what lies underfoot.
                got = self.gather.pick(wanted, progress)
            self.say(f"    gather: {got.value} - {self.gather.detail}")
            if not got.ok:
                return self._result(got, self.gather.detail)
        if complete() is True:
            return Result(SkillOutcome.SUCCEEDED, "quest completion confirmed", "done")
        return Result(SkillOutcome.ABORTED, "every spawn point walked and the objective is short",
                      "nothing")

    def _service_needed(self) -> str | None:
        self.checkpoint()
        state = self.client.state()
        if state is None:
            return None
        plan = service(state, context=self.policy_context)
        return plan.decision.why if plan else None

    def _objective_name(self) -> int | None:
        """The creature the armed guide step wants, when it names one; else None."""
        node = self._node()
        if node is None:
            return None
        target = node
        if node.objective_targets:
            self._quest_ids()
            with self.client._capturing:
                log = self.client.log.complete
            selection = select_objective(node, log)
            target = selection.target
        if target is None or target.target_kind != "creature" or not target.target_name:
            return None
        return name_id(target.target_name)

    def _fight(self, state) -> Result:
        # Inside an objective the fight is for its creature, not the nearest plate: the
        # first live run's nearest plate was a rabbit. Self-defence still takes whatever
        # is attacking us (Fight loosens the name in combat, never drops it).
        outcome = self.fight.run(self._objective_name())
        if outcome.ok:
            looted = self.loot.run(progress=self._progress, anchor=self.fight.last_plate,
                                   name_id=self.fight.killed_name_id)
            # A corpse not found costs its loot, not the kill: the selection can move on at
            # the kill and leave nothing to hover (run 20260924T033806-a3254d, three times).
            if not looted.ok and looted is not Looted.NO_CORPSE:
                return self._result(looted, f"post-kill loot: {self.loot.detail}")
            # Said, so whoever delegated the fight knows the corpse is done with: the tutor
            # went on clicking corpses this had already emptied.
            return self._result(outcome, f"{self.fight.detail}; corpse looted: "
                                         f"{looted.value} - {self.loot.detail}")
        return self._result(outcome, self.fight.detail)

    def _face(self, state) -> Result:
        """Turn toward the selected living unit: the shared facing primitive, nothing more."""
        result = self.targeting.face_selected()
        status = (SkillOutcome.SUCCEEDED if result.faced else
                  SkillOutcome.PREEMPTED if result.code in (FaceCode.BLIND, FaceCode.INTERRUPTED)
                  else SkillOutcome.ABORTED)
        return Result(status, result.detail, result.code.value)

    def _loot(self, state) -> Result:
        # Where and what the most recent fight killed, when it killed something.
        killed = self.fight.killed_name_id
        anchor = self.fight.last_plate if killed is not None else None
        return self._result(self.loot.run(progress=self._progress, anchor=anchor,
                                          name_id=killed), self.loot.detail)

    def _rest(self, state) -> Result:
        if state.vitals.power_type is PowerType.MANA and state.vitals.power is not None and state.vitals.power < 0.35:
            return self._result(self.rest.until(0.75, role=Role.DRINK), self.rest.detail)
        if self.fight.top_up():
            return Result(SkillOutcome.SUCCEEDED, "health topped up", "healthy")
        return self._result(self.rest.until(0.9), self.rest.detail)

    def _repair(self, state) -> Result:
        # Broken gear is no armour and no weapon: a level 6 paladin at full health lost the
        # first fight on its 310-yard walk to a repairer through wolf country (run
        # 20260924T042040-e86c88). Far from one, home by hearthstone first: a new
        # character's stone is bound beside its starting area's armourer.
        durability = state.bags.durability_min
        distance = self._repairer_yards()
        if (durability is not None and durability <= BROKEN_DURABILITY
                and distance is not None and distance > HEARTH_TO_REPAIR_YARDS):
            home = self.hearth.run()
            self.say(f"  broken gear and the nearest repairer {distance:.0f} yards off: "
                     f"hearthstone {home.value} {self.hearth.detail}".rstrip())
        return self._result(self.repair.run(), self.repair.detail)

    def _repairer_yards(self) -> float | None:
        here = self._position()
        if here is None:
            return None
        world = map_to_world(*here, self.client.bounds)
        placed = [n for n in self.graph.nodes if n.kind is StepKind.REPAIR
                  and n.world is not None and n.map_id == self.client.bounds.map_id]
        return min((math.dist(n.world[:2], world) for n in placed), default=None)

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
        if self.arm.decision.skill == "BAG_MAKE_SPACE":
            # A bag lying in the bags is the cheapest room there is: no merchant needed.
            equipper = Vendor(self.client.hid, self._read, lambda: False, self.client.origin,
                              self.client.size)
            if equipper.equip_bags(bag_slots()):
                after = self._read()
                if after and (after.get("bags.free") or 0) > 0:
                    return Result(SkillOutcome.SUCCEEDED, "equipped a bag from the bags", "done")
        wanted = {s.item_id for s in supplies}
        candidates = [m for m in merchants(self.client.bounds.map_id)
                      if (not wanted or wanted & m.items)
                      and (point := world_to_map(*m.world[:2], self.client.bounds)) is not None
                      and all(0 <= value <= 1 for value in point)]
        if not candidates:
            return Result(SkillOutcome.ABORTED, "no generated supplier in the measured zone", "unsupported")
        world = map_to_world(*here, self.client.bounds)
        ranked = sorted(candidates, key=lambda m: math.dist(m.world[:2], world))[:MERCHANT_TRIES]
        for merchant in ranked:
            def visit(merchant=merchant):
                return self._open_merchant(merchant.name, merchant.world,
                                           world_to_map(*merchant.world[:2], self.client.bounds))
            vendor = Vendor(self.client.hid, self._read, visit, self.client.origin, self.client.size)
            try:
                outcome = vendor.run(expected_name=merchant.name,
                                     supplies=tuple(s for s in supplies if s.item_id in merchant.items),
                                     min_free=1 if supplies else 6,
                                     timeout_s=self.travel_timeout + 120)
            except BodyFailure as failure:
                if merchant is ranked[-1] or failure.result.code not in MERCHANT_UNREACHABLE:
                    raise
                self.say(f"  {failure.result.detail}; trying the next merchant")
                continue
            return self._result(outcome, vendor.detail or
                                f"sold {vendor.sold_stacks} stacks; bought {vendor.bought_units} units")
        raise AssertionError("unreachable: the last merchant returns or raises")

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

    def _short_of_body(self, point) -> bool:
        """Walk to within `TRAP_RECLAIM_YARDS` of the body, from the graveyard's side.

        The graveyard is where this process saw the ghost appear; a session that starts
        as a ghost never saw that, and the ghost's own position is the same side of the
        body until it walks."""
        origin = self.recover.graveyard or self._position()
        if origin is None:
            return self._corpse_walk(point)
        body = map_to_world(*point, self.client.bounds)
        start = map_to_world(*origin, self.client.bounds)
        apart = math.dist(body, start)
        if apart <= TRAP_RECLAIM_YARDS:
            return True
        share = TRAP_RECLAIM_YARDS / apart
        short = (body[0] + (start[0] - body[0]) * share, body[1] + (start[1] - body[1]) * share)
        return self._corpse_walk(world_to_map(*short, self.client.bounds))

    def _talk_to(self, name: str):
        """Right-click a named unit: by its nameplate, or where a fresh hover finds it."""
        opened = self.interact.open_on(name)
        if opened in (Interacted.NOT_VISIBLE, Interacted.NO_TARGET):
            return "hovered" if self._hover_interact(name) else opened
        return opened

    def _hover_interact(self, name: str) -> bool:
        """Right-click where a fresh hover says `name` is, for a unit whose nameplate does
        not show: run 20260923T182125-9c54ea's ghost stood under the Spirit Healer, its plate
        behind the strip, and the plate-first interaction found none. Out of reach (run
        ...182544-7dad55: "You are too far away!"), it steps closer and clicks again.

        Silence is out of reach too. The server drops a right-click from beyond five yards
        without a word (`INTERACTION_DISTANCE`, CMaNGOS `GetNPCIfCanInteractWith`): six
        hover-proved clicks on the Spirit Healer in run 20260924T045140-ec8686 opened
        nothing and raised no error, so only a window opening is an answer."""
        wanted = name_id(name)
        (ox, oy), (w, h) = self.client.origin, self.client.size
        too_far = UI_ERROR_KEYS.index("out_of_range")
        for _ in range(HOVER_STEPS + 1):
            point = None
            for fx, fy in HOVER_POINTS:
                candidate = (ox + round(fx * w), oy + round(fy * h))
                after = self.targeting.probe(candidate, require_target=False).after or {}
                if after.get("cursor.has") is True and after.get("cursor.name_id") == wanted:
                    point, errors = candidate, after.get("ui.error_count")
                    break
            if point is None:
                return False
            if self.client.hid.click(*point, right=True) is False:
                return False
            deadline = time.monotonic() + HOVER_ANSWER_S
            while time.monotonic() < deadline:
                answer = self._read() or {}
                if answer.get("ui.modal") is True or answer.get("ui.gossip") is True:
                    return True
                if (answer.get("ui.error_last") == too_far
                        and answer.get("ui.error_count") != errors):
                    break
                time.sleep(0.1)
            if not self.client.hid.hold("w", HOVER_STEP_S):
                return False
        return False

    def _recover(self, state) -> Result:
        # A body where the character keeps dying is not worth getting up at: run
        # 20260923T181209-bc03ba got up beside a level 6 wolf at half health and died,
        # four times. Up at the Spirit Healer instead, and home by hearthstone.
        if self._revived_at is not None and time.monotonic() - self._revived_at < DEATH_TRAP_S:
            up = self.recover.run_spirit_healer()
            if up is Recovered.ALIVE:
                self._revived_at = None
                home = self.hearth.run()
                self.say(f"  up at the Spirit Healer; hearthstone: {home.value} {self.hearth.detail}")
                return self._result(up, f"up at the Spirit Healer; hearthstone {home.value}")
            self.say(f"  the Spirit Healer did not raise us ({up.value}); back to the body, "
                     f"to get up {TRAP_RECLAIM_YARDS:.0f} yards short of it")
        # Always short of the body. Whatever killed the character stands beside it, back at
        # its spawn: the first reclaim at the body itself, at half health, died again four
        # times in runs 20260924T045140-ec8686 and ...050644-f9f9fa, and a new session
        # never knows the last one's revive. Recover reads painted corpse coordinates.
        walk = self.recover.walk_to
        self.recover.walk_to = self._short_of_body
        try:
            outcome = self.recover.run(self.recover.corpse)
        finally:
            self.recover.walk_to = walk
        if outcome is Recovered.ALIVE:
            self._revived_at = time.monotonic()
        return self._result(outcome, self.recover.detail)

    def _release(self, state) -> Result:
        return self._result(self.recover.run(release_only=True), self.recover.detail)

    def _wait(self, state) -> Result:
        return Result(SkillOutcome.SUCCEEDED)
