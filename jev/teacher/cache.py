"""An answer cache keyed by `situation_key` — the fourth job of that key.

`ARCHITECTURE.md` §2 lists four jobs for `situation_key`, and this is the one that shows up
directly on the bill: a recent answer for this bucket is reusable without asking again. On
a subscription where the rate limit is one window shared by the whole farm, the cheapest
call is the one nobody makes.

Two properties, both of them load-bearing:

**Bounded.** An unbounded cache is a second corpus that nobody versioned, nobody grades and
nobody can explain. 256 entries: a zone's spine is a few hundred steps and only the
ambiguous ones ever reach the teacher, so this holds a whole zone's live buckets while
staying small enough to read in a debugger.

**Expiring.** A stale answer about a step is worse than no answer, because no answer falls
back to the scripted coach that is always valid while a stale one is followed. The TTL is
60 s, which is not arbitrary: `jev.coach.situation._age` bins step age at 60 s, and
`jev.learn.episode.grade` measures outcomes over a 60 s window. So a cached answer can
never outlive the age band that keyed it, and it can never be reused past the point where
the first use would already have been graded.

What is deliberately not cached: anything the verifier refused, and anything that failed.
Caching a refusal would re-serve the refusal for free, which turns one bad answer into a
minute of them; caching a failure would cache the absence of an answer, which §6 says is
not an answer at all.

Verification is **not** cached with the answer. A hit is re-verified against the asking
client's own state by `queue.py`, because the bin says the same answer is correct for both
states, not that the same character is standing in the same place.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from jev.coach.schema import TeacherReply

DEFAULT_MAXSIZE = 256
DEFAULT_TTL_S = 60.0


@dataclass(frozen=True)
class CachedAnswer:
    """A reply, plus where it came from.

    `decision_id` is the call that originally paid for this answer. It travels with the hit
    so the reusing client's `DecisionRow.dedup_of` can point back at it: one answer, however
    many clients used it, stays one answer in the corpus rather than looking like several
    independent agreements.
    """

    reply: TeacherReply
    decision_id: str
    stored_at: float
    model: str | None = None

    def age_s(self, now: float) -> float:
        return now - self.stored_at


@dataclass
class CacheStats:
    """Counters, because the saving is the whole argument for `situation_key` and an
    argument that is not in the numbers is an argument nobody can check."""

    hits: int = 0
    misses: int = 0
    expired: int = 0   # found, but too old to use — a miss that proves the TTL is working
    evicted: int = 0   # pushed out by the bound — proves the bound is working
    puts: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "cache_expired": self.expired,
            "cache_evicted": self.evicted,
            "cache_puts": self.puts,
            "cache_hit_rate": round(self.hit_rate, 4),
        }


@dataclass
class AnswerCache:
    """Bounded, TTL'd, least-recently-used.

    LRU rather than plain FIFO because the buckets that recur are the ones worth keeping:
    a step the farm is stuck on is asked about repeatedly, and that is exactly the entry a
    FIFO would evict while it was still earning.

    `now` is injectable so the TTL can be tested without sleeping. A test that sleeps to
    prove an expiry is a test that is slow and, on a loaded machine, occasionally a liar.
    Monotonic rather than wall clock: an NTP step backwards must not resurrect dead answers.
    """

    maxsize: int = DEFAULT_MAXSIZE
    ttl_s: float = DEFAULT_TTL_S
    now: Callable[[], float] = time.monotonic
    stats: CacheStats = field(default_factory=CacheStats)
    _entries: OrderedDict[str, CachedAnswer] = field(default_factory=OrderedDict, repr=False)

    def get(self, situation_key: str) -> CachedAnswer | None:
        entry = self._entries.get(situation_key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.age_s(self.now()) >= self.ttl_s:
            # Dropped on read rather than swept on a timer: the sweep would need a task, and
            # a background task in a cache is a lifecycle bug waiting for a test to find it.
            del self._entries[situation_key]
            self.stats.expired += 1
            self.stats.misses += 1
            return None
        self._entries.move_to_end(situation_key)
        self.stats.hits += 1
        return entry

    def put(
        self,
        situation_key: str,
        reply: TeacherReply,
        *,
        decision_id: str,
        model: str | None = None,
    ) -> CachedAnswer:
        if reply.is_empty():
            # An abstention is not an answer, so storing it would mean serving "I do not
            # know" for a minute at cache speed (ARCHITECTURE.md §6).
            raise ValueError("refusing to cache an empty reply: an abstention is not an answer")
        entry = CachedAnswer(
            reply=reply, decision_id=decision_id, stored_at=self.now(), model=model
        )
        self._entries[situation_key] = entry
        self._entries.move_to_end(situation_key)
        self.stats.puts += 1
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)
            self.stats.evicted += 1
        return entry

    def invalidate(self, situation_key: str) -> bool:
        """For when something learns the answer is wrong — a graded failure, or a graph
        patch that changed the step the answer was about."""
        return self._entries.pop(situation_key, None) is not None

    def clear(self) -> None:
        self._entries.clear()

    def keys(self) -> list[str]:
        """Oldest first. Useful in a postmortem, and in the eval board."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, situation_key: str) -> bool:
        """Non-counting membership, so introspection cannot flatter the hit rate."""
        entry = self._entries.get(situation_key)
        return entry is not None and entry.age_s(self.now()) < self.ttl_s
