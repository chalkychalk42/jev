"""An accept or a hand-in its body did is done once the quest log stays unread (V455, extracted
from bet-quests-2's V423).

The cases are the hive's parity shard (shard 7, the quest log read as the live strip paints it,
one slot a paint) on 7 Oct, each of which read the log until its body's first success and never
again while the playhead stayed on the step: hive-686 handing The Wayward Apprentice in at 18:47
(run 20261007T184547-fc8852, the hand-in "made" 585 times in 300 s unread), hive-707 taking
Report to Orgnil at 21:29 (20261007T212929-6de3a7, the accept "made" 418 times in 240 s) and
handing it in at 21:39 (20261007T213904-7a0d0e, failed over about 5.5 s after, never counted)."""

from jev.clients.source import ScriptedSource
from jev.coach.schema import Decision, Intent
from jev.guide.graph import FailEdge, FailWhen, Graph, Node
from jev.learn.episode import Recorder, SkillOutcome
from jev.orch.runtime import CONFIRMED_WAIT_S, Armed, ClientRuntime
from jev.world.state_v1 import ArmedBy, Char, Pos, Quest, Sense, State, StepKind, Ui, Vitals

BASE = dict(zone="zone", zone_id=1, coord_zone_id=1, map_id=0, pos=(0.5, 0.5),
            world=(0.0, 0.0, 0.0))
ACCEPT = ("TRAVEL_TO", "ACCEPT_QUEST")
TURNIN = ("TRAVEL_TO", "TURNIN_QUEST")
# Hive-686's log as it was read until The Wayward Apprentice (9254, complete) was handed in.
ALANA = (8491, 8885, 9254, 8346, 8477, 8888)
# Hive-707's before it took Report to Orgnil (823), and before it handed it in.
ORC = (817, 1517, 2161, 818, 808)
ORC_HELD = (817, 1517, 818, 808, 823)


def fails(rib, missing):
    """A quest step's edges as the hive's routes write them: stalled 240 s, three deaths, and
    the quest missing (a hand-in) or not offered (an accept), each to the rib of its levels."""
    return (FailEdge(when=FailWhen.TIMEOUT, value=240.0, goto=rib),
            FailEdge(when=FailWhen.DEATHS, value=3.0, goto=rib),
            FailEdge(when=missing, goto=rib))


def eversong() -> Graph:
    """Hive-686's route (`bloodelf_paladin.json`): The Wayward Apprentice's hand-in, then
    Corrupted Soil's accept (8487)."""
    rib = "grind_eversong_5_7"
    return Graph(graph_id="g", faction="horde", entry="wayward_in", nodes=(
        Node(id="wayward_in", kind=StepKind.QUEST_TURNIN, quest_id=9254, level=(7, 12),
             next=("soil",), skills=TURNIN, on_fail=fails(rib, FailWhen.QUEST_MISSING), **BASE),
        Node(id="soil", kind=StepKind.QUEST_ACCEPT, quest_id=8487, level=(7, 12),
             skills=ACCEPT, on_fail=fails(rib, FailWhen.QUEST_NOT_OFFERED), **BASE),
        Node(id=rib, kind=StepKind.GRIND, level=(5, 7), skills=("TRAVEL_TO", "GRIND_UNTIL"),
             mob_levels=(5, 7), **BASE),
    ))


def durotar() -> Graph:
    """Hive-707's route (`orc_shaman.json`): Report to Orgnil's accept, then Zalazane's (826);
    its hand-in, then Dark Storms' accept (806), which needs Report to Orgnil handed in."""
    rib = "grind_durotar_3_5"
    return Graph(graph_id="g", faction="horde", entry="orgnil", nodes=(
        Node(id="orgnil", kind=StepKind.QUEST_ACCEPT, quest_id=823, level=(4, 10),
             next=("zalazane",), skills=ACCEPT, on_fail=fails(rib, FailWhen.QUEST_NOT_OFFERED),
             **BASE),
        Node(id="zalazane", kind=StepKind.QUEST_ACCEPT, quest_id=826, level=(4, 10),
             next=("orgnil_in",), skills=ACCEPT,
             on_fail=fails(rib, FailWhen.QUEST_NOT_OFFERED), **BASE),
        Node(id="orgnil_in", kind=StepKind.QUEST_TURNIN, quest_id=823, level=(4, 10),
             next=("storms",), skills=TURNIN, on_fail=fails(rib, FailWhen.QUEST_MISSING),
             **BASE),
        Node(id="storms", kind=StepKind.QUEST_ACCEPT, quest_id=806, level=(4, 15),
             quest_prerequisites=((823,),), skills=ACCEPT,
             on_fail=fails(rib, FailWhen.QUEST_NOT_OFFERED), **BASE),
        Node(id=rib, kind=StepKind.GRIND, level=(3, 5), skills=("TRAVEL_TO", "GRIND_UNTIL"),
             mob_levels=(3, 5), **BASE),
    ))


def at(t, quests, *, complete=(), level=8):
    """A tick standing at the giver: `quests` the log read whole, or `None` unread (the strip's
    cycle not assembled since the log's count changed)."""
    log = None if quests is None else tuple(
        Quest(quest_id=q, complete=q in complete) for q in quests)
    return State(t=t, client_id="c", char=Char(level=level),
                 pos=Pos(zone="zone", coord_zone_id=1, mx=0.5, my=0.5), quests=log,
                 sense=Sense(addon_ok=True), ui=Ui(modal=False),
                 vitals=Vitals(hp=1, power=1, combat=False, dead=False, ghost=False))


def body(rt, skill, outcome=SkillOutcome.SUCCEEDED, detail=""):
    """The step's own arm ends as the body (the hive's server) answered it."""
    rt.armed = Armed(Decision(goal="the step", intent=Intent.ADVANCE, skill=skill,
                              abort_if=["quest_changed"], confidence=1.0, why="the step"),
                     ArmedBy.POLICY, rt.last_state.t, "guide.step", step_id=rt.tracker.step_id)
    rt.finish(outcome, detail)


def runtime(tmp_path, graph, states, start):
    return ClientRuntime("c", graph, ScriptedSource(states), Recorder(tmp_path),
                         start_step=start)


def test_hive_686s_hand_in_is_done_ten_seconds_into_the_unread_log(tmp_path):
    """Alana handed The Wayward Apprentice in at 91.6 s and the log was unread from 91.8 s to
    the run's end; the hand-in was armed and "made" again every half second (the hive's server
    answered "done", before JevHive ec3a9d1). A success again is the same word: the wait runs
    from the first unread tick, and at its end 9254 is handed in and the playhead on."""
    ticks = [at(91.1, ALANA, complete={9254}), at(91.6, ALANA, complete={9254})]
    unread = [91.8 + 0.5 * i for i in range(int(CONFIRMED_WAIT_S / 0.5) + 2)]
    rt = runtime(tmp_path, eversong(), ticks + [at(t, None) for t in unread], "wayward_in")
    rt.tick(choose=False)
    rt.tick(choose=False)
    body(rt, "TURNIN_QUEST")                                      # 91.6 s
    moved_at = None
    for t in unread:
        rt.tick(choose=False)
        if rt.tracker.step_id != "wayward_in":
            moved_at = t
            break
        assert 9254 not in rt.completed
        body(rt, "TURNIN_QUEST")                                  # "made" again
    assert rt.tracker.step_id == "soil" and 9254 in rt.completed
    assert moved_at is not None and 0 <= moved_at - 91.8 - CONFIRMED_WAIT_S < 0.5
    assert rt._tracker_event == "advance" and rt.counters.advances == 1


def test_hive_707s_accept_is_done_ten_seconds_into_the_unread_log(tmp_path):
    """Report to Orgnil taken at 1.5 s, the log unread from 1.8 s for the 240 s left, the accept
    "made" 418 times: on to Zalazane at the wait's end. Its hand-in, when the log is read again
    with the quest in it, is judged by its predicate as ever."""
    unread = [1.8 + 0.5 * i for i in range(int(CONFIRMED_WAIT_S / 0.5) + 1)]   # to 11.8 s
    ticks = [at(1.0, ORC, level=6), *(at(t, None, level=6) for t in unread),
             at(30.0, ORC_HELD, level=6), at(31.0, ORC, level=6)]
    rt = runtime(tmp_path, durotar(), ticks, "orgnil")
    rt.tick(choose=False)
    body(rt, "ACCEPT_QUEST")
    for t in unread:
        rt.tick(choose=False)
        if t < unread[-1]:
            assert rt.tracker.step_id == "orgnil", t
            body(rt, "ACCEPT_QUEST")
    assert rt.tracker.step_id == "zalazane"
    rt.tracker.enter("orgnil_in", rt.last_state)          # Zalazane taken, on to Orgnil
    rt.tick(choose=False)                                  # read: 823 held
    assert rt.tracker.memory.quest_was_in_log
    rt.tick(choose=False)                                  # read: 823 gone
    assert rt.tracker.step_id == "storms" and 823 in rt.completed


def test_hive_707s_hand_in_failed_over_after_its_body_did_it_is_done(tmp_path):
    """Since JevHive ec3a9d1 the hive's body answers a hand-in made again "quest 823 is not in
    the log": three of those failed Report to Orgnil's hand-in over about 5.5 s after it was made,
    not counted handed in; Dark Storms, which needs it, was then accepted 427 times unread, and
    Report to Orgnil's accept walked back to at 7, 8 and 9. With its body's success standing and the
    log unread since, the step is done instead."""
    def run(path, succeeded):
        ticks = [at(24.5, ORC_HELD, complete={823}, level=6),
                 *(at(t, None, level=6) for t in (25.5, 26.1, 27.1, 28.1))]
        rt = runtime(path, durotar(), ticks, "orgnil_in")
        rt.tick(choose=False)
        if succeeded:
            body(rt, "TURNIN_QUEST")                       # 25.0 s
        for _ in range(3):
            rt.tick(choose=False)
            body(rt, "TURNIN_QUEST", SkillOutcome.ABORTED, "quest 823 is not in the log")
        rt.tick(choose=False)
        return rt, rt.fail_over("TURNIN_QUEST", "quest 823 is not in the log")
    rt, moved = run(tmp_path / "fixed", True)
    assert moved and rt.tracker.step_id == "storms" and 823 in rt.completed
    assert "orgnil_in" not in rt._retried
    old, moved = run(tmp_path / "old", False)
    assert moved and 823 not in old.completed, "no success said: the fail edge, as before"


def test_where_the_log_is_read_every_tick_nothing_changes(tmp_path):
    """The hive's other shards read the whole log every tick: a body's success the log does not
    bear out (the quest still in it) moves nothing however long, and its fail edge is taken as
    if no success had been said."""
    ticks = [at(91.0 + 0.5 * i, ALANA, complete={9254}) for i in range(80)]
    def run(path, succeeded):
        rt = runtime(path, eversong(), ticks, "wayward_in")
        rt.tick(choose=False)
        for _ in range(60):
            if succeeded:
                body(rt, "TURNIN_QUEST")
            rt.tick(choose=False)
        assert rt._confirmed is None
        moved = rt.fail_over("TURNIN_QUEST", "out of attempts")
        return rt, moved
    said, moved_said = run(tmp_path / "said", True)
    silent, moved_silent = run(tmp_path / "silent", False)
    assert (said.tracker.step_id, said.completed, said._retried, moved_said) == (
        silent.tracker.step_id, silent.completed, silent._retried, moved_silent)
    assert moved_said and 9254 not in said.completed and "wayward_in" in said._retried


def test_a_read_log_that_holds_the_quest_takes_the_bodys_word_back(tmp_path):
    """The log read after the success with the quest still in it judges the hand-in not made:
    unread again for longer than the wait, nothing moves until the body says so again."""
    ticks = [at(0.0, ALANA, complete={9254}), at(1.0, None), at(3.0, None),
             at(4.0, ALANA, complete={9254}),
             *(at(5.0 + t, None) for t in range(int(CONFIRMED_WAIT_S) + 5))]
    rt = runtime(tmp_path, eversong(), ticks, "wayward_in")
    rt.tick(choose=False)
    body(rt, "TURNIN_QUEST")
    for _ in range(len(ticks) - 1):
        rt.tick(choose=False)
    assert rt.tracker.step_id == "wayward_in" and 9254 not in rt.completed


def test_a_hand_in_never_seen_held_is_not_taken_on_the_bodys_word(tmp_path):
    """Entered with the log unread and never read since, the hand-in's quest was never seen in
    it: the body's success alone does not count it handed in."""
    ticks = [at(t, None) for t in range(int(CONFIRMED_WAIT_S) + 5)]
    rt = runtime(tmp_path, eversong(), ticks, "soil")
    rt.tick(choose=False)
    rt.tracker.enter("wayward_in", rt.last_state)                # reached in play, log unread
    rt.tick(choose=False)
    body(rt, "TURNIN_QUEST")
    for _ in range(len(ticks) - 2):
        rt.tick(choose=False)
    assert rt.tracker.step_id == "wayward_in" and 9254 not in rt.completed


def test_only_the_steps_own_skill_succeeding_is_its_bodys_word(tmp_path):
    """A walk or a meal that succeeded at the giver is not the accept."""
    ticks = [at(1.0, ORC, level=6), *(at(2.0 + t, None, level=6)
                                      for t in range(int(CONFIRMED_WAIT_S) + 5))]
    rt = runtime(tmp_path, durotar(), ticks, "orgnil")
    rt.tick(choose=False)
    body(rt, "TRAVEL_TO")
    body(rt, "TURNIN_QUEST")
    for _ in range(len(ticks) - 1):
        rt.tick(choose=False)
    assert rt.tracker.step_id == "orgnil"
