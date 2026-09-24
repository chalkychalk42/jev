"""The production body reaches generated suppliers through its proven Interact seam."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_live_body import body
from test_runtime_records import seen

from jev.clients.interact import Result as Interacted
from jev.clients.vendor import Vended
from jev.coach.schema import Decision, Intent
from jev.guide.coords import ZoneBounds
from jev.orch.runtime import Armed
from jev.run.supervisor import BodyFailure
from jev.world.state_v1 import ArmedBy, Bags
from jev.world.vendor import Merchant


def test_body_buys_exact_empty_profile_supplies_at_nearest_matching_generated_shop(monkeypatch):
    b = body()
    b.client.read = lambda: {"vitals.hp": 1, "char.class_id": 2, "char.race_id": 1}
    b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE,
                           skill="BUY_AMMO_REAGENT_FOOD", abort_if=["dead"],
                           why="empty supplies", confidence=1), ArmedBy.POLICY, 0,
                  "guide", "d", "quest")
    vendors = (Merchant(1, "Far Food", 0, (80, 80, 0), frozenset({2070})),
               Merchant(2, "Water", 0, (51, 50, 0), frozenset({159})),
               Merchant(3, "Wrong Goods", 0, (50, 50, 0), frozenset({999})))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    visit = Mock(return_value=Interacted.VENDOR)
    b.interact = SimpleNamespace(open_on=visit)
    calls = []
    class FakeVendor:
        detail = "observed service"
        def __init__(self, hid, read, open_shop, origin, size):
            self.open_shop = open_shop
            assert hid is b.client.hid and size == (1600, 900)
        def run(self, **kwargs):
            assert self.open_shop()
            calls.append(kwargs)
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    state = seen().model_copy(update={"bags": Bags(food_id=2070, food_count=0,
                                                    drink_id=159, drink_count=0)})
    result = b.execute(b.arm, state, lambda: None)
    assert result.outcome.value == "succeeded"
    assert visit.call_args.args == ("Water",)
    assert visit.call_args.kwargs["node_world"] == (51, 50, 0)
    assert calls[0]["expected_name"] == "Water" and calls[0]["min_free"] == 1
    assert [(s.role, s.item_id) for s in calls[0]["supplies"]] == [("drink", 159)]


def test_body_does_not_bind_a_zero_count_to_another_food_identity(monkeypatch):
    b = body()
    b.client.read = lambda: {"vitals.hp": 1, "char.class_id": 2, "char.race_id": 1}
    b.arm = Armed(Decision(goal="supplies", intent=Intent.SERVICE,
                           skill="BUY_AMMO_REAGENT_FOOD", abort_if=["dead"],
                           why="empty supplies", confidence=1), ArmedBy.POLICY, 0,
                  "guide", "d", "quest")
    select = Mock()
    monkeypatch.setattr("jev.run.body.merchants", select)
    result = b.execute(b.arm, seen().model_copy(update={"bags": Bags(food_id=999, food_count=0)}),
                       lambda: None)
    assert result.code == "unsupported"
    select.assert_not_called()


def test_outside_zone_shop_is_not_selected_even_if_world_distance_is_shorter(monkeypatch):
    b = body()
    b.client.bounds = ZoneBounds(1, 0, 100, 0, 100, 0)
    b.client.position = lambda: (0.02, 0.02)  # world 98,98, close to a zone boundary
    b.arm = Armed(Decision(goal="bags", intent=Intent.SERVICE, skill="BAG_MAKE_SPACE",
                           abort_if=["dead"], why="full", confidence=1), ArmedBy.POLICY,
                  0, "guide", "d", "quest")
    vendors = (Merchant(1, "Outside", 0, (101, 101, 0), frozenset()),
               Merchant(2, "Inside", 0, (75, 75, 0), frozenset()))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    visit = Mock(return_value=Interacted.VENDOR)
    b.interact = SimpleNamespace(open_on=visit)
    class FakeVendor:
        detail = "observed service"
        def __init__(self, hid, read, open_shop, origin, size):
            self.open_shop = open_shop
        def run(self, **kwargs):
            assert kwargs["min_free"] == 6
            assert self.open_shop()
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    assert b.execute(b.arm, seen(), lambda: None).outcome.value == "succeeded"
    assert visit.call_args.args == ("Inside",)


def test_a_merchant_whose_body_cannot_be_clicked_is_passed_over_for_the_next(monkeypatch):
    """Dermot Johns stands behind his wagon; Godric Rothgar stood in plain view beside it
    (run 20260924T011327-6e5f4b stopped on its first full bags)."""
    b = body()
    b.arm = Armed(Decision(goal="bags", intent=Intent.SERVICE, skill="BAG_MAKE_SPACE",
                           abort_if=["dead"], why="full", confidence=1), ArmedBy.POLICY,
                  0, "guide", "d", "quest")
    vendors = (Merchant(1, "Behind The Wagon", 0, (50, 51, 0), frozenset()),
               Merchant(2, "In Plain View", 0, (52, 52, 0), frozenset()),
               Merchant(3, "Far Away", 0, (90, 90, 0), frozenset()))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    answers = {"Behind The Wagon": Interacted.NOT_VISIBLE, "In Plain View": Interacted.VENDOR}
    visit = Mock(side_effect=lambda name, **kw: answers[name])
    b.interact = SimpleNamespace(open_on=visit, detail="hover: ground")
    class FakeVendor:
        detail = "observed service"
        def __init__(self, hid, read, open_shop, origin, size):
            self.open_shop = open_shop
        def run(self, **kwargs):
            assert self.open_shop()
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    assert b.execute(b.arm, seen(), lambda: None).outcome.value == "succeeded"
    assert [c.args[0] for c in visit.call_args_list] == ["Behind The Wagon", "In Plain View"]


def test_a_merchant_that_refuses_for_another_reason_is_not_passed_over(monkeypatch):
    b = body()
    b.arm = Armed(Decision(goal="bags", intent=Intent.SERVICE, skill="BAG_MAKE_SPACE",
                           abort_if=["dead"], why="full", confidence=1), ArmedBy.POLICY,
                  0, "guide", "d", "quest")
    vendors = (Merchant(1, "Near", 0, (50, 51, 0), frozenset()),
               Merchant(2, "Next", 0, (52, 52, 0), frozenset()))
    monkeypatch.setattr("jev.run.body.merchants", lambda map_id: vendors)
    visit = Mock(return_value=Interacted.INTERRUPTED)
    b.interact = SimpleNamespace(open_on=visit, detail="combat")
    class FakeVendor:
        detail = ""
        def __init__(self, hid, read, open_shop, origin, size):
            self.open_shop = open_shop
        def run(self, **kwargs):
            self.open_shop()
            return Vended.DONE
    monkeypatch.setattr("jev.run.body.Vendor", FakeVendor)
    with pytest.raises(BodyFailure, match="Near: combat"):
        b.execute(b.arm, seen(), lambda: None)
    assert [c.args[0] for c in visit.call_args_list] == ["Near"]
