"""Moving a stock frame forward: painted points, pressed until the log reaches a goal."""

from __future__ import annotations

from jev.clients.advance import Advanced, AdvanceQuestFrame, Goal


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


def test_accepting_is_one_click_at_the_painted_point_in_screen_coordinates():
    hid = _Hid()
    skill = _skill(OPEN, ids=(783,), hid=hid)
    assert skill.run(783) is Advanced.DONE
    assert hid.clicks == [(310, 738, False)], "origin not applied, or pressed after the goal"


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
    assert _skill(OPEN, ids=(783,)).run(783) is Advanced.DONE
    result = _skill(OPEN, ids=(7,)).run(783, settle_s=0, confirm_tries=2)
    assert result is Advanced.CLICKED and not result.ok


def test_a_partial_log_is_never_read_as_confirmation():
    """The strip cycles one entry per paint. `None` means the cycle has not completed, and
    treating it as a short log is how a tracker concludes a quest is missing from a log it
    never finished reading."""
    result = _skill(OPEN, ids=None).run(783, settle_s=0, confirm_tries=3)
    assert result is Advanced.CLICKED
    assert "absent from the assembled log" in result_detail(_skill(OPEN, ids=None), 783)


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


# -- turning in: the same skill, the opposite goal ----------------------------------

def _stepping(*frames, ids_at):
    """A client that moves on: each read returns the next frame, and the log is whatever
    `ids_at` says it is once that many presses have happened."""
    hid = _Hid()
    state = {"read": 0}

    def read():
        i = min(state["read"], len(frames) - 1)
        state["read"] += 1
        return frames[i]

    skill = AdvanceQuestFrame(hid=hid, read=read,
                              quest_ids=lambda: ids_at(len(hid.clicks)),
                              window_origin=(10, 38), window_size=(1600, 900))
    return skill, hid


SHUT = {"ui.quest_frame": False, "ui.gossip": False}


def test_turning_in_takes_two_presses_and_this_is_the_same_skill():
    """Continue moves the progress page to the reward page; Complete Quest closes it. A
    skill capped at one press cannot turn anything in, which is why the cap is the goal
    and not a count."""
    skill, hid = _stepping(OPEN, OPEN, SHUT, ids_at=lambda n: (783,) if n < 2 else ())
    assert skill.run(783, Goal.CLEARED, settle_s=0) is Advanced.DONE
    assert len(hid.clicks) == 2, "a turn-in is Continue then Complete Quest"


def test_the_frame_closing_after_a_press_is_how_these_flows_end():
    """`NO_FRAME` is a refusal to start, not a failure to finish. Reading a shut frame as
    an error would fail every successful accept."""
    skill, hid = _stepping(OPEN, SHUT, ids_at=lambda n: (783,) if n else ())
    assert skill.run(783, settle_s=0) is Advanced.DONE
    assert len(hid.clicks) == 1


def test_the_goal_is_a_state_of_the_log_not_a_direction():
    """`HELD` and `CLEARED` are the only difference between accepting and turning in, and
    each is wrong for the other — otherwise a turn-in would confirm on the frame it was
    trying to clear."""
    assert _skill(OPEN, ids=(783,)).run(783, Goal.HELD) is Advanced.DONE
    assert _skill(OPEN, ids=(783,)).run(783, Goal.CLEARED,
                                        settle_s=0, confirm_tries=2) is Advanced.CLICKED
    assert _skill(OPEN, ids=()).run(783, Goal.CLEARED) is Advanced.DONE
    assert _skill(OPEN, ids=()).run(783, Goal.HELD,
                                    settle_s=0, confirm_tries=2) is Advanced.CLICKED


def test_a_partial_log_is_never_read_as_a_turn_in():
    """`CLEARED` is the dangerous direction: mid-cycle the strip has painted no slots, so
    every quest looks absent. `None` has to mean unread here too."""
    result = _skill(OPEN, ids=None).run(783, Goal.CLEARED, settle_s=0, confirm_tries=3)
    assert result is Advanced.CLICKED, "an unfinished cycle read as an empty log"


def test_pressing_is_bounded_and_every_press_re_reads_the_radio():
    """A frame that will not advance — a reward page waiting for a choice — is pressed a
    few times and then reported, not hammered. The bound is small because pressing harder
    has never been the answer."""
    hid = _Hid()
    reads = {"n": 0}

    def read():
        reads["n"] += 1
        return OPEN

    skill = AdvanceQuestFrame(hid=hid, read=read, quest_ids=lambda: (7,),
                              window_origin=(10, 38), window_size=(1600, 900))
    assert skill.run(783, settle_s=0, confirm_tries=1) is Advanced.CLICKED
    assert len(hid.clicks) == 4
    assert reads["n"] == 4, "a press that did not re-read the radio is a blind press"


def test_a_reward_page_waiting_for_a_choice_gets_the_painted_default_first():
    """Measured 23 September: "Wolves Across the Border" offered two rewards, and three
    presses of Complete Quest changed nothing. The addon paints its default choice; the
    skill clicks it, then the next read presses Complete Quest."""
    choice = {**OPEN, "ui.choice_count": 2, "ui.choice_made": False,
              "ui.choice_x": 0.05, "ui.choice_y": 0.40}
    chosen = {**choice, "ui.choice_made": True}
    skill, hid = _stepping(OPEN, choice, chosen, SHUT,
                           ids_at=lambda n: (783,) if n < 3 else ())
    assert skill.run(783, Goal.CLEARED, settle_s=0) is Advanced.DONE
    assert [c[:2] for c in hid.clicks] == [(10 + 300, 38 + 700), (10 + 80, 38 + 360),
                                           (10 + 300, 38 + 700)]


def test_choosing_which_reward_is_best_is_not_this_skill():
    """Naming what it does not do keeps the next failure from being answered with a
    fourth button in the list. The default choice is the addon's painted point; ranking
    rewards is a decision, not a button."""
    import inspect

    from jev.clients import advance

    src = inspect.getsource(advance)
    assert "Not covered, deliberately" in src, "the limitation is not written down"
    for picking in ("QuestInfoItem", "GetQuestReward", "choice_index", "best_reward"):
        assert picking not in src, f"{picking}: reward choice leaking into the button skill"
