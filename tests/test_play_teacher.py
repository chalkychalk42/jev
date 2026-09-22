import asyncio
import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from jev.play.knowledge import LocalKnowledge
from jev.play.observation import EFFECTS
from jev.play.teacher import (
    Capability,
    ClaudeVisionClient,
    ExpectedEffect,
    VisionTeacher,
    reply_schema,
)
from jev.teacher.client import TeacherResult

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def observation():
    return {"id": "observation-1", "screen": {"sha256": hashlib.sha256(PNG).hexdigest()},
            "values": {"target.hp": 1.0}, "context": {"goal": "collect wolf meat"}}


def reply(**changes):
    return {"observation_id": "observation-1", "capability": "approach",
            "action": {"kind": "key", "control": "move_forward", "duration_s": 0.3},
            "lookup": None, "rationale": "Target remains ahead with full health.",
            "expected_effect": "closer", **changes}


def cli_result(**changes):
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": "", "structured_output": reply(),
            "modelUsage": {"claude-test-actual": {"inputTokens": 100, "outputTokens": 20}},
            **changes}


class FakeVision:
    model_name = "claude-sub:requested"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def ask_image(self, prompt, image_png, *, json_schema, timeout_s):
        self.requests.append((json.loads(prompt), image_png, json_schema, timeout_s))
        item = self.replies.pop(0)
        if isinstance(item, TeacherResult):
            return item
        return TeacherResult(status="ok", text=json.dumps(item), model="actual-model",
                             tokens_in=100, tokens_out=20)


def test_image_is_embedded_as_image_and_tools_are_disabled():
    client = ClaudeVisionClient(binary="not-executed")
    prompt = 'literal `shell` $(commands) and "quotes"'
    payload = json.loads(client.image_input(prompt, PNG))
    content = payload["message"]["content"]
    assert content[0]["type"] == "image"
    assert base64.b64decode(content[0]["source"]["data"]) == PNG
    assert content[1] == {"type": "text", "text": prompt}
    argv = client.image_argv(reply_schema())
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv and "--bare" not in argv
    assert prompt not in argv


def test_tutor_effect_vocabulary_matches_independent_observer():
    assert set(get_args(ExpectedEffect)) == EFFECTS


def test_preflight_checks_native_flags_and_subscription_without_exposing_account(monkeypatch):
    invoked = []
    auth = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "email": "private-account@example.invalid", "token": "never-return-this"}
    help_output = " ".join(ClaudeVisionClient().image_argv(reply_schema()))

    def run(argv, **kwargs):
        invoked.append(argv)
        if argv[1:] == ["--version"]:
            output = "2.1.280 (Claude Code)"
        elif argv[1:] == ["--help"]:
            output = help_output
        elif argv[1:] == ["auth", "status", "--json"]:
            output = json.dumps(auth)
        else:
            raise AssertionError("preflight attempted an unexpected command")
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr("jev.play.teacher.subprocess.run", run)
    report = ClaudeVisionClient().preflight()
    assert report["ok"] and report["authenticated"] and report["subscription_auth"]
    assert len(invoked) == 3 and report["model_call_made"] is False
    assert report["vision_roundtrip_verified"] is False
    assert "private-account" not in json.dumps(report) and "never-return-this" not in json.dumps(report)


def test_preflight_unknown_auth_or_missing_flags_is_not_ready(monkeypatch):
    monkeypatch.setattr("jev.play.teacher.subprocess.run", lambda *a, **kw:
                        SimpleNamespace(stdout="{}", stderr="", returncode=0))
    result = ClaudeVisionClient().preflight()
    assert not result["ok"] and not result["authenticated"]


def test_structured_result_retains_actual_model_and_billed_usage():
    output = json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps(cli_result())
    result = ClaudeVisionClient().classify_stream(stdout=output, stderr="", returncode=0, latency_ms=20)
    assert result.ok and json.loads(result.text) == reply()
    assert result.model == "claude-test-actual"
    assert (result.tokens_in, result.tokens_out) == (100, 20)


def test_native_weekly_limit_is_transport_failure_even_with_success_subtype():
    # The first probe retained only a generic failure; the second retained sanitized
    # provider fields. Do not retroactively claim the unrecorded first diagnosis.
    fixture = json.loads((Path(__file__).parent / "fixtures" / "teaching-transport-attempts.json")
                         .read_text())
    first, second = fixture["attempts"]
    assert first["status"] == "transport_or_schema_failed" and "diagnostic" not in first
    assert all(attempt["model_calls"] == 1 and attempt["actual_model"] is None
               for attempt in fixture["attempts"])
    diagnostic = second["diagnostic"]
    document = cli_result(**{key: diagnostic[key] for key in
                             ("subtype", "is_error", "api_error_status", "terminal_reason", "stop_reason")},
                          modelUsage={}, structured_output=None, result=diagnostic["result_error"])
    result = ClaudeVisionClient().classify_stream(
        stdout=json.dumps(document), stderr="", returncode=1, latency_ms=2056)
    assert result.status == "transport" and result.text is None
    assert "429" in result.detail and "weekly limit" in result.detail


@pytest.mark.parametrize("output,code", [
    (json.dumps(cli_result(structured_output=None, result=json.dumps(reply()))), 0),
    (json.dumps(cli_result(is_error=True, result="rate limit", api_error_status=429)), 1),
    (json.dumps(cli_result(subtype="error_max_turns")), 0),
    (json.dumps(cli_result()), 2),
    (json.dumps({"type": "assistant", "message": {"content": []}}), 0),
    ("not json", 0),
    (json.dumps(cli_result()) + "\n" + json.dumps(cli_result()), 0),
])
def test_incomplete_or_failed_transport_can_never_supply_an_action(output, code):
    result = ClaudeVisionClient().classify_stream(stdout=output, stderr="", returncode=code, latency_ms=20)
    assert not result.ok and result.text is None


def test_tutor_receives_controls_image_goal_and_failed_action_history():
    client = FakeVision([reply()])
    recent = [{"action": {"kind": "click"}, "outcome": {"effects": [], "target_hp": 1.0}}]
    result = asyncio.run(VisionTeacher(client).decide(observation(), PNG,
                                                     controls={"bindings": ["move_forward"]}, recent=recent))
    assert result.ok and result.action.control == "move_forward"
    assert result.requested_model == "claude-sub:requested" and result.actual_model == "actual-model"
    prompt, image, schema, _ = client.requests[0]
    assert prompt["recent_action_results"] == recent
    assert prompt["observation"]["context"]["goal"] == "collect wolf meat"
    assert prompt["controls"]["bindings"] == ["move_forward"]
    assert prompt["reply_contract"]["capability"]["allowed"] == list(get_args(Capability))
    assert prompt["reply_contract"]["expected_effect"]["allowed"] == list(get_args(ExpectedEffect))
    assert image == PNG and "action" in schema["properties"]


def test_teacher_projection_preserves_game_state_and_every_control_without_mutating_records(tmp_path):
    from jev.play.controls import build_manifest

    bindings = tmp_path / "bindings.wtf"
    bindings.write_text("bind W MOVEFORWARD\nbind UP MOVEFORWARD\nbind ESCAPE TOGGLEGAMEMENU\n"
                        "bind ENTER OPENCHAT\nbind 1 ACTIONBUTTON1\n"
                        "modifiedclick ALT SELFCAST\n")
    controls = build_manifest(binding_paths=[bindings], skills=["ABORT_WAIT", "TRAVEL_TO"]).to_dict()
    observed = {**observation(), "features": {"screen.0.0.0": 0.125},
                "values": {"ui.modal": False, "target.hp": None, "target.name_id": 7},
                "state": {"quests": [{"quest_id": 33, "objectives": [{"have": 0, "need": 8}]}]},
                "origin": [1, 2], "size": [1600, 900], "freshness": {"paint_generation": 10}}
    recorded_observation, recorded_controls = deepcopy(observed), deepcopy(controls)
    client = FakeVision([reply()])
    result = asyncio.run(VisionTeacher(client).decide(observed, PNG, controls=controls))
    assert result.ok
    prompt, image, _, _ = client.requests[0]
    assert prompt["observation"] == {key: value for key, value in recorded_observation.items()
                                     if key != "features"}
    assert image == PNG
    projected = prompt["controls"]
    references = projected.pop("source_references")
    for rows in (projected["bindings"].values(), projected["action_slots"],
                 projected["binding_inventory"]):
        for row in rows:
            if "source_ref" in row:
                row["source"] = references[row.pop("source_ref")]
    assert projected == recorded_controls
    assert observed == recorded_observation and controls == recorded_controls


@pytest.mark.parametrize("bad,status", [
    (reply(observation_id="an-old-frame"), "stale"),
    (reply(capability="key"), "invalid"),
    (reply(capability="click"), "invalid"),
    (reply(action={"kind": "key", "control": "move_forward", "duration_s": 10.0}), "invalid"),
    (reply(action={"kind": "shell", "cmd": "anything"}), "invalid"),
    (reply(action={"kind": "observe"}, lookup="wolf"), "invalid"),
    (reply(extra="unrecognized"), "invalid"),
    (reply(expected_effect="definitely_succeeded"), "invalid"),
    (reply(expected_effect="observed"), "invalid"),
])
def test_stale_or_out_of_contract_replies_never_reach_executor(bad, status):
    result = asyncio.run(VisionTeacher(FakeVision([bad])).decide(observation(), PNG, controls={}))
    assert result.status == status and not result.ok and result.action is None


@pytest.mark.parametrize("action", [
    {"kind": "key", "control": "move_forward", "duration_s": 0.3},
    {"kind": "click", "button": "left", "intent": "ui", "ui_control": "quest_advance"},
    {"kind": "skill", "name": "ABORT_WAIT"},
    {"kind": "pointer", "x": 0.5, "y": 0.5},
    {"kind": "camera", "axis": "yaw", "pixels": 20},
    {"kind": "action_slot", "slot": 1},
])
def test_modal_contract_is_enforced_even_when_provider_ignores_narrowed_schema(action):
    client = FakeVision([reply(action=action, expected_effect="ui_closed")])
    observed = {**observation(), "values": {"ui.modal": True}}
    result = asyncio.run(VisionTeacher(client).decide(observed, PNG, controls={}))
    assert result.status == "invalid" and result.action is None
    assert "blocking modal" in result.detail
    schema = client.requests[0][2]
    assert schema == reply_schema(observed["values"])
    assert set(schema["$defs"]) == {"ObserveAction", "KeyAction"}
    assert schema["properties"]["action"]["anyOf"][1] == {"type": "null"}
    assert reply_schema({"ui.modal": False}) == reply_schema()


@pytest.mark.parametrize("action,effect", [
    ({"kind": "observe", "wait_s": 0.1}, "observed"),
    ({"kind": "key", "control": "escape", "duration_s": 0}, "ui_closed"),
])
def test_modal_contract_preserves_lookup_and_permitted_actions(action, effect):
    client = FakeVision([reply(action=None, lookup="Young Wolf"),
                         reply(action=action, expected_effect=effect)])
    observed = {**observation(), "values": {"ui.modal": True}}
    result = asyncio.run(VisionTeacher(client, knowledge=LocalKnowledge())
                         .decide(observed, PNG, controls={}))
    assert result.ok and result.action.kind == action["kind"]
    assert len(client.requests) == 2 and result.lookups
    assert client.requests[0][2] == client.requests[1][2] == reply_schema(observed["values"])


def test_lookup_returns_local_facts_then_one_action_and_charges_every_call():
    client = FakeVision([reply(action=None, lookup="Young Wolf"), reply()])
    reserved, recorded = [], []
    tutor = VisionTeacher(client, knowledge=LocalKnowledge(),
                          reserve_call=lambda: reserved.append(True) or True, record_call=recorded.append)
    result = asyncio.run(tutor.decide(observation(), PNG, controls={}))
    assert result.ok and len(reserved) == len(recorded) == len(result.calls) == 2
    assert result.tokens_in == 200 and result.tokens_out == 40
    assert client.requests[1][0]["lookup_results"][0]["records"]
    assert client.requests[1][3] <= client.requests[0][3]


def test_lookup_budget_ends_without_executing_or_unbounded_retries():
    client = FakeVision([reply(action=None, lookup="wolf")] * 2)
    result = asyncio.run(VisionTeacher(client, knowledge=LocalKnowledge(), max_lookups=1)
                         .decide(observation(), PNG, controls={}))
    assert result.status == "invalid" and len(client.requests) == 2


def test_budget_denial_or_changed_image_does_not_make_model_call():
    client = FakeVision([])
    result = asyncio.run(VisionTeacher(client, reserve_call=lambda: False)
                         .decide(observation(), PNG, controls={}))
    assert result.status == "budget" and not client.requests
    changed = {**observation(), "screen": {"sha256": "0" * 64}}
    result = asyncio.run(VisionTeacher(client).decide(changed, PNG, controls={}))
    assert result.status == "invalid" and not client.requests


def test_transport_failure_does_not_claim_requested_model_was_used():
    client = FakeVision([TeacherResult(status="timeout", model="claude-sub:requested", detail="late")])
    result = asyncio.run(VisionTeacher(client).decide(observation(), PNG, controls={}))
    assert result.status == "timeout" and result.actual_model is None
    assert result.requested_model == "claude-sub:requested"


@pytest.mark.parametrize("exception", [RuntimeError("broken transport"), TimeoutError()])
def test_raised_transport_failures_are_accounted_and_return_no_action(exception):
    class Broken(FakeVision):
        async def ask_image(self, *args, **kwargs):
            raise exception

    recorded = []
    result = asyncio.run(VisionTeacher(Broken([]), record_call=recorded.append)
                         .decide(observation(), PNG, controls={}))
    assert not result.ok and result.action is None
    assert len(recorded) == len(result.calls) == 1


def test_decision_deadline_cancels_even_a_transport_that_ignores_its_timeout():
    cancelled, recorded = [], []

    class Slow(FakeVision):
        async def ask_image(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    result = asyncio.run(VisionTeacher(Slow([]), record_call=recorded.append)
                         .decide(observation(), PNG, controls={}, timeout_s=0.01))
    assert result.status == "timeout" and cancelled == [True]
    assert len(recorded) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable shim")
def test_real_subprocess_protocol_sends_image_and_reads_structured_reply(tmp_path):
    shim = tmp_path / "vision-shim"
    output = json.dumps(cli_result())
    shim.write_text(f"#!{sys.executable}\nimport json,sys\n"
                    "data=json.loads(sys.stdin.readline())\n"
                    "assert data['message']['content'][0]['type']=='image'\n"
                    "assert sys.argv[sys.argv.index('--tools')+1]==''\n"
                    f"print({output!r})\n")
    shim.chmod(0o755)
    result = asyncio.run(ClaudeVisionClient(binary=str(shim)).ask_image(
        "observe", PNG, json_schema=reply_schema(), timeout_s=2))
    assert result.ok and result.model == "claude-test-actual"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable shim")
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_kill_and_reap_child(tmp_path, cancel):
    marker = tmp_path / "survived"
    ready = tmp_path / "ready"
    shim = tmp_path / "slow-vision"
    shim.write_text(f"#!{sys.executable}\nimport time,pathlib\n"
                    f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
                    "time.sleep(0.4)\n"
                    f"pathlib.Path({str(marker)!r}).write_text('survived')\n")
    shim.chmod(0o755)

    async def scenario():
        task = asyncio.create_task(ClaudeVisionClient(binary=str(shim)).ask_image(
            "observe", PNG, json_schema=reply_schema(), timeout_s=10 if cancel else 0.15))
        if cancel:
            for _ in range(100):
                if ready.exists():
                    break
                await asyncio.sleep(0.005)
            assert ready.exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).status == "timeout"
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert not marker.exists()
