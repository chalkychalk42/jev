"""A hunter keeps a pet (V389): its spells, beasts, diets and foods from this server's world
database, the pet in Jev's state, the policy's service for it and the body's care of it; and the
rods' charm let go for the next rod (V390)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from test_live_body import body
from test_runtime_records import seen

from jev.clients.fight import Fought
from jev.clients.pet import NO_PET, PET_DEAD, PET_NOT_DEAD, PetCast, Tameable, TameOn
from jev.clients.rest import Rested
from jev.clients.use import UseOn
from jev.coach.policy import ROUTINE_RULES, Context, routine_only, services
from jev.coach.schema import Decision, Intent
from jev.coach.verifier import verify
from jev.guide.coords import world_to_map
from jev.guide.generate import WorldDB
from jev.learn.episode import SkillOutcome
from jev.orch.runtime import SERVICING_SKILLS, Armed
from jev.perceive import fields
from jev.perceive.radio_frame import RadioReading, name_id, to_state
from jev.run.body import PET_FOOD_DESIRED, TAME_NEAR_YARDS
from jev.run.hunt import Hunted
from jev.skills.catalog import NAMES
from jev.world import pets
from jev.world.state_v1 import (
    ArmedBy,
    Bags,
    Char,
    Flags,
    GuidePos,
    Pet,
    SenseFault,
    StepKind,
    Vitals,
)
from jev.world.vendor import Supply

DB = "data/knowledge/tbc-243.sqlite"
DUROTAR = 14
SCORPID, BOAR, RAPTOR = 3127, 3099, 3123          # Venomtail Scorpid, Dire Mottled Boar...
JERKY, HAUNCH, MUTTON, BREAD, WATER = 117, 2287, 3770, 4540, 159
GRIMTAK = (117, 159, 2287, 3770)                    # Razor Hill's butcher's goods


def _spawn(entry: int) -> tuple[float, float]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        row = db.execute("select position_x, position_y from world_creature where id = ? "
                         "order by guid limit 1", (entry,)).fetchone()
    return float(row[0]), float(row[1])


# -- the world: beasts, diets, foods, costs -----------------------------------------------------


def test_a_level_10_hunter_tames_what_stands_at_its_level_or_one_below_and_nothing_elite():
    """By a Venomtail Scorpid's spawn in Durotar (levels 9-10): the scorpids and the raptors
    round them (8-10), never the rods' Dire Mottled Boars (6-7); at 12, no scorpid."""
    x, y = _spawn(SCORPID)
    found = {beast.entry: beast for beast, _ in pets.tameable(1, x, y, TAME_NEAR_YARDS, 10)}
    assert SCORPID in found and found[SCORPID].family == 20 and found[SCORPID].name_id == name_id(
        "Venomtail Scorpid")
    assert BOAR not in found
    nearest, points = pets.tameable(1, x, y, TAME_NEAR_YARDS, 10)[0]
    assert nearest.entry == SCORPID and min(abs(p[0] - x) + abs(p[1] - y) for p in points) < 1
    assert SCORPID not in {b.entry for b, _ in pets.tameable(1, x, y, TAME_NEAR_YARDS, 12)}
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        ranks = dict(db.execute("select Entry, Rank from world_creature_template"))
    kinds, _ = pets._beasts()
    assert kinds and all(ranks[entry] == 0 for entry in kinds), "no elite is a solo taming"


def test_each_family_eats_its_diet_and_a_food_is_worth_less_as_the_pet_outgrows_it():
    assert pets.eats(20, JERKY) and not pets.eats(20, BREAD), "a scorpid eats meat alone"
    assert pets.eats(5, JERKY) and pets.eats(5, BREAD), "a boar eats everything"
    assert not pets.eats(20, WATER)
    # Tough Jerky is item level 5 (`Pet::GetCurrentFoodBenefitLevel`).
    assert [pets.worth(20, level, JERKY) for level in (10, 11, 15, 16, 19, 20)] == [
        35000, 17000, 17000, 8000, 8000, 0]


def test_the_food_bought_is_the_cheapest_that_feeds_the_pet_in_full():
    assert pets.to_buy(20, 10, GRIMTAK).entry == JERKY
    assert pets.to_buy(20, 11, GRIMTAK).entry == HAUNCH, "jerky is worth half to a level 11"
    assert pets.to_buy(20, 21, GRIMTAK).entry == MUTTON
    assert pets.to_buy(20, 10, (BREAD, WATER)) is None, "no meat sold"
    food, full = pets.in_bags(20, 11, {JERKY: 4, HAUNCH: 3, WATER: 10})
    assert (food, full) == (HAUNCH, 3), "the haunch feeds it in full, the jerky by half"
    assert pets.in_bags(20, 20, {JERKY: 4}) == (None, 0)


def test_tame_beast_and_revive_pet_cost_their_share_of_a_hunters_base_mana():
    # 206 base mana at 10 (`player_classlevelstats`): 48% and 80%.
    assert pets.mana_cost(pets.TAME_BEAST, pets.HUNTER, 10) == 98
    assert pets.mana_cost(pets.REVIVE_PET, pets.HUNTER, 10) == 164
    assert pets.mana_cost(pets.CALL_PET, pets.HUNTER, 10) == 0
    assert pets.mana_cost(pets.TAME_BEAST, None, 10) is None


# -- the pet in Jev's state -------------------------------------------------------------------


def _reading(**pet):
    values = {f.name: None for f in fields.FIELDS}
    values.update({f"pet.{k}": v for k, v in pet.items()})
    return RadioReading(values, True, SenseFault.NONE, seq=1)


def test_the_pet_is_unknown_on_the_live_strip_and_transcribed_from_a_server_bodys():
    assert to_state(_reading(), t=0, client_id="live").pet == Pet()
    pet = to_state(_reading(has=True, dead=False, entry=SCORPID, level=10, hp=0.5, happiness=1,
                            loyalty=1, food_id=JERKY, food_count=5, charmed=False),
                   t=0, client_id="hive").pet
    assert pet.has and pet.happiness == 1 and pet.food_id == JERKY and pet.charmed is False


# -- the policy -------------------------------------------------------------------------------


def _state(**kw):
    base = seen(char=Char(cls="hunter", level=10), bags=Bags(free=10, money_copper=500),
                vitals=Vitals(hp=1, power=1, combat=False, dead=False, ghost=False))
    return base.model_copy(update=kw)


@pytest.mark.parametrize("need", ["call", "revive", "feed", "dismiss", "tame"])
def test_what_the_pet_needs_is_a_routine_service_the_verifier_lets_through(need):
    context = Context(pet_due=lambda state: need)
    plans = [p for p in services(_state(), context=context) if p.rule == "service.pet"]
    assert len(plans) == 1
    decision = plans[0].decision
    assert decision.skill == "TEND_PET" and decision.params == {"service": "pet", "pet": need}
    assert verify(decision, _state(), NAMES).ok
    assert routine_only("service.pet") and "service.pet" in ROUTINE_RULES
    assert "TEND_PET" in SERVICING_SKILLS


def test_a_taming_waits_for_a_meal_and_nothing_is_tended_in_a_fight():
    context = Context(pet_due=lambda state: "tame")
    hurt = _state(vitals=Vitals(hp=0.5, power=1, combat=False, dead=False, ghost=False))
    assert not [p for p in services(hurt, context=context) if p.rule == "service.pet"]
    call = Context(pet_due=lambda state: "call")
    assert [p for p in services(hurt, context=call) if p.rule == "service.pet"]
    fighting = _state(vitals=Vitals(hp=1, power=1, combat=True, dead=False, ghost=False))
    assert services(fighting, context=call) == []
    broken = Context(pet_due=lambda state: 1 / 0)
    assert not [p for p in services(_state(), context=broken) if p.rule == "service.pet"]


def test_the_pets_food_out_sends_the_hunter_to_the_merchant_with_its_own_supplies():
    context = Context(pet_food=lambda state: HAUNCH)
    supplies = [p for p in services(_state(), context=context) if p.rule == "service.supplies"]
    assert supplies and supplies[0].decision.why == "the pet's food is out"
    broke = _state(bags=Bags(free=10, money_copper=50))
    assert not [p for p in services(broke, context=context) if p.rule == "service.supplies"], (
        "125 copper the five")


# -- the body ---------------------------------------------------------------------------------

KNOWN = frozenset({pets.TAME_BEAST, pets.CALL_PET, pets.DISMISS_PET, pets.FEED_PET,
                   pets.REVIVE_PET})


def _durotar():
    db = WorldDB(DB)
    try:
        return db.bounds[DUROTAR]
    finally:
        db.con.close()


def _body(known=KNOWN):
    b = body(StepKind.GRIND)
    b.client.spells = SimpleNamespace(known=known)
    b.client.bounds = _durotar()
    return b


def _at(bounds, x, y, **kw):
    mx, my = world_to_map(x, y, bounds)
    return _state(pos=seen().pos.model_copy(update={"mx": mx, "my": my}), **kw)


def test_a_hunter_with_tame_beast_tends_its_pet_as_the_server_shows_it():
    b = _body()
    bounds = b.client.bounds
    x, y = _spawn(SCORPID)
    here = _at(bounds, x, y, guide=GuidePos(kind=StepKind.GRIND))
    assert b.pet_due(here) is None, "no pet read: the live strip"
    out = Pet(has=True, dead=False, level=10, happiness=2, food_id=JERKY, food_count=5,
              charmed=False)
    assert b.pet_due(here.model_copy(update={"pet": out})) is None
    assert b.pet_due(here.model_copy(update={"pet": out.model_copy(update={"dead": True})})) \
        == "revive"
    unhappy = out.model_copy(update={"happiness": 1})
    assert b.pet_due(here.model_copy(update={"pet": unhappy})) == "feed"
    assert b.pet_due(here.model_copy(update={"pet": unhappy.model_copy(
        update={"food_id": None})})) is None, "nothing it eats"
    gone = Pet(has=False, charmed=False)
    assert b.pet_due(here.model_copy(update={"pet": gone})) == "call"
    assert b.pet_due(here.model_copy(update={"pet": gone.model_copy(update={"charmed": True})})) \
        == "dismiss"
    b._pet_none = True
    assert b.pet_due(here.model_copy(update={"pet": gone})) == "tame", "by the scorpids"
    walking = here.model_copy(update={"pet": gone, "guide": GuidePos(kind=StepKind.QUEST_TURNIN)})
    assert b.pet_due(walking) is None, "tamed at its work, not on the way to a hand-in"
    far = _at(bounds, x + 2000, y, guide=GuidePos(kind=StepKind.GRIND), pet=gone)
    assert b.pet_due(far) is None
    flying = here.model_copy(update={"pet": gone, "flags": Flags(on_taxi=True)})
    assert b.pet_due(flying) is None
    b._pet_none, b._pet_dead = False, True
    assert b.pet_due(here.model_copy(update={"pet": gone})) == "revive"
    assert b.pet_due(here.model_copy(update={"pet": out})) is None and not b._pet_dead
    mage = here.model_copy(update={"pet": gone, "char": Char(cls="mage", level=10)})
    assert b.pet_due(mage) is None


def test_taming_waits_for_feed_pet_and_nothing_is_tended_before_tame_beast():
    b = _body(KNOWN - {pets.FEED_PET, pets.REVIVE_PET})
    x, y = _spawn(SCORPID)
    here = _at(b.client.bounds, x, y, guide=GuidePos(kind=StepKind.GRIND),
               pet=Pet(has=False, charmed=False))
    assert b.pet_due(here) == "call"
    b._pet_none = True
    assert b.pet_due(here) is None, "an unfed pet runs out of loyalty in ten minutes"
    assert _body(frozenset()).pet_due(here) is None


def test_the_pets_food_is_named_while_none_it_eats_in_full_is_in_the_bags():
    b = _body()
    b._sold_in_box = lambda: frozenset(GRIMTAK)
    pet = Pet(has=True, dead=False, entry=SCORPID, level=11, happiness=2, food_count=0)
    assert b.pet_food(_state(pet=pet)) == HAUNCH
    assert b.pet_food(_state(pet=pet.model_copy(update={"food_count": 3}))) is None
    assert b.pet_food(_state(pet=pet.model_copy(update={"dead": True}))) is None


def test_the_supplies_buy_the_pets_food_or_more_of_the_hunters_own_when_it_is_the_same():
    b = _body()
    b._in_zone = lambda merchants: [SimpleNamespace(items=frozenset(GRIMTAK))]
    jerky = Supply(item_id=JERKY, name="Tough Jerky", role="food", slot=12)
    level_10 = _state(pet=Pet(has=True, dead=False, entry=SCORPID, level=10, food_count=0))
    (bought,) = b._with_pet_food((jerky,), level_10)
    assert bought.item_id == JERKY and bought.desired == 10 + PET_FOOD_DESIRED
    level_12 = _state(pet=Pet(has=True, dead=False, entry=SCORPID, level=12, food_count=0))
    own, food = b._with_pet_food((jerky,), level_12)
    assert own == jerky and (food.item_id, food.role, food.desired) == (HAUNCH, "pet", 10)
    assert b._with_pet_food((), level_12.model_copy(update={"pet": Pet(has=False)})) == ()


class _Pet:
    """A body's pet as the server answers its spells: `answers` maps a spell to its PetCast and
    what the strip shows after."""

    def __init__(self, b, answers, values):
        self.casts, self.values = [], values
        b._pet_cast = self.cast
        b._read = lambda: dict(self.values)
        b.say = lambda line: None
        self.answers = answers

    def cast(self, spell, *, item=None):
        self.casts.append((spell, item))
        answer, after = self.answers[spell]
        self.values.update(after)
        return answer


def _armed(b, need):
    d = Decision(goal="g", intent=Intent.SERVICE, skill="TEND_PET", abort_if=["dead"],
                 confidence=0.7, why="fixture", params={"service": "pet", "pet": need})
    b.arm = Armed(d, ArmedBy.POLICY, 0, "service.pet", "d", b.graph.nodes[0].id)
    return d


VALUES = {"pet.has": False, "pet.dead": None, "vitals.combat": False, "vitals.power": 1.0,
          "vitals.power_max": 256, "char.class_id": 3, "char.level": 10}


def test_a_call_that_finds_no_pet_kept_says_so_and_one_that_finds_it_dead_revives_it(monkeypatch):
    monkeypatch.setattr("jev.run.body.PET_POLL_S", 0.0)
    monkeypatch.setattr("jev.run.body.PET_ANSWER_S", 0.0)
    b = _body()
    server = _Pet(b, {pets.CALL_PET: (PetCast(True, codes=(NO_PET,)), {})}, dict(VALUES))
    _armed(b, "call")
    result = b._pet(_state())
    assert (result.outcome, result.code) == (SkillOutcome.SUCCEEDED, "no_pet") and b._pet_none
    b._pet_none = False
    server = _Pet(b, {pets.CALL_PET: (PetCast(True, tame=(PET_DEAD,)), {}),
                      pets.REVIVE_PET: (PetCast(True), {"pet.has": True, "pet.dead": False})},
                  dict(VALUES))
    result = b._pet(_state())
    assert result.code == "revived" and server.casts == [(pets.CALL_PET, None),
                                                         (pets.REVIVE_PET, None)]
    assert not b._pet_dead and not b._pet_none
    server = _Pet(b, {pets.CALL_PET: (PetCast(True), {"pet.has": True, "pet.dead": False})},
                  dict(VALUES))
    assert b._pet(_state()).code == "called"


def test_a_revive_drinks_for_its_mana_first_and_a_pet_alive_is_called(monkeypatch):
    monkeypatch.setattr("jev.run.body.PET_POLL_S", 0.0)
    monkeypatch.setattr("jev.run.body.PET_ANSWER_S", 0.0)
    b = _body()
    low = {**VALUES, "vitals.power": 0.3}
    server = _Pet(b, {pets.REVIVE_PET: (PetCast(True, tame=(PET_NOT_DEAD,)), {}),
                      pets.CALL_PET: (PetCast(True), {"pet.has": True, "pet.dead": False})},
                  low)
    drank = []

    def drink(fraction, *, role, timeout_s=45.0):
        drank.append(round(fraction, 2))
        server.values["vitals.power"] = fraction
        return Rested.HEALTHY

    b.rest = SimpleNamespace(until=drink, detail="")
    _armed(b, "revive")
    result = b._pet(_state())
    assert drank == [round(164 / 256 + 0.05, 2)], "Revive Pet's 164 of 256"
    assert result.code == "called"


def test_a_feed_casts_feed_pet_on_the_food_and_says_the_servers_refusal():
    b = _body()
    server = _Pet(b, {pets.FEED_PET: (PetCast(True), {})}, {**VALUES, "pet.has": True,
                                                           "pet.dead": False,
                                                           "pet.food_id": JERKY})
    _armed(b, "feed")
    assert b._pet(_state()).code == "fed" and server.casts == [(pets.FEED_PET, JERKY)]
    server.answers[pets.FEED_PET] = (PetCast(True, codes=(0x82,)), {})
    result = b._pet(_state())
    assert result.outcome is SkillOutcome.ABORTED and "0x82" in result.detail


def test_the_body_validates_a_pet_need_and_never_ends_a_session_on_one(monkeypatch):
    b = _body()
    d = _armed(b, "tame")
    assert b.validate(d, b.arm.step_id) is None
    bogus = d.model_copy(update={"params": {"service": "pet", "pet": "teach"}})
    assert b.validate(bogus, b.arm.step_id) is not None
    # A taming hunt refused for mana, or too hurt with nothing to eat, is the pet's wait.
    x, y = _spawn(SCORPID)
    b._world_position = lambda: (x, y)
    b._mana_for = lambda cost: True
    b._read = lambda: dict(VALUES)
    b.say = lambda line: None
    for hunted in (Hunted.REFUSED, Hunted.NO_FOOD):
        monkeypatch.setattr("jev.run.body.Hunt.run", lambda self, *a, h=hunted, **kw: h)
        result = b._pet(_state())
        assert result.code == f"tame_{hunted.value}" and result.outcome is SkillOutcome.ABORTED


def test_a_taming_hunts_the_beasts_spawns_with_tame_beast_for_its_pull(monkeypatch):
    b = _body()
    x, y = _spawn(SCORPID)
    b._world_position = lambda: (x, y)
    b._mana_for = lambda cost: True
    b.say = lambda line: None
    tamed = {"pet.has": False}
    b._read = lambda: {**VALUES, **tamed, "pet.dead": False}
    seen_runs = []

    def run(self, centre, radius, pull, **kw):
        seen_runs.append((self.fight, pull, kw["spawns"]))
        tamed["pet.has"] = True
        return Hunted.DONE

    monkeypatch.setattr("jev.run.body.Hunt.run", run)
    _armed(b, "tame")
    result = b._pet(_state())
    assert result.code == "tamed"
    engagement, pull, spawns = seen_runs[0]
    assert isinstance(engagement, TameOn) and engagement.mana == 98
    assert isinstance(pull, Tameable) and (pull.low, pull.high) == (9, 10)
    assert name_id("Venomtail Scorpid") in pull.names and spawns


def test_tameable_takes_a_beast_of_its_levels_neutral_or_hostile_and_of_normal_rank():
    pull = Tameable(own=None, names=frozenset({7}), low=9, high=10)
    beast = {"target.name_id": 7, "target.level": 10, "target.reaction": 4,
             "target.classification": 1}
    assert pull.takes(beast) and pull.named(7) and not pull.named(8)
    assert not pull.takes({**beast, "target.level": 11})
    assert not pull.takes({**beast, "target.reaction": 5}), "friendly"
    assert not pull.takes({**beast, "target.classification": 2}), "elite"


def test_tame_on_does_not_pull_short_of_its_mana_and_the_live_one_never_casts():
    tame = TameOn(fight=SimpleNamespace(detail=""), item_id=0, complete=lambda: False,
                  read=lambda: {"vitals.power": 0.2, "vitals.power_max": 256,
                                "vitals.combat": False}, mana=98)
    assert tame.run(7) is Fought.REFUSED and "98" in tame.detail
    assert tame._press({}) is Fought.REFUSED


def test_a_rods_charm_is_let_go_where_a_body_can_and_fought_as_it_turns():
    """V390: the next rod's use is not spent at its channel's end, nor the charm waited out."""
    fought = []
    fight = SimpleNamespace(detail="", run=lambda name, **kw: fought.append(name) or Fought.KILLED)

    class Server(UseOn):
        def charmed(self):
            return True

        def dismiss(self):
            self.dismissed = True
            return True

    use = Server(fight=fight, read=lambda: {"vitals.combat": False}, item_id=15919,
                 complete=lambda: False)
    assert use.run(42) is Fought.KILLED and use.dismissed and fought == [42]
    assert "let go" in use.detail
    assert not UseOn(fight=fight, read=dict, item_id=1, complete=lambda: False).dismiss()


def test_what_durotars_merchants_sell_feeds_a_level_10_and_a_level_12_scorpid():
    """The policy's look at the zone's goods, from the vendor catalog: Razor Hill's Grimtak and
    Innkeeper Grosk sell Tough Jerky and Haunch of Meat."""
    sold = _body()._sold_in_box()
    assert {JERKY, HAUNCH} <= sold
    assert pets.to_buy(20, 10, sold).entry == JERKY and pets.to_buy(20, 12, sold).entry == HAUNCH
