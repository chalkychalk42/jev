"""A quest whose reward teaches a spell is kept on the route whatever it pays (V388), read from
this server's world database."""

from __future__ import annotations

from jev.guide.graph import Graph, Node
from jev.run.cli import worthless_quests
from jev.world.state_v1 import StepKind

DB = "data/knowledge/tbc-243.sqlite"


def test_a_spell_taught_at_the_hand_in_is_worth_the_quest_and_a_buff_is_not():
    """Training the Beast (6081) teaches Feed Pet and Revive Pet and the last taming (6082) Tame
    Beast and Call Pet, which no trainer teaches; The Stolen Tome (1598) a warlock's Summon Imp.
    Spirit of the Wind (889) casts a run-speed buff and teaches nothing, the first taming (6062)
    nothing (the route keeps it for the taming it opens), and Thunderbrew Lager (117) is a keg
    of lager."""
    graph = Graph(graph_id="w", faction="horde", entry="n117", nodes=tuple(
        Node(id=f"n{q}", kind=StepKind.QUEST_ACCEPT, zone="z", zone_id=1, quest_id=q)
        for q in (117, 889, 1598, 6062, 6081, 6082)))
    assert worthless_quests(DB, graph) == frozenset({117, 889, 6062})
