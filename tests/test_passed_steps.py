"""A step passed over is passed for the level, not for good (V342), and the hive gives a step
the live bot's attempts (V341)."""

from jev.clients.source import ScriptedSource
from jev.guide.graph import FailEdge, FailWhen, Graph, Node
from jev.guide.tracker import Event
from jev.guide.tracker import Verdict as TrackVerdict
from jev.learn.episode import Recorder
from jev.orch.runtime import RETURNS, ClientRuntime
from jev.run import cli
from jev.world.state_v1 import Char, Pos, Quest, Sense, State, StepKind, Ui, Vitals

ACCEPT = ("TRAVEL_TO", "ACCEPT_QUEST")
DO = ("TRAVEL_TO", "GRIND_UNTIL")
TURNIN = ("TRAVEL_TO", "TURNIN_QUEST")


def spine():
    """Modelled on hive-389's Coldridge Valley (Gwaiglo, a level 7 dwarf hunter, 4 Oct): The
    Boar Hunter (183, levels 1-6) and The Stolen Journal (218, 1-8), the second needing the
    first handed in, as the follow-ups the route keeps do; a level gate to 12; Operation
    Recombobulation (412, MinLevel 7); and the rib of levels 5-8 it ground on instead."""
    base = dict(zone="zone", zone_id=1, coord_zone_id=1, map_id=0, pos=(0.5, 0.5),
                world=(0.0, 0.0, 0.0))
    fail = (FailEdge(when=FailWhen.TIMEOUT, value=10, goto="rib"),)
    return Graph(graph_id="g", faction="alliance", entry="boar", nodes=(
        Node(id="boar", kind=StepKind.QUEST_ACCEPT, quest_id=183, level=(1, 6), next=("boar_do",),
             skills=ACCEPT, on_fail=fail, **base),
        Node(id="boar_do", kind=StepKind.QUEST_OBJECTIVE, quest_id=183, level=(1, 6),
             next=("boar_in",), skills=DO, on_fail=fail, **base),
        Node(id="boar_in", kind=StepKind.QUEST_TURNIN, quest_id=183, level=(1, 6),
             next=("journal",), skills=TURNIN, on_fail=fail, **base),
        Node(id="journal", kind=StepKind.QUEST_ACCEPT, quest_id=218, level=(1, 8),
             quest_prerequisites=((183,),), next=("journal_in",), skills=ACCEPT, on_fail=fail,
             **base),
        Node(id="journal_in", kind=StepKind.QUEST_TURNIN, quest_id=218, level=(1, 8),
             next=("gate",), skills=TURNIN, on_fail=fail, **base),
        Node(id="gate", kind=StepKind.DING_GATE, level=(1, 12), next=("recombobulation",),
             skills=("GRIND_UNTIL",), **base),
        Node(id="recombobulation", kind=StepKind.QUEST_ACCEPT, quest_id=412, level=(7, 10),
             skills=ACCEPT, on_fail=fail, **base),
        Node(id="rib", kind=StepKind.GRIND, level=(5, 8), skills=("TRAVEL_TO", "GRIND_UNTIL"),
             mob_levels=(5, 8), **base),
    ))


def state(t, level, *quests, combat=False):
    return State(t=t, client_id="c", char=Char(level=level),
                 pos=Pos(zone="zone", coord_zone_id=1, mx=0.5, my=0.5), quests=tuple(quests),
                 sense=Sense(addon_ok=True), ui=Ui(modal=False),
                 vitals=Vitals(hp=1, power=1, combat=combat, dead=False, ghost=False))


def runtime(tmp_path, states, **kwargs):
    return ClientRuntime("c", spine(), ScriptedSource(states), Recorder(tmp_path), **kwargs)


def test_the_hive_gives_a_step_the_live_bots_attempts():
    """V341: the hive's one attempt a step made its first unreachable walk to a quest giver the
    quest's failure; the live bot walks three times (`--retries`), and the hive reads it here."""
    assert cli.RETRIES == 3


def test_an_accept_passed_over_comes_back_at_the_next_level_not_before(tmp_path):
    """Gwaiglo passed The Boar Hunter's accept at level 6 and ground at the gate; the next
    level returns to it, and to its objective and hand-in after it, each back to the gate."""
    states = [state(0, 6), state(1, 7), state(2, 7, Quest(quest_id=183, complete=False))]
    rt = runtime(tmp_path, states, start_step="gate", start_retried=frozenset({"boar"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "gate", "passed at this level: the gate is ground"
    assert "passed:boar@6" in rt._retried, "a pass from before counts at the level first read"
    rt.tick(choose=False)                                  # level 7
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("boar", "gate")
    assert "boar" not in rt._retried and "returned:boar@7" in rt._retried
    rt.tick(choose=False)                                  # taken: back to the gate, then on
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("boar_do", "gate")


def test_a_quest_that_unlocks_another_comes_first(tmp_path):
    """The Stolen Journal needs The Boar Hunter handed in: with both accepts passed over, the
    first is returned to first; with the first handed in, the second."""
    passed = frozenset({"boar", "journal", "passed:boar@6", "passed:journal@6"})
    rt = runtime(tmp_path, [state(0, 7)], start_step="gate", start_retried=passed)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "boar"
    done = runtime(tmp_path, [state(0, 7)], start_step="gate", completed={183},
                   start_retried=passed)
    done.tick(choose=False)
    assert done.tracker.step_id == "journal"
    neither = runtime(tmp_path, [state(0, 7)], start_step="gate",
                      start_retried=passed | {f"returned:boar@{n}" for n in range(RETURNS)})
    neither.tick(choose=False)
    assert neither.tracker.step_id == "gate", "its prerequisite given up: nothing to go back to"


def test_a_return_that_fails_goes_straight_back_and_comes_again_at_most_returns_times(tmp_path):
    """A step that cannot be done is not tried for ever: once a level, `RETURNS` levels."""
    states = [state(t, 6 + t) for t in range(RETURNS + 2)]
    g = spine()
    g = g.model_copy(update={"nodes": tuple(              # a band none of these levels outgrows
        n.model_copy(update={"level": (1, 12)}) if n.id == "boar" else n for n in g.nodes)})
    rt = ClientRuntime("c", g, ScriptedSource(states), Recorder(tmp_path), start_step="gate",
                       start_retried=frozenset({"boar"}))
    rt.tick(choose=False)                                  # level 6: passed at it
    seen = []
    for t in range(1, RETURNS + 2):
        rt.tick(choose=False)
        seen.append(rt.tracker.step_id)
        if rt.tracker.step_id == "boar":
            rt._apply(TrackVerdict(Event.FAIL, goto="rib", reason="timeout_s=10"), states[t])
            assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("gate", None), \
                "straight back, no rib"
            assert "boar" in rt._retried, "passed again, for the level"
    assert seen == ["boar"] * RETURNS + ["gate"]


def test_a_grey_quest_is_not_gone_back_to(tmp_path):
    """The Boar Hunter's band ends at 6 (its level 3 and three): at 8 it pays in full, at 9 it
    pays 80% (TBC), and it is not walked back to."""
    rt = runtime(tmp_path, [state(0, 9)], start_step="gate",
                 start_retried=frozenset({"boar", "passed:boar@8"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "gate"
    full = runtime(tmp_path, [state(0, 8)], start_step="gate",
                   start_retried=frozenset({"boar", "passed:boar@7"}))
    full.tick(choose=False)
    assert full.tracker.step_id == "boar"


def test_a_rib_returns_to_a_hand_in_complete_in_the_log(tmp_path):
    """Gwaiglo ground the rib while its way back, Operation Recombobulation, was at its level
    and The Boar Hunter sat complete in the log, its hand-in passed over (hive-389, 18:04-18:15
    on 4 Oct): only a wait for an accept above the level looked back for it (V317)."""
    rt = runtime(tmp_path, [state(0, 7, Quest(quest_id=183, complete=True))],
                 start_step="rib", start_rejoin="recombobulation",
                 start_retried=frozenset({"boar_in", "boar", "boar_do"}))
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("boar_in", "recombobulation")


def test_a_hand_in_whose_objective_was_passed_over_returns_to_it_at_the_next_level(tmp_path):
    """At its own level the hand-in is passed by, as before (session 116's Goldtooth); a level
    on, the objective comes first and the hand-in after it."""
    quest = Quest(quest_id=183, complete=False)
    rt = runtime(tmp_path, [state(0, 6, quest)], start_step="boar_in",
                 start_retried=frozenset({"boar_do", "passed:boar_do@6"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "journal"
    later = runtime(tmp_path, [state(0, 7, quest)], start_step="boar_in",
                    start_retried=frozenset({"boar_do", "passed:boar_do@6"}))
    later.tick(choose=False)
    assert (later.tracker.step_id, later.tracker.memory.rejoin_to) == ("boar_do", "boar_in")


def test_a_return_that_must_wait_or_is_refused_goes_back_where_it_was_going(tmp_path):
    """A step returned to is behind the spine: waiting for it on a rib, or passing its quest
    by from it, would leave the character there, the spine walked again from behind."""
    rt = runtime(tmp_path, [state(0, 7), state(1, 7)], start_step="gate",
                 start_retried=frozenset({"boar", "passed:boar@6"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "boar"
    rt.policy_context.step_waits("boar", 10_000.0, "its walk is refused through a death camp",
                                 1.0)
    rt.armed = None
    rt.tick(choose=False)
    assert (rt.tracker.step_id, rt.tracker.memory.rejoin_to) == ("gate", None)
    assert "boar" in rt._retried

    refused = runtime(tmp_path, [state(0, 7)], start_step="gate",
                      start_retried=frozenset({"boar", "passed:boar@6"}))
    refused.tick(choose=False)
    assert refused.not_offered("boar") is True
    assert refused.tracker.step_id == "gate", "not the step past its quest, behind"


def test_combat_on_the_way_to_a_return_is_no_attempt_and_no_return_starts_in_combat(tmp_path):
    """A fight is the step's to finish, then the same step again (`ClientRuntime.finish`)."""
    from jev.coach.schema import Decision, Intent
    from jev.learn.episode import SkillOutcome
    from jev.orch.runtime import Armed
    from jev.world.state_v1 import ArmedBy

    rt = runtime(tmp_path, [state(0, 7, combat=True), state(1, 7)], start_step="gate",
                 start_retried=frozenset({"boar", "passed:boar@6"}))
    rt.tick(choose=False)
    assert rt.tracker.step_id == "gate", "not in a fight"
    rt.tick(choose=False)
    assert rt.tracker.step_id == "boar"
    walk = Decision(goal="travel", intent=Intent.ADVANCE, skill="TRAVEL_TO", abort_if=["dead"],
                    confidence=0.8, why="not yet at boar")
    rt.armed = Armed(walk, ArmedBy.POLICY, 1.0, "guide.travel", step_id="boar")
    rt.finish(SkillOutcome.PREEMPTED, "combat interrupted the leg or service")
    assert rt.tracker.memory.attempts == 0 and rt.tracker.step_id == "boar"
