"""Moving a stock frame forward: one click, at a painted point, confirmed by a whole log."""

from __future__ import annotations

from jev.clients.advance import Advanced, AdvanceQuestFrame


class _Hid:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, right=False):
        self.clicks.append((x, y, right))


def _skill(values, ids=None, hid=None):
    return AdvanceQuestFrame(hid=hid or _Hid(), read=lambda: values,
                             quest_ids=lambda: ids, window_origin=(10, 38),
                             window_size=(1600, 900))


# Fractions of the interface, not pixels — see `fields.py`.
OPEN = {"ui.quest_frame": True, "ui.gossip": False,
        "ui.advance_x": 0.1875, "ui.advance_y": 0.7778}


def test_nothing_is_clicked_unless_the_radio_says_the_frame_is_open():
    hid = _Hid()
    skill = _skill({"ui.quest_frame": False, "ui.gossip": False}, hid=hid)
    assert skill.run(783) is Advanced.NO_FRAME
    assert hid.clicks == []


def test_nothing_is_clicked_when_no_button_is_painted():
    """Frame open and no advance button means Accept is not what this frame offers —
    a quest already in the log, say. Sweeping for a yellow rectangle is what the painted
    point exists to avoid."""
    hid = _Hid()
    skill = _skill({"ui.quest_frame": True, "ui.advance_x": None, "ui.advance_y": None},
                   hid=hid)
    assert skill.run(783) is Advanced.NO_BUTTON
    assert hid.clicks == []


def test_an_unreadable_frame_is_not_permission():
    hid = _Hid()
    assert _skill(None, hid=hid).run(783) is Advanced.BLIND
    assert hid.clicks == []


def test_exactly_one_click_at_the_painted_point_in_screen_coordinates():
    hid = _Hid()
    skill = _skill(OPEN, ids=(783,), hid=hid)
    assert skill.run(783) is Advanced.ACCEPTED
    assert hid.clicks == [(310, 738, False)], "origin not applied, or more than one click"


def test_the_button_arrives_as_a_fraction_and_is_scaled_by_the_captured_frame():
    """Pixels were tried and were wrong by exactly a hundred on the vertical: the addon's
    conversion mixed the button's effective scale with UIParent's, and UIParent's pixel
    height is not the client height. A ratio needs neither."""
    hid = _Hid()
    _skill(OPEN, ids=(783,), hid=hid).run(783)
    x, y, _ = hid.clicks[0]
    assert x == 10 + round(0.1875 * 1600)
    assert y == 38 + round(0.7778 * 900)


def test_success_is_the_quest_appearing_in_the_log():
    assert _skill(OPEN, ids=(783,)).run(783) is Advanced.ACCEPTED
    result = _skill(OPEN, ids=(7,)).run(783, settle_s=0, confirm_tries=2)
    assert result is Advanced.CLICKED and not result.ok


def test_a_partial_log_is_never_read_as_confirmation():
    """The strip cycles one entry per paint. `None` means the cycle has not completed, and
    treating it as a short log is how a tracker concludes a quest is missing from a log it
    never finished reading."""
    result = _skill(OPEN, ids=None).run(783, settle_s=0, confirm_tries=3)
    assert result is Advanced.CLICKED
    assert "not in the assembled log" in result_detail(_skill(OPEN, ids=None), 783)


def result_detail(skill, quest_id):
    skill.run(quest_id, settle_s=0, confirm_tries=2)
    return skill.detail


def test_accept_complete_and_continue_are_one_skill():
    """They are the same intent at different moments. A caller that had to tell them apart
    would need a state machine for quests it cannot see."""
    import inspect

    from jev.perceive import fields

    src = inspect.getsource(fields)
    assert "ui.advance_x" in src and "ui.advance_y" in src
    assert "ui.accept_x" not in src, "a second pair of fields per button type"
