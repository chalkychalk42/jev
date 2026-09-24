"""Where the character had got to, remembered between runs.

A playhead is state. Recomputing it from nothing every run is what
`Tracker.resume` does from the graph entry, and it cannot be right on its own: a quest
turned in last session and a quest never accepted are **both simply absent from the log**,
so a cold scan stops at the accept step of a quest that is already finished and walks the
character back to the NPC that no longer has anything to give it. That is not a
hypothetical — it happened on the run straight after 783 was handed in.

The strip cannot fix this. 2.4.3 has no completed-quest query; `GetQuestsCompleted`
arrived in 3.0. What the client *does* know is that a finished quest is not offered, and
finding that out costs a walk to every NPC in the chain.

So the position is written down. On the next run the scan starts **from the remembered
step rather than the entry**, which means it can still move forward over anything already
done — a log read at startup is always believed over the file — but it cannot walk
backwards into a quest it has already finished.

One per character
-----------------
Each character keeps its own, named by the key the strip paints for it (`char.key`, its
name and realm hashed), and the run reads that key before anything is loaded. With one
file for the installation, a fresh level 1 would have started at another character's
quest 21, Northshire's first five quests marked done. A character with no file yet is new
to the bot and starts from its own quest log.

Deliberately not a save file
----------------------------
The step, the graph it belongs to, and the way back from a grind rib. Anything richer is a
second model of the world that has to be kept true, and the log is already the source of
truth for everything except these things it cannot express. A rib is shared by every step
in its zone, so the graph cannot say where it leads back to either: quest 15 sat complete in
the log for a session because the hand-in that failed into a rib was not written down.
"""

from __future__ import annotations

import json
import pathlib
from contextlib import suppress
from dataclasses import dataclass

from jev.persist import atomic_json

DEFAULT_PATH = pathlib.Path("var/playhead.json")
CHARACTERS = pathlib.Path("var/playheads")


def for_character(key: int, root: pathlib.Path = CHARACTERS) -> pathlib.Path:
    """Where one character's playhead lives, by the key the strip paints for it."""
    return root / f"character-{key:08x}.json"


@dataclass(frozen=True)
class Remembered:
    """Where the playhead was, and which quests are finished.

    `completed` is the one that matters. `step_id` is a convenience and is fragile by
    nature — regenerate the guide and it can name a node that no longer exists, which is
    exactly what happened when two unplaceable quests were dropped: the position was lost
    and the scan fell all the way back to the entry and walked to Deputy Willem for a
    quest handed in twenty minutes earlier.

    Quest ids do not have that problem. They are facts about the character, not about the
    current shape of the guide, so they survive regeneration, re-ordering and a different
    guide entirely.
    """

    graph_id: str
    step_id: str | None = None
    completed: frozenset[int] = frozenset()
    # Where `step_id` leads when it is done, when that is not its own next step.
    rejoin_to: str | None = None
    # Deaths on `step_id` so far. Counted in memory alone, a death edge needed all its deaths
    # inside one fifteen-minute session, and a rib's four in two sessions never counted.
    deaths: int = 0


def load(graph_id: str, path: pathlib.Path = DEFAULT_PATH) -> Remembered:
    """What is remembered for this graph. Never raises.

    A missing file, unreadable JSON or a different graph all read as "nothing remembered".
    A corrupt file is treated as no memory rather than as an error: the scan is a correct
    fallback, and refusing to run because a cache is malformed is worse than starting
    slightly further back than necessary.

    `completed` survives a graph mismatch deliberately — those are the character's quests,
    not the guide's.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Remembered(graph_id=graph_id)
    if not isinstance(data, dict):
        return Remembered(graph_id=graph_id)
    done = data.get("completed")
    completed = frozenset(q for q in done if isinstance(q, int)) if isinstance(done, list) \
        else frozenset()
    step = data.get("step_id")
    if data.get("graph_id") != graph_id or not isinstance(step, str) or not step:
        step = None
    rejoin = data.get("rejoin_to")
    if step is None or not isinstance(rejoin, str) or not rejoin:
        rejoin = None
    deaths = data.get("deaths")
    if step is None or not isinstance(deaths, int) or isinstance(deaths, bool) or deaths < 0:
        deaths = 0
    return Remembered(graph_id=graph_id, step_id=step, completed=completed, rejoin_to=rejoin,
                      deaths=deaths)


def save(graph_id: str, step_id: str | None = None,
         completed: frozenset[int] | set[int] = frozenset(),
         path: pathlib.Path = DEFAULT_PATH, rejoin_to: str | None = None,
         deaths: int = 0) -> None:
    """Write the position. Best effort: failing to remember must not fail the run."""
    with suppress(OSError):
        atomic_json(path, {"graph_id": graph_id, "step_id": step_id,
                           "completed": sorted(completed), "rejoin_to": rejoin_to,
                           "deaths": deaths})


def with_completed(remembered: Remembered, quest_id: int) -> Remembered:
    """A record with one more quest finished."""
    return Remembered(graph_id=remembered.graph_id, step_id=remembered.step_id,
                      completed=remembered.completed | {quest_id},
                      rejoin_to=remembered.rejoin_to, deaths=remembered.deaths)
