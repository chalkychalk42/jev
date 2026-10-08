"""A pet's feed waits for its effect (V490): Feed Pet eats its food at once and gives happiness
ten ticks over 20 s, the first 2 s after it, and a feed cast while one runs replaces it before
its ticks. Fed again a second later, as the hive's hunters fed theirs, a pet eats a food a second
and stays unhappy; its loyalty runs out and it leaves.

The case is recorded play (`tests/fixtures/hunter-feed-storm.json`): hive-768, an orc hunter at
10, with an Elder Mottled Boar of level 9 tamed at 10:36:49 on 8 Oct; from 10:44:30 to 10:47:30
its body cast Feed Pet 170 times on Tough Jerky, the server refusing none, the bags' jerky falling
by 170, and every state the strip painted read the pet unhappy. Across the hive's hunters on
7-8 Oct, 94% of 825 feeds were followed by the next within 2 s.

`ServerPet` is the server's arithmetic for one pet at loyalty 1, as CMaNGOS has it: happiness
falls 8,750 every 7.5 s (`Pet::LooseHappiness`), loyalty moves every 12 s by +20 happy, +10
content and -20 unhappy (`Pet::TickLoyaltyChange`), and Feed Pet's effect (spell 1539) is one
aura, replaced by the next feed (`Unit::AddSpellAuraHolder`, AURA_REMOVE_BY_STACK).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_pet import KNOWN, VALUES, _armed, _body, _state

from jev.clients.pet import PetCast
from jev.learn.episode import SkillOutcome
from jev.world import pets
from jev.world.state_v1 import Pet

FIXTURE = Path(__file__).parent / "fixtures" / "hunter-feed-storm.json"
DB = "data/knowledge/tbc-243.sqlite"
JERKY = 117


class ServerPet:
    """One hunter's pet at loyalty 1 as the server keeps it, on a clock of seconds."""

    MAX, STATE = 1_050_000, 333_000
    DECAY_S, DECAY = 7.5, (140 >> 1) * 125          # 8,750: 70,000 a minute at loyalty 1
    LOYALTY_S = 12.0

    def __init__(self, happiness: int, *, loyalty: int = 1000, per_tick: int = 35_000):
        self.happiness, self.loyalty, self.per_tick = happiness, loyalty, per_tick
        self.now = 0.0
        self.next_decay, self.next_loyalty = self.DECAY_S, self.LOYALTY_S
        self.effect: list[float] | None = None        # [next tick, ticks left]
        self.eaten = self.ticks = self.broken = 0

    def state(self) -> int:
        if self.happiness < self.STATE:
            return pets.UNHAPPY
        return pets.HAPPY if self.happiness >= 2 * self.STATE else pets.CONTENT

    def run(self, until: float) -> None:
        while True:
            due = [(self.next_decay, "decay"), (self.next_loyalty, "loyalty")]
            if self.effect is not None:
                due.append((self.effect[0], "tick"))
            at, what = min(due)
            if at > until:
                break
            self.now = at
            if what == "decay":
                self.happiness = max(0, self.happiness - self.DECAY)
                self.next_decay += self.DECAY_S
            elif what == "loyalty":
                self.loyalty += {pets.HAPPY: 20, pets.CONTENT: 10, pets.UNHAPPY: -20}[self.state()]
                if self.loyalty < 0:
                    # One time in three it leaves; the other two set it to 500 (`ModifyLoyalty`).
                    self.broken += 1
                    self.loyalty = 500
                self.next_loyalty += self.LOYALTY_S
            else:
                self.happiness = min(self.MAX, self.happiness + self.per_tick)
                self.ticks += 1
                self.effect[1] -= 1
                self.effect = None if self.effect[1] == 0 else [at + pets.FEED_TICK_S,
                                                                self.effect[1]]
        self.now = until

    def feed(self, at: float) -> None:
        self.run(at)
        self.eaten += 1
        ticks = round(pets.FEED_EFFECT_S / pets.FEED_TICK_S)
        self.effect = [at + pets.FEED_TICK_S, ticks]       # a running one is replaced


def _case() -> dict:
    return json.loads(FIXTURE.read_text())


def test_feed_pets_effect_is_ten_ticks_two_seconds_apart_from_the_world_database():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        trigger = db.execute("select EffectTriggerSpell1 from world_spell_template where Id = ?",
                             (pets.FEED_PET,)).fetchone()[0]
        aura, amplitude, misc, duration = db.execute(
            "select EffectApplyAuraName1, EffectAmplitude1, EffectMiscValue1, DurationIndex "
            "from world_spell_template where Id = ?", (pets.FEED_EFFECT,)).fetchone()
        ms = db.execute("select c1 from dbc_SpellDuration where id = ?", (duration,)).fetchone()[0]
    assert trigger == pets.FEED_EFFECT
    assert (aura, misc) == (24, 4), "a periodic energize of happiness (POWER_HAPPINESS 4)"
    assert amplitude / 1000 == pets.FEED_TICK_S and ms / 1000 == pets.FEED_EFFECT_S
    jerky = pets.foods()[JERKY]
    assert pets.benefit(9, jerky.item_level) == 35_000, "Tough Jerky, item level 5, to a level 9"


def test_the_recorded_feeds_a_second_apart_ate_170_jerky_and_left_the_pet_unhappy():
    case = _case()
    feeds = case["feeds"]
    assert len(feeds) == 170
    gaps = [b - a for a, b in zip(feeds, feeds[1:], strict=False)]
    assert sum(1 for g in gaps if g < pets.FEED_TICK_S) / len(gaps) > 0.98
    eaten = sum(max(0, a[5] - b[5]) for a, b in zip(case["states"], case["states"][1:],
                                                     strict=False)
                if a[5] is not None and b[5] is not None)
    assert eaten == 170, "every feed ate its jerky: the server took each one"
    assert {s[3] for s in case["states"]} == {pets.UNHAPPY}
    # Tamed at 166,500 at 10:36:49 and unfed: nothing left by 10:39:12 at 70,000 a minute.
    server = ServerPet(0, per_tick=pets.benefit(case["pet"]["level"], case["food_item_level"]))
    moments = sorted([(t, "feed") for t in feeds] + [(s[0], "state") for s in case["states"]])
    for t, what in moments:
        if what == "feed":
            server.feed(t)
        else:
            server.run(t)
            assert server.state() == pets.UNHAPPY, f"the strip read it unhappy at {t:.1f} s"
    server.run(180.0)
    assert server.eaten == 170
    assert server.ticks <= 10, "of 1,700 the jerky held, under one feed's worth was given"
    assert server.loyalty < 1000, "unhappy, its loyalty fell"


def _hunter(monkeypatch, server: ServerPet):
    """A hunter's body tending `server`'s pet on the test's clock: the policy asks `pet_due` at
    the recorded cadence (0.8 s), and an armed feed casts as the hive's body does."""
    clock = [0.0]
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: clock[0])
    b = _body(KNOWN)
    values = {**VALUES, "pet.has": True, "pet.dead": False, "pet.food_id": JERKY}

    def cast(spell, *, item=None):
        assert (spell, item) == (pets.FEED_PET, JERKY)
        server.feed(clock[0])
        return PetCast(True)

    b._pet_cast = cast
    b._read = lambda: {**values, "pet.happiness": server.state()}
    b.say = lambda line: None
    return b, clock


def _tend(b, clock, server, seconds: float, step: float = 0.8):
    """`seconds` of the policy's looks; the pet's state at each, and the feeds cast."""
    seen, fed = [], 0
    while clock[0] < seconds:
        server.run(clock[0])
        pet = Pet(has=True, dead=False, entry=3100, level=9, happiness=server.state(),
                  loyalty=1, food_id=JERKY, food_count=10, charmed=False)
        if b.pet_due(_state(pet=pet)) == "feed":
            _armed(b, "feed")
            assert b._pet(_state(pet=pet)).code == "fed"
            fed += 1
        seen.append(server.state())
        clock[0] += step
    return seen, fed


def test_a_feed_waits_out_its_effect_and_the_pet_is_kept_happy(monkeypatch):
    server = ServerPet(0)
    b, clock = _hunter(monkeypatch, server)
    seen, fed = _tend(b, clock, server, 30 * 60)
    assert fed <= 12, f"{fed} feeds in half an hour, against 170 in three minutes recorded"
    assert server.eaten == fed and server.ticks >= 10 * (fed - 1)
    assert seen.count(pets.UNHAPPY) / len(seen) < 0.03, "unhappy only until the first feeds tick"
    assert seen.count(pets.HAPPY) / len(seen) > 0.75
    assert server.broken == 0 and server.loyalty > 1000, "its loyalty grows: it stays"


def test_without_the_wait_the_recorded_storm_comes_back(monkeypatch):
    monkeypatch.setattr("jev.run.body.FEED_AGAIN_S", 0.0)
    server = ServerPet(0)
    b, clock = _hunter(monkeypatch, server)
    seen, fed = _tend(b, clock, server, 3 * 60)
    assert fed == len(seen), "a feed at every look, as recorded (170 in three minutes)"
    assert set(seen) == {pets.UNHAPPY} and server.ticks == 0
    seen, _ = _tend(b, clock, server, 15 * 60)
    assert server.broken >= 1, "its loyalty ran out within a quarter of an hour"


def test_the_wait_holds_while_the_strip_still_reads_it_unhappy(monkeypatch):
    server = ServerPet(0)
    b, clock = _hunter(monkeypatch, server)
    unhappy = Pet(has=True, dead=False, entry=3100, level=9, happiness=pets.UNHAPPY, loyalty=1,
                  food_id=JERKY, food_count=10, charmed=False)
    assert b.pet_due(_state(pet=unhappy)) == "feed"
    _armed(b, "feed")
    assert b._pet(_state(pet=unhappy)).code == "fed"
    clock[0] = pets.FEED_EFFECT_S - 0.1
    assert b.pet_due(_state(pet=unhappy)) is None, "its effect still runs"
    clock[0] = pets.FEED_EFFECT_S
    assert b.pet_due(_state(pet=unhappy)) == "feed"
    content = unhappy.model_copy(update={"happiness": pets.CONTENT})
    happy = unhappy.model_copy(update={"happiness": pets.HAPPY})
    assert b.pet_due(_state(pet=content)) == "feed", "kept happy, not only out of unhappiness"
    assert b.pet_due(_state(pet=happy)) is None


@pytest.mark.parametrize("gone", [{"dead": True}, {"has": False}])
def test_a_pet_dead_or_put_away_loses_the_effect_and_is_fed_again_when_out(monkeypatch, gone):
    server = ServerPet(0)
    b, clock = _hunter(monkeypatch, server)
    pet = Pet(has=True, dead=False, entry=3100, level=9, happiness=pets.UNHAPPY, loyalty=1,
              food_id=JERKY, food_count=10, charmed=False)
    _armed(b, "feed")
    b._pet(_state(pet=pet))
    assert b.pet_due(_state(pet=pet)) is None
    clock[0] = 5.0
    b.pet_due(_state(pet=pet.model_copy(update=gone)))
    assert b.pet_due(_state(pet=pet)) == "feed"


def test_a_feed_the_server_refused_starts_no_wait(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("jev.run.body.time.monotonic", lambda: clock[0])
    b = _body(KNOWN)
    b._pet_cast = lambda spell, *, item=None: PetCast(True, codes=(0x82,))
    b._read = lambda: {**VALUES, "pet.has": True, "pet.dead": False, "pet.food_id": JERKY,
                       "pet.happiness": pets.UNHAPPY}
    b.say = lambda line: None
    pet = Pet(has=True, dead=False, level=9, happiness=pets.UNHAPPY, food_id=JERKY,
              food_count=10, charmed=False)
    _armed(b, "feed")
    assert b._pet(_state(pet=pet)).outcome is SkillOutcome.ABORTED
    assert b.pet_due(_state(pet=pet)) == "feed", "nothing was eaten: no effect runs"
    sent_none = PetCast(False, detail="the live strip paints no pet")
    b._pet_cast = lambda spell, *, item=None: sent_none
    assert b._pet(_state(pet=pet)).outcome is SkillOutcome.ABORTED
    assert b.pet_due(_state(pet=pet)) == "feed"
