"""V392: the rib for a level is chosen by experience an hour net of death time, from the hive's
yield table where there is one, and exactly as before where there is none.

The table here is the hive's own measure of 7 Oct 01:25-03:50 at 11-20 (strategy-4-place,
from `hive.timebudget`'s band tables): kills, a kill's cycle in seconds and its experience by
level difference, deaths a kill, and 149 s a death."""
import json

from test_rib_bars import RIBS, _pos, _runtime, at15, priest_graph

from jev.guide.graph import rib_for, rib_levels, rib_pays, rib_xp
from jev.learn.yields import Yields
from jev.orch.runtime import RIB_WAIT_SHARE

DEATHS_A_KILL = {-6: .07, -5: .05, -4: .07, -3: .09, -2: .16, -1: .21, 0: .39, 1: .60, 2: 1.04}
# Level difference: (kills, cycle s, experience a kill).
BAND_11_15 = {-6: (838, 58.3, 0), -5: (1086, 76.2, 39), -4: (1348, 80.9, 50),
              -3: (1115, 85.6, 63), -2: (917, 87.8, 76), -1: (788, 100.6, 87),
              0: (506, 119.5, 100), 1: (256, 119.8, 105), 2: (83, 123.8, 112)}
BAND_16_20 = {-6: (800, 72.2, 0), -5: (767, 82.5, 53), -4: (864, 78.7, 66),
              -3: (905, 89.6, 78), -2: (843, 94.7, 94), -1: (634, 102.3, 112),
              0: (377, 103.3, 128), 1: (147, 115.1, 132), 2: (19, 123.8, 132)}


def measured(path, *, drop=()):
    def cells(band):
        return {str(d): {"kills": n, "kills_h": round(3600 / cycle, 3), "xp_kill": xp,
                         "deaths_kill": DEATHS_A_KILL[d], "deaths": round(n * DEATHS_A_KILL[d])}
                for d, (n, cycle, xp) in band.items() if d not in drop and n >= 40}
    table = {"format": 1, "written": "2026-10-07T04:00",
             "classes": {"all": {"death_s": 149.0, "deaths": 2000,
                                 "bands": {"11-15": cells(BAND_11_15),
                                           "16-20": cells(BAND_16_20)}}}}
    path.write_text(json.dumps(table))
    return Yields.load(path)


def test_net_of_death_time_a_kill_pays_most_two_levels_below_at_11_15_and_one_at_16_20(tmp_path):
    """The investigator's finding, through the table: net of 149 s a death, a kill's cycle paid
    most at -2 (11-15) and -1 (16-20), not at +0..+1 where a kill's experience is highest."""
    yields = measured(tmp_path / "rib-yield.json")
    at13 = {d: yields.rate("priest", 13, 13 + d) for d in range(-5, 2)}
    at18 = {d: yields.rate("priest", 18, 18 + d) for d in range(-5, 2)}
    assert max(at13, key=at13.get) == -2 and max(at18, key=at18.get) == -1, (at13, at18)
    assert at13[1] < at13[-4], "a level above pays less an hour than four below"
    assert yields.rate("priest", 13, 5) == 0.0, "grey: nothing, whatever the time"
    assert yields.rate("priest", 25, 25) is None, "no band: the table says nothing"


def test_the_rib_is_chosen_by_net_yield_with_the_table_and_by_a_kill_without(tmp_path):
    """At Loch Modan's 15-17 rib, a level 15 priest: by a kill's experience it stays (123 a
    kill, the nearest of the ribs within 90% of the best); by experience an hour net of death
    time its creatures of 15-16 pay 80% of the 13-14 ribs', and it takes the nearest of those."""
    graph = priest_graph()
    ribs = [n for n in graph.nodes if n.id in RIBS]
    here = _pos(RIBS["loch_modan_15_17"][1])
    assert rib_for(ribs, 15, near=here).id == "loch_modan_15_17"
    yields = measured(tmp_path / "rib-yield.json")

    def rate(rib, level):
        return yields.rib_rate(*rib_levels(rib), level, "priest")
    pays = rib_pays(ribs, 15, rate)
    assert pays["loch_modan_15_17"] < 0.85 * max(pays.values())
    chosen = rib_for(ribs, 15, near=here, rate=rate)
    assert rib_levels(chosen) == (13, 14), chosen.id
    assert chosen.id == "loch_modan_13_15", "the nearest of the best: 282 yards"


def test_without_a_table_or_with_a_rib_it_cannot_value_the_choice_is_as_before(tmp_path):
    """A missing or unreadable file, another format, or a creature level the table has no cell
    for: every rib is valued by a kill's experience, as today - the live client has none."""
    graph = priest_graph()
    ribs = [n for n in graph.nodes if n.id in RIBS]
    assert Yields.load(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert Yields.load(tmp_path / "bad.json") is None
    (tmp_path / "other.json").write_text(json.dumps({"format": 2, "classes": {"all": {}}}))
    assert Yields.load(tmp_path / "other.json") is None
    assert rib_pays(ribs, 15) == {r.id: rib_xp(r, 15) for r in ribs}
    holed = measured(tmp_path / "holed.json", drop=(1,))       # no cell a level above at 11-15

    def rate(rib, level):
        return holed.rib_rate(*rib_levels(rib), level, "priest")
    assert rib_pays(ribs, 15, rate) == {r.id: rib_xp(r, 15) for r in ribs}
    rt = _runtime(tmp_path, [at15(0)])
    assert rt._rate() is None, "no table, no rate"


def test_the_runtime_reads_the_table_for_its_class_and_floors_waits_by_it(tmp_path):
    """The runtime's every rib choice values ribs by the table for the character's class
    (`ClientRuntime.yields`, `_rate`), and so does the least a rib's wait takes (V391's 70% of
    the best): Dun Morogh's 10-11 creatures pay a level 15 character 43% of the best a kill but
    75% an hour net of death time, so with every better rib waiting it is taken with the table
    and the wait is stood without."""
    for yields, taken in ((measured(tmp_path / "rib-yield.json"), "dun_morogh_9_11"),
                          (None, "loch_modan_15_17")):
        rt = _runtime(tmp_path, [at15(1000.0 + i) for i in range(3)], yields=yields)
        rt.tick(choose=False)
        assert rt._cls == "priest"
        pays = rib_pays(list(rt._ribs_all), 15, rt._rate())
        share = pays["dun_morogh_9_11"] / max(pays.values())
        assert (share >= RIB_WAIT_SHARE) is (yields is not None), share
        for rib in RIBS:
            if rib != "dun_morogh_9_11":
                rt.policy_context.step_waits(rib, 5000.0, "camp", 1000.0)
        rt.tick(choose=False)
        assert rt.tracker.step_id == taken


def test_a_class_is_read_from_its_own_band_only_where_it_covers_what_pays(tmp_path):
    """A class's cells stand only where they cover every difference that pays the character;
    else its rates are all the pooled band's. The hive's hunters at 11-15 had cells only to two
    levels below, dying 310 times a hundred kills there (6-7 Oct): mixed with the pooled cells
    above, two below looked a quarter as good as one below."""
    table = json.loads(json.dumps({"format": 1, "classes": {}}))
    pooled = measured(tmp_path / "pooled.json")
    table["classes"]["all"] = pooled.classes["all"]
    hunter = {str(d): dict(cell, deaths_kill=3.1)
              for d, cell in pooled.classes["all"]["bands"]["11-15"].items() if int(d) <= -2}
    table["classes"]["hunter"] = {"death_s": 89.0, "deaths": 500, "bands": {"11-15": hunter}}
    whole = {str(d): dict(cell) for d, cell in pooled.classes["all"]["bands"]["11-15"].items()}
    table["classes"]["priest"] = {"death_s": 300.0, "deaths": 500, "bands": {"11-15": whole}}
    path = tmp_path / "rib-yield.json"
    path.write_text(json.dumps(table))
    yields = Yields.load(path)
    for d in range(-5, 2):
        assert yields.rate("hunter", 13, 13 + d) == pooled.rate("hunter", 13, 13 + d)
    assert yields.rate("priest", 13, 12) < pooled.rate("priest", 13, 12), "its own deaths' time"
