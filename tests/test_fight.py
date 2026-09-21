"""Killing one unit: select by Tab, face by clicking, and do not claim a kill."""

from __future__ import annotations

import inspect
import time

from jev.clients.fight import (
    DEAD_HP,
    FLEE_HP,
    HEAL_GIVE_UP,
    LOST_HP,
    MAX_CLOSE_BURSTS,
    MAX_SELECTS,
    MIN_START_HP,
    REAIM_AFTER_S,
    REAIM_EVERY,
    SLOT_KEYS,
    Fight,
    Fought,
)
from jev.world.combat import GENERIC, Ability, Role, for_class


class _Hid:
    def __init__(self):
        self.taps = []
        self.clicks = []
        self.holds = []

    def hold(self, key, seconds, **_):
        self.holds.append((key, seconds))
        return True

    def tap(self, key):
        self.taps.append(key)
        return True

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))


A_FRAME = object()   # stands in for pixels; `find` is not what these tests exercise


def _fight(frames, hid=None, frame=A_FRAME):
    """`frames` is a list of decoded readings, served one per read."""
    seq = list(frames)
    state = {"i": 0}

    def read():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    return Fight(hid=hid or _Hid(), read=read, read_frame=lambda: frame,
                 window_origin=(10, 38))


ALIVE = {"target.has": True, "target.hp": 1.0, "target.name_id": 1161,
         "bars.ready": 0b111, "bars.usable": 0b111, "bars.gcd": 0.0,
         "bars.casting": False, "char.class_id": 2, "vitals.dead": False}


def test_selection_is_tab_and_never_chat():
    """A bot that can talk is a bot that can say the wrong thing — see `interact`. Tab is
    a keybind, the client picks the nearest attackable unit, and the radio says what
    answered."""
    src = inspect.getsource(Fight)
    assert "slash(" not in src and "type_text(" not in src
    hid = _Hid()
    assert _fight([ALIVE], hid=hid).select(1161) is None
    assert hid.taps == ["tab"]


def test_a_corpse_is_not_a_fight():
    """Corpses stay selectable. Tabbing onto one and 'fighting' it burns the timeout."""
    hid = _Hid()
    dead = {**ALIVE, "target.hp": 0.0}
    assert _fight([dead, dead, dead, dead], hid=hid).select(None) is Fought.NO_TARGET
    assert hid.taps == ["tab"] * MAX_SELECTS


def test_the_wrong_unit_costs_a_selection_and_tab_moves_on():
    hid = _Hid()
    other = {**ALIVE, "target.name_id": 999}
    f = _fight([other, other, ALIVE], hid=hid)
    assert f.select(1161) is None
    assert hid.taps == ["tab"] * 3, "gave up, or accepted the wrong unit"


def test_engaging_refuses_when_the_unit_cannot_be_seen():
    """2.4.3 has no facing API — `GetPlayerFacing` is 3.0 and `pos.facing` reads None on
    every live frame — so clicking the model is the only way to aim the character. No
    sighting means no way to face it, and swinging anyway hits whatever the camera is
    pointed at."""
    hid = _Hid()
    f = Fight(hid=hid, read=lambda: ALIVE, read_frame=lambda: None)
    assert f.engage() is False
    assert hid.clicks == []
    assert "face" in f.detail


def test_a_vanishing_target_at_full_health_is_not_a_kill():
    """Vanishing is one observation with two causes. Claiming the kill is how a counter
    that never moves looks like a working fight."""
    f = _fight([ALIVE, {**ALIVE, "target.has": False}])
    f.last_hp = 1.0
    assert f._settle() is Fought.LOST
    f.last_hp = 0.0
    assert f._settle() is Fought.KILLED
    assert LOST_HP > DEAD_HP


def test_the_rotation_respects_the_global_cooldown_and_casting():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "bars.gcd": 0.7})
    f._rotate({**ALIVE, "bars.casting": True})
    assert hid.taps == [], "pressed through the GCD or through a cast"
    f._rotate(ALIVE)
    assert hid.taps == ["2"], "a lapsed buff outranks a swing"


def test_a_slot_the_client_says_is_not_ready_is_not_pressed():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "bars.ready": 0b101})     # the buff in slot 2 is on cooldown
    assert hid.taps == ["1"], "ignored bars.ready"


def test_a_seal_is_not_re_pressed_every_tick():
    """`bars.ready` says a self-buff is pressable on every single tick, so without a
    worth-pressing interval the character stands there re-sealing and never swings. The
    interval is the spell's own duration less a margin, read from the world database."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate(ALIVE)
    f._rotate(ALIVE)
    assert hid.taps == ["2", "1"], "the seal was re-pressed while still up"
    seal = for_class(2, 1).first(Role.BUFF)
    assert seal is not None and seal.every_s == 25.0   # 30s duration, 5s margin


def test_melee_auto_attack_is_a_toggle_and_is_pressed_once():
    """Spell 6603 toggles the swing: pressing it while already swinging stops it. The
    first live rotation pressed `[1, 2, 2, ..., 1, 2, ...]` and turned the character's
    attack on and off all fight."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    for _ in range(4):
        f._rotate(ALIVE)
    assert hid.taps.count("1") == 1, "auto-attack was toggled more than once"
    attack = for_class(2, 1).first(Role.ATTACK)
    assert attack is not None and attack.toggle


def test_a_toggle_is_not_pressed_once_damage_is_already_landing():
    """Health coming off the target means the swing is already going; pressing the toggle
    then is how it stops."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.last_hp = 0.6
    for _ in range(3):
        f._rotate(ALIVE)
    assert "1" not in hid.taps


def test_healing_is_a_role_not_a_class():
    """No `if paladin`. The engine asks for a row with role=heal and presses it if the
    bars say it is ready; a warrior is the same list with one fewer row."""
    import inspect

    # Code, not prose: the docstrings name paladins on purpose, to say why there is no
    # module for them.
    code = "".join(ln.split("#")[0] for ln in inspect.getsource(Fight).splitlines()
                   if not ln.strip().startswith(("#", '"', "'")))
    for branch in ("class_id ==", "class_id in", "PaladinHeal", '== "paladin"'):
        assert branch not in code, f"{branch}: the engine is branching on class"

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f._rotate({**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
               "vitals.power": 0.9, "vitals.power_max": 100})
    assert hid.taps == ["3"], "stood there at 20% with a heal on the bar"
    assert for_class(1, 1).first(Role.HEAL) is None, "a warrior grew a heal"


def test_a_heal_is_not_started_without_the_mana_to_finish_it():
    """Out of mana falls through to swinging, and the caller falls through to food, a
    vendor, or breaking off. There is deliberately no drinking inside a fight."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    dying = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
             "vitals.power": 0.02, "vitals.power_max": 100}
    f._rotate(dying)
    assert "3" not in hid.taps


def test_a_heal_is_confirmed_by_the_client_not_by_having_tapped_a_key():
    """A press the client ignored looks identical to one that worked if nobody checks,
    and what it hides is a picker predicate that never fires."""
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt])
    f._rotate(hurt)
    assert f._pending_heal is not None
    f._watch_heal({**hurt, "vitals.hp": 0.5}, hurt["bars.ready"])
    assert f.heals_landed == 1 and f.heals_ignored == 0

    f2 = _fight([hurt])
    f2._rotate(hurt)
    f2._pending_heal = (0.2, time.monotonic() - 5.0)
    f2._watch_heal(hurt, hurt["bars.ready"])
    assert f2.heals_ignored == 1, "a press nothing happened after was counted as a heal"


def test_slot_keys_cover_the_bar_including_food_and_water():
    """10-12 are `0`, `minus`, `equals` — and 11 and 12 are where a fresh character's food
    and water sit, so getting them wrong is not academic."""
    assert SLOT_KEYS[9] == "9" and SLOT_KEYS[10] == "0"
    assert SLOT_KEYS[11] == "minus" and SLOT_KEYS[12] == "equals"
    assert len(SLOT_KEYS) == 12


def test_a_class_with_no_profile_still_fights():
    assert for_class(None) is GENERIC
    assert for_class(2).name == "paladin"
    assert all(isinstance(a, Ability) for a in for_class(7).abilities)


# -- not dying ----------------------------------------------------------------------

def test_a_fight_is_not_started_on_low_health():
    """Both live deaths were pulls taken at health that could not absorb a mistake."""
    hurt = {**ALIVE, "vitals.hp": 0.2}
    f = _fight([hurt])
    assert f.run(timeout_s=1) is Fought.TOO_HURT
    assert MIN_START_HP > FLEE_HP


def test_a_losing_fight_is_broken_off_rather_than_finished():
    """A fight is usually lost several seconds before the character falls over, and
    finishing it standing up costs a two-hundred-yard corpse run."""
    sinking = {**ALIVE, "vitals.hp": 0.1}
    f = _fight([ALIVE, sinking])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True
    assert f.run(timeout_s=5) is Fought.LOSING
    assert "broke off" in f.detail


def test_an_isolated_nameplate_is_preferred_over_a_central_one():
    """A pull in the middle of a camp is what killed this character twice, and a plate
    with no neighbour is the best evidence available that a mob has none either."""
    from jev.perceive.units import Plate, RingColour

    def plate(cx):
        return Plate(cx=cx, cy=300.0, w=140, colour=RingColour.YELLOW)

    crowd_a, crowd_b, alone = plate(790.0), plate(830.0), plate(1400.0)
    f = Fight(hid=_Hid(), read=lambda: ALIVE,
              read_frame=lambda: None, window_centre_x=800)
    import jev.clients.fight as mod

    real, mod.find_plates = mod.find_plates, lambda _f: [crowd_a, crowd_b, alone]
    try:
        assert f._candidates(object())[0] is alone, "picked the one with company"
    finally:
        mod.find_plates = real


def test_resting_stops_for_combat_and_says_when_there_is_no_food():
    from jev.clients.rest import Rest, Rested

    hid = _Hid()
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 0.4, "vitals.combat": True}).until() \
        is Rested.INTERRUPTED
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 0.4, "vitals.combat": False,
                                       "bars.usable": 0b111}).until() is Rested.NO_FOOD
    assert Rest(hid=hid, read=lambda: {"vitals.hp": 1.0}).until() is Rested.HEALTHY


def test_walking_at_it_and_swinging_at_it_are_the_same_loop():
    """Closing used to be a gate: walk until the target takes damage, *then* start the
    rotation. Damage comes from swinging, swinging is the rotation, and the rotation was
    behind the gate — so a live run reported `unreachable pressed [] closed 8` eight
    times over, having walked at a kobold without once pressing anything at it."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True
    f.run(timeout_s=2)
    assert f.pressed, "walked at it and never swung"
    assert f.closed > 0, "swung at it and never closed"


def test_a_target_that_never_takes_damage_is_given_up_not_waited_out():
    """Out of bursts with the target still at full health is the answer already; more
    seconds cannot improve on it.

    Whether anything was *pressed* says nothing about whether it was reached — a seal
    lands on the character, not on the kobold. Requiring "pressed nothing" here let two
    live fights walk eight bursts and then stand in the rotation for the full forty-five
    seconds: `pressed [2, 1, 2] closed 8`, twice."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)          # rotation is live; it will press the seal
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True
    f.closed = MAX_CLOSE_BURSTS
    assert f.run(timeout_s=5) is Fought.UNREACHABLE
    assert "cannot reach" in f.detail
    assert f.pressed, "this is the case where it presses and still cannot reach"


def test_in_combat_the_name_filter_loosens_but_does_not_come_off():
    """Something already hitting us does not have to be the quest mob - a Kobold Worker
    beat this character to 27% while every attempt refused to fight anything but a Kobold
    Vermin. But dropping the name entirely made it attack a Timber Wolf that was minding
    its own business, so the wanted name is still passed and `defend` is what loosens
    it."""
    seen = []
    f = _fight([{**ALIVE, "vitals.combat": True, "target.has": False}])
    f.acquire = lambda name_id, **kw: seen.append((name_id, kw.get("defend"))) \
        or Fought.NO_TARGET
    f.run(1161, timeout_s=1)
    assert seen == [(1161, True)], "forgot what it came for, or refused to defend itself"

    seen.clear()
    f2 = _fight([{**ALIVE, "vitals.combat": False, "target.has": False}])
    f2.acquire = lambda name_id, **kw: seen.append((name_id, kw.get("defend"))) \
        or Fought.NO_TARGET
    f2.run(1161, timeout_s=1)
    assert seen == [(1161, False)], "picked a fight with something that was not the mob"


def test_a_guard_does_not_freeze_a_fight_already_started():
    """Breaking off is only a choice when nothing is hitting us. In combat it is not a
    choice, it is standing still: this returned LOSING on the first iteration, before the
    rotation, so the caller rested, was interrupted because something was attacking,
    tried again, and got LOSING again - eight times, pressing nothing, while health went
    29, 27, 21, 18, 18, 15, 9, 6, dead."""
    hid = _Hid()
    sinking = {**ALIVE, "vitals.hp": 0.1, "vitals.combat": True}
    f = _fight([sinking], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True
    f.run(timeout_s=1)
    assert f.pressed, "stood at 10% health in combat and pressed nothing"

    # Out of combat it is a real choice, and still taken.
    free = {**ALIVE, "vitals.hp": 0.1, "vitals.combat": False}
    g = _fight([ALIVE, free])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda: True
    assert g.run(timeout_s=5) is Fought.LOSING


def test_a_heal_is_not_pressed_again_until_the_last_one_answers():
    """Holy Light is a 2.5 second cast. A live fight pressed it fifteen times in a row,
    none of which healed anything, because nothing stopped the next tick from pressing
    again."""
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    for _ in range(5):
        f._rotate(hurt)
    assert hid.taps.count("3") == 1, "spammed the heal without waiting for an answer"

    # Once it answers, the next one is allowed.
    f._watch_heal({**hurt, "vitals.hp": 0.5}, hurt["bars.ready"])
    f._rotate(hurt)
    assert hid.taps.count("3") == 2


def test_closing_re_aims_because_only_a_click_turns_the_character():
    """A right-click is the only thing that turns this character, and it happens once,
    before the walking starts. When it misses - no ring, so the click went below the
    nameplate and landed on grass - nothing faces the target and `W` walks the old
    heading for every burst after it. Six fights in one run reported `closed 8` and
    landed nothing."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    engages = []
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: engages.append(1) or True
    f.run(timeout_s=6)
    assert f.closed >= REAIM_EVERY, "did not close far enough to need re-aiming"
    assert len(engages) > 1, "walked the whole way without ever re-aiming"


def test_a_heal_that_never_lands_is_dropped_for_the_rest_of_the_fight():
    """The confirmation exists to be acted on. Three live runs reported heals 0/4, 0/5
    and 0/5 - a two and a half second cast on a level 1 paladin being hit in a camp does
    not complete, and every attempt costs a global cooldown not spent swinging.

    Per fight, not forever: at a level where the cast finishes it lands, and nothing here
    has to know which level that is."""
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    for _ in range(HEAL_GIVE_UP):
        f._rotate(hurt)
        f._pending_heal = (0.2, time.monotonic() - 5.0)
        f._watch_heal(hurt, hurt["bars.ready"])
    assert f.heals_ignored == HEAL_GIVE_UP and f.heals_landed == 0

    before = list(hid.taps)
    f._rotate(hurt)
    assert hid.taps.count("3") == before.count("3"), "kept casting a heal that never lands"
    assert hid.taps != before, "gave up on the heal and then did nothing at all"


def test_a_heal_that_does_land_is_not_dropped():
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    f._rotate(hurt)
    f._watch_heal({**hurt, "vitals.hp": 0.6}, hurt["bars.ready"])
    f.heals_ignored = HEAL_GIVE_UP          # some missed, but one landed
    f._rotate(hurt)
    assert hid.taps.count("3") == 2


def test_giving_up_on_a_heal_outlives_the_fight_it_was_learned_in():
    """Scoping the tally per run() meant re-learning it every engagement: a live run
    pressed `[3, 2, 3]` and burned two more global cooldowns discovering again that a
    heal it had already abandoned twice does not land."""
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    f.heals_ignored, f.heals_landed = HEAL_GIVE_UP, 0
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True

    f.run(timeout_s=1)
    assert "3" not in f.pressed_keys(), "a new fight forgot what the last one proved"


def test_a_landed_heal_clears_the_give_up():
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.2, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    f.heals_ignored, f.heals_landed = 5, 1
    f._rotate(hurt)
    assert hid.taps.count("3") == 1, "abandoned a heal that does land"


# -- topping up between fights ------------------------------------------------------

def test_topping_up_is_out_of_combat_only():
    """In a fight a heal is a global cooldown not spent swinging, and it cannot finish
    under pushback. This is the other situation entirely."""
    f = _fight([{**ALIVE, "vitals.hp": 0.4, "vitals.combat": True}])
    assert f.top_up() is False
    assert f.top_ups == 0, "cast a top-up while something was hitting us"


def test_the_in_combat_give_up_does_not_silence_the_top_up():
    """Heals that fail to pushback say nothing about one cast standing still. Letting
    the in-combat tally gate this would be the wrong lesson learned twice."""
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.4, "vitals.combat": False,
            "vitals.power": 0.9, "vitals.power_max": 100}
    healed = {**hurt, "vitals.hp": 0.95}
    f = _fight([hurt, hurt, healed, healed], hid=hid)
    f.heals_ignored, f.heals_landed = 5, 0      # given up on, in combat
    assert f.top_up(settle_s=2.0) is True
    assert hid.taps.count("3") == 1
    assert f.top_ups_landed == 1


def test_a_top_up_that_does_not_land_falls_through_rather_than_repeating():
    hid = _Hid()
    hurt = {**ALIVE, "vitals.hp": 0.4, "vitals.combat": False,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt], hid=hid)
    assert f.top_up(settle_s=0.8) is False
    assert f.top_ups == 1 and f.top_ups_landed == 0, "kept casting into nothing"


def test_the_out_of_combat_band_is_much_higher_than_the_in_combat_one():
    from jev.world.combat import HEAL_IN_COMBAT, HEAL_OUT_OF_COMBAT

    assert HEAL_OUT_OF_COMBAT >= 0.75
    assert HEAL_OUT_OF_COMBAT > HEAL_IN_COMBAT * 1.5


# -- watched live ------------------------------------------------------------------

def test_a_bystander_is_not_attacked_just_because_we_are_in_combat():
    """Watched live: after killing a kobold the character attacked a Timber Wolf that was
    minding its own business. Dropping the name filter entirely in combat was too much -
    what it is for is self-defence, and `target.attacking_me` says outright whether
    something is hitting us."""
    wolf = {**ALIVE, "target.name_id": 999, "target.attacking_me": False,
            "vitals.combat": True}
    f = _fight([wolf])
    assert f._acceptable(1161, defend=True) is False, "attacked a bystander"

    biting = {**wolf, "target.attacking_me": True}
    g = _fight([biting])
    assert g._acceptable(1161, defend=True) is True, "refused to defend itself"

    # Out of combat the filter is absolute: nothing gets attacked for being nearby.
    h = _fight([biting])
    assert h._acceptable(1161, defend=False) is False


def test_the_quest_mob_is_still_preferred_while_defending():
    kobold = {**ALIVE, "target.name_id": 1161, "target.attacking_me": False}
    f = _fight([kobold])
    assert f._acceptable(1161, defend=True) is True


def test_it_re_aims_when_the_target_stops_taking_damage():
    """Watched live: getting attacked and not retaliating. Re-aiming stopped the moment
    the first hit landed, so a fight that went wrong - the kobold walked round us,
    something pulled us sideways - left the character swinging at empty air.

    Health coming off the target is the only evidence it is still pointed at, so the
    absence of that is what triggers a re-aim."""
    hid = _Hid()
    engages = []
    f = _fight([{**ALIVE, "target.hp": 0.6}], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: engages.append(1) or True
    f.last_hp = 0.6                       # damage has landed; closing is over
    # Real time, because `run` starts the damage clock itself. The target's health never
    # moves in this frame, which is the whole point.
    f.run(timeout_s=REAIM_AFTER_S + 1.5)
    assert len(engages) > 1, "never turned back towards a target it had stopped hitting"


def test_it_stops_walking_once_the_target_is_within_reach():
    """Watched live: "we target and try to attack but then just keep running forwards and
    passed them". Closing ended only when the target lost health, so a character facing
    slightly wrong walked through the kobold and out the other side, still holding W, for
    all eight bursts.

    `target.in_melee` is too loose to prove we can hit something - it is about eleven
    yards - but exact enough to prove we should stop running at it."""
    hid = _Hid()
    near = {**ALIVE, "target.in_melee": True}
    f = _fight([near], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda: True
    f.run(timeout_s=1.5)
    assert not hid.holds, "kept running at something it was already standing next to"

    far = {**ALIVE, "target.in_melee": False}
    g = _fight([far], hid=hid)
    g.acquire = lambda name_id, **_: None
    g.engage = lambda: True
    g.run(timeout_s=1.5)
    assert hid.holds, "never closed on something out of reach"
