"""A merchant gossip uses the same observed-list selection as quest interactions."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_live_body import body

from jev.clients.choose import Chose
from jev.clients.interact import Result as Interacted


def test_merchant_gossip_selects_the_observed_vendor_identity_then_confirms_frame():
    live = body()
    live.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.GOSSIP))
    readings = iter([{"merchant.gossip_name_id": 123}, {"ui.vendor": True}])
    live.client.read = lambda: next(readings)
    choose = Mock(return_value=Chose.CHOSE)
    live.chooser = SimpleNamespace(run_id=choose)
    assert live._open_merchant("Merchant", (50, 50, 0), (0.5, 0.5))
    choose.assert_called_once_with(123)


@pytest.mark.parametrize("values", [{}, {"merchant.gossip_name_id": None}])
def test_unobserved_vendor_option_never_guesses_a_line(values):
    live = body()
    live.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.GOSSIP))
    live.client.read = lambda: values
    choose = Mock()
    live.chooser = SimpleNamespace(run_id=choose)
    assert not live._open_merchant("Merchant", (50, 50, 0), (0.5, 0.5))
    choose.assert_not_called()


def test_changed_gossip_without_a_merchant_frame_is_not_success():
    live = body()
    live.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.GOSSIP))
    readings = iter([{"merchant.gossip_name_id": 123}, {"ui.vendor": False}])
    live.client.read = lambda: next(readings)
    live.chooser = SimpleNamespace(run_id=Mock(return_value=Chose.CHOSE))
    assert not live._open_merchant("Merchant", (50, 50, 0), (0.5, 0.5))
