"""Killing one unit: select, face by turning, swing on observed state, never claim a kill."""

from __future__ import annotations

import inspect
import math
import random
import time

import pytest

from jev.clients.fight import (
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
from jev.clients.hid import Humaniser
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
from jev.clients.travel import TURN_RATE_SEED
from jev.world.combat import GENERIC, Ability, Role, for_class


class _Hid:
    def __init__(self):
        self.taps = []
        self.clicks = []
        self.holds = []

    def hold(self, key, seconds, **_):
        self.holds.append((key, seconds))
        return True

    def key_down(self, key):
        self.downs = [*getattr(self, "downs", []), key]
        return True

    def key_up(self, key):
        self.ups = [*getattr(self, "ups", []), key]
        return True

    def tap(self, key):
        self.taps.append(key)
        return True

    def chord(self, modifier, key):
        """The modifier held around the key, as `Hid.chord` does it."""
        self.chords = [*getattr(self, "chords", []), (modifier, key)]
        return self.tap(key)

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

    def cancel_pending_spell(self, values=None):
        if values and values.get("bars.targeting") is True:
            self.cancelled = getattr(self, "cancelled", 0) + 1
            self.hid.tap("esc")
            return True
        return False

    def track_selected(self, hint, **_):
        return None                    # no plate to steer on unless a test gives one

    def turn_toward(self, offset):
        self.turned_toward = [*getattr(self, "turned_toward", []), offset]
        return self.hid.hold("d" if offset > 0 else "a", round(abs(offset) * 0.9, 3))

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


def _rotate_answered(f, values):
    """One look, then the client's answer to anything it pressed: the global cooldown."""
    f._rotate(values)
    if f._pending_press is not None:
        f._rotate({**values, "bars.gcd": 0.5})


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
    assert f.targeting.faces == [{"expected_name_id": None, "hint": None, "search_s": 0.0,
                                  "stop": None, "deadline_s": None}]


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
    _rotate_answered(f, ALIVE)
    _rotate_answered(f, ALIVE)
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
        _rotate_answered(f, ALIVE)
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

    real, mod.find_plates = mod.find_plates, lambda _f, **_: [crowd_a, crowd_b, alone]
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


def test_a_target_that_never_takes_damage_is_given_up_not_waited_out(combat_clock):
    """Out of bursts with the target still at full health is the answer already; more
    seconds cannot improve on it.

    Whether anything was *pressed* says nothing about whether it was reached — a seal
    lands on the character, not on the kobold. Requiring "pressed nothing" here let two
    live fights walk eight bursts and then stand in the rotation for the full forty-five
    seconds: `pressed [2, 1, 2] closed 8`, twice."""
    from jev.clients.fight import MAX_APPROACH_S

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)          # rotation is live; it will press the seal
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(timeout_s=45) is Fought.UNREACHABLE
    assert "cannot reach" in f.detail
    assert f._approach_s >= MAX_APPROACH_S and combat_clock[0] < 45
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
        _rotate_answered(f, hurt)
    assert hid.taps.count("3") == 1, "spammed the heal without waiting for an answer"

    # Once it answers, the next one is allowed.
    f._watch_heal({**hurt, "vitals.hp": 0.5}, hurt["bars.ready"])
    f._rotate(hurt)
    assert hid.taps.count("3") == 2


def test_every_approach_is_preceded_by_facing(combat_clock):
    """`W` walks whatever heading the character has, so a unit that moves - or a first
    turn that fell short - is walked past. Six fights in one run reported `closed 8` and
    landed nothing, walking a heading nothing had set."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    engages = []
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: engages.append(1) or True
    f.run(timeout_s=45)
    assert f.closed > 1, "did not approach more than once"
    assert len(engages) == f.closed + 1, "walked without facing first"


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
        _rotate_answered(f, hurt)
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
    _rotate_answered(f, hurt)
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


def test_far_off_it_walks_in_one_go_and_near_it_steps(combat_clock):
    """Watched by the operator on 23 September: "4 paces, then 4 paces, then a couple tiny
    steps until it swings". Far off, forward is now held for one continuous walk; within
    `target.in_melee` (about ten yards, not the five a swing needs) it steps and looks."""
    hid = _Hid()
    far = {**ALIVE, "target.in_melee": False}
    f = _fight([far], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    f.run(timeout_s=3.0)
    assert getattr(hid, "downs", []) and hid.downs == hid.ups, "forward left held"
    assert hid.holds == [], "strode in bursts from far away"

    near = {**ALIVE, "target.in_melee": True}
    g = _fight([near])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=1.5)
    assert g.hid.holds and all(secs == CLOSE_STEP_S for _k, secs in g.hid.holds)
    assert not getattr(g.hid, "downs", []), "ran at something already this close"


def test_the_walk_stops_at_the_first_swing_that_resolves(combat_clock):
    """A resolved swing, landed or missed, is the reach signal (schema 12)."""
    far = {**ALIVE, "target.in_melee": False, "combat.swings": 3}
    swung = {**far, "combat.swings": 4}
    f = _fight([far, far, far, far, swung])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    f._reach_at = None
    assert f._close(far, near=False) is True
    assert f.hid.downs == ["w"] and f.hid.ups == ["w"]
    assert f._reach_at is not None


def test_a_unit_running_at_the_character_is_not_walked_into(combat_clock):
    near_attacking = {**ALIVE, "target.in_melee": True, "target.attacking_me": True}
    f = _fight([{**ALIVE, "target.in_melee": False}, near_attacking])
    assert f._close({**ALIVE, "target.in_melee": False}, near=False) is False
    assert combat_clock[0] < 0.5, "kept walking at something already coming"


def test_past_ten_yards_the_walk_runs_on_only_a_bounded_way(combat_clock):
    from jev.clients.fight import NEAR_OVERRUN_S

    near = {**ALIVE, "target.in_melee": True}
    f = _fight([{**ALIVE, "target.in_melee": False}, near])
    assert f._close({**ALIVE, "target.in_melee": False}, near=False) is False
    assert NEAR_OVERRUN_S <= combat_clock[0] <= NEAR_OVERRUN_S + 0.3


def test_the_walk_steers_on_the_tracked_plate(combat_clock):
    from jev.perceive.units import Plate, RingColour

    far = {**ALIVE, "target.in_melee": False}
    f = _fight([far])
    f.last_plate = Plate(1000.0, 400.0, 147, RingColour.YELLOW)
    f.targeting.track_selected = lambda hint, **_: (hint, 0.12)
    f._close(far, near=False, deadline=1.0)
    assert f.hid.holds and f.hid.holds[0][0] == "d", "did not steer toward an off-centre plate"


def test_reach_expires_so_a_unit_that_runs_is_followed(combat_clock):
    """A kobold at low health runs; standing to swing at air until the timeout lost it."""
    from jev.clients.fight import REACH_HOLD_S

    f = _fight([ALIVE])
    f._reach_at = 0.0
    combat_clock[0] = REACH_HOLD_S + 0.1
    assert not (f._reach_at is not None and combat_clock[0] - f._reach_at < REACH_HOLD_S)


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
    assert f.targeting.faces == [{"expected_name_id": None, "hint": None, "search_s": 0.0,
                                  "stop": None, "deadline_s": None}]


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


def test_a_fight_at_constant_injured_health_still_closes_and_starts_its_attack(combat_clock):
    f = _fight([{**ALIVE, "target.hp": 0.6, "vitals.combat": True}])
    assert f.run(timeout_s=0.6) is Fought.TIMEOUT
    assert getattr(f.hid, "downs", []) == ["w"], "never walked at it"
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
    walked = f.hid.holds or getattr(f.hid, "downs", [])
    assert walked and f.pressed, "acquisition consumed the budget before any attack"
    assert combat_clock[0] >= delay + 0.6


@pytest.mark.parametrize("timeout_s", [None, 15.0])
def test_reaim_cadence_does_not_extend_the_configured_fight_timeout(combat_clock, timeout_s):
    """A hit then a stall earns a re-aim, then - once reach has expired - approaches again,
    all within the existing fight deadline. Standing to re-aim at a unit that had run
    lost fleeing kobolds until the fight timed out."""
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
    from jev.clients.fight import REACH_HOLD_S

    outcome = f.run(**arguments)
    assert outcome in (Fought.TIMEOUT, Fought.UNREACHABLE)
    assert f._damage_seen
    assert len(aimed_at) >= 3, "the local attempt never exercised repeated re-aims"
    first_gap = aimed_at[1] - aimed_at[0]
    assert REAIM_AFTER_S <= first_gap <= REAIM_AFTER_S + 0.21, "the stall was not re-aimed"
    assert getattr(f.hid, "downs", []), "a stall past the reach window never closed again"
    assert combat_clock[0] <= configured + 0.21
    assert REACH_HOLD_S > REAIM_AFTER_S


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
    assert all(key == "w" and duration == CLOSE_STEP_S for key, duration in f.hid.holds)
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


def test_heals_are_cast_on_the_caster_whatever_is_selected(combat_clock):
    """Measured 23 September: a between-fights Holy Light with a looted corpse selected
    waited for a target click, its button lit, and the next three fights selected
    nothing. The self-cast modifier casts it on the character instead."""
    f = _fight([{**ALIVE, "vitals.combat": False, "vitals.hp": 0.6,
                 "vitals.power": 0.9, "vitals.power_max": 100}])
    f.top_up(tries=1)
    assert getattr(f.hid, "chords", []) == [("alt", "3")]
    swing = _fight([{**ALIVE, "vitals.combat": True, "vitals.hp": 1.0, "bars.ready": 0b1}])
    swing.run(timeout_s=0.3)
    assert getattr(swing.hid, "chords", []) == [], "the attack toggle is not self-cast"


def test_a_spell_waiting_for_a_target_is_cancelled_before_anything_is_clicked():
    waiting = {**ALIVE, "bars.targeting": True}
    f = _fight([waiting, ALIVE])
    f.acquire = lambda name_id, **_: None
    f.run(timeout_s=0.3)
    assert "esc" in f.hid.taps and getattr(f.targeting, "cancelled", 0) >= 1


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
                                                           0.0]


NOT_VISIBLE = FaceResult(FaceCode.NOT_VISIBLE, "no plate proved to be the selected unit's", turns=0)


class _Sighting(_Targeting):
    """The plate shows only after `hidden` looks: a unit Tab picked beyond plate range."""

    def __init__(self, read, hid, hidden):
        super().__init__(read, hid)
        self.hidden = hidden

    def face_selected(self, **request):
        self.faces.append(request)
        return NOT_VISIBLE if len(self.faces) <= self.hidden else FACED


def _tab_frames(mark_x=None):
    """Grass before the key; after it, the pick's ring and name where it stands."""
    import numpy as np

    before = np.full((900, 1600, 3), (70, 90, 30), dtype=np.uint8)
    after = before.copy()
    if mark_x is not None:
        after[500:508, mark_x - 16:mark_x + 16] = (250, 255, 30)
        after[460:464, mark_x - 12:mark_x + 12] = (245, 255, 5)
    frames = iter([before])
    return lambda: next(frames, after)


def test_a_tab_pick_without_a_plate_is_turned_toward_by_its_mark_then_walked_at():
    """Measured 23 September: Tab chose a Young Wolf in plain view beyond nameplate
    distance, and the turning search swung it out of view. The ring and name the client
    draws at selection say where it is: turn toward them once, then walk and look."""
    from jev.clients.fight import SIGHT_STRIDE_S

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.read_frame = _tab_frames(mark_x=400)
    f.targeting = _Sighting(f.read, hid, hidden=3)
    assert f.select(1161) is None and f._ahead is True
    assert f._mark_offset == pytest.approx((400 - 800) / 1600, abs=0.01)
    assert f.engage(ALIVE) is True
    assert hid.holds[0][0] == "a", "turned away from the mark"
    assert hid.holds[1:] == [("w", SIGHT_STRIDE_S)] * 3
    assert [r["search_s"] for r in f.targeting.faces] == [0.0] * 4, "searched away from the pick"
    assert f.closed == 3


def test_a_tab_pick_with_no_mark_on_screen_is_not_walked_at():
    """Four blind walks at plateless Tab picks in one live run found none of them."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.read_frame = _tab_frames(mark_x=None)
    f.targeting = _Sighting(f.read, hid, hidden=10 ** 6)
    assert f.select(1161) is None and f._mark_offset is None
    assert f.engage(ALIVE) is False
    assert hid.holds == [] and f.closed == 0


def test_walking_toward_an_unseen_tab_pick_is_bounded():
    from jev.clients.fight import SIGHT_STRIDES

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.read_frame = _tab_frames(mark_x=1000)
    f.targeting = _Sighting(f.read, hid, hidden=10 ** 6)
    assert f.select(1161) is None
    assert f.engage(ALIVE) is False
    assert [key for key, _ in hid.holds] == ["d"] + ["w"] * SIGHT_STRIDES
    assert f._aim_failure() is Fought.NOT_VISIBLE


def test_a_unit_that_starts_fighting_during_the_walk_is_searched_for_all_round():
    from jev.clients.targeting import FACE_SEARCH_MAX_S

    hid = _Hid()
    fighting = {**ALIVE, "vitals.combat": True}
    f = _fight([ALIVE, fighting], hid=hid)
    f.read_frame = _tab_frames(mark_x=1000)
    f.targeting = _Sighting(f.read, hid, hidden=10 ** 6)
    assert f.select(1161) is None
    assert f.engage(ALIVE) is False
    assert [key for key, _ in hid.holds] == ["d", "w"], "kept walking while something was hitting us"
    assert f.targeting.faces[-1]["search_s"] == FACE_SEARCH_MAX_S


def test_a_kept_selection_is_not_walked_at_blind():
    """A selection kept from an earlier fight is not known to be ahead: no stride."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.targeting = _Sighting(f.read, hid, hidden=10 ** 6)
    assert f.engage(ALIVE) is False
    assert hid.holds == [] and f.closed == 0


def test_a_kept_selection_with_no_plate_on_screen_is_dropped_for_a_new_choice():
    """Measured 23 September: one far wolf stayed selected for five minutes and absorbed
    twenty-eight fights, because a kept selection skipped acquisition entirely."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.targeting = _Sighting(f.read, hid, hidden=1)
    acquired = []
    f.acquire = lambda name_id, **kw: acquired.append((name_id, kw)) or None
    f.run(1161, timeout_s=0.3)
    assert acquired == [(1161, {"defend": False})], "a stale selection skipped acquisition"
    assert len(f.targeting.faces) >= 2


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


def test_an_already_selected_unit_of_the_wanted_name_is_the_fight():
    """Jev (or a previous look) selected the wolf; re-acquiring could only swap it."""
    f = _fight([ALIVE])
    f.acquire = lambda *a, **k: pytest.fail("re-acquired a unit that was already selected")
    f.engage = lambda *_: False
    assert f.run(1161, timeout_s=1) is Fought.NOT_VISIBLE
    assert f._selected_name_id == 1161


def test_a_selected_unit_of_another_name_is_not_the_fight():
    seen = []
    f = _fight([{**ALIVE, "target.name_id": 1648}])     # a rabbit
    f.acquire = lambda name_id, **kw: seen.append(name_id) or Fought.NO_TARGET
    assert f.run(1161, timeout_s=1) is Fought.NO_TARGET
    assert seen == [1161]


def _plate_frame(cx=None):
    import numpy as np

    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    if cx is not None:
        frame[380:387, cx - 73:cx + 74] = (230, 200, 10)       # a full yellow plate
    return frame


def test_with_no_wanted_plate_in_view_the_character_looks_round_before_tab():
    """Plates exist only near the character and the camera shows about a hundred
    degrees of that circle: a wolf behind the character is invisible until it turns."""
    from jev.clients.fight import SCAN_TURN_S

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.read_frame = lambda: _plate_frame(900 if len(hid.holds) == 2 else None)
    assert f.acquire(1161) is None
    assert hid.holds == [("d", SCAN_TURN_S)] * 2, "looked round past the plate, or not at all"
    assert hid.taps == [] and len(hid.clicks) == 1, "fell back to Tab with a plate in view"
    assert f.selected_plate is not None and f._ahead is False


def test_a_full_look_round_with_nothing_wanted_falls_back_to_tab():
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.read_frame = lambda: _plate_frame(None)
    assert f.acquire(1161) is None
    assert len(hid.holds) == 3 and hid.taps == ["tab"]
    assert f._ahead is True


class _DrawnHid(_Hid):
    """The fake device, carrying the humaniser a live `Hid` has."""

    TURN_LEFT, TURN_RIGHT = "a", "d"

    def __init__(self, seed: int):
        super().__init__()
        self.h = Humaniser(rng=random.Random(seed))


def test_a_drawn_look_round_varies_its_way_turns_and_count_and_still_sees_the_circle():
    """PLAN 2.1: three quarter turns to the right at every stop is a pattern. Drawn, the
    way round, each turn and the count vary, and the views still cover the circle: no
    two more than 96 degrees apart, the last within 96 of the first."""
    looks = []
    for seed in range(40):
        hid = _DrawnHid(seed)
        f = _fight([ALIVE], hid=hid)
        f.read_frame = lambda: _plate_frame(None)
        assert f.acquire(1161) is None
        (key,) = {k for k, _ in hid.holds}
        turns = [math.degrees(s * TURN_RATE_SEED) for _, s in hid.holds]
        assert all(84 - 1e-6 <= d <= 96 + 1e-6 for d in turns)
        assert sum(turns) >= 264 - 1e-6 > sum(turns[:-1]), "short of the circle, or past it"
        looks.append((key, len(turns), round(sum(turns))))
    assert {k for k, _, _ in looks} == {"a", "d"}
    assert {n for _, n, _ in looks} == {3, 4}
    assert len({s for _, _, s in looks}) > 15


def test_the_tab_burst_is_drawn_per_search():
    counts = set()
    for seed in range(30):
        hid = _DrawnHid(seed)
        dead = {**ALIVE, "target.hp": 0.0}
        assert _fight([dead], hid=hid).select(None) is Fought.NO_TARGET
        counts.add(len(hid.taps))
    assert counts == {3, 4, 5}


def test_self_defence_does_not_look_round():
    """Whatever is hitting us is chosen by Tab and found by the facing search."""
    hid = _Hid()
    f = _fight([{**ALIVE, "target.attacking_me": True}], hid=hid)
    f.read_frame = lambda: _plate_frame(None)
    assert f.acquire(1161, defend=True) is None
    assert hid.holds == [] and hid.taps == ["tab"]


def test_self_defence_turns_round_once_when_tab_finds_nothing_in_front():
    """Tab picks in front of the character; run 20260923T175710-b5044f pressed it three
    times with the attacker behind and found nothing."""
    hid = _Hid()
    nothing = {**ALIVE, "target.has": False}
    f = _fight([nothing], hid=hid)
    f.read_frame = lambda: _plate_frame(None)
    assert f.acquire(1161, defend=True) is Fought.NO_TARGET
    assert hid.holds == [("d", math.pi / TURN_RATE_SEED)], "turned round once, and only once"
    assert hid.taps == ["tab"] * (2 * MAX_SELECTS), "a Tab burst either side of the turn"


def test_a_refused_turn_stops_the_look_round():
    hid = _Hid()
    hid.hold = lambda key, seconds, **_: False
    f = _fight([ALIVE], hid=hid)
    f.read_frame = lambda: _plate_frame(None)
    assert f.acquire(1161) is Fought.REFUSED
    assert hid.taps == []


def test_a_kill_whose_selection_moves_on_is_a_kill_not_a_loss(combat_clock):
    """Run 20260923T233909-8b1484: a Timber Wolf at 20% gave way to another unit at full
    health on the tick its experience arrived; reported lost, the kill went unlooted."""
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.2,
                "char.level": 2, "char.xp_pct": 0.449}
    moved_on = {**fighting, "target.name_id": 99, "target.hp": 1.0, "char.xp_pct": 0.496}
    f = _fight([fighting, fighting, moved_on])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    assert f.killed_name_id == 1161


def test_experience_a_paint_behind_the_new_selection_still_proves_the_kill(combat_clock):
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.2,
                "char.level": 2, "char.xp_pct": 0.449}
    moved_on = {**fighting, "target.name_id": 99, "target.hp": 1.0}
    paid = {**moved_on, "char.xp_pct": 0.496}
    f = _fight([fighting, fighting, moved_on, paid])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED


def test_a_kill_whose_selection_moves_on_to_one_of_the_same_name_is_a_kill(combat_clock):
    """Run 20260924T013702-7f5692: a Kobold Worker at 19% became another Kobold Worker at
    full health far off; the fight chased it until its plate was lost, the kill unlooted."""
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.19,
                "char.level": 4, "char.xp_pct": 0.68}
    moved_on = {**fighting, "target.hp": 1.0, "target.in_melee": False,
                "target.attacking_me": False, "char.xp_pct": 0.72}
    f = _fight([fighting, fighting, moved_on])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    assert f.killed_name_id == 1161


def test_a_small_rise_in_the_selections_health_is_not_another_unit(combat_clock):
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.5}
    healed = {**fighting, "target.hp": 0.6}
    dead = {**fighting, "target.hp": 0.0}
    f = _fight([fighting, healed, dead])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED


def test_a_selection_that_moves_on_without_experience_is_lost(combat_clock):
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.2,
                "char.level": 2, "char.xp_pct": 0.449}
    f = _fight([fighting, fighting, {**fighting, "target.name_id": 99, "target.hp": 1.0}])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.LOST and "changed" in f.detail


def test_an_attacker_in_melee_whose_plate_cannot_be_proved_is_fought_by_the_clients_errors(
        combat_clock):
    """Two Defias Thugs side by side: every hover below the selected one's plate landed on
    the other, and three fights in a row gave up "not visible" while the pair beat the
    character to death (run 20260924T002817-cee9c2). Something hitting us in melee is in
    reach; the swing goes on, and "facing the wrong way" turns the character a quarter."""
    from jev.clients.fight import TURN_RATE_SEED

    hit = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": True,
           "target.hp": 0.8, "ui.error_count": 3, "ui.error_last": 0, "bars.attacking": True}
    behind = {**hit, "ui.error_count": 4, "ui.error_last": 3}          # 3 = not_facing
    dead = {**behind, "target.hp": 0.0}
    f = _fight([hit, hit, behind, behind, dead])
    f.targeting.face = NOT_VISIBLE
    outcome = f.run(1161, timeout_s=10.0)
    assert outcome is Fought.KILLED, f.detail
    quarter = (math.pi / 2) / TURN_RATE_SEED
    turns = [h for h in f.hid.holds if h[0] == "d" and h[1] == pytest.approx(quarter)]
    assert len(turns) == 1, "did not turn on 'facing the wrong way'"


def test_blind_melee_turns_quarters_the_same_way_until_the_errors_stop(combat_clock):
    """A Mangy Wolf at the character's side stayed at its side through eight half turns,
    forty seconds at 5% health (run 20260924T082110-0f56c6)."""
    from jev.clients.fight import TURN_RATE_SEED

    hit = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": True,
           "target.hp": 0.05, "ui.error_count": 3, "ui.error_last": 0, "bars.attacking": True}
    wrong = [{**hit, "ui.error_count": 4 + i, "ui.error_last": 3} for i in range(3)]
    dead = {**wrong[-1], "target.hp": 0.0}
    f = _fight([hit, hit, wrong[0], wrong[1], wrong[2], dead])
    f.targeting.face = NOT_VISIBLE
    assert f.run(1161, timeout_s=10.0) is Fought.KILLED, f.detail
    quarter = (math.pi / 2) / TURN_RATE_SEED
    turns = [h for h in f.hid.holds if h[0] in ("a", "d") and h[1] == pytest.approx(quarter)]
    assert len(turns) == 3 and {k for k, _ in turns} == {"d"}, "quarters, the same way"


def test_an_attacker_whose_plate_never_settles_on_the_centre_is_fought_where_it_stands(
        combat_clock):
    """A Mangy Wolf in melee drifted faster than the pulses turned; eight turns left its
    plate 0.18 of the width off centre and the fight was given up at full health."""
    hit = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": True,
           "target.hp": 0.8, "ui.error_count": 3, "ui.error_last": 0, "bars.attacking": True}
    dead = {**hit, "target.hp": 0.0}
    f = _fight([hit, hit, hit, dead])
    f.targeting.face = FaceResult(FaceCode.UNSETTLED,
                                  "8 turns left the plate +0.179 of the width off centre",
                                  0.179, None, 8, 3.1)
    assert f.run(1161, timeout_s=10.0) is Fought.KILLED, f.detail


def test_facing_the_wrong_way_with_the_plate_on_the_centre_line_turns_round(combat_clock):
    """A unit directly behind projects onto the centre line; the fight walked away from a
    Mangy Wolf, closing, until the character died (run 20260924T035309-97796e)."""
    hit = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": True,
           "target.hp": 0.4, "ui.error_count": 3, "ui.error_last": 0, "bars.attacking": True}
    behind = {**hit, "ui.error_count": 4, "ui.error_last": 3}          # 3 = not_facing
    dead = {**behind, "target.hp": 0.0}
    f = _fight([hit, hit, behind, behind, dead])
    f.targeting.face = FACED
    assert f.run(1161, timeout_s=10.0) is Fought.KILLED, f.detail
    turns = [h for h in f.hid.holds if h[0] == "d" and h[1] > 1.0]
    assert len(turns) == 1, "did not turn round on 'facing the wrong way'"


def test_wrong_way_with_a_centred_plate_first_faces_the_camera_then_turns_round(combat_clock):
    """Turned round by the keys, a camera that no longer looks where the character faces
    keeps the wolf "centred, wrong way": 16 s without a hit (run 20260924T043610)."""
    hit = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": True,
           "target.hp": 0.4, "ui.error_count": 3, "ui.error_last": 0, "bars.attacking": True}
    behind = {**hit, "ui.error_count": 4, "ui.error_last": 3}
    again = {**hit, "ui.error_count": 5, "ui.error_last": 3}
    still = {**hit, "ui.error_count": 6, "ui.error_last": 3}
    dead = {**still, "target.hp": 0.0}
    f = _fight([hit, hit, behind, behind, again, again, again, still, still, still, still,
                dead])
    f.targeting.face = FACED
    realigned = []
    f.realign = lambda: realigned.append(True) or True
    assert f.run(1161, timeout_s=10.0) is Fought.KILLED, f.detail
    assert realigned == [True], "faced the camera first"
    turns = [h for h in f.hid.holds if h[0] == "d" and h[1] > 1.0]
    assert len(turns) == 1, "then, still the wrong way, turned round"


def test_an_unproved_plate_is_still_not_fought_blind_out_of_melee():
    far = {**ALIVE, "vitals.combat": True, "target.attacking_me": True, "target.in_melee": False}
    f = _fight([far])
    f.acquire = lambda name_id, **_: None
    f.targeting.face = NOT_VISIBLE
    assert f.run(1161, timeout_s=2.0) is Fought.NOT_VISIBLE


def test_a_fresh_selection_whose_plate_cannot_be_proved_is_chosen_again_once(combat_clock):
    """A same-name unit in front answered every hover, and seven fights gave up "not
    visible" (run 20260924T002817-cee9c2). Any unit of the wanted name will do for a kill:
    choose again, once, by a click that proves itself."""
    reacquired = []
    faces = iter([NOT_VISIBLE, FACED, FACED, FACED, FACED, FACED, FACED, FACED])
    swung = {**ALIVE, "target.melee_range": True, "target.hp": 0.0}
    f = _fight([ALIVE, ALIVE, swung])
    f.acquire = lambda name_id, **_: reacquired.append(name_id)
    f.targeting.face_selected = lambda **_: next(faces)
    assert f.run(None, timeout_s=5.0) is Fought.KILLED, f.detail
    assert reacquired == [None, None], "the first pick and exactly one more"


def test_a_second_unprovable_pick_ends_the_fight(combat_clock):
    reacquired = []
    f = _fight([ALIVE])
    f.acquire = lambda name_id, **_: reacquired.append(name_id)
    f.targeting.face = NOT_VISIBLE
    assert f.run(None, timeout_s=5.0) is Fought.NOT_VISIBLE
    assert reacquired == [None, None], "chose again more than once"


def test_a_selection_that_becomes_another_unit_of_the_same_name_and_health_is_seen(combat_clock):
    """The strip's GUID (schema 14) says what neither name nor health could: at full health a
    second Kobold Worker is indistinguishable from the first."""
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 1.0,
                "target.guid": 501, "char.level": 4, "char.xp_pct": 0.68}
    other = {**fighting, "target.guid": 502, "char.xp_pct": 0.72}
    f = _fight([fighting, fighting, other])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    unpaid = _fight([fighting, fighting, {**fighting, "target.guid": 502}])
    unpaid.acquire = lambda name_id, **_: None
    unpaid.engage = lambda *_: True
    assert unpaid.run(1161) is Fought.LOST and "changed" in unpaid.detail


def _race(f, now, looks, *, step=0.5, casting=()):
    """Feed `f` one look per `step` seconds: (our health, target health) pairs."""
    for i, (mine, theirs) in enumerate(looks):
        now[0] += step
        f._sample_race({**ALIVE, "vitals.hp": mine, "target.hp": theirs,
                        "bars.casting": i in casting, "target.guid": 7})


def test_a_target_two_swings_from_dead_is_finished_before_the_heal(combat_clock):
    """Healed at 44% with its wolf at 18%; the wolf sat at 18% through two casts and the
    character died with both wolves alive (run 20260924T050644-f9f9fa)."""
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    # Target 0.60 -> 0.20 over eight seconds of swinging; us 0.70 -> 0.38 over six.
    looks = [(0.70 - 0.02 * i, 0.60 - 0.025 * i) for i in range(17)]
    _race(f, combat_clock, looks)
    hurt = {**ALIVE, "vitals.hp": 0.38, "target.hp": 0.18, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100, "target.guid": 7}
    f._rotate(hurt)
    assert "3" not in hid.taps, "stopped swinging to heal a fight it was about to win"


def test_the_heal_still_comes_when_the_target_will_outlast_us(combat_clock):
    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    looks = [(0.70 - 0.04 * i, 0.95 - 0.005 * i) for i in range(9)]
    _race(f, combat_clock, looks)
    hurt = {**ALIVE, "vitals.hp": 0.36, "target.hp": 0.90, "vitals.combat": True,
            "vitals.power": 0.9, "vitals.power_max": 100, "target.guid": 7}
    f._rotate(hurt)
    assert hid.taps == ["3"]


def test_time_spent_casting_is_not_counted_against_the_kill(combat_clock):
    """A cast stops the swings: the target's health standing still through it says
    nothing about how fast the swings kill."""
    from jev.clients.fight import FINISH_EVIDENCE_S

    f = _fight([ALIVE])
    # Swinging: 0.50 -> 0.30 in four seconds. Then five seconds casting, target unmoved.
    looks = [(0.60 - 0.01 * i, 0.50 - 0.025 * i) for i in range(9)]
    looks += [(0.51 - 0.02 * i, 0.30) for i in range(10)]
    _race(f, combat_clock, looks, casting=range(9, 19))
    assert f._finishes_first({"vitals.hp": 0.33, "target.hp": 0.30}) is True
    assert FINISH_EVIDENCE_S <= 4.0


def test_no_heal_is_held_on_too_little_evidence_or_below_the_floor(combat_clock):
    from jev.clients.fight import FINISH_FLOOR

    f = _fight([ALIVE])
    _race(f, combat_clock, [(0.50, 0.30), (0.45, 0.25)])
    assert f._finishes_first({"vitals.hp": 0.40, "target.hp": 0.05}) is False, "one second"
    _race(f, combat_clock, [(0.45 - 0.02 * i, 0.25 - 0.02 * i) for i in range(12)])
    assert f._finishes_first({"vitals.hp": FINISH_FLOOR - 0.01, "target.hp": 0.01}) is False


def test_a_new_selection_starts_the_race_again(combat_clock):
    f = _fight([ALIVE])
    _race(f, combat_clock, [(0.70 - 0.02 * i, 0.60 - 0.03 * i) for i in range(12)])
    combat_clock[0] += 0.5
    f._sample_race({**ALIVE, "vitals.hp": 0.45, "target.hp": 1.0, "target.guid": 8})
    assert len(f._race) == 1
    assert f._finishes_first({"vitals.hp": 0.45, "target.hp": 1.0}) is False


def test_a_cast_does_not_age_the_evidence_that_the_target_is_in_reach(combat_clock):
    """Each Holy Light aged the last hit past the re-aim and reach windows, and the fight
    stepped, levelled the camera and turned at a wolf already in melee - eleven seconds
    without a swing after one heal (run 20260924T052148-85c63f)."""
    from jev.clients.fight import REACH_HOLD_S, REAIM_AFTER_S

    f = _fight([ALIVE])
    f._reach_at = f._damage_at = f._last_aim_at = 0.0
    f._hold_clocks_while_casting({"bars.casting": False})
    for _ in range(8):                        # four seconds of casting
        combat_clock[0] += 0.5
        f._hold_clocks_while_casting({"bars.casting": True})
    combat_clock[0] += 0.5
    f._hold_clocks_while_casting({"bars.casting": False})
    assert combat_clock[0] - f._reach_at == pytest.approx(0.5)
    assert combat_clock[0] - max(f._damage_at, f._last_aim_at) < REAIM_AFTER_S < REACH_HOLD_S


def test_without_a_cast_the_evidence_ages_as_ever(combat_clock):
    from jev.clients.fight import REAIM_AFTER_S

    f = _fight([ALIVE])
    f._reach_at = f._damage_at = f._last_aim_at = 0.0
    for _ in range(9):
        f._hold_clocks_while_casting({"bars.casting": False})
        combat_clock[0] += 0.5
    assert combat_clock[0] - f._damage_at > REAIM_AFTER_S
    assert f._reach_at == 0.0


def test_an_attacker_that_cannot_be_hit_is_drawn_out_by_backing_off(combat_clock):
    """A Mangy Wolf inside a tree trunk bit the character from 69% to 35% while the fight
    stepped into the bark twelve times (run 20260924T053651-ac99b2)."""
    from jev.clients.fight import DRAW_OUT_S, UNANSWERED_STEPS

    biting = {**ALIVE, "target.in_melee": True, "target.attacking_me": True,
              "vitals.combat": True}
    g = _fight([biting])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=4.0)
    backs = [secs for key, secs in g.hid.holds if key == "s"]
    assert backs and backs[0] == DRAW_OUT_S
    steps = [key for key, _secs in g.hid.holds]
    assert steps.index("s") == UNANSWERED_STEPS, "backed off after the unanswered steps"


def test_an_attacker_being_hit_is_not_backed_away_from(combat_clock):
    biting = {**ALIVE, "target.in_melee": True, "target.attacking_me": True,
              "vitals.combat": True}
    hits = [{**biting, "target.hp": 1.0 - 0.02 * i} for i in range(40)]
    g = _fight(hits)
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=3.0)
    assert all(key != "s" for key, _secs in g.hid.holds)


def test_the_second_draw_out_turns_round_and_runs_clear(combat_clock):
    """Backing off from a tree's roots with the wolf below went further up the trunk, five
    times (run 20260924T054447-632295)."""
    from jev.clients.fight import DRAW_OUT_S, RUN_CLEAR_S

    biting = {**ALIVE, "target.in_melee": True, "target.attacking_me": True,
              "vitals.combat": True}
    g = _fight([biting])
    g.acquire = lambda name_id, **_: None
    g.engage = lambda *_: True
    g.run(timeout_s=8.0)
    moves = [(key, secs) for key, secs in g.hid.holds
             if key == "d" or (key in ("s", "w") and secs in (DRAW_OUT_S, RUN_CLEAR_S))]
    assert ("s", DRAW_OUT_S) in moves and ("w", RUN_CLEAR_S) in moves
    first_back = moves.index(("s", DRAW_OUT_S))
    run = moves.index(("w", RUN_CLEAR_S))
    assert first_back < run and moves[run - 1][0] == "d", "turned round before running"


def test_in_combat_a_selected_bystander_is_not_the_fight(combat_clock):
    """A Defias Thug at full health, neither biting nor near, stayed selected for 40 s
    while another killed the character (run 20260924T064025-090aa8)."""
    bystander = {**ALIVE, "vitals.combat": True, "target.name_id": 1161,
                 "target.attacking_me": False, "target.in_melee": False}
    seen = []
    f = _fight([bystander])
    f.acquire = lambda name_id, **kw: seen.append((name_id, kw.get("defend"))) \
        or Fought.NO_TARGET
    assert f.run(1161, timeout_s=1) is Fought.NO_TARGET
    assert seen == [(1161, True)], "fought the bystander instead of looking for the attacker"

    biting = {**bystander, "target.attacking_me": True}
    g = _fight([biting])
    g.acquire = lambda name_id, **kw: pytest.fail("the unit biting us is the fight")
    g.engage = lambda *_: True
    g.run(1161, timeout_s=0.5)


def test_defending_takes_what_is_attacking_before_a_bystander_of_the_wanted_name():
    bystander = {**ALIVE, "target.name_id": 1161, "target.attacking_me": False,
                 "vitals.combat": True}
    attacker = {**bystander, "target.name_id": 999, "target.attacking_me": True}
    f = _fight([bystander])
    assert f._acceptable(1161, defend=True, values=bystander) is True, "still a fight"
    assert f._acceptable(1161, defend=True, values=bystander, attackers_only=True) is False
    assert f._acceptable(1161, defend=True, values=attacker, attackers_only=True) is True


def test_defending_clicks_through_a_bystanders_plate_to_the_attackers():
    """Attackers first, then the wanted name: the plate nearer the centre was a bystander
    Defias Thug, the other the one biting (run 20260924T064025-090aa8)."""
    import numpy as np

    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    frame[380:387, 727:874] = (230, 200, 10)          # centre plate: the bystander
    frame[380:387, 1327:1474] = (230, 200, 10)        # right plate: the attacker
    hid = _Hid()
    base = {**ALIVE, "vitals.combat": True, "target.name_id": 1161}
    bystander = {**base, "target.attacking_me": False, "target.in_melee": False}
    attacker = {**base, "target.attacking_me": True, "target.in_melee": True}

    def read():
        if not hid.clicks:
            return {**base, "target.has": False}
        x = hid.clicks[-1][0] - 10                    # the fight's window origin
        return attacker if x > 1100 else bystander

    f = Fight(hid=hid, read=read, read_frame=lambda: frame, window_origin=(10, 38),
              targeting=_Targeting(read, hid))
    assert f._pick_plate(1161, defend=True) is None
    assert f._selected_name_id == 1161 and read() is attacker
    assert len(hid.clicks) == 2, "the bystander's plate, then the attacker's"


# -- trained spells (`jev.world.training`, `jev.world.combat.from_bar`) --------------

TRAINED_BAR = {1: 6603, 2: 20154, 3: 639, 4: 465, 5: 19740, 6: 20271, 7: 498, 8: 853,
               9: 633, 10: 0, 11: None, 12: None}
ALL_READY = 0b111111111111


def _trained(hid, **values):
    from jev.world.combat import from_bar

    f = _fight([ALIVE], hid=hid)
    f.profile = from_bar(TRAINED_BAR, for_class(2, 1))
    return f, {**ALIVE, "bars.ready": ALL_READY, "bars.usable": ALL_READY,
               "vitals.power": 0.9, "vitals.power_max": 300, **values}


def test_the_bar_s_census_is_the_profile_its_items_kept():
    from jev.world.combat import from_bar

    profile = from_bar(TRAINED_BAR, for_class(2, 1))
    roles = {a.slot: a.role for a in profile.abilities}
    assert roles == {1: Role.ATTACK, 2: Role.BUFF, 3: Role.HEAL, 4: Role.AURA, 5: Role.BUFF,
                     6: Role.ATTACK, 7: Role.SAVE, 8: Role.STUN, 9: Role.LAST_RESORT,
                     11: Role.DRINK, 12: Role.FOOD}
    blessing = next(a for a in profile.abilities if a.slot == 5)
    assert blessing.self_cast and blessing.lasting and blessing.every_s == 595.0
    assert from_bar(None, for_class(2, 1)) == for_class(2, 1)


def test_divine_protection_goes_up_before_the_heal_it_protects():
    """Pushback left a level 6 paladin's Holy Lights unfinished for 22 s against one wolf:
    immune first, then the heal."""
    hid = _Hid()
    f, hurt = _trained(hid, **{"vitals.hp": 0.3, "vitals.combat": True,
                               "target.attacking_me": True})
    f._rotate(hurt)
    assert hid.taps == ["7"]
    f._rotate({**hurt, "bars.ready": ALL_READY & ~(1 << 6)})     # on its cooldown now
    assert hid.taps == ["7", "3"], "the heal under the save, not a stun"
    assert f._pending_heal is not None
    # With the save spent before this fight, the stun clears the way instead.
    hid2 = _Hid()
    f2, _ = _trained(hid2)
    f2._rotate({**hurt, "bars.ready": ALL_READY & ~(1 << 6)})
    assert hid2.taps == ["8"], "the stun is the guard when the save is spent"
    f2._rotate({**hurt, "bars.ready": ALL_READY & ~(1 << 6) & ~(1 << 7)})
    assert hid2.taps[-1] == "3" and f2._pending_heal is not None


def test_the_last_resort_is_for_a_fight_about_to_be_lost():
    hid = _Hid()
    f, dying = _trained(hid, **{"vitals.hp": 0.1, "vitals.combat": True})
    f._rotate(dying)
    assert hid.taps == ["9"] and hid.chords == [("alt", "9")]   # on the caster
    hid2 = _Hid()
    f2, hurt = _trained(hid2, **{"vitals.hp": 0.3, "vitals.combat": True})
    f2._rotate(hurt)
    assert "9" not in hid2.taps


def test_an_aura_and_a_blessing_are_kept_between_fights_and_lost_to_a_death(combat_clock):
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    for _ in range(4):
        _rotate_answered(f, calm)
    assert hid.taps == ["4", "2", "5", "6"], "aura, seal, blessing, then Judgement"
    assert ("alt", "5") in hid.chords                           # the blessing, on the caster
    f._last_use = {}                                            # a new fight
    hid.taps.clear()
    _rotate_answered(f, calm)
    assert hid.taps == ["2"], "the aura and the blessing were pressed again next fight"
    f.buffs_lost()
    hid.taps.clear()
    f._last_use = {}
    f._rotate(calm)
    assert hid.taps == ["4"], "the aura was not pressed again after a death"


def test_judgement_spends_the_seal_and_the_seal_goes_straight_back_on(combat_clock):
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
    _rotate_answered(f, calm)
    _rotate_answered(f, calm)
    assert hid.taps == ["2", "6"]
    f._rotate({**calm, "bars.ready": ALL_READY & ~(1 << 5)})   # Judgement on its cooldown
    assert hid.taps == ["2", "6", "2"], "the seal Judgement released was not put back"


def test_a_starting_spell_keeps_its_starting_role_at_any_rank_and_page():
    """Heroic Strike rank 2 is still the warrior's blow, found on button 2 though the class
    starts it on its stance page (action 74); the food stays on its key."""
    from jev.world.combat import from_bar

    bar = {1: 6603, 2: 284, **{s: 0 for s in range(3, 12)}, 12: None}
    rows = {a.slot: (a.name, a.role) for a in from_bar(bar, for_class(1, 1)).abilities}
    assert rows == {1: ("Attack", Role.ATTACK), 2: ("Heroic Strike", Role.ATTACK),
                    12: ("Tough Jerky", Role.FOOD)}


def test_below_a_quarter_the_heal_comes_behind_its_save_however_close_the_kill(combat_clock):
    """Held nine times from 40% against a wolf "a second from dead" that took five more:
    won at 14%, Divine Protection spent on the kill, dead to the next wolf (run
    20260924T114311-570633)."""
    from jev.world.combat import from_bar

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.profile = from_bar(TRAINED_BAR, for_class(2, 1))
    looks = [(0.70 - 0.03 * i, 0.60 - 0.03 * i) for i in range(17)]
    _race(f, combat_clock, looks)
    hurt = {**ALIVE, "bars.ready": ALL_READY, "bars.usable": ALL_READY, "vitals.hp": 0.23,
            "target.hp": 0.07, "vitals.combat": True, "vitals.power": 0.9,
            "vitals.power_max": 300, "target.guid": 7, "target.attacking_me": True}
    assert f._finishes_first(hurt) is False
    f._rotate(hurt)
    assert hid.taps == ["7"], "Divine Protection first, then the heal"


def test_a_spell_the_catalog_does_not_know_keeps_its_slot_s_starting_row():
    from jev.world.combat import from_bar

    bar = {**TRAINED_BAR, 2: 999999}
    rows = {a.slot: (a.name, a.role) for a in from_bar(bar, for_class(2, 1)).abilities}
    assert rows[2] == ("Seal of Righteousness", Role.BUFF)


def test_the_heal_comes_straight_after_the_save_whatever_the_line(combat_clock):
    """Divine Protection at 40%, then a stun, and health sat at exactly 40% - not below
    the heal's line - until the immunity ran out; the heal came after it and was pushed
    back to nothing (run 20260924T122236-108178)."""
    from jev.world.combat import from_bar

    hid = _Hid()
    f = _fight([ALIVE], hid=hid)
    f.profile = from_bar(TRAINED_BAR, for_class(2, 1))
    hurt = {**ALIVE, "bars.ready": ALL_READY, "bars.usable": ALL_READY, "vitals.hp": 0.39,
            "vitals.combat": True, "vitals.power": 0.9, "vitals.power_max": 300,
            "target.attacking_me": True}
    f._rotate(hurt)
    assert hid.taps == ["7"], "the save first"
    combat_clock[0] += 1.6                                     # its global cooldown
    immune = {**hurt, "vitals.hp": 0.40, "bars.ready": ALL_READY & ~(1 << 6)}
    f._rotate(immune)
    assert hid.taps == ["7", "3"], "the heal next, not the stun, at exactly 40%"
    assert hid.chords[-1] == ("alt", "3")


def test_a_fight_begun_near_death_heals_before_it_looks_for_the_attacker(combat_clock):
    """At 17% the selection and facing of a fresh attacker took five seconds with nothing
    pressed, and the heal came at 5% (run 20260924T132256-fc8503)."""
    hurt = {**ALIVE, "vitals.combat": True, "vitals.hp": 0.17, "target.has": False,
            "target.hp": None, "vitals.power": 0.9, "vitals.power_max": 100}
    casting = {**hurt, "bars.casting": True}
    healed = {**hurt, "vitals.hp": 0.65}
    hid = _Hid()
    f = _fight([hurt, casting, casting, healed, healed], hid=hid, frame=None)
    f.run(timeout_s=1)
    assert hid.taps[0] == "3", f"looked for the attacker before healing: {hid.taps}"
    assert hid.chords[0] == ("alt", "3")
    assert "tab" in hid.taps[1:], "never looked for the attacker after the heal"


def test_the_grey_level_is_the_servers():
    """`MaNGOS::XP::GetGrayLevel`: level 3-4 Defias were grey to a level 9 paladin."""
    from jev.clients.fight import grey_level

    assert [grey_level(n) for n in (1, 5, 6, 9, 10, 39, 40, 59, 60, 70)] == \
        [0, 0, 1, 4, 4, 31, 31, 47, 51, 61]


def test_a_grey_target_gone_below_half_health_is_a_kill(monkeypatch):
    """Session 74: six level 3-4 Defias last seen at 3-34% health, each settled "lost" at
    level 9 because a grey kill grants no experience, and none was looted."""
    monkeypatch.setattr("jev.clients.fight.time.sleep", lambda _: None)
    gone = {**ALIVE, "target.has": False, "target.hp": None, "target.name_id": None,
            "char.level": 9, "char.xp_pct": 0.95}
    f = _fight([gone] * 4)
    f._xp_start = (9, 0.95)
    f._selected_name_id = 1161
    f._target_level = 4
    f.last_hp = 0.34
    assert f._settle(gone) is Fought.KILLED
    assert f.killed_name_id == 1161
    for level, hp in ((5, 0.34), (4, 0.9), (None, 0.1)):
        g = _fight([gone] * 4)
        g._xp_start = (9, 0.95)
        g._target_level = level
        g.last_hp = hp
        assert g._settle(gone) is Fought.LOST, (level, hp)


def test_a_grey_target_whose_selection_moves_on_is_a_kill(combat_clock):
    fighting = {**ALIVE, "vitals.combat": True, "target.melee_range": True, "target.hp": 0.2,
                "target.level": 3, "char.level": 9, "char.xp_pct": 0.95}
    f = _fight([fighting, fighting, {**fighting, "target.name_id": 99, "target.hp": 1.0}])
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    assert f.killed_name_id == 1161


def test_a_press_the_client_drops_is_pressed_again(combat_clock):
    """Run 20260924T140621-fc3531: Snap Kick's stun swallowed the seal, the rotation
    stamped it as up, and Judgement stayed unusable for 25 s while the character swung
    unsealed against a Defias Bandit that killed it."""
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
    f._rotate(calm)
    assert hid.taps == ["2"]
    f._rotate(calm)
    assert hid.taps == ["2"], "pressed again before the client could answer"
    combat_clock[0] += 0.9                     # a look later: no cast, no cooldown
    f._rotate(calm)
    assert hid.taps == ["2", "2"], "the dropped seal was counted as up"


def test_an_answered_press_is_kept(combat_clock):
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
    f._rotate(calm)
    combat_clock[0] += 0.3
    f._rotate({**calm, "bars.gcd": 0.8})
    combat_clock[0] += 1.5
    f._rotate(calm)
    assert hid.taps == ["2", "6"], "the seal went up, and Judgement came next"


def test_a_first_look_after_the_global_cooldown_cannot_tell(combat_clock):
    """A face or an approach step between looks: an answer would be over by then."""
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
    f._rotate(calm)
    combat_clock[0] += 2.0
    f._rotate(calm)
    assert hid.taps == ["2", "6"]


def test_a_heal_pressed_under_a_stun_is_pressed_again_not_the_seal(combat_clock):
    """The same run: a Holy Light pressed under a stun held the heal row off for its whole
    watch, and the rotation sealed and judged at 12% health until the character died."""
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
    f._last_use = {2: 0.0}
    f._saved_at = -100.0
    hurt = {**calm, "vitals.hp": 0.12, "vitals.combat": True,
            "bars.ready": ALL_READY & ~(1 << 6) & ~(1 << 7) & ~(1 << 8)}   # save, stun, last resort spent
    f._rotate(hurt)
    assert [k for m, k in hid.chords] == ["3"]
    combat_clock[0] += 0.9
    f._rotate(hurt)
    assert [k for m, k in hid.chords] == ["3", "3"], "the unanswered heal was not retried"
    assert "2" not in hid.taps, "sealed at 12% health instead of healing"


def test_a_press_the_client_never_answers_is_counted_after_three(combat_clock):
    """An aura already up, or a Judgement on a unit out of reach: re-pressed on every look,
    it would hold the rotation on that row for the whole fight."""
    hid = _Hid()
    f, calm = _trained(hid, **{"bars.attacking": True})
    f._lasting = {"Blessing of Might": 0.0}
    f._last_use = {2: 0.0}
    for _ in range(4):
        f._rotate(calm)
        combat_clock[0] += 0.9
    assert hid.taps[:3] == ["4", "4", "4"]
    assert hid.taps[3] != "4", "the aura was pressed a fourth time in a row"


def test_a_fight_keeps_mana_back_for_a_heal_and_its_save(combat_clock):
    """Session 88: re-sealing after every Judgement left 54 of 300 mana, short of Holy
    Light's 60, and the character died under Divine Protection with nothing to cast."""
    pool = 300

    def fight_at(mana: float, *, combat: bool):
        hid = _Hid()
        f, calm = _trained(hid, **{"bars.attacking": True, "vitals.combat": combat,
                                   "vitals.power_max": pool})
        f._lasting = {"Devotion Aura": 0.0, "Blessing of Might": 0.0}
        f._rotate({**calm, "vitals.power": mana / pool})
        return f, hid

    probe, _ = fight_at(pool, combat=True)
    heal, save = probe.profile.first(Role.HEAL), probe.profile.first(Role.SAVE)
    seal = next(a for a in probe.profile.by_role(Role.BUFF) if not a.lasting)
    reserve = heal.mana + save.mana
    _, hid = fight_at(reserve + seal.mana - 5, combat=True)
    assert str(seal.slot) not in hid.taps, "the seal spent the heal's mana"
    _, hid = fight_at(reserve + seal.mana + 30, combat=True)
    assert hid.taps[:1] == [str(seal.slot)], "with mana to spare the seal goes on"
    _, hid = fight_at(reserve + seal.mana - 5, combat=False)
    assert hid.taps[:1] == [str(seal.slot)], "out of a fight nothing is held back"


def test_hurt_while_looking_for_the_target_heals_first_then_looks_again():
    """Session 126: twelve seconds spent looking for the selected gnoll's plate took the
    character from 94% to 22% with nothing pressed. In a fight the look stops for the heal,
    which needs no facing, and the clock bounds the look."""
    from jev.clients.fight import FIGHT_FACE_S

    hurt = {**ALIVE, "vitals.combat": True, "vitals.hp": 0.3, "target.attacking_me": True,
            "combat.auto_attack": True}
    f = _fight([hurt])
    answers = [FaceResult(FaceCode.INTERRUPTED, "30% health in a fight: the heal first"), FACED]
    requests = []

    def face(**request):
        requests.append(request)
        return answers.pop(0)

    f.targeting.face_selected = face
    healed = []
    f._heal_first = lambda values: healed.append(values) or values
    assert f.engage(hurt) is True
    assert healed, "the heal came before the second look"
    assert len(requests) == 2
    assert callable(requests[0]["stop"]) and requests[0]["deadline_s"] == FIGHT_FACE_S
    assert requests[0]["stop"](hurt) and not requests[0]["stop"]({**hurt, "vitals.hp": 0.9})


class _Lines:
    def __init__(self, line="0.50"):
        self.line, self.picks, self.outcomes = line, [], []

    def pick(self, objective, options):
        self.picks.append((objective, tuple(options)))
        return self.line

    def outcome(self, objective, option, won, seconds=0.0):
        self.outcomes.append((objective, option, won))


@pytest.mark.parametrize(("result", "low", "recorded"), [
    (Fought.KILLED, 0.55, True),        # came to blows and never went low: it went well
    (Fought.KILLED, 0.10, False),       # won, but below the bad line: it went badly
    (Fought.DIED, 0.0, False),
    (Fought.NO_TARGET, None, None),     # never came to blows: nothing learned
])
def test_each_fight_holds_a_drawn_heal_line_and_records_how_it_went(result, low, recorded):
    from jev.clients.fight import HEAL_LINES

    f = _fight([ALIVE])
    lines = _Lines("0.60")
    f.choices = lines

    def fought(name_id, timeout_s):
        f._low_hp = low
        return result

    f._fight = fought
    assert f.run() is result
    assert lines.picks == [("all", HEAL_LINES)] and f.heal_below == 0.60
    assert lines.outcomes == ([] if recorded is None else [("all", "0.60", recorded)])


def test_a_fight_cut_short_is_recorded_only_when_it_was_going_badly():
    f = _fight([ALIVE])
    lines = _Lines()
    f.choices = lines

    def cut(low):
        def fought(name_id, timeout_s):
            f._low_hp = low
            raise RuntimeError("cancelled: dead or ghost")
        return fought

    for low in (0.05, 0.8):
        f._fight = cut(low)
        with pytest.raises(RuntimeError):
            f.run()
    assert lines.outcomes == [("all", "0.50", False)], "a stop at 80% says nothing"


# -- a caster (V164) -----------------------------------------------------------------

MAGE = for_class(8, 1)
AT_RANGE = {**ALIVE, "char.class_id": 8, "char.race_id": 1, "target.in_melee": False,
            "target.melee_range": False, "vitals.combat": True, "vitals.power": 1.0,
            "vitals.power_max": 165, "char.level": 1, "char.xp_pct": 0.1}


def _mage(frames, hid=None):
    f = _fight(frames, hid=hid)
    f.profile = MAGE
    return f


def test_a_mage_is_a_caster_and_a_paladin_is_not():
    """Fireball reaches 35 yards (the client's own spell data); Judgement 10."""
    assert MAGE.caster and not for_class(2, 1).caster
    fireball = next(a for a in MAGE.abilities if a.name == "Fireball")
    frost_armor = next(a for a in MAGE.abilities if a.name == "Frost Armor")
    assert fireball.spell_id == 133 and frost_armor.lasting, "thirty minutes outlasts a fight"


def test_a_caster_with_the_mana_casts_and_never_swings_its_staff():
    hid = _Hid()
    f = _mage([AT_RANGE], hid=hid)
    for _ in range(4):
        _rotate_answered(f, AT_RANGE)
    assert "1" not in hid.taps, "the staff's swing is for when the mana is gone"
    assert hid.taps.count("2") >= 2, "Fireball, again and again"
    assert hid.taps.count("3") == 1, "Frost Armor once, not every look"


def test_out_of_mana_a_caster_swings_its_staff():
    hid = _Hid()
    dry = {**AT_RANGE, "bars.usable": 0b101, "vitals.power": 0.05}
    f = _mage([dry], hid=hid)
    _rotate_answered(f, dry)
    assert hid.taps == ["1"]


def test_a_caster_fight_is_cast_from_where_it_stands(combat_clock):
    """A mage walked into melee like a paladin would never have been a mage."""
    casting = {**AT_RANGE, "bars.casting": True}
    hurt = {**AT_RANGE, "target.hp": 0.4}
    dead = {**AT_RANGE, "target.hp": 0.0, "char.xp_pct": 0.2}
    hid = _Hid()
    f = _mage([AT_RANGE, AT_RANGE, casting, hurt, {**hurt, "bars.casting": True}, dead],
              hid=hid)
    f._lasting["Frost Armor"] = 0.0             # up already, for thirty minutes
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    assert [key for key, _ in hid.holds if key == "w"] == [], "not a step toward it"
    assert "2" in hid.taps and "1" not in hid.taps
    assert f.ended_far, "the corpse lies out there: the loot walks to it"


def test_a_spell_that_does_not_reach_steps_in_and_one_out_of_sight_steps_aside(combat_clock):
    far = {**AT_RANGE, "ui.error_count": 1, "ui.error_last": 2}          # out of range
    unseen = {**AT_RANGE, "ui.error_count": 2, "ui.error_last": 4}       # no line of sight
    dead = {**AT_RANGE, "ui.error_count": 2, "target.hp": 0.0, "char.xp_pct": 0.2}
    hid = _Hid()
    f = _mage([AT_RANGE, far, unseen, dead], hid=hid)
    f.acquire = lambda name_id, **_: None
    f.engage = lambda *_: True
    assert f.run(1161) is Fought.KILLED
    keys = [key for key, _ in hid.holds]
    assert keys.count("w") == 1, "one step in, on the client's word"
    assert set(keys) - {"w"}, "and a step aside for the line of sight"


def test_a_caster_draws_no_heal_line_it_cannot_use():
    picks = []

    class Choices:
        def pick(self, objective, options):
            picks.append(options)
            return options[0]

        def outcome(self, *a, **k):
            pass

    f = _mage([{**AT_RANGE, "target.has": False}])
    f.choices = Choices()
    f.acquire = lambda name_id, **_: Fought.NO_TARGET
    f.run(1161)
    assert picks == []


def test_a_casters_opener_slows_and_at_contact_an_instant_comes_first():
    """V165: Frostbolt before the unit has come for it, Fire Blast once it is hitting."""
    from dataclasses import replace

    from jev.world.combat import Ability, CombatProfile

    fireball = Ability(slot=2, role=Role.ATTACK, name="Fireball", mana=30, spell_id=133)
    frostbolt = Ability(slot=5, role=Role.ATTACK, name="Frostbolt", mana=25, spell_id=116)
    blast = Ability(slot=6, role=Role.ATTACK, name="Fire Blast", mana=40, spell_id=2136)
    mage = replace(MAGE, abilities=(*MAGE.abilities, frostbolt, blast))
    assert isinstance(mage, CombatProfile) and mage.caster
    order = Fight._caster_order
    attacks = (fireball, frostbolt, blast)
    assert order(attacks, {**AT_RANGE, "target.attacking_me": False})[0] is frostbolt
    assert order(attacks, {**AT_RANGE, "target.attacking_me": True})[0] is fireball
    assert order(attacks, {**AT_RANGE, "target.in_melee": True})[0] is blast
