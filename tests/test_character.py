"""The campaign's characters: names, when to switch, and the create screen driven only at
measured plates (`tools/character.py`, `jev.clients.session`)."""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import character  # noqa: E402

from jev.clients import session as module  # noqa: E402
from jev.clients.hid import Hid  # noqa: E402
from jev.clients.session import ENTER_WORLD, Session, Stage  # noqa: E402


def test_names_are_pronounceable_letters_of_a_length_the_server_takes():
    rng = random.Random(3)
    names = {character.make_name(rng) for _ in range(300)}
    assert len(names) > 100, "enough of them to find a free one"
    for name in names:
        assert character.NAME.fullmatch(name), name
    with pytest.raises(ValueError):
        character.name_in_use("Robert'); DROP TABLE characters;--")


def test_the_key_is_the_one_the_strip_paints():
    assert character.key_of("Testvvi", "Forever Dev") == "548c8582"


def _campaign(**first) -> dict:
    return {"format": 1, "realm": "R", "active": 0, "switch_tried": None, "characters": [
        {"name": "Testvvi", "class": "paladin", "race": "human", "key": "548c8582", "row": 2,
         "created": True, "until_level": 20, "until": "2026-09-26T10:30:00+01:00", **first},
        {"name": "Kelvaran", "class": "mage", "race": "human", "key": "00000001", "row": 3,
         "created": False}]}


HOUR_15 = datetime.fromisoformat("2026-09-26T10:30:00+01:00").timestamp()


@pytest.mark.parametrize(("now", "level", "tried", "expected"), [
    (HOUR_15 - 60, 16, None, "stay"),            # before the deadline, below the level
    (HOUR_15 - 60, 20, None, "switch"),          # the level comes first
    (HOUR_15 + 1, 16, None, "switch"),           # the deadline, whatever the level
    (HOUR_15 + 1, None, None, "switch"),         # the level unread: the deadline still holds
    (HOUR_15 + 1, 16, HOUR_15 - 600, "stay"),    # tried ten minutes ago: not again yet
    (HOUR_15 + 1, 16, HOUR_15 - 4000, "switch"),  # an hour on, again
])
def test_the_switch_comes_at_the_level_or_the_deadline_whichever_is_first(now, level, tried,
                                                                         expected):
    campaign = _campaign()
    campaign["switch_tried"] = tried
    assert character.due(campaign, now, level)[0] == expected


def test_the_last_character_plays_on():
    campaign = _campaign()
    campaign["active"] = 1
    assert character.due(campaign, HOUR_15 + 10**6, 60)[0] == "stay"


def test_a_plan_names_the_active_character_and_the_next(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(character, "CAMPAIGN", tmp_path / "campaign.json")
    assert character.main(["plan", "--realm", "Forever Dev", "--active", "Testvvi", "--row", "2",
                           "--until", "2026-09-26T10:30:00+01:00", "--next-row", "3"]) == 0
    campaign = json.loads((tmp_path / "campaign.json").read_text())
    first, second = campaign["characters"]
    assert (first["key"], first["until_level"], first["created"]) == ("548c8582", 20, True)
    assert (second["name"], second["class"], second["row"], second["created"]) == \
        (None, "mage", 3, False)
    assert character.main(["plan", "--realm", "X", "--active", "Y", "--row", "0",
                           "--next-row", "1"]) == 1, "an existing campaign is kept"
    monkeypatch.setattr(character, "name_in_use", lambda name: name.startswith("A"))
    assert character.main(["name", "--seed", "5"]) == 0
    name = capsys.readouterr().out.strip().splitlines()[-1]
    saved = json.loads((tmp_path / "campaign.json").read_text())["characters"][1]
    assert saved["name"] == name and not name.startswith("A")
    assert saved["key"] == character.key_of(name, "Forever Dev")


def test_no_campaign_means_stay(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(character, "CAMPAIGN", tmp_path / "none.json")
    assert character.main(["due"]) == 0
    assert capsys.readouterr().out.strip() == "stay"


# --- the create screen, on frames with plates where the measurements say ----------------

SELECT_ROW, STEP = (0.85, 0.12), 0.07
PLATES = dict(CREATE_NEW=(0.84, 0.86), CHARACTER_ROW_FIRST=SELECT_ROW, CHARACTER_ROW_STEP=STEP,
              CREATE_ACCEPT=(0.88, 0.90), CREATE_BACK=(0.88, 0.95), NAME_BOX=(0.50, 0.93),
              CREATE_REFUSED_OKAY=(0.50, 0.55), RACE_BUTTONS={"human": (0.05, 0.20)},
              CLASS_BUTTONS={"mage": (0.08, 0.60)})


def _plates(*at, text_at=None) -> np.ndarray:
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    for x, y in at:
        px, py = int(x * 1600), int(y * 900)
        frame[py - 12:py + 12, px - 70:px + 70] = (120, 20, 10)
    if text_at is not None:
        px, py = int(text_at[0] * 1600), int(text_at[1] * 900)
        frame[py - 4:py + 4, px - 30:px + 30] = (200, 200, 200)
    return frame


SELECT = _plates(ENTER_WORLD)
CREATE = _plates(PLATES["CREATE_ACCEPT"], PLATES["CREATE_BACK"])
TYPED = _plates(PLATES["CREATE_ACCEPT"], PLATES["CREATE_BACK"], text_at=PLATES["NAME_BOX"])
REFUSED = _plates(PLATES["CREATE_ACCEPT"], PLATES["CREATE_BACK"], PLATES["CREATE_REFUSED_OKAY"])


class _Screen:
    """A client whose screen changes as it is clicked, and which records every press."""

    def __init__(self, s: Session, frame: np.ndarray, after: dict):
        self.frame, self.after, self.presses = frame, after, []
        s.read_frame = lambda: self.frame
        s.hid.click = self.click
        s.hid.chord = lambda *a, **k: self.presses.append(("chord", *a)) or True
        s.hid.type_text = self.type_text
        s._wait = lambda seconds: None

    def click(self, x, y, *a, **k):
        self.presses.append((x, y))
        self.frame = self.after.get((x, y), self.frame)
        return True

    def type_text(self, text):
        self.presses.append(("type", text))
        self.frame = TYPED
        return True


def _session() -> Session:
    return Session(hid=Hid(hwnd=1), read_frame=lambda: None, radio_ok=lambda: False,
                   window_size=(1600, 900))


@pytest.fixture
def measured(monkeypatch):
    for name, value in PLATES.items():
        monkeypatch.setattr(module, name, value)


def test_the_create_screen_is_told_apart_by_its_plates(measured):
    assert module.stage(CREATE, False) is Stage.CREATE
    assert module.stage(REFUSED, False) is Stage.CREATE_REFUSED
    assert module.stage(SELECT, False) is Stage.CHARACTER


def test_a_character_is_made_by_clicks_at_measured_plates_and_its_name_checked(measured):
    s = _session()
    at = s._screen
    screen = _Screen(s, SELECT, {at(PLATES["CREATE_NEW"]): CREATE,
                                 at(PLATES["CREATE_ACCEPT"]): SELECT})
    assert s.create_character("Kelvaran", "human", "mage") is True
    assert screen.presses == [at(PLATES["CREATE_NEW"]), at(PLATES["RACE_BUTTONS"]["human"]),
                              at(PLATES["CLASS_BUTTONS"]["mage"]), at(PLATES["NAME_BOX"]),
                              ("chord", "ctrl", "a"), ("type", "Kelvaran"),
                              at(PLATES["CREATE_ACCEPT"])]


def test_a_refused_name_is_said_and_the_list_comes_back(measured):
    s = _session()
    at = s._screen
    screen = _Screen(s, SELECT, {at(PLATES["CREATE_NEW"]): CREATE,
                                 at(PLATES["CREATE_ACCEPT"]): REFUSED,
                                 at(PLATES["CREATE_REFUSED_OKAY"]): CREATE,
                                 at(PLATES["CREATE_BACK"]): SELECT})
    assert s.create_character("Kelvaran", "human", "mage") is False
    assert s.refused and "refused" in s.detail
    assert screen.presses[-2:] == [at(PLATES["CREATE_REFUSED_OKAY"]), at(PLATES["CREATE_BACK"])]
    assert module.stage(screen.frame, False) is Stage.CHARACTER


def test_an_empty_name_box_is_never_accepted(measured):
    s = _session()
    at = s._screen
    screen = _Screen(s, SELECT, {at(PLATES["CREATE_NEW"]): CREATE})
    screen.type_text = lambda text: screen.presses.append(("type", text)) or True  # typed at
    s.hid.type_text = screen.type_text
    assert s.create_character("Kelvaran", "human", "mage") is False
    assert at(PLATES["CREATE_ACCEPT"]) not in screen.presses


def test_nothing_is_pressed_before_the_screens_are_measured():
    s = _session()
    screen = _Screen(s, SELECT, {})
    assert s.create_character("Kelvaran", "human", "mage") is False
    assert s.select_row(3) is False
    assert screen.presses == [] and "not measured" in s.detail


def test_a_row_is_picked_by_its_measured_place(measured):
    s = _session()
    screen = _Screen(s, SELECT, {})
    assert s.select_row(3) is True
    assert screen.presses == [s._screen((SELECT_ROW[0], SELECT_ROW[1] + 3 * STEP))]


def test_the_world_is_waited_for_pressing_nothing_after_enter(measured):
    s = _session()
    screen = _Screen(s, SELECT, {})
    reads = iter([False, False, False, True])
    s.radio_ok = lambda: next(reads)
    assert s.enter_world(timeout_s=60) is True
    assert screen.presses == [s._screen(ENTER_WORLD)], "one click, then only waiting"


def test_signing_in_can_stop_at_character_select(measured):
    s = _session()
    screen = _Screen(s, CREATE, {s._screen(PLATES["CREATE_BACK"]): SELECT})
    assert s.sign_in("acct", "pw", timeout_s=5, stop_at_select=True) is True
    assert screen.presses == [s._screen(PLATES["CREATE_BACK"])], "back from the create screen"
