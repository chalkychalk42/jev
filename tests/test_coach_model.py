"""Jev, the coach's model: its call, its cap, and the decisions it is given (PLAN §9)."""

from __future__ import annotations

import json
import random

import httpx

from jev.clients.source import ScriptedSource
from jev.coach import judge as jev_judge
from jev.coach import policy
from jev.coach.judge import Judge
from jev.coach.model import Answer, Budget, CoachModel
from jev.guide.graph import Graph
from jev.learn.choices import Choice, ChoiceLog, ChoiceMemory, Stations, station_key
from jev.learn.episode import Recorder, read
from jev.orch.runtime import ClientRuntime
from jev.world.state_v1 import ArmedBy, Bags, Pos, Sense, State, Ui, Vitals

GRAPH = "content/tbc/ally_human_1_12.json"
OPTIONS = {"frostbolt": "cast Frostbolt", "drink": "stop and drink"}


def _reply(choice="frostbolt", tokens=450, confidence=0.4):
    return {"model": "jev-1.13.0", "usage": {"input_tokens": tokens, "output_tokens": 40},
            "answers": {"choice": {"type": "choice", "choice": choice, "confidence": confidence,
                                   "probabilities": {"frostbolt": 0.7, "drink": 0.3}}}}


def _model(tmp_path, handler, usd_per_day=10.0) -> CoachModel:
    return CoachModel("key", budget=Budget(tmp_path / "usage.json", usd_per_day),
                      record=tmp_path / "calls.jsonl", transport=httpx.MockTransport(handler))


def test_an_answer_is_a_choice_among_the_options_offered(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_reply())

    answer = _model(tmp_path, handler).choose("fight.next", {"me": {"lvl": 9}}, "Best?", OPTIONS)
    assert answer.ok and answer.choice == "frostbolt" and answer.tokens == 450
    assert answer.probabilities == {"frostbolt": 0.7, "drink": 0.3}
    body = seen[0]
    assert body["model"] == "jev-latest"
    assert body["questions"]["choice"] == {"type": "choice", "instructions": "Best?",
                                           "criteria": OPTIONS}
    row = read(tmp_path / "calls.jsonl")[0]
    assert row["point"] == "fight.next" and row["status"] == "ok" and row["options"] == OPTIONS


def test_an_answer_naming_no_option_offered_is_an_error_not_a_choice(tmp_path):
    model = _model(tmp_path, lambda request: httpx.Response(200, json=_reply("flee")))
    answer = model.choose("p", {}, "?", OPTIONS)
    assert answer.status == "error" and answer.choice is None and not answer.ok


def test_a_refusal_or_a_timeout_is_said_as_itself_and_never_retried(tmp_path):
    calls = []

    def busy(request):
        calls.append(1)
        return httpx.Response(429)

    assert _model(tmp_path, busy).choose("p", {}, "?", OPTIONS).status == "error"
    assert len(calls) == 1

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert _model(tmp_path, slow).choose("p", {}, "?", OPTIONS).status == "timeout"


def test_no_key_is_no_call(tmp_path):
    model = CoachModel(None, record=tmp_path / "calls.jsonl")
    assert model.choose("p", {}, "?", OPTIONS).status == "off"
    assert not model.available


def test_the_days_spend_is_capped_across_processes(tmp_path):
    handler = lambda request: httpx.Response(200, json=_reply(tokens=1_000_000))  # noqa: E731
    first = _model(tmp_path, handler, usd_per_day=0.05)
    assert first.choose("p", {}, "?", OPTIONS).ok             # $0.04 spent
    assert first.choose("p", {}, "?", OPTIONS).ok             # $0.08: over, but asked below it
    again = _model(tmp_path, handler, usd_per_day=0.05)       # another process, same var/
    assert again.choose("p", {}, "?", OPTIONS).status == "budget"
    usage = json.loads((tmp_path / "usage.json").read_text())
    assert usage["calls"] == 2 and usage["status"] == {"ok": 2}


def test_yesterdays_spend_is_not_todays(tmp_path):
    (tmp_path / "usage.json").write_text(json.dumps({"day": "2000-01-01", "usd": 99.0}))
    assert Budget(tmp_path / "usage.json", 1.0).allows()


class FakeModel:
    """A model that answers from a script and remembers what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def choose(self, point, state, instructions, options, *, record=None):
        self.asked.append((point, state, options))
        choice = self.answers.pop(0) if self.answers else None
        return (Answer("ok", choice=choice) if choice in options
                else Answer("error", detail="no answer"))


def _state(**kw) -> State:
    base = dict(vitals=Vitals(hp=1.0, power=1.0, dead=False, ghost=False, combat=False),
                bags=Bags(free=10, durability_min=1.0, money_copper=0),
                pos=Pos(zone="Elwynn", zone_id=12, coord_zone_id=12, mx=0.5, my=0.5),
                ui=Ui(loot=False, modal=False), sense=Sense(addon_ok=True, vision_conf=1.0))
    return State(t=0.0, client_id="c", **{**base, **kw})


def test_every_service_due_is_a_candidate_and_the_first_is_the_floors():
    state = _state(bags=Bags(free=1, durability_min=0.2, money_copper=10_000))
    plans = policy.services(state, context=policy.Context())
    assert [p.rule for p in plans] == ["service.bags_full", "service.durability"]
    assert policy.service(state, context=policy.Context()).rule == "service.bags_full"


def test_a_preempt_or_a_fight_is_never_put_to_jev():
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    for state in (_state(vitals=Vitals(dead=True)),
                  _state(vitals=Vitals(hp=0.9, combat=True))):
        floor = policy.decide(state, node)
        assert jev_judge.candidates(state, node, policy.Context(), floor) == []


def test_jev_picks_among_the_coachs_own_plans_and_a_single_plan_is_not_asked():
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    model = FakeModel("guide.travel")
    judge = Judge(model)
    state = _state(bags=Bags(free=1, durability_min=1.0, money_copper=0))
    floor = policy.decide(state, node, context=policy.Context())
    options = jev_judge.candidates(state, node, policy.Context(), floor)
    assert floor.rule == "service.bags_full"
    picked = judge.arm(state, node, options)
    assert picked is not None and picked.rule == "guide.travel"
    point, seen, offered = model.asked[0]
    assert point == "coach.arm" and set(offered) == {"service.bags_full", "guide.travel"}
    assert seen["bags"]["free"] == 1 and seen["step"]["kind"] == node.kind.value
    # The same question again within the minute keeps its answer.
    assert judge.arm(state, node, options).rule == "guide.travel" and len(model.asked) == 1
    assert judge.arm(state, node, options[:1]) is None and len(model.asked) == 1


def test_the_runtime_arms_jevs_pick_and_says_so(tmp_path):
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    at = dict(pos=Pos(zone=node.zone, zone_id=node.zone_id, coord_zone_id=node.coord_zone_id,
                      mx=node.pos[0] + 0.2, my=node.pos[1]),
              bags=Bags(free=1, durability_min=1.0, money_copper=0))
    states = [State(t=float(i), client_id="c01", **{**_state().model_dump(
        exclude={"t", "client_id", "pos", "bags"}), **at}) for i in range(3)]
    model = FakeModel("guide.travel")
    runtime = ClientRuntime(client_id="c01", graph=graph, source=ScriptedSource(states),
                            recorder=Recorder(root=tmp_path), judge=Judge(model))
    runtime.run(ticks=1, period_s=0)
    assert runtime.armed.by is ArmedBy.JEV and runtime.armed.rule == "jev:guide.travel"
    assert runtime.armed.decision.skill == "TRAVEL_TO"
    assert runtime.counters.jev_applied == 1


def test_no_answer_leaves_the_floor(tmp_path):
    graph = Graph.load(GRAPH)
    node = graph.get(graph.entry)
    states = [State(t=float(i), client_id="c01", **{**_state().model_dump(
        exclude={"t", "client_id", "bags"}), "bags": Bags(free=1, durability_min=1.0)})
        for i in range(2)]
    runtime = ClientRuntime(client_id="c01", graph=graph, source=ScriptedSource(states),
                            recorder=Recorder(root=tmp_path), judge=Judge(FakeModel()))
    runtime.run(ticks=1, period_s=0)
    assert runtime.armed.by is ArmedBy.POLICY and runtime.armed.rule == "service.bags_full"
    assert node is not None


def test_a_jev_armed_rule_is_still_the_coachs_rule():
    assert policy.reflex("jev:fight.rotation") and not policy.reflex("jev:guide.step")
    assert policy.routine_only("jev:service.train") and policy.own_rule("guide.step") == "guide.step"


def test_jev_picks_a_learned_choice_with_the_records_in_front_of_it(tmp_path):
    memory = ChoiceMemory(tmp_path / "choices.json")
    memory.record("fight.heal_below", "all@0.4", True, 30.0)
    model = FakeModel("0.4")
    log = ChoiceLog(tmp_path / "choices.jsonl")
    choice = Choice(memory, "fight.heal_below", log=log, rng=random.Random(1),
                    judge=Judge(model), judged=frozenset({"all"}))
    assert choice.pick("all", ["0.3", "0.4", "0.5"]) == "0.4"
    point, seen, offered = model.asked[0]
    assert point == "fight.heal_below" and offered["0.4"].startswith("tried 1, paid off 1")
    assert offered["0.3"] == "never tried" and seen["choice"]["objective"] == "all"
    assert read(tmp_path / "choices.jsonl")[0]["by"] == "jev"
    # An objective Jev is not given is drawn as before, and asks nothing.
    assert choice.pick("pack", ["0.3", "0.4"]) in ("0.3", "0.4") and len(model.asked) == 1


def test_jev_picks_where_a_lap_begins_once_a_minute(tmp_path):
    memory = ChoiceMemory(tmp_path / "choices.json")
    stations = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (200.0, 0.0, 0.0)]

    class Nearest(FakeModel):
        def choose(self, point, state, instructions, options, *, record=None):
            self.asked.append((point, state, options))
            return Answer("ok", choice=next(k for k, text in options.items()
                                            if text.startswith("10 yd away")))

    class Here(Judge):
        def origin(self):
            return (190.0, 0.0)

    model = Nearest()
    clock = iter([0.0, 10.0, 100.0]).__next__
    chooser = Stations(memory, "hunt.station", "creature:1", rng=random.Random(2),
                       clock=clock, judge=Here(model))
    assert chooser.order(stations)[0] == stations[2]
    assert all(text.endswith("never tried") for text in model.asked[0][2].values())
    chooser.order(stations)
    assert len(model.asked) == 1                 # ten seconds on: the draw alone
    assert chooser.order(stations)[0] == stations[2] and len(model.asked) == 2


def test_jev_is_told_where_the_character_stands_by_the_body():
    judge = Judge(FakeModel(), where=lambda: (10.0, 20.0))
    assert judge.origin() == (10.0, 20.0)
    assert Judge(FakeModel()).origin() is None
