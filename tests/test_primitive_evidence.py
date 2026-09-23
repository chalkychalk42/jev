"""Actual primitive composition records causal observations without extra client reads."""

from types import SimpleNamespace

from test_execution_evidence import arm
from test_fight import ALIVE, _Hid, _Targeting
from test_interact import ELWYNN
from test_repair import AT_VENDOR, FIXED, WORN, _repair
from test_vendor import Shop

from jev.clients.fight import Fight
from jev.clients.interact import Interact
from jev.clients.interact import Result as Interacted
from jev.clients.loot import Loot, Looted
from jev.clients.recover import Recover, Recovered
from jev.clients.repair import Repaired
from jev.clients.rest import Rest, Rested
from jev.clients.targeting import ClickCode, ClickResult
from jev.clients.vendor import Vended
from jev.learn.episode import Recorder, read
from jev.perceive.radio_frame import name_id
from jev.perceive.units import Plate, RingColour
from jev.run.evidence import bind
from jev.run.hunt import Hunt, Hunted
from jev.world.vendor import Supply

PLATE = Plate(cx=700, cy=300, w=70, colour=RingColour.YELLOW)


def captured(recorder):
    recorder.close()
    return read(recorder.dir / "executions.jsonl")


def test_hunt_records_selection_click_observation_and_loot_under_one_arm(tmp_path, monkeypatch):
    monkeypatch.setattr("jev.clients.fight.time.sleep", lambda _: None)
    monkeypatch.setattr("jev.clients.fight.find_plates", lambda _: [PLATE])
    nothing = {**ALIVE, "target.has": False, "target.name_id": None, "target.hp": None}
    values = iter([nothing, ALIVE, {**ALIVE, "target.hp": 0.5}, {**ALIVE, "target.hp": 0}])
    reads = []
    def fight_read():
        value = next(values)
        reads.append(value)
        return value
    fight_hid = _Hid()
    fight = Fight(fight_hid, fight_read, lambda: object(),
                  targeting=_Targeting(fight_read, fight_hid))
    loot_hid = _Hid()
    have = [0]
    def take(*args, **kwargs):
        have[0] = 1
        return True
    loot_hid.click = take
    def loot_read():
        return {"bags.free": 8, "ui.loot": False,
                "target.has": True, "target.hp": 0, "target.name_id": 1161}
    loot = Loot(loot_hid, loot_read, lambda: object(),
                targeting=_Targeting(loot_read, loot_hid, action=ClickResult(
                    ClickCode.CLICKED, (700, 495), "delivered", 1)))
    hunt = Hunt(fight, SimpleNamespace(), lambda: {}, lambda _: True,
                lambda: (have[0], 1), loot=loot, say=lambda _: None)
    rec = Recorder(tmp_path)
    with bind(rec, arm(), client_id="c"):
        assert hunt.run((0, 0, 0), 30, 1161) is Hunted.DONE
    rows = captured(rec)
    names = [r["operation"] for r in rows]
    assert names.index("selection.request") < names.index("engage.request") < names.index("loot.request")
    assert len(reads) == 4, "tracing must not consume extra radio observations"
    health = [r["data"]["target.hp"] for r in rows if r["operation"] == "combat.observed"]
    assert health == [None, 1, 0.5, 0]
    objective = [r["data"] for r in rows if r["operation"] == "loot.objective"]
    assert objective == [{"before": [0, 1], "after": [1, 1]}]
    root = next(r for r in rows if r["operation"] == "hunt" and r["phase"] == "begin")
    children = [r for r in rows if r["phase"] == "begin" and r["operation"] in {"fight", "loot"}]
    assert len(children) == 2 and all(r["parent_operation_id"] == root["operation_id"] for r in children)
    assert {r["decision_id"] for r in rows} == {"c:decision"}
    assert not (rec.dir / "skills.jsonl").exists()


def test_interaction_records_identity_between_plate_and_model_click(tmp_path, monkeypatch):
    monkeypatch.setattr("jev.clients.interact.time.sleep", lambda _: None)
    selected = {"target.has": True, "target.name_id": name_id("Merchant")}
    values = iter([selected, {**selected, "ui.vendor": True}])
    hid = _Hid()
    def read_values():
        return next(values)
    skill = Interact(hid, ELWYNN, read_values, lambda: object(),
                     lambda: (0.5, 0.5), (800, 450), targeting=_Targeting(read_values, hid))
    rec = Recorder(tmp_path)
    with bind(rec, arm(), client_id="c"):
        assert skill._try(PLATE, name_id("Merchant")) is Interacted.VENDOR
    rows = captured(rec)
    names = [r["operation"] for r in rows]
    assert names.index("selection.request") < names.index("selection.observed") < names.index("interact.click")
    assert next(r for r in rows if r["operation"] == "selection.observed")["data"] == {
        "wanted_name_id": name_id("Merchant"), "observed_name_id": name_id("Merchant")}
    assert rows[-1]["code"] == "vendor"


def test_loot_without_a_delta_does_not_record_confirmed_empty_corpse(tmp_path):
    hid = _Hid()
    def read_values():
        return {"bags.free": 8, "ui.loot": False, "target.has": True, "target.hp": 0,
                "target.name_id": 1161}
    skill = Loot(hid, read_values, lambda: object(), targeting=_Targeting(read_values, hid))
    rec = Recorder(tmp_path)
    with bind(rec, arm(), client_id="c"):
        assert skill.run(settle_s=0) is Looted.NOTHING
    rows = captured(rec)
    assert not any(r["operation"] == "loot.change" for r in rows)
    assert rows[-1]["code"] == "nothing"
    assert "no observed" in rows[-1]["detail"]
    assert "empty" not in rows[-1]["detail"]


def test_repair_and_vendor_record_existing_measured_deltas(tmp_path, monkeypatch):
    monkeypatch.setattr("jev.clients.repair.time.sleep", lambda _: None)
    repair = _repair([WORN, AT_VENDOR, FIXED])
    shop = Shop()
    rec = Recorder(tmp_path)
    with bind(rec, arm(), client_id="c"):
        assert repair.run() is Repaired.DONE
        assert shop.body().run(expected_name="Merchant", min_free=1,
            supplies=(Supply(2070, "Darnassian Bleu", "food", desired=10),)) is Vended.DONE
    rows = captured(rec)
    repair_values = [r["data"]["bags.durability_min"] for r in rows if r["operation"] == "repair.observed"]
    assert repair_values == [0.1, 0.1, 1, 1]
    sales = [r["data"] for r in rows if r["operation"] == "sale.observed"]
    purchases = [r["data"] for r in rows if r["operation"] == "purchase.observed"]
    assert sales[0]["bags.money_copper"] == 112
    assert [r["bags.money_copper"] for r in purchases] == [92, 72]
    assert [r["merchant.owned"] for r in purchases] == [5, 10]


def test_rest_and_recovery_observe_without_extra_reads(tmp_path):
    rest_reads, recovery_reads = [], []
    def rest_read():
        rest_reads.append(True)
        return {"vitals.hp": 1}
    def recovery_read():
        recovery_reads.append(True)
        return {"vitals.dead": False, "vitals.ghost": False}
    rec = Recorder(tmp_path)
    with bind(rec, arm(), client_id="c"):
        assert Rest(_Hid(), rest_read).until() is Rested.HEALTHY
        assert Recover(_Hid(), recovery_read).run() is Recovered.NOT_DEAD
    rows = captured(rec)
    assert len(rest_reads) == len(recovery_reads) == 1
    assert {r["operation"] for r in rows} >= {"rest.observed", "recovery.observed"}
