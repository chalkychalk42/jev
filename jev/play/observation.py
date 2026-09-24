"""Owned visual observations and independently measured action effects.

Pixels, radio and geometry describe one capture. A model's predicted effect is only a
hypothesis: this module, not the model, decides whether it happened.
"""

from __future__ import annotations

import hashlib
import io
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jev.guide.objectives import select_objective
from jev.perceive import radio_frame
from jev.perceive.fields import SEQ_MODULUS
from jev.run.client import STALE_AFTER_S

OBSERVATION_VERSION = 1
EFFECTS = frozenset({
    "observed", "selected", "target_cleared", "target_hp_decreased", "target_dead",
    "closer", "moved", "scene_changed", "ui_opened", "ui_closed", "quest_progress",
    "quest_accepted", "quest_cleared", "healed", "power_restored", "recovered",
    "released", "repaired", "bags_freed", "supplies_bought", "supplies_replenished",
    "loot_received", "arrived", "faced", "attacking", "reward_chosen",
})
# A turn that moved the selected unit's plate this much nearer the centre line (fraction of
# the image width), or onto it, faced the unit more. Smaller moves are within plate jitter.
FACED_PROGRESS = 0.03


def fingerprint(value) -> str:
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    data: dict
    png: bytes
    pixels: object | None = None

    @property
    def id(self):
        return self.data["id"]


def visual_features(pixels) -> dict[str, float]:
    """A versioned, fixed-size visual input; no outcome or future pixels leak into it.

    This deliberately small first student must abstain when the picture is unfamiliar.
    The teacher receives the original full-resolution PNG, never this thumbnail alone.
    """
    from PIL import Image

    picture = Image.fromarray(pixels).resize((16, 9), Image.Resampling.BOX)
    return {f"screen.{y}.{x}.{c}": value / 255.0
            for y in range(9) for x in range(16)
            for c, value in enumerate(picture.getpixel((x, y)))}


def detections(pixels, values: dict) -> dict:
    """Where the client drew nameplates in this capture, as fractions of the image.

    `selected_plate` is the only whole plate in the colours the target's reaction can
    take, when there is exactly one - a likely target plate, not a proved one (selection
    does not fade other plates). Several are reported as ambiguous. Detections inform
    the tutor and the effect judge; identity comes from hover, never from here.
    """
    from jev.perceive import units

    height, width = pixels.shape[:2]
    selected, ambiguous = None, False
    if values.get("target.has") is True and values.get("target.hp") != 0:
        bright = units.selected_plates(pixels, values.get("target.reaction"))
        if len(bright) == 1:
            plate = bright[0]
            selected = {"x": round(float(plate.cx) / width, 4),
                        "y": round(float(plate.cy) / height, 4)}
        ambiguous = len(bright) > 1
    others = []
    for plate in units.find_plates(pixels):
        x, y = round(float(plate.cx) / width, 4), round(float(plate.cy) / height, 4)
        if selected and abs(x - selected["x"]) < 0.01 and abs(y - selected["y"]) < 0.01:
            continue
        others.append({"x": x, "y": y})
    return {"version": 1, "selected_plate": selected, "ambiguous": ambiguous,
            "plates": others[:8]}


def context_for(graph, arm, state, values) -> dict:
    node = graph.get(arm.step_id or "")
    destination = node
    if node and node.objective_targets:
        selection = select_objective(node, state.quests)
        if selection.target is not None:
            destination = selection.target
    name = destination.target_name if destination else None
    point = destination.pos if destination else None
    return {
        "skill": arm.decision.skill, "goal": arm.decision.goal,
        "kind": node.kind.value if node else None,
        "step_id": arm.step_id, "arm_id": arm.arm_id,
        "graph_id": graph.graph_id, "quest_id": node.quest_id if node else None,
        "coord_zone_id": graph.coord_zone_id,
        "target_name": name, "target_name_id": radio_frame.name_id(name) if name else None,
        "target_kind": destination.target_kind if destination else None,
        "destination": list(point) if point else None,
        "arrival_radius": node.r if node else None,
        "until_level": arm.decision.params.get("until_level"),
        "selected_name_id": values.get("target.name_id"),
        "ui_name_id": (radio_frame.name_id(node.title) if node and node.title
                       and arm.decision.skill in {"ACCEPT_QUEST", "TURNIN_QUEST"}
                       else values.get("merchant.gossip_name_id")),
    }


class LiveObserver:
    def __init__(self, client, graph, *, screenshots=None):
        self.client, self.graph, self.screenshots = client, graph, screenshots

    def _capture(self):
        with self.client._capturing:
            captured_at = time.time()
            frame = self.client.cap.grab()
            reading = radio_frame.read(frame.rgb)
            if not reading.ok:
                raise RuntimeError(f"play observation unread: {reading.fault}")
            self.client._note_seq(reading.values.get("seq"))
            if self.client.frozen_for() > STALE_AFTER_S:
                raise RuntimeError("play observation frozen")
            self.client.log.observe(reading.values)
            state = self.client.state_from(reading, captured_at=captured_at)
            values = self.client._navigation_values(reading.values)
            generation = self.client._paint_generation
        # Resizing/moving invalidates existing body geometry. Reattach on next launch;
        # never silently continue with stale offsets in a delegated legacy routine.
        if frame.origin != self.client.origin or frame.size != self.client.size:
            raise RuntimeError("client geometry changed; reattach before continuing")
        return captured_at, frame, values, state, generation

    def guard(self):
        from jev.play.executor import GuardState

        captured_at, frame, values, _, _generation = self._capture()
        return GuardState(values=values, captured_at=captured_at,
                          origin=frame.origin, size=frame.size,
                          cursor=self.client.hid.cursor_position())

    def observe(self, arm, *, retain=True) -> Observation:
        captured_at, frame, values, state, generation = self._capture()
        data = {
            "schema": OBSERVATION_VERSION, "id": uuid.uuid4().hex, "captured_at": captured_at,
            "values": values, "state": state.model_dump(mode="json"),
            "origin": list(frame.origin), "size": list(frame.size),
            "screen": {"width": frame.size[0], "height": frame.size[1]},
            "features": visual_features(frame.rgb),
            "detections": detections(frame.rgb, values),
            "context": context_for(self.graph, arm, state, values), "synthetic": False,
            "freshness": {"paint_generation": generation},
        }
        observation = Observation(data, b"", frame.rgb)
        return self.retain(observation) if retain else observation

    def retain(self, observation: Observation) -> Observation:
        """Persist the chosen owned frame, without a second capture or every poll on disk."""
        if observation.png:
            return observation
        output = io.BytesIO()
        scaled(observation.pixels).save(output, format="PNG", compress_level=1)
        png = output.getvalue()
        screen = {**observation.data["screen"], "sha256": hashlib.sha256(png).hexdigest()}
        if self.screenshots is not None:
            retained = self.screenshots.record_frame("play-observation", observation.pixels,
                                                     captured_at=observation.data["captured_at"])
            if retained.get("status") != "ok":
                raise RuntimeError("play observation could not be retained")
            screen["record"] = retained
            screen["path"] = str(Path(self.screenshots.directory) / retained["file"])
        return Observation({**observation.data, "screen": screen}, png)


# The tutor's copy of a frame carries a scale along its edges: a tick and a label at every
# tenth of the width and height, within this many pixels of the border. It gave "the Kobold
# Worker nameplate at (0.65, 0.36)" as x 0.98, twice (run 20260924T032302-458737): a model
# reading pixels off an unmarked picture divides by the wrong width.
SCALE_BORDER_PX = 18


def scaled(pixels):
    """The frame for the tutor: unchanged except for the scale drawn along its edges."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.fromarray(pixels).copy()
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = ImageFont.load_default(size=14)
    ink, shade = (255, 230, 0), (0, 0, 0)
    tick = SCALE_BORDER_PX - 6
    for tenth in range(1, 10):
        x, y = round(width * tenth / 10), round(height * tenth / 10)
        label = f".{tenth}"
        for start, end in (((x, 0), (x, tick)), ((x, height - 1 - tick), (x, height - 1)),
                           ((0, y), (tick, y)), ((width - 1 - tick, y), (width - 1, y))):
            draw.line([start, end], fill=ink, width=2)
        for corner in ((x + 3, 0), (x + 3, height - SCALE_BORDER_PX), (0, y + 2),
                       (width - 22, y + 2)):
            draw.rectangle([corner, (corner[0] + 20, corner[1] + 16)], fill=shade)
            draw.text((corner[0] + 2, corner[1]), label, fill=ink, font=font)
    return image


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _increase(before, after, name):
    a, b = before.get(name), after.get(name)
    return _number(a) and _number(b) and b > a


def _quests(observation):
    quests = observation.get("state", {}).get("quests")
    return None if quests is None else {q["quest_id"]: q for q in quests}


def measured_effects(before: dict, after: dict) -> tuple[list[str], float]:
    a, b = before["values"], after["values"]
    effects = ["observed"]
    progress = 0.0
    wanted = before.get("context", {}).get("target_name_id")
    selected = b.get("target.has") is True and b.get("target.name_id") is not None
    # A corpse does not get up within one action: a selection that was dead and is now
    # alive is another unit, even of the same name - the next kobold after a kill, which
    # a name-only test never credited (run 20260923T191946-2b79ed).
    revived = (a.get("target.has") is True and a.get("target.hp") == 0
               and _number(b.get("target.hp")) and b.get("target.hp") > 0)
    # The unit's own identity where the strip paints it: another Kobold Worker at full
    # health is a new selection, which neither its name nor its health could show.
    other = (a.get("target.guid") is not None and b.get("target.guid") is not None
             and a.get("target.guid") != b.get("target.guid"))
    if selected and (wanted is None or b.get("target.name_id") == wanted) and (
            a.get("target.has") is False or a.get("target.name_id") != b.get("target.name_id")
            or revived or other):
        effects.append("selected")
    if a.get("target.has") is True and b.get("target.has") is False:
        effects.append("target_cleared")
        # The client can clear the selection at the kill itself (measured 23 September:
        # the wolf at 20% one paint, gone the next, experience arriving with it). In a
        # fight nothing else grants experience, so that pairing is the kill.
        if _increase(a, b, "char.xp_pct") or _increase(a, b, "char.level"):
            effects.append("target_dead")
    # A kill can move the selection on in the same paint its experience arrives: a Timber
    # Wolf at 20% gave way to another unit at full health (run 20260923T233909-8b1484).
    # A living selection replaced while experience arrived is the kill.
    moved_on = (b.get("target.has") is True
                and (b.get("target.name_id") != a.get("target.name_id")
                     or (_number(a.get("target.hp")) and _number(b.get("target.hp"))
                         and b.get("target.hp") > a.get("target.hp"))))
    if ("target_dead" not in effects and a.get("target.has") is True
            and _number(a.get("target.hp")) and a.get("target.hp") > 0 and moved_on
            and (_increase(a, b, "char.xp_pct") or _increase(a, b, "char.level"))):
        effects.append("target_dead")
    same = (a.get("target.has") is True and b.get("target.has") is True
            and a.get("target.name_id") is not None
            and a.get("target.name_id") == b.get("target.name_id"))
    if same:
        ah, bh = a.get("target.hp"), b.get("target.hp")
        if _number(ah) and _number(bh) and bh < ah:
            effects.append("target_hp_decreased")
            progress += ah - bh
            if bh == 0:
                effects.append("target_dead")
        if ((a.get("target.in_melee") is False and b.get("target.in_melee") is True)
                or (a.get("target.melee_range") is False and b.get("target.melee_range") is True)):
            effects.append("closer")  # into ~11 yards, or into the Attack action's reach
        plate_a = (before.get("detections") or {}).get("selected_plate")
        plate_b = (after.get("detections") or {}).get("selected_plate")
        if plate_a and plate_b:
            was, now = abs(plate_a["x"] - 0.5), abs(plate_b["x"] - 0.5)
            if was - now >= FACED_PROGRESS or (now <= 0.05 < was):
                effects.append("faced")
    if a.get("bars.attacking") is False and b.get("bars.attacking") is True:
        effects.append("attacking")
    if (a.get("pos.zone_id") is not None and a.get("pos.zone_id") == b.get("pos.zone_id")
            and a.get("pos.coord_zone_id") == b.get("pos.coord_zone_id")
            and all(_number(v.get(k)) for v in (a, b) for k in ("pos.mx", "pos.my"))
            and (a["pos.mx"], a["pos.my"]) != (b["pos.mx"], b["pos.my"])):
        effects.append("moved")
    fa, fb = before.get("features", {}), after.get("features", {})
    if fa and fb and fa != fb:
        effects.append("scene_changed")
    windows = ("ui.loot", "ui.gossip", "ui.vendor", "ui.quest_frame", "ui.modal")
    if any(a.get(k) is False and b.get(k) is True for k in windows):
        effects.append("ui_opened")
    if any(a.get(k) is True and b.get(k) is False for k in windows):
        effects.append("ui_closed")
    if a.get("ui.choice_made") is False and b.get("ui.choice_made") is True:
        effects.append("reward_chosen")
    for field, effect in (("vitals.hp", "healed"), ("vitals.power", "power_restored"),
                          ("bags.durability_min", "repaired"), ("bags.free", "bags_freed")):
        if _increase(a, b, field):
            effects.append(effect)
    if (a.get("vitals.dead") is True or a.get("vitals.ghost") is True) and (
            b.get("vitals.dead") is False and b.get("vitals.ghost") is False):
        effects.append("recovered")
        progress += 1
    if a.get("vitals.ghost") is False and b.get("vitals.ghost") is True:
        effects.append("released")
    qa, qb = _quests(before), _quests(after)
    quest = before.get("context", {}).get("quest_id")
    if qa is not None and qb is not None and quest is not None:
        if quest not in qa and quest in qb:
            effects.append("quest_accepted")
            progress += 1
        # Absence alone could be abandonment. Hand-in needs observed reward too.
        if (quest in qa and quest not in qb
                and (_increase(a, b, "char.xp_pct") or _increase(a, b, "char.level")
                     or _increase(a, b, "bags.money_copper"))):
            effects.append("quest_cleared")
            progress += 1
        if quest in qa and quest in qb:
            old_rows = qa[quest].get("objectives", [])
            new_rows = qb[quest].get("objectives", [])
            old = {o["counter_index"]: o for o in old_rows
                   if type(o.get("counter_index")) is int
                   and sum(r.get("counter_index") == o["counter_index"] for r in old_rows) == 1}
            gain = sum(max(0, o["have"] - old[o["counter_index"]]["have"])
                       for o in new_rows if type(o.get("counter_index")) is int
                       and o["counter_index"] in old
                       and sum(r.get("counter_index") == o["counter_index"] for r in new_rows) == 1
                       and o.get("need") == old[o["counter_index"]].get("need"))
            completed = qa[quest].get("complete") is False and qb[quest].get("complete") is True
            if gain or completed:
                effects.append("quest_progress")
                progress += gain or 1
    # A delegated fight selects its own kill, so the target the rules above compare was
    # not selected before the action. Measured 23 September: COMBAT_PROFILE killed a
    # Kobold Worker (XP 60, "Kobold Worker slain: 1/10") and was judged a failure. Only a
    # kill grants experience together with a corpse left selected, or with a counter.
    gained = _increase(a, b, "char.xp_pct") or _increase(a, b, "char.level")
    corpse = b.get("target.has") is True and b.get("target.hp") == 0
    if gained and "target_dead" not in effects and (corpse or "quest_progress" in effects):
        effects.append("target_dead")
    for supply in ("food", "drink"):
        identity, count = f"bags.{supply}_id", f"bags.{supply}_count"
        same_item = (type(a.get(identity)) is int and a[identity] > 0
                     and a[identity] == b.get(identity))
        if same_item and _increase(a, b, count):
            # A composed visit may open and close its merchant inside one action.
            # Stable item/count evidence proves supplies arrived; it does not claim
            # purchase attribution or require an intermediate shop endpoint.
            effects.append("supplies_replenished")
            if (a.get("ui.vendor") is True and _number(a.get("bags.money_copper"))
                    and _number(b.get("bags.money_copper"))
                    and b["bags.money_copper"] < a["bags.money_copper"]):
                effects.append("supplies_bought")
    if (a.get("ui.loot") is True or (same and a.get("target.hp") == 0)) and (
            _increase(a, b, "bags.money_copper") or "quest_progress" in effects
            or (_number(a.get("bags.free")) and _number(b.get("bags.free"))
                and b["bags.free"] < a["bags.free"])):
        effects.append("loot_received")
    point, radius = (before.get("context", {}).get(k) for k in ("destination", "arrival_radius"))
    coord = before.get("context", {}).get("coord_zone_id")
    same_frame = (b.get("pos.coord_zone_id") == coord if coord is not None else
                  a.get("pos.zone_id") is not None and a.get("pos.zone_id") == b.get("pos.zone_id"))
    if (point and same_frame and _number(radius) and all(_number(b.get(k)) for k in ("pos.mx", "pos.my"))
            and math.dist(point, (b["pos.mx"], b["pos.my"])) <= radius):
        effects.append("arrived")
    return sorted(set(effects)), progress


def judge(before: dict, after: dict | None, *, expected_effect: str, delivered: bool,
          detail: str = "", action: dict | None = None) -> dict:
    """No teacher confidence, accepted keypress or disappearance is a success label."""
    base = {"verified": False, "success": False, "effects": [], "fatal": False,
            "progress": 0.0, "reason": detail}
    if after is None or after.get("captured_at", 0) <= before.get("captured_at", 0):
        return {**base, "reason": detail or "missing fresh post-action observation"}
    a, b = before["values"], after["values"]
    old_generation = before.get("freshness", {}).get("paint_generation")
    new_generation = after.get("freshness", {}).get("paint_generation")
    progressed = (type(old_generation) is int and type(new_generation) is int
                  and new_generation > old_generation)
    sequence_progressed = (type(a.get("seq")) is int and type(b.get("seq")) is int
                           and 0 < (b["seq"] - a["seq"]) % SEQ_MODULUS < 128)
    if (b.get("vitals.dead") is None or b.get("vitals.ghost") is None
            or not (progressed or sequence_progressed)):
        return {**base, "reason": detail or "outcome telemetry incomplete or unchanged"}
    effects, progress = measured_effects(before, after)
    fatal = ((a.get("vitals.dead") is False and b.get("vitals.dead") is True)
             or (a.get("vitals.ghost") is False and b.get("vitals.ghost") is True
                 and (action or {}).get("name") != "RELEASE_SPIRIT"))
    # Damage after a selection/toggle request may belong to a different same-name unit
    # or an earlier swing. Retain it, but do not use it to teach target acquisition.
    identity_action = ((action or {}).get("kind") == "click" and
                       (action or {}).get("intent") == "select") or (
        (action or {}).get("kind") == "key" and
        (action or {}).get("control") in {"target_next", "target_previous"})
    if identity_action and expected_effect in {"target_hp_decreased", "target_dead", "closer"}:
        return {**base, "effects": effects, "fatal": fatal,
                "reason": "selection cannot establish continuous target identity"}
    success = delivered and not fatal and expected_effect in effects and expected_effect in EFFECTS
    return {"verified": True, "success": success, "effects": effects, "fatal": fatal,
            "progress": progress, "reason": detail or (
                f"observed {expected_effect}" if success else f"did not observe {expected_effect}")}


def capability(observation: dict) -> str:
    """Contextual competence buckets are shared across quests, units and classes."""
    v, context = observation["values"], observation.get("context", {})
    skill = context.get("skill")
    if skill == "ABORT_WAIT":
        return "interact"
    if skill in {"RELEASE_SPIRIT", "CORPSE_RUN"}:
        return "recover"
    if skill == "TRAVEL_TO":
        return "travel"
    if skill in {"VENDOR_REPAIR", "BAG_MAKE_SPACE", "BUY_AMMO_REAGENT_FOOD"}:
        return "service"
    if skill == "EAT_DRINK":
        return "rest"
    if skill in {"ACCEPT_QUEST", "TURNIN_QUEST"}:
        return "interact"
    if v.get("ui.loot") is True or (v.get("target.has") is True and v.get("target.hp") == 0):
        return "loot"
    if v.get("target.has") is not True or (
            context.get("target_name_id") is not None
            and v.get("target.name_id") != context["target_name_id"]):
        return "acquire"
    if v.get("vitals.combat") is True or v.get("target.attacking_me") is True:
        return "combat"
    return "approach"
