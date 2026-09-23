"""Killing one unit: select, face by turning, swing on observed state, never claim a kill."""

from __future__ import annotations

import inspect
import time
from itertools import pairwise

import pytest

from jev.clients.fight import (
    CLOSE_BURST_S,
    CLOSE_NUDGE_S,
    CLOSE_STEP_S,
    DEAD_HP,
    FLEE_HP,
    HEAL_GIVE_UP,
    MAX_CLOSE_BURSTS,
    MAX_SELECTS,
    MIN_START_HP,
    REAIM_AFTER_S,
    SLOT_KEYS,
    TOGGLE_SETTLE_S,
    Fight,
    Fought,
)
from jev.clients.targeting import (
    ClickCode,
    ClickResult,
    FaceCode,
    FaceResult,
    HoverCode,
    HoverResult,
    PaintCode,
    PaintResult,
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
        return True


A_FRAME = object()   # shared Targeting has its own pixel/hover tests


FACED = FaceResult(FaceCode.FACED, "selected plate on the centre line", 0.01, None, 1, 0.1)


class _Targeting:
    """The shared facing boundary, independent of perception and radio sequencing."""

    def __init__(self, read, hid, face=None, action=None):
        self.read, self.hid = read, hid
        self.face = face or FACED
        self.action = action or ClickResult(ClickCode.CLICKED, (710, 438), "delivered", 1)
        self.faces = []        # facing requests
        self.hover_name = 1161
        self.requests = []     # living-unit click requests (Interact)
        self.paints = 0

    def wait_for_paint(self):
        self.paints += 1
        after = self.read()
        code = PaintCode.FRESH if after is not None else PaintCode.BLIND
        return PaintResult(code, None, after, "observed post-selection paint")

    def face_selected(self, **request):
        self.faces.append(request)
        return self.face

    def probe(self, point, require_target=True):
        """Every plate is the wanted unit's, unless a test says otherwise."""
        after = {"cursor.has": True, "cursor.dead": False,
                 "cursor.name_id": self.hover_name, "cursor.world": True}
        return HoverResult(HoverCode.OTHER, point, None, after, "fixture hover")

    def click_selected(self, **request):
        self.requests.append(request)
        if self.action.delivered:
            assert self.hid.click(*self.action.point, right=True) is True
        return self.action

    def click_corpse(self, **request):
        return self.click_selected(**request)


def _fight(frames, hid=None, frame=A_FRAME):
    """`frames` is a list of decoded readings, served one per read."""
    seq = list(frames)
    state = {"i": 0}

    def read():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    hid = hid or _Hid()
    return Fight(hid=hid, read=read, read_frame=lambda: frame,
                 window_origin=(10, 38), targeting=_Targeting(read, hid))


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


def test_engaging_refuses_when_the_unit_cannot_be_faced():
    """2.4.3 has no facing API, and a right-click does not turn the character either:
    measured with auto-attack on and the wolf at the character's side. Facing is turning
    until the unit's own plate is centred; no plate after the search means no way to face
    it, and walking anyway walks whatever heading the character happens to have."""
    hid = _Hid()
    targeting = _Targeting(lambda: ALIVE, hid,
        FaceResult(FaceCode.NOT_VISIBLE, "no plate for the selected unit", turns=0))
    f = Fight(hid=hid, read=lambda: ALIVE, read_frame=lambda: None, targeting=targeting)
    assert f.engage() is False
    assert hid.clicks == [] and hid.holds == [] and hid.taps == []
    assert "no plate" in f.detail
    assert f._aim_failure() is Fought.NOT_VISIBLE


def test_engaging_never_clicks_the_body_it_turns_and_starts_the_swing():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    assert f.engage() is True
    assert hid.clicks == [], "aimed by clicking, which does not turn the character"
    assert hid.taps == ["1"], "faced the unit and never started swinging"
    assert f.targeting.faces == [{"expected_name_id": None, "hint": None, "search_s": 0.6}]


def test_a_vanishing_target_at_full_health_is_not_a_kill():
    """Vanishing is one observation with two causes. Claiming the kill is how a counter
    that never moves looks like a working fight."""
    f = _fight([ALIVE, {**ALIVE, "target.has": False}])
    f.last_hp = 1.0
    assert f._settle() is Fought.LOST
    f.last_hp = 0.0
    assert f._settle() is Fought.KILLED
    assert DEAD_HP == 0


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
    attack on and off all fight. Without an observed state, one press per fight."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    for _ in range(4):
        f._rotate(ALIVE)
    assert hid.taps.count("1") == 1, "auto-attack was toggled more than once"
    attack = for_class(2, 1).first(Role.ATTACK)
    assert attack is not None and attack.toggle


def test_an_observed_swing_is_never_toggled_off():
    """The right-click's attack was switched straight back off by the rotation's own
    press. With the radio's state the toggle is pressed only when it is observed off."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    swinging = {**ALIVE, "bars.attacking": True, "bars.ready": 0b101}
    for _ in range(4):
        f._rotate(swinging)
    assert "1" not in hid.taps


def test_an_observed_stopped_swing_is_restarted_after_the_paint_settles(combat_clock):
    """A new target, or a swing the client stopped, is observed off and started again -
    but never inside the window where the radio has not painted the last press."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    stopped = {**ALIVE, "bars.attacking": False, "bars.ready": 0b101}
    f._rotate(stopped)
    f._rotate(stopped)
    assert hid.taps.count("1") == 1, "pressed again before the press could be painted"
    combat_clock[0] += TOGGLE_SETTLE_S + 0.1
    f._rotate(stopped)
    assert hid.taps.count("1") == 2, "an observed stopped swing was never restarted"


def test_a_toggle_is_not_pressed_once_damage_is_already_landing():
    """Health coming off the target means the swing is already going; pressing the toggle
    then is how it stops."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.last_hp = 0.6
    f._damage_seen = True
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


def test_broken_equipment_is_reported_but_never_refused():
    """A weapon at zero durability is an unequipped weapon: the paladin swung its fists
    for 4-5 damage, every pull ran the full 45 seconds, and each death took another 10%
    off everything else. `bags.durability_min` read 0.0 for the whole evening and nothing
    looked at it, so the logs blamed the camp, the health band and the target picker.

    Refusing to fight on it was the wrong correction. At zero there is no durability left
    for a death to cost, so the refusal protects nothing - and it deadlocks a character
    that is both broken and broke, because fighting is the only way to the money. The
    state is reported; going to a merchant is the loop's decision, and it needs money to
    make it."""
    hurt_and_broken = {**ALIVE, "vitals.hp": 0.2, "bags.durability_min": 0.0}
    f = _fight([hurt_and_broken])
    assert f.run(timeout_s=1) is Fought.TOO_HURT, "broken pre-empted a health guard"
    assert f.broken is True, "nothing would know the gear is broken"

    for durability in (0.01, 1.0, None):
        hurt = {**ALIVE, "vitals.hp": 0.2, "bags.durability_min": durability}
        f = _fight([hurt])
        assert f.run(timeout_s=1) is Fought.TOO_HURT, durability
        assert f.broken is False, durability


def test_a_losing_fight_is_broken_off_rather_than_finished():
    """A fight is usually lost several seconds before the character falls over, and
    finishing it standing up costs a two-hundred-yard corpse run."""
    sinking = {**ALIVE, "vitals.hp": 0.1}
    f = _fight([ALIVE, sinking])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
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
    f.engage = lambda *_: True
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
    f.engage = lambda *_: True
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
    f.engage = lambda *_: True
    f.run(timeout_s=1)
    assert f.pressed, "stood at 10% health in combat and pressed nothing"

    # Out of combat it is a real choice, and still taken.
    free = {**ALIVE, "vitals.hp": 0.1, "vitals.combat": False}
    g = _fight([ALIVE, free])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
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


def test_every_stride_is_preceded_by_facing():
    """`W` walks whatever heading the character has, so a unit that moves - or a first
    turn that fell short - is walked past. Six fights in one run reported `closed 8` and
    landed nothing, walking a heading nothing had set."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    engages = []
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: engages.append(1) or True
    f.run(timeout_s=6)
    assert f.closed > 1, "did not stride at all"
    assert len(engages) == f.closed + 1, "strode without facing first"


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
    f.engage = lambda *_: True

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
    f = _fight([ALIVE, {**ALIVE, "target.hp": 0.6}], hid=hid)
    def acquired(name_id, **_):
        f._damage_mark = 1.0  # health at confirmed selection, before the observed drop
    f.acquire = acquired
    f.engage = lambda *_: engages.append(1) or True
    # Real time, because `run` starts the damage clock itself. The target's health never
    # moves in this frame, which is the whole point.
    f.run(timeout_s=REAIM_AFTER_S + 1.5)
    assert len(engages) > 1, "never turned back towards a target it had stopped hitting"
    assert f.hid.holds == [], "observed damage already ended closing"


def test_it_creeps_the_last_yards_rather_than_stopping_or_charging_through():
    """Two live failures, opposite directions. Running until the target lost health went
    straight through the kobold and out the other side. Then stopping at
    `target.in_melee` parked the character three quarters of the way there and left it
    standing - that flag is CheckInteractDistance index 3, about eleven yards, and a
    melee swing needs five.

    So the stride shortens instead of ending."""
    hid = _Hid()
    near = {**ALIVE, "target.in_melee": True}
    f = _fight([near], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    f.run(timeout_s=1.5)
    assert hid.holds, "stood still eleven yards from something it needed to be five from"
    assert all(secs == CLOSE_NUDGE_S for _k, secs in hid.holds), "charged through it"

    far = {**ALIVE, "target.in_melee": False}
    g = _fight([far], hid=hid)
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=1.5)
    assert any(secs == CLOSE_BURST_S for _k, secs in g.hid.holds), "crept from far away"


def test_the_attack_actions_range_check_ends_closing_exactly():
    """Schema 9 paints the Attack action's own range check. In reach, the character
    stands and swings; near but out of reach, it steps rather than nudges."""
    hid = _Hid()
    reach = {**ALIVE, "target.in_melee": True, "target.melee_range": True}
    f = _fight([reach], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    f.run(timeout_s=1.0)
    assert hid.holds == [], "walked while a swing already reached"
    assert f.pressed, "stood in reach and never swung"

    step = {**ALIVE, "target.in_melee": True, "target.melee_range": False}
    g = _fight([step])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=1.0)
    assert g.hid.holds and all(secs == CLOSE_STEP_S for _k, secs in g.hid.holds)


def test_a_new_facing_error_turns_back_to_the_target():
    """The client says "facing the wrong way" on a swing; that is an immediate re-face,
    not three and a half seconds of swinging at air."""
    engages = []
    first = {**ALIVE, "target.melee_range": True, "ui.error_count": 3, "ui.error_last": 0}
    wrong = {**first, "ui.error_count": 4, "ui.error_last": 3}      # 3 = not_facing
    f = _fight([first, first, wrong, wrong])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: engages.append(1) or True
    f.run(timeout_s=0.9)
    assert len(engages) == 2, "a facing error did not re-face, or an old one did"
    assert f.hid.holds == []


def test_an_unfaced_unit_is_never_walked_at_or_clicked():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.targeting.face = FaceResult(FaceCode.UNSETTLED, "plate still off centre", 0.2)
    assert f.run(timeout_s=1) is Fought.NOT_VISIBLE
    assert hid.clicks == [] and hid.holds == []
    assert f.targeting.faces == [{"expected_name_id": None, "hint": None, "search_s": 0.6}]


@pytest.mark.parametrize("last_hp", [None, 1.0, 0.03])
def test_unknown_or_low_health_followed_by_target_loss_is_not_a_kill(last_hp):
    f = _fight([ALIVE])
    f.last_hp = last_hp
    assert f._settle() is Fought.LOST


def test_refused_tab_cannot_accept_a_preexisting_matching_target():
    hid = _Hid()
    hid.tap = lambda _: False
    f = _fight([ALIVE], hid=hid)
    assert f.select(1161) is Fought.REFUSED
    assert f.targeting.paints == 0
    assert not f.targeting.faces


def test_refused_plate_selection_cannot_accept_a_preexisting_matching_target(monkeypatch):
    from jev.perceive.units import Plate, RingColour

    hid = _Hid()
    hid.click = lambda *_, **__: False
    f = _fight([ALIVE], hid=hid)
    monkeypatch.setattr(f, "_candidates", lambda _: [Plate(700, 300, 140, RingColour.YELLOW)])
    assert f.acquire(1161) is Fought.REFUSED
    assert f.targeting.paints == 0
    assert not f.targeting.faces


def test_refused_refacing_stops_before_any_following_movement():
    near = {**ALIVE, "vitals.combat": True, "target.in_melee": True}
    f = _fight([near])
    results = iter([FACED, FaceResult(FaceCode.REFUSED, "turn input refused")])
    f.targeting.face_selected = lambda **_: next(results)
    assert f.run(timeout_s=1) is Fought.REFUSED
    assert f.hid.holds == []
    assert f.closed == 0


def test_initially_injured_target_is_not_evidence_of_our_landed_damage():
    f = _fight([{**ALIVE, "target.hp": 0.6}])
    f.last_hp = 0.6
    f._rotate({**ALIVE, "target.hp": 0.6, "bars.ready": 0b101})
    assert f.hid.taps == ["1"]
    assert not f._damage_seen


def test_a_fight_at_constant_injured_health_still_closes_and_starts_its_attack():
    f = _fight([{**ALIVE, "target.hp": 0.6, "vitals.combat": True}])
    assert f.run(timeout_s=0.6) is Fought.TIMEOUT
    assert f.hid.holds
    assert "1" in f.hid.taps
    assert not f._damage_seen


def test_selection_cancellation_propagates():
    from jev.run.supervisor import Cancelled

    f = _fight([ALIVE])
    def cancelled():
        raise Cancelled("stop requested")
    f.targeting.wait_for_paint = cancelled
    with pytest.raises(Cancelled, match="stop requested"):
        f.select(1161)


@pytest.fixture
def combat_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("jev.clients.fight.time.monotonic", lambda: now[0])
    monkeypatch.setattr("jev.clients.fight.time.sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.mark.parametrize("phase", ["wait_for_paint", "face_selected"])
def test_acquisition_and_initial_verification_do_not_spend_the_fight_timeout(combat_clock, phase):
    """Slow selection/body verification must still leave time for a first attack."""
    f = _fight([ALIVE], frame=None)
    original = getattr(f.targeting, phase)
    calls = []
    delay = 5.0

    def slow_first_call(*args, **kwargs):
        if not calls:
            combat_clock[0] += delay
        calls.append(combat_clock[0])
        return original(*args, **kwargs)

    setattr(f.targeting, phase, slow_first_call)
    assert f.run(timeout_s=0.6) is Fought.TIMEOUT
    assert f.hid.holds and f.pressed, "acquisition consumed the budget before any attack"
    assert combat_clock[0] >= delay + 0.6


@pytest.mark.parametrize("timeout_s", [None, 15.0])
def test_reaim_cadence_does_not_extend_the_configured_fight_timeout(combat_clock, timeout_s):
    """A hit then a stall earns spaced re-aims within the existing fight deadline."""
    started = {**ALIVE, "vitals.combat": True}
    stalled = {**started, "target.hp": 0.6}
    f = _fight([started, stalled])
    original = f.targeting.face_selected
    aimed_at = []

    def measured_action(**kwargs):
        aimed_at.append(combat_clock[0])
        return original(**kwargs)

    f.targeting.face_selected = measured_action
    configured = 45.0 if timeout_s is None else timeout_s
    arguments = {} if timeout_s is None else {"timeout_s": timeout_s}
    assert f.run(**arguments) is Fought.TIMEOUT
    assert f._damage_seen and not f.hid.holds
    assert len(aimed_at) >= 3, "the local attempt never exercised repeated re-aims"
    assert all(REAIM_AFTER_S <= later - earlier <= REAIM_AFTER_S + 0.21
               for earlier, later in pairwise(aimed_at))
    assert configured <= combat_clock[0] <= configured + 0.21
    assert f.detail == f"{configured:.0f}s and it is still standing"


def test_one_facing_per_stride_and_the_stride_budget_is_bounded(combat_clock):
    from collections import Counter

    f = _fight([{**ALIVE, "vitals.combat": True, "target.in_melee": True}])
    original = f.targeting.face_selected
    faced_at_closed = []

    def measured_action(**kwargs):
        faced_at_closed.append(f.closed)
        return original(**kwargs)

    f.targeting.face_selected = measured_action
    assert f.run() is Fought.UNREACHABLE
    assert f.closed == MAX_CLOSE_BURSTS
    counts = Counter(faced_at_closed)
    assert counts[0] == 2, "the engagement and the first stride each face once"
    assert all(counts[n] == 1 for n in range(1, MAX_CLOSE_BURSTS)), "a stride faced twice"


@pytest.mark.parametrize("timeout_s", [45.0, 5.0])
def test_slow_fresh_verification_keeps_the_existing_closing_and_timeout_bounds(
        combat_clock, timeout_s):
    """A former 12s damage cap stopped the live attempt before eight validated steps.

    Each successful verification here costs two seconds. The established eight-step
    budget remains usable when it fits the configured timeout; a shorter explicit
    timeout still stops the attempt before all steps have been delivered.
    """
    f = _fight([{**ALIVE, "vitals.combat": True, "target.in_melee": True}])
    original = f.targeting.face_selected

    def slow_verified_action(**kwargs):
        combat_clock[0] += 2.0
        return original(**kwargs)

    f.targeting.face_selected = slow_verified_action
    outcome = f.run(timeout_s=timeout_s)
    assert len(f.hid.holds) == f.closed
    assert all(key == "w" and duration == CLOSE_NUDGE_S for key, duration in f.hid.holds)
    if timeout_s == 45.0:
        assert outcome is Fought.UNREACHABLE
        assert f.closed == MAX_CLOSE_BURSTS
        assert 2.0 * MAX_CLOSE_BURSTS < combat_clock[0] < timeout_s
    else:
        assert outcome is Fought.TIMEOUT
        assert 0 < f.closed < MAX_CLOSE_BURSTS
    assert f.pressed, "fresh verification displaced the existing rotation"


def test_the_last_faced_plate_is_kept_for_the_corpse():
    """A corpse has no plate. Where the living unit's plate last stood is where to look."""
    from jev.perceive.units import Plate, RingColour

    f = _fight([ALIVE])
    plate = Plate(700, 300, 147, RingColour.YELLOW)
    f.targeting.face = FaceResult(FaceCode.FACED, "centred", 0.0, plate, 1, 0.1)
    assert f.engage() is True
    assert f.last_plate is plate
    f.targeting.face = FaceResult(FaceCode.NOT_VISIBLE, "no plate")
    assert f.engage() is False
    assert f.last_plate is plate, "a failed look erased where the unit was last seen"


@pytest.mark.parametrize("values, key", [
    ({**ALIVE, "vitals.combat": True, "vitals.hp": 0.2, "bars.attacking": True,
      "vitals.power": 0.9, "vitals.power_max": 100}, "3"),
    ({**ALIVE, "vitals.combat": True, "vitals.hp": 1.0, "bars.ready": 0b1}, "1"),
])
def test_refused_heal_or_toggle_stops_without_recording_a_press_or_pending_effect(
        combat_clock, values, key):
    f = _fight([values])
    attempts = []
    f.hid.tap = lambda pressed: attempts.append(pressed) or False
    assert f.run(timeout_s=1) is Fought.REFUSED
    assert attempts == [key]
    assert f._pending_heal is None and not f._toggled
    assert f.pressed == [] and f._last_use == {}
    assert f.heals_landed == f.heals_ignored == 0


def test_refused_top_up_stops_without_waiting_or_recording_a_cast(combat_clock, monkeypatch):
    f = _fight([{**ALIVE, "vitals.combat": False, "vitals.hp": 0.6,
                 "vitals.power": 0.9, "vitals.power_max": 100}])
    attempts = []
    f.hid.tap = lambda key: attempts.append(key) or False
    monkeypatch.setattr(f, "_watch_top_up", lambda *_: pytest.fail("waited for a refused heal"))
    assert f.top_up(tries=4) is False
    assert attempts == ["3"]
    assert f.top_ups == f.top_ups_landed == 0
    assert f.pressed == [] and f._last_use == {}
    assert f._pending_heal is None


def test_unready_heal_slot_without_health_gain_is_not_a_landed_heal(combat_clock):
    """Cooldown/cast state can change even when pushback prevents the heal landing."""
    hurt = {**ALIVE, "vitals.combat": True, "vitals.hp": 0.2,
            "vitals.power": 0.9, "vitals.power_max": 100}
    f = _fight([hurt])
    f._rotate(hurt)
    assert f._pending_heal is not None
    heal = for_class(hurt["char.class_id"]).first(Role.HEAL)
    unready = hurt["bars.ready"] & ~(1 << (heal.slot - 1))
    combat_clock[0] = 0.1
    f._watch_heal({**hurt, "bars.ready": unready}, unready)
    assert f.heals_landed == f.heals_ignored == 0
    assert f._pending_heal is not None
    combat_clock[0] = 3.0
    f._watch_heal({**hurt, "bars.ready": unready}, unready)
    assert f.heals_landed == 0 and f.heals_ignored == 1
    assert f._pending_heal is None


def test_a_unit_fighting_us_is_searched_for_all_the_way_round():
    from jev.clients.targeting import FACE_SEARCH_MAX_S

    f = _fight([ALIVE])
    f.engage({**ALIVE, "vitals.combat": True})
    f.engage({**ALIVE, "target.attacking_me": True})
    f.engage(ALIVE)
    assert [r["search_s"] for r in f.targeting.faces] == [FACE_SEARCH_MAX_S, FACE_SEARCH_MAX_S,
                                                           0.6]


def test_the_last_proved_plate_is_the_hint_for_the_next_look():
    from jev.perceive.units import Plate, RingColour

    plate = Plate(810.0, 400.0, 147, RingColour.YELLOW)
    f = _fight([ALIVE])
    f.targeting.face = FaceResult(FaceCode.FACED, "centred", 0.0, plate, 0, 0.0)
    f.engage(ALIVE)
    f.engage(ALIVE)
    assert [r["hint"] for r in f.targeting.faces] == [None, plate]


def test_plate_acquisition_skips_units_whose_hover_is_not_the_wanted_name(monkeypatch):
    """The first live run clicked the nearest plate every look and selected a rabbit."""
    from jev.perceive.units import Plate, RingColour

    hid = _Hid()
    f = _fight([{**ALIVE, "target.has": False}, ALIVE], hid=hid)
    monkeypatch.setattr(f, "_candidates", lambda _: [Plate(700, 300, 147, RingColour.YELLOW)])
    f.targeting.hover_name = 1648                    # a rabbit
    assert f.acquire(1161) is not None or hid.clicks == []
    assert hid.clicks == [], "clicked a plate the client said was someone else's"
    f.targeting.hover_name = 1161
    assert f.acquire(1161) is None
    assert hid.clicks == [(710, 338, False)]


def test_a_kill_that_clears_the_selection_is_proved_by_experience(monkeypatch):
    """Live, 23 Sep: the wolf at 20% one paint and gone the next, XP arriving with it."""
    monkeypatch.setattr("jev.clients.fight.time.sleep", lambda _: None)
    before = {**ALIVE, "char.level": 2, "char.xp_pct": 0.618}
    gone = {**before, "target.has": False, "target.hp": None, "target.name_id": None}
    f = _fight([gone, {**gone, "char.xp_pct": 0.677}])
    f._xp_start = (2, 0.618)
    f._selected_name_id = 2864
    f.last_hp = 0.2
    assert f._settle(gone) is Fought.KILLED, "experience that lags a paint still proves it"
    assert f.killed_name_id == 2864
    g = _fight([gone, gone, gone, gone])
    g._xp_start = (2, 0.618)
    g.last_hp = 0.2
    assert g._settle(gone) is Fought.LOST, "without experience a vanished unit is not a kill"
