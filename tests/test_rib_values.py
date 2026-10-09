"""V560: a rib is valued by what the hive measured that rib itself to pay an hour, not only by its
creatures' levels.

The yield table (V392) knows a rib by its creatures' levels alone, so every rib of a band paid
alike and the nearest of them won. On 8 Oct the hive's characters ground their routes' ribs at
1,208 experience an hour against 2,322 on their route's two best measured ribs of the same levels
(22,462 rib hours). At 13-14, Dun Morogh's 13-15 ribs had 42 and 46 hours of play at 204 and 141
an hour - two laps of stations with nothing to fight - while Loch Modan's mangy mountain boars a
zone away paid 3,010. The fixture is the hive's own measure of 9 Oct 01:14 for the ribs of the
priest graph (`tests/fixtures/rib-values-loch-modan.json`)."""

import json
from pathlib import Path

from test_rib_bars import RIBS, _pos, _runtime, at15, priest_graph

from jev.guide.graph import rib_for, rib_pays
from jev.learn.values import CLASS_HOURS, Values
from jev.orch.runtime import RIB_MEASURED_H, RIB_PRIOR_H

FIXTURE = Path(__file__).parent / "fixtures" / "rib-values-loch-modan.json"


def _values() -> Values:
    return Values.load(FIXTURE)


def _ribs():
    return [n for n in priest_graph().nodes if n.id in RIBS]


def test_a_band_the_hive_marked_crowded_counts_and_a_class_s_own_number_stands_from_two_hours():
    v = _values()
    # Every band in the hive is crowded; the judge's reader drops them, the rib choice does not.
    assert v.grind("dun_morogh_13_15", 13) is None
    assert v.played("dun_morogh_13_15", 13) == 204
    # A priest's own 145 over 17.6 h stands for a priest; 2.0 h are needed.
    assert v.played("dun_morogh_13_15", 13, "priest") == 145
    assert v.played("loch_modan_13_15_loch_crocolisk", 15, "priest") == 844      # 2.69 h
    assert v.played("loch_modan_13_15_loch_crocolisk", 15, "priest", min_hours=3.0) == 1313
    assert CLASS_HOURS == 2.0
    assert v.played("dun_morogh_13_15", 17) is None, "no band at 17-18 in the fixture"


def test_a_rib_of_few_hours_is_drawn_to_the_level_s_mean():
    v = _values()
    mean = v.played_mean(15, "warrior", RIB_MEASURED_H)
    raw = v.played("dun_morogh_11_13_stonesplinter_tr", 15, "warrior", RIB_MEASURED_H)
    drawn = v.played("dun_morogh_11_13_stonesplinter_tr", 15, "warrior", RIB_MEASURED_H,
                     toward=mean, prior_hours=RIB_PRIOR_H)
    assert raw == 2113                                        # 3.71 h only
    assert abs(drawn - (2113 * 3.71 + mean * RIB_PRIOR_H) / (3.71 + RIB_PRIOR_H)) < 1e-6
    assert min(raw, mean) < drawn < max(raw, mean)


def test_the_rib_paying_most_as_played_is_taken_not_the_nearest_of_its_band(tmp_path):
    """A level 13 priest at Thelsamar: by the band estimate the ribs of 13-14 pay alike, and
    the nearest wins; by what each paid it is Loch Modan's mangy mountain boars (2,035 a priest
    over 5.1 h), never Dun Morogh's 13-15 ribs (145 and 94)."""
    v = _values()
    rt = _runtime(tmp_path, [at15(1000.0)], values=v)
    rt._cls = "priest"
    rate = rt._rate()
    ribs = _ribs()
    pays = rib_pays(ribs, 13, rate)
    assert pays["dun_morogh_13_15"] < 400 and pays["dun_morogh_13_15_stonesplinter_se"] < 400
    chosen = rib_for(ribs, 13, near=_pos(RIBS["dun_morogh_13_15"][1]), rate=rate)
    assert chosen.id == "dun_morogh_9_11", chosen.id     # a priest's 2,227 over 9.1 h at 13-14
    assert pays[chosen.id] >= 0.9 * max(pays.values())
    without = rib_for(ribs, 13, near=_pos(RIBS["dun_morogh_13_15"][1]))
    assert without.id.startswith("dun_morogh_13_15"), "before: the nearest of the band"


def test_a_rib_not_measured_takes_the_level_s_mean_and_no_values_is_as_before(tmp_path):
    v = _values()
    rt = _runtime(tmp_path, [at15(1000.0)], values=v)
    rt._cls = "warrior"
    rate = rt._rate()
    by_id = {r.id: r for r in _ribs()}
    mean = v.played_mean(13, "warrior", RIB_MEASURED_H)
    unmeasured = by_id["loch_modan_15_17"]               # no 13-14 band in the fixture
    assert rate(unmeasured, 13) == mean
    assert _runtime(tmp_path, [at15(1000.0)])._rate() is None, "no values, no table: as before"
    empty = tmp_path / "values.json"
    empty.write_text(json.dumps({"format": 1, "quests": {}, "grinds": {}}))
    assert Values.load(empty) is None
