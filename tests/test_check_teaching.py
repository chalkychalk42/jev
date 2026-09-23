import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev.teacher.client import TeacherResult
from tools import check_teaching
from tools.check_teaching import observe_schema, parser, run

FIXTURE = (Path(__file__).parent / "fixtures" / "target-localization-evaluation"
           / "000099-20260922T182233.847933Z.png")


class FakeTransport:
    model_name = "claude-sub:requested"

    def __init__(self, *, lookup=False):
        self.calls = 0
        self.lookup = lookup
        self.schemas = []

    async def ask_image(self, prompt, image_png, *, json_schema, timeout_s):
        self.calls += 1
        self.schemas.append(json_schema)
        observation_id = re.search(r"^observation_id: (\S+)$", prompt, re.M).group(1)
        offered = prompt.split("ACTIONS AVAILABLE NOW\n", 1)[1].split("\n\n", 1)[0]
        assert offered.splitlines() == ["- observe [seconds 0-2.0]: wait up to 2 seconds "
                                        "and look again"]
        assert image_png == FIXTURE.read_bytes()
        reply = {"observation_id": observation_id,
                 "action": "lookup" if self.lookup else "observe",
                 **({"query": "Young Wolf"} if self.lookup else {"seconds": 0}),
                 "why": "Offline transport verification only."}
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
    assert client.schemas == [observe_schema()]


def test_smoke_never_makes_a_second_call_even_if_model_requests_lookup():
    client = FakeTransport(lookup=True)
    result = run(parser().parse_args(["--smoke-image", str(FIXTURE)]), client=client)
    assert not result["ok"] and result["model_calls"] == client.calls == 1


def test_smoke_schema_only_allows_zero_wait_observation():
    schema = observe_schema()
    assert schema["properties"]["action"]["enum"] == ["observe"]
    assert schema["properties"]["seconds"] == {"type": "number", "const": 0}
    assert schema["additionalProperties"] is False


def test_glm_offline_readiness_does_not_construct_transport_or_read_credentials(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.env"
    fixture.write_text("JEV_TEST_GLM_CREDENTIAL=fixture-private-key\n")
    prohibited = []
    for target in (
        "tools.check_teaching.make_vision_client", "jev.play.providers.credential",
        "jev.run.client.attach", "httpx.AsyncClient", "httpx.Client", "subprocess.Popen",
    ):
        mock = Mock(side_effect=AssertionError(f"offline readiness invoked {target}"))
        monkeypatch.setattr(target, mock)
        prohibited.append(mock)
    result = run(parser().parse_args([
        "--teacher-provider", "glm", "--teacher-env-file", str(fixture),
        "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
    ]))
    assert result["ok"] and result["model_calls"] == 0
    assert result["game_input_executed"] is False
    assert result["vision_roundtrip_verified"] is False
    assert "fixture-private-key" not in json.dumps(result)
    for mock in prohibited:
        mock.assert_not_called()


def test_glm_transport_preflight_checks_fixture_configuration_locally(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.env"
    fixture.write_text("JEV_TEST_GLM_CREDENTIAL=fixture-private-key\n")
    monkeypatch.delenv("JEV_TEST_GLM_CREDENTIAL", raising=False)
    network = Mock(side_effect=AssertionError("local preflight invoked HTTP"))
    monkeypatch.setattr("httpx.AsyncClient", network)
    monkeypatch.setattr("httpx.Client", network)
    result = run(parser().parse_args([
        "--transport-check", "--teacher-provider", "glm",
        "--teacher-env-file", str(fixture), "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
        "--teacher-base-url", "https://open.bigmodel.cn/api/paas/v4",
    ]))
    assert result["ok"] and result["model_calls"] == 0
    assert result["transport"]["provider"] == "glm"
    assert result["transport"]["requested_model"] == "glm-4.6v-flash"
    assert result["transport"]["credential_present"] is True
    assert result["transport"]["authentication_verified"] is False
    assert result["transport"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert "fixture-private-key" not in json.dumps(result)
    network.assert_not_called()


def test_smoke_factory_receives_explicit_provider_and_narrows_fake_transport_schema(
        tmp_path, monkeypatch):
    client = FakeTransport()
    client.model_name = "glm-api:glm-4.6v-flash"
    construct = Mock(return_value=client)
    monkeypatch.setattr(check_teaching, "make_vision_client", construct)
    fixture = tmp_path / "never-read.env"
    result = run(parser().parse_args([
        "--smoke-image", str(FIXTURE), "--teacher-provider", "glm",
        "--teacher-model", "glm-4.6v-flash", "--teacher-env-file", str(fixture),
        "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
        "--teacher-base-url", "https://open.bigmodel.cn/api/paas/v4",
    ]))
    assert result["ok"] and result["model_calls"] == client.calls == 1
    assert result["smoke"]["reply"]["requested_model"] == "glm-api:glm-4.6v-flash"
    assert result["smoke"]["reply"]["actual_model"] == "actual-test-model"
    assert client.schemas == [observe_schema()]
    construct.assert_called_once_with(
        provider="glm", binary=None, model="glm-4.6v-flash",
        base_url="https://open.bigmodel.cn/api/paas/v4", env_file=fixture,
        key_env="JEV_TEST_GLM_CREDENTIAL",
    )


@pytest.mark.parametrize("value", [None, "fixture private credential"])
def test_glm_readiness_credential_failure_is_redacted(tmp_path, monkeypatch, capsys, value):
    fixture = tmp_path / "fixture.env"
    if value is not None:
        fixture.write_text(f"JEV_TEST_GLM_CREDENTIAL={value}\n")
    monkeypatch.delenv("JEV_TEST_GLM_CREDENTIAL", raising=False)
    assert check_teaching.main([
        "--transport-check", "--teacher-provider", "glm",
        "--teacher-env-file", str(fixture), "--teacher-key-env", "JEV_TEST_GLM_CREDENTIAL",
    ]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["ok"] is False and result["game_input_executed"] is False
    assert "missing or invalid" in result["error"]
    assert "fixture private credential" not in output
