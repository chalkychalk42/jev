"""Choosing a list line by name, and refusing when the name does not decide it."""

from __future__ import annotations

from jev.clients.choose import ChooseListLine, Chose
from jev.perceive.fields import FIELDS
from jev.perceive.radio_frame import RadioReading, SenseFault, list_lines, name_id


class _Hid:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))


def reading(*titles, x=0.12, y0=0.34, dy=0.03):
    """A strip painting a list of these titles, in frame order."""
    v = {f.name: None for f in FIELDS}
    v["ui.gossip"] = True
    v["ui.list_x"] = x if titles else None
    for i, title in enumerate(titles):
        v[f"ui.list_y{i}"] = y0 + i * dy
        v[f"ui.list_hash{i}"] = name_id(title)
    return RadioReading(values=v, ok=True, fault=SenseFault.NONE)


def _skill(*readings, hid=None):
    seq = list(readings)
    state = {"i": 0}

    def read():
        r = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return r

    return ChooseListLine(hid=hid or _Hid(), read=read,
                          window_origin=(10, 38), window_size=(1600, 900))


def test_a_list_decodes_in_frame_order():
    lines = list_lines(reading("A Threat Within", "Kobold Camp Cleanup"))
    assert [line.index for line in lines] == [0, 1]
    assert lines[0].name_id == name_id("A Threat Within")
    assert lines[1].y > lines[0].y, "lines are painted top down"


def test_a_line_with_no_hash_is_not_clickable():
    """Position without identity is exactly what this skill exists to not act on."""
    r = reading("A Threat Within", "Kobold Camp Cleanup")
    r.values["ui.list_hash1"] = None
    assert [line.index for line in list_lines(r)] == [0]


def test_nothing_is_clicked_when_no_list_is_showing():
    hid = _Hid()
    skill = _skill(reading(), hid=hid)
    assert skill.run("A Threat Within") is Chose.NO_LIST
    assert hid.clicks == []


def test_nothing_is_clicked_when_no_line_matches():
    hid = _Hid()
    skill = _skill(reading("Kobold Camp Cleanup"), hid=hid)
    assert skill.run("A Threat Within") is Chose.NO_MATCH
    assert hid.clicks == []


def test_two_lines_of_the_same_name_are_refused_rather_than_guessed():
    """Two hand-ins with one title are indistinguishable on the strip. A click would be a
    coin toss that opens the wrong quest and moves the playhead somewhere it did not ask
    to be."""
    hid = _Hid()
    skill = _skill(reading("A Threat Within", "A Threat Within"), hid=hid)
    assert skill.run("A Threat Within") is Chose.AMBIGUOUS
    assert hid.clicks == []


def test_the_matching_line_is_clicked_once_at_its_painted_point():
    hid = _Hid()
    before = reading("Kobold Camp Cleanup", "A Threat Within")
    skill = _skill(before, reading(), hid=hid)
    assert skill.run("A Threat Within") is Chose.CHOSE
    assert len(hid.clicks) == 1
    x, y, right = hid.clicks[0]
    assert right is False
    assert (x, y) == (10 + round(0.12 * 1600), 38 + round(0.37 * 900))
    assert skill.line.index == 1, "matched by name, not by being first"


def test_the_same_list_still_showing_is_not_success():
    """Clicking a line always closes or replaces the list. Unchanged means the click
    missed, which is a position problem and worth telling apart from one that worked."""
    same = reading("A Threat Within")
    skill = _skill(same, same)
    assert skill.run("A Threat Within") is Chose.NO_CHANGE


def test_an_unreadable_frame_is_not_permission():
    hid = _Hid()
    assert _skill(None, hid=hid).run("A Threat Within") is Chose.BLIND
    assert hid.clicks == []


def test_the_hash_is_the_one_that_identifies_a_target():
    """Both sides already agree on `fnv1a16` for unit names, and that agreement is proven
    live. A second hash for list text would be a second thing to keep in step."""
    import inspect

    from jev.clients import choose

    assert "name_id" in inspect.getsource(choose)
    assert name_id("Deputy Willem") == name_id("Deputy Willem")


def test_the_guide_carries_the_title_this_matches_against():
    """Parsing it back out of `objectives[0]` would mean hashing "turn in A Threat
    Within", which is not what the client draws."""
    from jev.guide.graph import Graph

    g = Graph.load("content/tbc/ally_human_1_12.json")
    node = g.get("alli_human_1_12_783_a_threat_within_turnin")
    assert node.title == "A Threat Within"
    assert node.title not in node.objectives[0].split(node.title)[0], "title is a fact"


def test_markup_is_stripped_before_the_line_is_hashed():
    """The client draws a gossip line as markup, not as a title: `A Threat Within` arrives
    as `|cXXXXXXXXA Threat Within|r`, 27 characters for a 15-character quest. Measured off
    a live strip, which is how this was found.

    Hashing it raw is wrong twice. It does not match the title the guide holds — the
    immediate bug — and the colour is the quest's **difficulty relative to the character's
    level**, so the same quest hashes differently at level 1 and at level 10. That second
    one would not have failed here. It would have started failing weeks later, on a quest
    that used to work.
    """
    import pathlib

    lua = pathlib.Path("addons/JevRadio/Helpers.lua").read_text(encoding="utf-8")
    assert "local function plain(s)" in lua
    for escape in ("|c%x%x%x%x%x%x%x%x", "|r", "|T.-|t", "|H.-|h(.-)|h"):
        assert escape in lua, f"{escape} is not stripped"
    # Applied where it matters: the hash, not just defined.
    line = next(ln for ln in lua.splitlines() if "return nameid(text)" in ln)
    assert line, "the hash no longer goes through nameid"
    assert "plain(btn.GetText and btn:GetText())" in lua, "the raw text is hashed again"


def test_the_title_the_guide_holds_has_no_markup():
    """The other half of the agreement. Both sides hash a plain title or neither works."""
    from jev.guide.graph import Graph

    g = Graph.load("content/tbc/ally_human_1_12.json")
    for node in g.nodes:
        assert "|" not in node.title, f"{node.id} carries markup in its title"
