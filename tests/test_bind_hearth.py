"""Binding the hearthstone at the inn nearest the guide's work (`LiveBody._bind`)."""

from types import SimpleNamespace
from unittest.mock import Mock

from test_live_body import body
from test_runtime_records import seen

from jev.clients.choose import Chose
from jev.clients.interact import Result as Interacted
from jev.coach.policy import Context, decide
from jev.coach.schema import Decision, Intent
from jev.orch.runtime import Armed
from jev.world.home import load_home, save_home
from jev.world.state_v1 import ArmedBy
from jev.world.vendor import Innkeeper

# The body fixture's step stands at world (50, 50) on map 0, in a 100-yard zone.
NEAR = Innkeeper(295, "Innkeeper Farley", 0, (60.0, 50.0, 0.0))


def _body(monkeypatch, tmp_path, inns=(NEAR,), *, near=500.0, far=900.0):
    b = body()
    b.home_memory = tmp_path / "character.home.json"
    monkeypatch.setattr("jev.run.body.innkeepers", lambda map_id, side: inns)
    monkeypatch.setattr("jev.run.body.INN_NEAR_YARDS", near)
    monkeypatch.setattr("jev.run.body.HOME_FAR_YARDS", far)
    return b


def _state():
    return seen().model_copy(update={"guide": seen().guide.model_copy(update={"step_id": "quest"})})


def test_an_inn_near_the_work_is_home_when_home_is_unknown_or_far(monkeypatch, tmp_path):
    b = _body(monkeypatch, tmp_path)
    assert b.bindable(_state()), "home unknown"
    save_home(b.home_memory, (60.0, 50.0, 0.0), name="Innkeeper Farley")
    assert not b.bindable(_state()), "this inn is home already"
    save_home(b.home_memory, (5000.0, 5000.0, 0.0), name="Northshire")
    assert b.bindable(_state()), "home is far from the work"
    b = _body(monkeypatch, tmp_path, inns=())
    assert not b.bindable(_state()), "no inn near the work"


def test_the_policy_binds_as_a_routine_and_once_a_step(monkeypatch, tmp_path):
    context = Context()
    context.bindable = lambda state: True
    plan = decide(_state(), context=context)
    assert plan.decision.skill == "BIND_HEARTH" and plan.rule == "service.bind"
    context.bind_failed("quest")
    assert decide(_state(), context=context).decision.skill != "BIND_HEARTH"
    fresh = Context()
    fresh.bindable = lambda state: True
    assert decide(seen(), context=fresh).decision.skill == "BIND_HEARTH", \
        "a state with no step yet is not a failed one"


def test_binding_chooses_the_line_accepts_and_remembers(monkeypatch, tmp_path):
    b = _body(monkeypatch, tmp_path)
    b.arm = Armed(Decision(goal="bind", intent=Intent.SERVICE, skill="BIND_HEARTH",
                           abort_if=["dead"], why="home is far", confidence=1, params={"service": "bind"}),
                  ArmedBy.POLICY, 0, "quest", "d", "service")
    b.interact = SimpleNamespace(open_on=Mock(return_value=Interacted.GOSSIP), detail="")
    popup = {"up": False}

    def choose(line):
        popup["up"] = True                      # the confirmation follows the line
        return Chose.CHOSE

    chose = Mock(side_effect=choose)
    b.chooser = SimpleNamespace(run=chose, detail="")
    b.client.read = lambda: ({"vitals.hp": 1.0, "ui.modal": True, "ui.advance_x": 0.5,
                              "ui.advance_y": 0.4} if popup["up"]
                             else {"vitals.hp": 1.0, "ui.modal": False, "ui.gossip": False})
    clicks = []

    def click(x, y, **kw):
        clicks.append((x, y))
        popup["up"] = False
        return True

    b.client.hid.click = click
    result = b.execute(b.arm, _state(), lambda: None)
    assert result.outcome.value == "succeeded", result.detail
    chose.assert_called_once_with("Make this inn your home.")
    assert clicks == [(800, 360)], "the confirmation's Accept"
    assert load_home(b.home_memory) == (60.0, 50.0, 0.0)


def test_a_hearthstone_arrival_is_remembered_as_home(monkeypatch, tmp_path):
    from jev.clients.hearth import Hearthed

    b = _body(monkeypatch, tmp_path)
    b.hearth = SimpleNamespace(run=lambda: Hearthed.HOME, detail="")
    b.client.position = lambda: (0.2, 0.3)
    assert b._go_home() is Hearthed.HOME
    home = load_home(b.home_memory)
    assert home is not None and home[2] == 0.0
