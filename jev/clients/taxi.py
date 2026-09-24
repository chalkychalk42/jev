"""Take a flight from a flight master's open map (`ui.taxi`, schema 16).

The strip paints the map one node per paint (`taxi.index` of `taxi.total`): the node's
name hashed as names are, whether it is where the character stands (1) or somewhere it can
fly (2), and where the node's button is. A flight is one click on the destination's
button. The character is on its gryphon when `flags.on_taxi` says so, and has landed when
it says so no longer.

Nothing here decides where to fly or whether to (`jev.world.taxi.flight`), and nothing
here opens the map: the body walks to the flight master and talks to it first.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from jev.run.evidence import event, traced

# A whole census of the map: one node a paint, ten paints a second, and a reader that
# lands on each only now and then (the spellbook's lesson, `jev.clients.spellbook`).
MAP_S = 12.0
# From the click to the gryphon.
TAKEOFF_S = 6.0
HERE, THERE = 1, 2


class Flew(StrEnum):
    LANDED = "landed"
    NO_MAP = "no_map"            # the flight master's map is not open
    NO_NODE = "no_node"          # the destination is not on this map as somewhere to fly
    NOT_TAKEN = "not_taken"      # clicked, and the character never took off
    TIMEOUT = "timeout"          # took off, and had not landed in time
    BLIND = "blind"
    REFUSED = "refused"

    @property
    def ok(self) -> bool:
        return self is Flew.LANDED


@dataclass
class TaxiDesk:
    hid: object
    read: Callable[[], dict | None]
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    # index -> (name hash, type, button x, button y), from the last read of the map.
    nodes: dict[int, tuple] = field(default_factory=dict, init=False)
    here: int | None = field(default=None, init=False)
    detail: str = field(default="", init=False)

    @traced("taxi.map")
    def read_map(self, *, wanted: int | None = None, seconds: float = MAP_S) -> bool:
        """Read the open map's nodes; the node painted as here becomes `here`. Stops early
        once every node is read, or `wanted` is read as somewhere to fly with its button."""
        self.nodes, self.here = {}, None
        deadline = self.clock() + seconds
        total = None
        while self.clock() < deadline:
            values = self.read()
            if values is None:
                self.sleep(0.05)
                continue
            if values.get("ui.taxi") is not True:
                self.detail = "the flight master's map is not open"
                return False
            total = values.get("taxi.total") or total
            index = values.get("taxi.index")
            if index is not None:
                entry = (values.get("taxi.name_id"), values.get("taxi.type"),
                         values.get("taxi.x"), values.get("taxi.y"))
                self.nodes[index] = entry
                if entry[1] == HERE:
                    self.here = entry[0]
                if (wanted is not None and entry[0] == wanted and entry[1] == THERE
                        and entry[2] is not None and entry[3] is not None and self.here is not None):
                    return True
            if total and len(self.nodes) >= total:
                return True
            self.sleep(0.05)
        self.detail = f"read {len(self.nodes)} of {total or '?'} nodes of the map"
        return bool(self.nodes)

    @traced("taxi.fly")
    def fly(self, name_id: int, *, flight_s: float) -> Flew:
        """Click the node called `name_id` and ride it down."""
        self.detail = ""
        if not self.read_map(wanted=name_id):
            return Flew.NO_MAP if not self.nodes else Flew.NO_NODE
        target = next((entry for entry in self.nodes.values()
                       if entry[0] == name_id and entry[1] == THERE
                       and entry[2] is not None and entry[3] is not None), None)
        if target is None:
            self.detail = f"no node {name_id} to fly to on this map"
            return Flew.NO_NODE
        ox, oy = self.window_origin
        w, h = self.window_size
        point = (ox + round(target[2] * w), oy + round(target[3] * h))
        event("taxi.take", data={"name_id": name_id, "point": list(point), "here": self.here})
        if self.hid.click(*point) is False:
            return Flew.REFUSED
        if not self._await(lambda v: v.get("flags.on_taxi") is True, TAKEOFF_S):
            self.detail = "clicked the node and never took off"
            return Flew.NOT_TAKEN
        if not self._await(lambda v: v.get("flags.on_taxi") is False, flight_s):
            self.detail = f"still flying after {flight_s:.0f} s"
            return Flew.TIMEOUT
        event("taxi.landed", data={"name_id": name_id})
        return Flew.LANDED

    def close(self) -> None:
        """Shut the map, if it is still open. Escape closes it and nothing else here."""
        values = self.read()
        if values and values.get("ui.taxi") is True and values.get("ui.modal") is not True:
            self.hid.tap("esc")
            self.sleep(0.3)

    def _await(self, ready, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            values = self.read()
            if values is not None and ready(values):
                return True
            self.sleep(0.25)
        return False
