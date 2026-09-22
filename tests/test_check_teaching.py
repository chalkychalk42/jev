import json
from pathlib import Path

from jev.teacher.client import TeacherResult
from tools.check_teaching import observe_schema, parser, run

FIXTURE = (Path(__file__).parent / "fixtures" / "target-localization-evaluation"
           / "000099-20260922T182233.847933Z.png")


class FakeTransport:
    model_name = "claude-sub:requested"

    def __init__(self, *, lookup=False):
        self.calls = 0
        self.lookup = lookup

    async def ask_image(self, prompt, image_png, *, json_schema, timeout_s):
        self.calls += 1
        data = json.loads(prompt)
        assert data["observation"]["synthetic"] is True
        assert data["controls"]["allowed_actions"] == [{"kind": "observe", "wait_s": 0}]
        assert image_png == FIXTURE.read_bytes()
        reply = {"observation_id": data["observation"]["id"], "capability": "observe",
                 "action": None if self.lookup else {"kind": "observe", "wait_s": 0.0},
                 "lookup": "Young Wolf" if self.lookup else None,
                 "rationale": "Offline transport verification only.", "expected_effect": "observed"}
        return TeacherResult(status="ok", text=json.dumps(reply), model="actual-test-model",
                             tokens_in=100, tokens_out=20)


def test_default_readiness_is_offline_without_transport_or_model_calls():
    client = FakeTransport()
    result = run(parser().parse_args([]), client=client)
    assert result["ok"] and result["offline"]["schemas_validated"]
    assert result["model_calls"] == client.calls == 0
    assert result["game_input_executed"] is False
    assert result["vision_roundtrip_verified"] is False


def test_saved_frame_smoke_makes_one_image_call_and_never_executes_input():
    client = FakeTransport()
    args = parser().parse_args(["--smoke-image", str(FIXTURE)])
    result = run(args, client=client)
    assert result["ok"] and result["model_calls"] == client.calls == 1
    assert result["game_input_executed"] is False
    assert result["smoke"]["observation"]["values"]["seq"] is not None
    assert result["smoke"]["reply"]["actual_model"] == "actual-test-model"
    assert result["smoke"]["reply"]["requested_model"] == "claude-sub:requested"


def test_smoke_never_makes_a_second_call_even_if_model_requests_lookup():
    client = FakeTransport(lookup=True)
    result = run(parser().parse_args(["--smoke-image", str(FIXTURE)]), client=client)
    assert not result["ok"] and result["model_calls"] == client.calls == 1


def test_smoke_schema_only_allows_zero_wait_observation():
    schema = observe_schema()
    assert schema["properties"]["action"] == {"$ref": "#/$defs/ObserveAction"}
    assert schema["properties"]["lookup"] == {"type": "null"}
    assert schema["properties"]["capability"]["const"] == "observe"
    assert schema["$defs"]["ObserveAction"]["properties"]["wait_s"]["const"] == 0
