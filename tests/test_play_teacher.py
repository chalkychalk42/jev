import asyncio
import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev.play import tutor
from jev.play.controls import build_manifest
from jev.play.knowledge import LocalKnowledge
from jev.play.teacher import MAX_TRANSIENT_RETRIES, ClaudeVisionClient, VisionTeacher
from jev.teacher.client import TeacherResult

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


ALIVE = {"ui.modal": False, "vitals.dead": False, "vitals.ghost": False, "target.has": True,
         "target.hp": 1.0, "target.name_id": 2864, "target.reaction": 4, "seq": 5}
CONTROLS = build_manifest(ALIVE, skills=("COMBAT_PROFILE", "LOOT")).to_dict()


def observation(**values):
    return {"id": "observation-1", "screen": {"sha256": hashlib.sha256(PNG).hexdigest()},
            "values": {**ALIVE, **values}, "size": [1600, 900],
            "context": {"goal": "collect wolf meat", "skill": "GRIND_UNTIL",
                        "target_name": "Young Wolf", "target_name_id": 2864},
            "detections": {"selected_plate": {"x": 0.81, "y": 0.41}, "plates": []}}


def reply(**changes):
    return {"observation_id": "observation-1", "action": "move_forward", "seconds": 0.3,
            "why": "Target remains ahead with full health.", **changes}


def reply_schema():
    return tutor.schema(tutor.menu(observation(), CONTROLS))


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
        self.requests.append((prompt, image_png, json_schema, timeout_s))
        # The last reply repeats, so a re-ask of a bad reply gets the same bad reply.
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
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


def test_tutor_sees_goal_state_plate_history_and_only_the_available_actions():
    client = FakeVision([reply()])
    recent = [{"action": {"kind": "key", "control": "turn_right", "duration_s": 0.25},
               "outcome": {"effects": ["observed", "faced"], "success": True,
                           "reason": "observed faced"}, "delivery": {"code": "delivered"}}]
    result = asyncio.run(VisionTeacher(client).decide(
        observation(), PNG, controls=CONTROLS, recent=recent, skills=("COMBAT_PROFILE",)))
    assert result.ok and result.action.control == "move_forward"
    assert result.action.duration_s == 0.3
    assert result.requested_model == "claude-sub:requested" and result.actual_model == "actual-model"
    prompt, image, schema, _ = client.requests[0]
    assert "observation_id: observation-1" in prompt
    assert "Unit to find: Young Wolf" in prompt
    assert "(0.31 right of centre: turn right to face it)" in prompt
    assert "turn_right 0.25s -> worked (delivered); effects: faced" in prompt
    assert "- skill:COMBAT_PROFILE:" in prompt and "loot_corpse" not in prompt
    assert image == PNG
    assert schema["properties"]["action"]["enum"] == [c.name for c in tutor.menu(
        observation(), CONTROLS, skills=("COMBAT_PROFILE",))]


def test_the_prompt_is_compact_and_records_are_not_mutated():
    """28,080 tokens went into one modal request, 26k characters of it unusable bindings."""
    observed, controls = observation(), deepcopy(CONTROLS)
    recorded_observation, recorded_controls = deepcopy(observed), deepcopy(controls)
    client = FakeVision([reply()])
    result = asyncio.run(VisionTeacher(client, knowledge=LocalKnowledge())
                         .decide(observed, PNG, controls=controls))
    assert result.ok
    prompt = client.requests[0][0]
    assert len(prompt) < 6000, len(prompt)
    assert "binding_inventory" not in prompt and "TOGGLEPETBOOK" not in prompt
    assert observed == recorded_observation and controls == recorded_controls


@pytest.mark.parametrize("bad,status,why", [
    (reply(observation_id="an-old-frame"), "stale", "observation_id"),
    (reply(action="key"), "invalid", "not available"),
    (reply(action="loot_corpse", x=0.5, y=0.5), "invalid", "not available"),
    (reply(seconds=None), "invalid", "needs seconds"),
    (reply(seconds=10.0), "invalid", "seconds"),
    (reply(seconds=True), "invalid", "seconds"),
    (reply(seconds=0.01), "invalid", "at least"),
    (reply(x=0.5), "invalid", "does not take x"),
    (reply(action="interact_unit", x=812, y=400), "invalid", "x"),
    (reply(action="lookup", query="wolf"), "invalid", "not available"),
    ({"action": "observe"}, "invalid", "observation_id"),
])
def test_stale_or_out_of_contract_replies_never_reach_executor(bad, status, why):
    result = asyncio.run(VisionTeacher(FakeVision([bad])).decide(
        observation(), PNG, controls=CONTROLS))
    assert result.status == status and not result.ok and result.action is None
    assert why in result.detail


def test_unit_clicks_bind_identity_from_the_observation_not_the_model():
    client = FakeVision([reply(action="interact_unit", x=0.81, y=0.5, seconds=None)])
    result = asyncio.run(VisionTeacher(client).decide(observation(), PNG, controls=CONTROLS))
    assert result.ok
    assert (result.action.intent, result.action.button) == ("interact", "right")
    assert result.action.expected_target_id == 2864 and result.action.expected_dead is False
    corpse = observation(**{"target.hp": 0.0})
    client = FakeVision([reply(action="loot_corpse", x=0.5, y=0.6, seconds=None)])
    result = asyncio.run(VisionTeacher(client).decide(corpse, PNG, controls=CONTROLS))
    assert result.ok and result.action.expected_dead is True


@pytest.mark.parametrize("action", [
    reply(), reply(action="quest_advance", seconds=None),
    reply(action="skill:COMBAT_PROFILE", seconds=None),
    reply(action="camera_yaw", pixels=20, seconds=None), reply(action="slot_1", seconds=None),
])
def test_modal_contract_is_enforced_even_when_provider_ignores_narrowed_schema(action):
    client = FakeVision([action])
    observed = observation(**{"ui.modal": True})
    result = asyncio.run(VisionTeacher(client).decide(observed, PNG, controls=CONTROLS,
                                                      skills=("COMBAT_PROFILE",)))
    assert result.status == "invalid" and result.action is None
    assert "not available" in result.detail
    schema = client.requests[0][2]
    assert schema["properties"]["action"]["enum"] == ["observe", "escape"]


@pytest.mark.parametrize("action,kind", [
    ({"action": "observe", "seconds": 0.1}, "observe"),
    ({"action": "escape"}, "key"),
])
def test_modal_contract_preserves_lookup_and_permitted_actions(action, kind):
    first = {"observation_id": "observation-1", "action": "lookup", "query": "Young Wolf",
             "why": "check"}
    client = FakeVision([first, {"observation_id": "observation-1", "why": "close it",
                                 **action}])
    observed = observation(**{"ui.modal": True})
    result = asyncio.run(VisionTeacher(client, knowledge=LocalKnowledge())
                         .decide(observed, PNG, controls=CONTROLS))
    assert result.ok and result.action.kind == kind
    assert len(client.requests) == 2 and result.lookups
    assert "LOOKUP RESULTS" in client.requests[1][0]


def test_lookup_returns_local_facts_then_one_action_and_charges_every_call():
    lookup = reply(action="lookup", query="Young Wolf", seconds=None)
    client = FakeVision([lookup, reply()])
    reserved, recorded = [], []
    tutor_ = VisionTeacher(client, knowledge=LocalKnowledge(),
                           reserve_call=lambda: reserved.append(True) or True,
                           record_call=recorded.append)
    result = asyncio.run(tutor_.decide(observation(), PNG, controls=CONTROLS))
    assert result.ok and len(reserved) == len(recorded) == len(result.calls) == 2
    assert result.tokens_in == 200 and result.tokens_out == 40
    assert result.lookups[0]["records"]
    assert client.requests[1][3] <= client.requests[0][3]


def test_lookup_budget_ends_without_executing_or_unbounded_retries():
    lookup = reply(action="lookup", query="wolf", seconds=None)
    client = FakeVision([lookup] * 2)
    result = asyncio.run(VisionTeacher(client, knowledge=LocalKnowledge(), max_lookups=1)
                         .decide(observation(), PNG, controls=CONTROLS))
    assert result.status == "invalid" and len(client.requests) == 3   # lookup, bad, re-ask


def test_a_transient_provider_failure_is_retried_within_the_deadline_and_charged():
    busy = TeacherResult(status="transport", model="claude-sub:requested",
                         detail="overloaded", retry_after_s=0.01)
    client = FakeVision([busy, reply()])
    reserved, recorded, slept = [], [], []

    async def sleep(seconds):
        slept.append(seconds)

    result = asyncio.run(VisionTeacher(client, reserve_call=lambda: reserved.append(1) or True,
                                       record_call=recorded.append, sleep=sleep)
                         .decide(observation(), PNG, controls=CONTROLS))
    assert result.ok and len(result.calls) == len(reserved) == len(recorded) == 2
    assert slept == [2.0], "the first wait is the backoff floor, not the provider's hint alone"


def test_an_invalid_reply_is_asked_for_once_more_with_its_rejection():
    client = FakeVision([reply(seconds=None), reply()])
    result = asyncio.run(VisionTeacher(client).decide(observation(), PNG, controls=CONTROLS))
    assert result.ok and len(result.calls) == 2
    assert "YOUR PREVIOUS REPLY WAS REJECTED: move_forward needs seconds" in client.requests[1][0]
    assert "REJECTED" not in client.requests[0][0]


def test_transient_retries_are_bounded_and_permanent_failures_are_not_retried():
    busy = TeacherResult(status="transport", model="m", detail="overloaded", retry_after_s=0.0)
    client = FakeVision([busy] * (MAX_TRANSIENT_RETRIES + 2))

    async def sleep(_seconds):
        return None

    result = asyncio.run(VisionTeacher(client, sleep=sleep).decide(
        observation(), PNG, controls=CONTROLS))
    assert result.status == "transport" and len(result.calls) == MAX_TRANSIENT_RETRIES + 1
    broke = TeacherResult(status="transport", model="m", detail="insufficient balance")
    result = asyncio.run(VisionTeacher(FakeVision([broke, reply()]), sleep=sleep).decide(
        observation(), PNG, controls=CONTROLS))
    assert result.status == "transport" and len(result.calls) == 1


def test_budget_denial_or_changed_image_does_not_make_model_call():
    client = FakeVision([])
    result = asyncio.run(VisionTeacher(client, reserve_call=lambda: False)
                         .decide(observation(), PNG, controls=CONTROLS))
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
