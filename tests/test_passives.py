"""V562: the passives worth their price are bought, though nothing presses them.

On 9 Oct 01:00 not one of the hive's 116 warriors, paladins, hunters and rogues online (levels
11-15 on average) had Parry, sold by their trainers at levels 1-12 for one or eight silver, and
none of its 32 rogues had Dual Wield, three silver at level 10: training bought only what a fight
presses or the bar holds (V237), "a passive ... pressed by nothing"."""

from jev.world import training
import pytest

from jev.world import training as training_module
from jev.world.training import PASSIVES_BOUGHT, buy_order, shopping, starting_bar, worth_buying


def _offers(spell_ids, level):
    return [o for offers in training.catalog()["offers"].values() for o in
            (training.Offer(x["spell"], x["level"], x["cost"]) for x in offers)
            if o.spell_id in spell_ids and o.level <= level]


@pytest.fixture
def bought(monkeypatch):
    monkeypatch.setattr(training_module, "PASSIVES_SUSPENDED", False)


def test_while_suspended_no_passive_is_worth_buying():
    """V564: the census does not show a passive once learned; bought, it was bought again at
    every visit, so none is bought until learned skills are remembered."""
    assert training_module.PASSIVES_SUSPENDED
    assert not worth_buying(3127, frozenset({6603}), starting_bar(1, 1))


def test_parry_and_dual_wield_are_worth_buying_and_go_first_at_their_level(bought):
    rogue = frozenset({6603, 1752, 2098})                 # Attack, Sinister Strike, Eviscerate
    bar = starting_bar(4, 1)
    assert worth_buying(3127, rogue, bar) and worth_buying(674, rogue, bar)
    assert not worth_buying(674, rogue | {674}, bar)
    offers = _offers({3127, 674, 1757, 6760}, 12)         # Parry, Dual Wield, SS 2, Evisc 2
    bought = shopping(offers, rogue, bar)
    ids = [o.spell_id for o in bought]
    assert 3127 in ids and 674 in ids
    by_level = {}
    for o in bought:
        by_level.setdefault(o.level, []).append(o.spell_id)
    for level, there in by_level.items():
        passives = [s for s in there if s in PASSIVES_BOUGHT]
        assert there[:len(passives)] == passives, (level, there)


def test_a_passive_ranks_before_a_strike_of_its_level():
    parry = next(o for o in _offers({3127}, 8) if o.level == 8)
    strike = training.Offer(1757, 8, 100)
    assert buy_order(parry) < buy_order(strike)
