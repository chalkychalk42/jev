"""The nameplates a look needs are shown from the state the strip paints (V320)."""

from __future__ import annotations

import pytest
from test_fight import _fight, _Hid, _Targeting
from test_interact import ELWYNN, PLATE, SELECTED

from jev.clients.fight import Fought
from jev.clients.interact import Interact, Result
from jev.clients.plates import BINDING, FIELD, Plates, keys, show
from jev.play.controls import CONTROL_SPECS


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr("jev.clients.plates.time.sleep", lambda _: None)
    monkeypatch.setattr("jev.clients.interact.time.sleep", lambda _: None)


class _Client:
    """A client whose plates the stock bindings toggle, as FrameXML's do."""

    def __init__(self, enemy, friendly, painted=True):
        self.on = {Plates.ENEMY: enemy, Plates.FRIENDLY: friendly}
        self.painted = painted
        self.hid = _Hid()
        self.hid.chord = self.chord
        self.hid.tap = self.tap

    def read(self):
        values = dict(SELECTED)
        if self.painted:
            values.update({FIELD[kind]: on for kind, on in self.on.items()})
        return values

    def tap(self, key):
        self.hid.taps.append(key)
        if key == "v":
            self.on[Plates.ENEMY] = not self.on[Plates.ENEMY]
        return True

    def chord(self, modifier, key):
        self.hid.chords = [*getattr(self.hid, "chords", []), (modifier, key)]
        if (modifier, key) == ("shift", "v"):
            self.on[Plates.FRIENDLY] = not self.on[Plates.FRIENDLY]
        return True


def test_the_bindings_are_the_controls_tables():
    assert BINDING[Plates.FRIENDLY] == CONTROL_SPECS["toggle_friendly_nameplates"][1] == "SHIFT-V"
    assert keys("SHIFT-V") == ("shift", "v") and keys("V") == (None, "v")


def test_friendly_plates_seen_hidden_are_shown_by_their_binding_once():
    """The client relaunched at 22:56 on 27 Sep drew no friendly plate for 34 hours."""
    client = _Client(enemy=True, friendly=False)
    assert show(client.hid, client.read, Plates.FRIENDLY) is True
    assert client.hid.chords == [("shift", "v")] and client.hid.taps == []
    assert client.on[Plates.FRIENDLY] is True
    assert show(client.hid, client.read, Plates.FRIENDLY) is True
    assert client.hid.chords == [("shift", "v")], "shown: never pressed again"


def test_enemy_plates_seen_hidden_are_shown_by_theirs():
    client = _Client(enemy=False, friendly=True)
    assert show(client.hid, client.read, Plates.ENEMY) is True
    assert client.hid.taps == ["v"] and client.on == {Plates.ENEMY: True, Plates.FRIENDLY: True}


@pytest.mark.parametrize("painted", [False, True])
def test_a_state_not_read_presses_nothing(painted):
    """An addon before schema 20 cannot say, and a toggle from an unread state is as likely
    to hide the plates as to show them."""
    client = _Client(enemy=None, friendly=None, painted=painted)
    assert show(client.hid, client.read, Plates.FRIENDLY) is None
    assert show(client.hid, lambda: None, Plates.FRIENDLY) is None
    assert client.hid.taps == [] and not getattr(client.hid, "chords", [])


def test_a_press_the_client_did_not_answer_is_not_taken_for_shown():
    client = _Client(enemy=True, friendly=False)
    client.hid.chord = lambda modifier, key: True          # delivered, and nothing changed
    assert show(client.hid, client.read, Plates.FRIENDLY, wait_s=0.0) is False


def test_an_interaction_shows_friendly_plates_before_it_looks(monkeypatch):
    """A merchant was stood beside for every repair and sale of sessions 387-426 and never
    found: with the plates shown first, the one look finds him."""
    client = _Client(enemy=True, friendly=False)
    monkeypatch.setattr("jev.clients.interact.find_plates",
                        lambda _: [PLATE] if client.on[Plates.FRIENDLY] else [])
    inter = Interact(hid=client.hid, bounds=ELWYNN, read=client.read,
                     read_frame=lambda: object(), read_pos=lambda: (0.5, 0.5),
                     window_centre=(800, 450), targeting=_Targeting(client.read, client.hid))
    inter._window_open = lambda: Result.VENDOR
    assert inter.open_on("Supplier") is Result.VENDOR
    assert client.hid.chords == [("shift", "v")]
    assert inter.hid.holds == [], "found in the first look: no turning round"


def test_a_fight_shows_enemy_plates_before_it_picks():
    client = _Client(enemy=False, friendly=True)
    fight = _fight([client.read()], hid=client.hid)
    fight.read = client.read
    fight.acquire = lambda *a, **k: Fought.NO_TARGET
    fight.run(None, timeout_s=1.0)
    assert client.hid.taps[:1] == ["v"] and client.on[Plates.ENEMY] is True
