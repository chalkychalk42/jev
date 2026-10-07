"""V391: a rib's bar ends with its cause, and a walk refused through a death camp bars nothing.

Real supervisor and worker threads with a fake body, as `test_supervisor`; the route is the
hive's dwarf priest's at level 15, its ribs' creatures' levels and places as the world database
gives them (`hive_dwarf_priest_1_20`, 7 Oct)."""
import threading

from test_runtime_records import seen

from jev.clients.source import ScriptedSource
from jev.guide import playhead
from jev.guide.graph import Graph, Node, rib_xp
from jev.learn.episode import Recorder, SkillOutcome
from jev.orch.runtime import (
    RIB_BAR,
    RIB_BAR_S,
    RIB_WAIT_SHARE,
    ClientRuntime,
    bar_entry,
    bar_of,
)
from jev.run.supervisor import Result, Supervisor
from jev.world.state_v1 import Char, Pos, StepKind

T0 = 1791332641.0                    # 7 Oct 01:24:01, hive-591's first refusal
CAMP_END = T0 + 2734.0               # 02:09:35, the camp its walks were refused through
# Hive-591's ribs at level 15: creatures' levels and world places (x, y) on Eastern Kingdoms.
RIBS = {
    "loch_modan_15_17": ((15, 16), (-5835.0, -4107.0)),
    "loch_modan_13_15_mangy_mountain_b": ((14, 15), (-5615.0, -3682.0)),
    "loch_modan_13_15_loch_crocolisk": ((14, 15), (-5186.0, -3509.0)),
    "dun_morogh_13_15": ((13, 14), (-6047.0, -2777.0)),
    "loch_modan_13_15": ((13, 14), (-5835.0, -3825.0)),
    "dun_morogh_13_15_stonesplinter_se": ((13, 14), (-6039.0, -2785.0)),
    "loch_modan_11_13_loch_frenzy": ((12, 13), (-4842.0, -3226.0)),
    "dun_morogh_11_13_stonesplinter_tr": ((11, 12), (-5858.0, -2884.0)),
    "loch_modan_11_13": ((11, 12), (-5037.0, -2929.0)),
    "dun_morogh_9_11": ((10, 11), (-5128.0, -2996.0)),
}
THELSAMAR = (-5390.0, -2953.0)       # where its walks were refused from, by Myra Tyrngaarde


def _pos(world):
    """The guide's map fractions for a world place: one linear frame (`frame_yards`)."""
    return (0.5 + (world[1] + 3500.0) / 6000.0, 0.5 + (world[0] + 5500.0) / 6000.0)


def priest_graph() -> Graph:
    base = dict(zone="Loch Modan", zone_id=38, map_id=0)
    ribs = tuple(Node(id=name, kind=StepKind.GRIND, level=levels, mob_levels=levels,
                      pos=_pos(world), world=(*world, 0.0), skills=("GRIND_UNTIL",),
                      **base)
                 for name, (levels, world) in RIBS.items())
    return Graph(graph_id="g", faction="alliance", entry="gated", nodes=(
        Node(id="gated", kind=StepKind.QUEST_ACCEPT, quest_id=1, level=(16, 20),
             pos=_pos(THELSAMAR), world=(*THELSAMAR, 0.0), skills=("TRAVEL_TO", "ACCEPT_QUEST"),
             **base),
        *ribs))


def at15(t, **kw):
    here = _pos(THELSAMAR)
    return seen(t, char=Char(level=15, cls="priest", key=591),
                pos=Pos(zone="Loch Modan", mx=here[0], my=here[1]), **kw)


def _runtime(tmp_path, states, **kw):
    kw.setdefault("start_step", "loch_modan_15_17")
    return ClientRuntime("c", priest_graph(), ScriptedSource(states), Recorder(tmp_path),
                         start_rejoin="gated", start_entry_level=15, character_key=591,
                         spread_ribs=True, **kw)


class CampBody:
    """Every grind's walks are refused through the camp until it ends, as `LiveBody._grind`
    reports it (`Hunted.CAMP`: the step waits, then `camp`); after, the hunt goes on."""

    available = frozenset({"TRAVEL_TO", "GRIND_UNTIL", "ACCEPT_QUEST", "ABORT_WAIT", "IDLE"})
    travelling = False

    def __init__(self, context):
        self.context, self.grinds, self.after = context, [], []
        self.hold = threading.Event()

    def execute(self, arm, state, checkpoint):
        if arm.decision.skill != "GRIND_UNTIL":
            return Result(SkillOutcome.SUCCEEDED, "done", "done")
        if state.t < CAMP_END:
            self.grinds.append(arm.step_id)
            self.context.step_waits(arm.step_id, CAMP_END, "its walk is refused through a "
                                    "death camp", state.t)
            return Result(SkillOutcome.ABORTED, "every walk to a station is refused through a "
                          "death camp, held by deaths at its level until 02:09:35", "camp")
        self.after.append(arm.step_id)
        while not self.hold.wait(0.001):
            checkpoint()
        return Result(SkillOutcome.PREEMPTED, "run ended", "preempted")

    def release(self):
        return


def test_hive_591s_refused_walks_bar_nothing_and_stop_above_seven_tenths_of_the_best(tmp_path):
    """Hive-591, a level 15 dwarf priest, had six walks refused through one death camp in six
    seconds (7 Oct 01:24:01-07), each sending it to the next rib down: Loch Modan's 15-17 (123
    experience a kill), Dun Morogh's 13-15 and its Stonesplinters, Loch Modan's 13-15 (97.5),
    its Loch Frenzies (82.5), Dun Morogh's Stonesplinter Troggs (67.5), and Loch Modan's 11-13,
    where it stayed. Now no rib is barred for a refusal; it waits out the camp on ribs paying 70%
    of the best or more, then stands clear of the camp; when the camp ends (02:09:35) its rib
    is chosen again among the best."""
    states = [at15(T0 + i / 2) for i in range(80)] + [at15(CAMP_END + 60 + i / 2)
                                                     for i in range(20)]
    rt = _runtime(tmp_path, states)
    # Its Loch Modan 14-15 ribs waited on the same camp already.
    for rib in ("loch_modan_13_15_mangy_mountain_b", "loch_modan_13_15_loch_crocolisk"):
        rt.policy_context.step_waits(rib, CAMP_END, "camp", T0)
    body = CampBody(rt.policy_context)
    supervisor = Supervisor(rt, body, say=lambda line: None, max_failures=3)
    try:
        for i in range(80):
            supervisor.step(i / 2)
            if supervisor.worker is not None and supervisor.worker.arm is not None:
                supervisor.worker.done.wait(1)
        best = rib_xp(priest_graph().get("loch_modan_15_17"), 15)
        pays = {r: rib_xp(priest_graph().get(r), 15) for r in body.grinds}
        assert body.grinds[0] == "loch_modan_15_17" and len(body.grinds) == 4, body.grinds
        assert len(set(body.grinds)) == len(body.grinds), "each refused rib tried once"
        assert all(p >= RIB_WAIT_SHARE * best for p in pays.values()), pays
        assert not any(r.startswith(RIB_BAR) for r in rt._retried), "a refusal bars nothing"
        assert not supervisor.stopped.is_set(), supervisor.failure
        assert rt.armed is not None and rt.armed.rule == "wait.step", "stood clear of the camp"
        assert rt.tracker.memory.rejoin_to == "gated", "its way back kept"
        for i in range(80, 100):                                  # the camp has ended
            supervisor.step(i / 2)
            if body.after:
                break
        assert body.after, "the grind goes on once the camp ends"
        assert rib_xp(priest_graph().get(body.after[0]), 15) >= 0.85 * best, body.after
    finally:
        body.hold.set()
        supervisor.close()


def test_a_rib_with_none_free_at_seven_tenths_is_stood_not_traded_for_a_grey_one(tmp_path):
    """V391: with every rib paying 70% of the best waiting, a refused rib waits where it is; one
    of 67.5 a kill, where hive-591 went, is not taken."""
    rt = _runtime(tmp_path, [at15(T0 + i) for i in range(3)])
    for rib in RIBS:
        if rib_xp(priest_graph().get(rib), 15) >= RIB_WAIT_SHARE * 123.0:
            rt.policy_context.step_waits(rib, CAMP_END, "camp", T0)
    rt.tick(choose=False)
    assert rt.tracker.step_id == "loch_modan_15_17"
    assert rt.fail_over("GRIND_UNTIL", "every walk to a station is refused", camp=True) is False
    assert rt.tracker.step_id == "loch_modan_15_17" and not rt._barred(15)


def test_an_empty_disk_bars_its_rib_half_an_hour_and_the_rib_comes_back(tmp_path):
    """V391: "walked the whole disk and found nothing to fight" barred the rib for the level, 5
    to 7 played hours at 11-20; now for `RIB_BAR_S`, and when the bar ends the rib is chosen
    again as at a ding, its way back and level carried."""
    states = ([at15(T0)] + [at15(T0 + 1 + i) for i in range(3)]
              + [at15(T0 + RIB_BAR_S + 5 + i) for i in range(3)])
    rt = _runtime(tmp_path, states)
    rt.tick(choose=False)
    assert rt.fail_over("GRIND_UNTIL", "walked the whole disk and found nothing to fight")
    assert rt._barred(15) == {"loch_modan_15_17"}
    (entry,) = [r for r in rt._retried if r.startswith(RIB_BAR)]
    assert bar_of(entry)[1:] == (15, float(round(T0 + RIB_BAR_S)))
    for _ in range(3):
        rt.tick(choose=False)
    first = rt.tracker.step_id
    assert first != "loch_modan_15_17" and rt.tracker.memory.rejoin_to == "gated"
    for _ in range(3):
        rt.tick(choose=False)
    assert not rt._barred(15) and not any(r.startswith(RIB_BAR) for r in rt._retried)
    assert rib_xp(priest_graph().get(rt.tracker.step_id), 15) >= 0.85 * 123.0
    assert (rt.tracker.memory.rejoin_to, rt.tracker.memory.level_at_entry) == ("gated", 15)


def test_a_death_camp_bars_its_rib_until_the_camp_ends(tmp_path):
    """V391: the rib a death camp lies on is barred until the camp's own end
    (`Danger.camp_until`, as `LiveBody._keep_death` says it), not for the level; then the
    rib is free again and chosen again."""
    from jev.world.state_v1 import Vitals

    up = Vitals(hp=1, power=1, combat=False, dead=False, ghost=False)
    states = ([at15(T0, vitals=up)] + [at15(T0 + 1 + i, vitals=up) for i in range(3)]
              + [at15(CAMP_END + 1 + i, vitals=up) for i in range(3)])
    rt = _runtime(tmp_path, states)
    rt.tick(choose=False)
    world = RIBS["loch_modan_15_17"][1]
    rt.policy_context.camp_left(0, world[0], world[1], until=CAMP_END)
    rt.tick(choose=False)
    assert rt.tracker.step_id != "loch_modan_15_17", "left once up"
    (entry,) = [r for r in rt._retried if r.startswith(RIB_BAR)]
    assert bar_of(entry) == ("loch_modan_15_17", 15, CAMP_END)
    rt.tracker.memory.arrived = True                      # the leave is made
    for _ in range(5):
        rt.tick(choose=False)
    assert not rt._barred(15), "the camp's end is the bar's"


def test_a_bars_end_survives_a_session_restart_and_an_old_bar_ends_at_once(tmp_path):
    """V391: bars are kept in the character's playhead (`retried`): a bar saved with its end
    holds it in the next session; one saved before ends had none, and ends now."""
    path = tmp_path / "character-0000024f.json"
    ends = T0 + RIB_BAR_S
    playhead.save("g", "loch_modan_13_15", frozenset(), path, rejoin_to="gated",
                  retried=frozenset({bar_entry("loch_modan_15_17", 15, ends),
                                     "rib-bar:loch_modan_13_15_mangy_mountain_b@15",
                                     "detour:gated@15"}), entry_level=15)
    memory = playhead.load("g", path)
    rt = _runtime(tmp_path, [at15(T0 + 60 + i) for i in range(2)],
                  start_step=memory.step_id, start_retried=memory.retried)
    rt.tick(choose=False)
    assert rt._barred(15) == {"loch_modan_15_17"}, "kept with its end"
    assert bar_entry("loch_modan_15_17", 15, ends) in rt._retried
    assert not any("mangy" in r for r in rt._retried), "no end: ended now"
    assert "detour:gated@15" in rt._retried, "other entries untouched"
    rt.tick(choose=False)
    assert rt.tracker.step_id != "loch_modan_15_17"


def test_a_bar_is_lifted_when_its_end_is_further_off_than_any_bar_holds(tmp_path):
    """A wall clock set back (the WSL clock, 28 Sep) holds no rib off for good."""
    rt = _runtime(tmp_path, [at15(T0)],
                  start_retried=frozenset({bar_entry("loch_modan_15_17", 15, T0 + 86_400)}))
    rt.tick(choose=False)
    assert not rt._barred(15) and not any(r.startswith(RIB_BAR) for r in rt._retried)
