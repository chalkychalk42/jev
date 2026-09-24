"""Assemble the main action bar and the spellbook from a strip that paints one entry of each
per paint (`fields.py`, schema 15).

As with the quest log (`jev.perceive.questlog`), a partial census is **unread**, not short:
a spellbook half seen would send the character to a trainer for spells it knows, and a bar
half seen would put a new spell over one it has. Each census is whole only when every
entry has been seen under one unchanged revision; a new revision throws the half-built one
away, and the last whole one is kept until the next is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BAR_SLOTS = 12


@dataclass(frozen=True)
class BookEntry:
    index: int
    spell_id: int | None
    passive: bool | None


@dataclass
class SpellCensus:
    """Accumulates the bar and spellbook censuses into whole snapshots."""

    _bar_revision: int | None = field(default=None, init=False)
    _bar: dict[int, int | None] = field(default_factory=dict, init=False)
    _bar_whole: dict[int, int | None] | None = field(default=None, init=False)
    _book_revision: int | None = field(default=None, init=False)
    _book_total: int | None = field(default=None, init=False)
    _book: dict[int, BookEntry] = field(default_factory=dict, init=False)
    _book_whole: tuple[BookEntry, ...] | None = field(default=None, init=False)
    # The revision each whole census was taken under.
    bar_revision: int | None = field(default=None, init=False)
    book_revision: int | None = field(default=None, init=False)

    def observe(self, values: dict[str, Any]) -> None:
        self._observe_bar(values)
        self._observe_book(values)

    def _observe_bar(self, values: dict[str, Any]) -> None:
        revision, slot = values.get("bars.revision"), values.get("bars.slot")
        if revision is None or slot is None or not 1 <= slot <= BAR_SLOTS:
            return
        if revision != self._bar_revision:
            self._bar_revision = revision
            self._bar.clear()
        self._bar[slot] = values.get("bars.slot_spell")
        if len(self._bar) == BAR_SLOTS:
            self._bar_whole = dict(self._bar)
            self.bar_revision = revision

    def _observe_book(self, values: dict[str, Any]) -> None:
        revision, total = values.get("spells.revision"), values.get("spells.total")
        index = values.get("spells.index")
        if revision is None or total is None:
            return
        if revision != self._book_revision or total != self._book_total:
            self._book_revision, self._book_total = revision, total
            self._book.clear()
        if total == 0:
            self._book_whole = ()
            self.book_revision = revision
            return
        if index is None or not 1 <= index <= total:
            return
        self._book[index] = BookEntry(index, values.get("spells.id"), values.get("spells.passive"))
        if len(self._book) == total:
            self._book_whole = tuple(self._book[i] for i in sorted(self._book))
            self.book_revision = revision

    @property
    def bar(self) -> dict[int, int | None] | None:
        """Slot to spell id (0 empty, `None` an item or unread), or `None` if never whole."""
        return None if self._bar_whole is None else dict(self._bar_whole)

    @property
    def book(self) -> tuple[BookEntry, ...] | None:
        return self._book_whole

    @property
    def known(self) -> frozenset[int] | None:
        """Every spell id in the spellbook, or `None` while no whole census has been seen."""
        if self._book_whole is None:
            return None
        return frozenset(e.spell_id for e in self._book_whole if e.spell_id)

    def forget_bar(self) -> None:
        """After a drag: the bar is unread until a census under the new revision is whole."""
        self._bar_whole = None
        self._bar.clear()

    def reset(self) -> None:
        self._bar_revision = self._book_revision = self._book_total = None
        self.bar_revision = self.book_revision = None
        self._bar.clear()
        self._book.clear()
        self._bar_whole = None
        self._book_whole = None
