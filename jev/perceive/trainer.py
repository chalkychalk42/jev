"""Assemble the class trainer's list from a strip that paints one row per paint (`fields.py`,
schema 18).

As with the spellbook (`jev.perceive.spellbook`), a partial census is **unread**, not short:
a list half seen can leave out the one spell worth buying, and a row number read under one
list names another row once the list has changed (a purchase takes the bought row out of
it). So the census is whole only when every row of the short cycle has been seen - the
headers and the services learnable now, which two paints in three describe and
`trainer.short` counts - under one unchanged revision, row count and short count. A
change to any of the three throws the half-built census away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# `trainer.type`, as the addon paints it.
HEADER, AVAILABLE, UNAVAILABLE, USED, FOLDED = 0, 1, 2, 3, 4
# The short cycle: what a buyer acts on. A header shut folds its services out of the list.
SHORT_KINDS = frozenset({HEADER, AVAILABLE, FOLDED})
_KEY = ("trainer.revision", "trainer.total", "trainer.short")


@dataclass(frozen=True)
class TrainerRow:
    index: int                    # 1-based, in the stock window's order
    name_id: int | None           # `radio_frame.name_id` of its name
    rank: int | None              # the number in its rank text, 0 without one
    kind: int | None              # HEADER, AVAILABLE, UNAVAILABLE, USED or FOLDED
    cost: int | None              # copper; `None` for a header


def list_key(values: dict[str, Any]) -> tuple | None:
    """Which list a paint describes: its revision, row count and short count, or `None`
    when the strip paints no trainer's list (a closed window, or schema 17)."""
    key = tuple(values.get(name) for name in _KEY)
    return None if None in key else key


@dataclass
class TrainerCensus:
    """Accumulates the open trainer's list into a whole short cycle."""

    _key: tuple | None = field(default=None, init=False)
    _rows: dict[int, TrainerRow] = field(default_factory=dict, init=False)

    def observe(self, values: dict[str, Any]) -> None:
        key = list_key(values)
        if key is None:
            return
        if key != self._key:
            self._key = key
            self._rows.clear()
        index = values.get("trainer.index")
        if index is None or not 1 <= index <= key[1]:
            return
        self._rows[index] = TrainerRow(index, values.get("trainer.name_id"),
                                       values.get("trainer.rank"), values.get("trainer.type"),
                                       values.get("trainer.cost"))

    @property
    def key(self) -> tuple | None:
        """The list the rows were read from (`list_key`)."""
        return self._key

    @property
    def short(self) -> tuple[TrainerRow, ...] | None:
        """The headers and the services learnable now, in list order, or `None` until every
        one of them has been seen under the current list."""
        if self._key is None:
            return None
        rows = [r for r in self._rows.values() if r.kind in SHORT_KINDS]
        if len(rows) < self._key[2]:
            return None
        return tuple(sorted(rows, key=lambda r: r.index))

    @property
    def rows(self) -> tuple[TrainerRow, ...]:
        """Every row seen under the current list, in list order, whole or not."""
        return tuple(self._rows[i] for i in sorted(self._rows))

    def reset(self) -> None:
        self._key = None
        self._rows.clear()
