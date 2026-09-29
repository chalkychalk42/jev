"""Have the client draw the nameplates a look needs (V320).

Every unit the body selects is found by its nameplate first (`jev.perceive.units`): a
merchant, a smith, an innkeeper, a quest giver by its green one, a unit to fight by its red.
The client draws a kind of plate only while its binding has it shown, and the state lives
in the client, not here: the keeper relaunched the client at 22:56 on 27 Sep, and it came
back with friendly plates hidden (`FRIENDNAMEPLATES_ON = nil` in the account's saved
variables). Before then target.candidates had seen 1-16 green plates an hour; from then, in
34 hours and 4,183 looks, none, and every interaction with a friendly unit ended "no observed
nameplate for selection" or walked on until something cut it short: in sessions 387-426, 123
repair, bag, bind, accept, training and flight arms, 167 minutes, one done (a trainer whose
plate the finder took for a neutral one), the gear at 0% durability throughout and the bags
full.

Schema 20 paints the binding's state (`ui.plates_enemy`, `ui.plates_friendly`). A kind seen
hidden is shown by its binding and seen shown on a later paint, the toggle pressed only from
the state read on the frame in hand (ARCHITECTURE section 6). An older strip cannot say, and
nothing is pressed: a toggle from an unread state is as likely to hide them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from jev.play.controls import CONTROL_SPECS
from jev.run.evidence import event


class Plates(StrEnum):
    ENEMY = "enemy"
    FRIENDLY = "friendly"


FIELD = {Plates.ENEMY: "ui.plates_enemy", Plates.FRIENDLY: "ui.plates_friendly"}
# The stock bindings, as the controls table has them (V, SHIFT-V: this client's
# bindings-cache.wtf binds the same).
BINDING = {Plates.ENEMY: CONTROL_SPECS["toggle_enemy_nameplates"][1],
           Plates.FRIENDLY: CONTROL_SPECS["toggle_friendly_nameplates"][1]}
# How long a press is looked for in the paints after it: the binding runs on the key's
# down, and the strip paints ten times a second.
SHOWN_WAIT_S = 1.5
SHOWN_POLL_S = 0.1


def keys(binding: str) -> tuple[str | None, str]:
    """A binding as (modifier, key): "SHIFT-V" is ("shift", "v"), "V" is (None, "v")."""
    parts = binding.lower().split("-")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def show(hid, read: Callable[[], dict | None], kind: Plates, *, values: dict | None = None,
         wait_s: float = SHOWN_WAIT_S) -> bool | None:
    """The client draws `kind`'s plates: `True` seen so, at once or after its binding was
    pressed; `False` seen hidden and not shown by the press, or the press refused; `None`
    for a strip that cannot say (before schema 20, or blind), when nothing is pressed.
    `values` is a reading in hand, else one is read."""
    values = read() if values is None else values
    shown = values.get(FIELD[kind]) if values else None
    if shown is not False:
        return shown
    modifier, key = keys(BINDING[kind])
    event("plates.show", data={"kind": kind.value, "binding": BINDING[kind]})
    pressed = hid.chord(modifier, key) if modifier else hid.tap(key)
    if not pressed:
        return False
    deadline = time.monotonic() + wait_s
    while True:
        after = read()
        if after is not None and after.get(FIELD[kind]) is True:
            event("plates.shown", data={"kind": kind.value})
            return True
        if time.monotonic() >= deadline:
            event("plates.not_shown", data={"kind": kind.value})
            return False
        time.sleep(SHOWN_POLL_S)
