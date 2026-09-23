"""Explicit transport selection and credential boundaries, using fixture secrets only."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from jev.play import providers

KEY_ENV = "JEV_TEST_GLM_CREDENTIAL"


@pytest.mark.parametrize(("provider", "expected"), [
    ("claude", "sonnet"), ("glm", "glm-4.6v-flash"),
])
def test_model_defaults_are_provider_specific_and_explicit_models_are_preserved(provider, expected):
    assert providers.model_for(provider) == expected
    assert providers.model_for(provider, "explicit-model") == "explicit-model"


def test_unknown_provider_is_refused_before_credentials_are_loaded(monkeypatch):
    read = Mock(side_effect=AssertionError("unsupported provider loaded credentials"))
    monkeypatch.setattr(providers, "credential", read)
    with pytest.raises(ValueError, match="unsupported visual teacher provider"):
        providers.make_vision_client(provider="unknown")
    read.assert_not_called()


def test_process_credential_takes_precedence_without_reading_dotenv(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.env"
    fixture.write_text(f"{KEY_ENV}=fixture-file-credential\n")
    monkeypatch.setenv(KEY_ENV, "fixture-process-credential")
    read = Mock(side_effect=AssertionError("environment credential read dotenv"))
    monkeypatch.setattr(Path, "read_text", read)
    assert providers.credential(KEY_ENV, fixture) == "fixture-process-credential"
    read.assert_not_called()


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_named_dotenv_credential_is_loaded_without_unrelated_values(tmp_path, monkeypatch, quote):
    fixture = tmp_path / "fixture.env"
    fixture.write_text(
        "OTHER_KEY=unrelated-fixture-value\n"
        f"{KEY_ENV}_OTHER=wrong-fixture-value\n"
        f"{KEY_ENV} = {quote}fixture-file-credential{quote}\n"
    )
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert providers.credential(KEY_ENV, fixture) == "fixture-file-credential"


@pytest.mark.parametrize("value", ["", "fixture secret\nwith whitespace"])
def test_invalid_process_credential_never_falls_back_or_exposes_value(tmp_path, monkeypatch, value):
    fixture = tmp_path / "fixture.env"
    fixture.write_text(f"{KEY_ENV}=fixture-valid-fallback\n")
    monkeypatch.setenv(KEY_ENV, value)
    with pytest.raises(ValueError, match="missing or invalid") as caught:
        providers.credential(KEY_ENV, fixture)
    assert "fixture secret" not in str(caught.value)
    assert "fixture-valid-fallback" not in str(caught.value)


def test_missing_credential_is_a_safe_error(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(ValueError, match=f"credential {KEY_ENV} is missing or invalid"):
        providers.credential(KEY_ENV, tmp_path / "missing-fixture.env")


@pytest.mark.parametrize("name", ["", "bad-name", "fixture secret=value", "KEY\nVALUE"])
def test_invalid_environment_name_is_not_echoed(name):
    with pytest.raises(ValueError) as caught:
        providers.credential(name)
    assert str(caught.value) == "credential environment name is invalid"


def test_claude_selection_never_reads_glm_credentials(tmp_path, monkeypatch):
    from jev.play import teacher

    construct = Mock(return_value=object())
    read = Mock(side_effect=AssertionError("Claude selection loaded GLM credentials"))
    monkeypatch.setattr(teacher, "ClaudeVisionClient", construct)
    monkeypatch.setattr(providers, "credential", read)
    result = providers.make_vision_client(provider="claude", binary="fixture-claude",
                                          env_file=tmp_path / "never-read.env")
    assert result is construct.return_value
    construct.assert_called_once_with(binary="fixture-claude", model="sonnet", effort=None)
    read.assert_not_called()


def test_reasoning_effort_is_a_claude_setting_only():
    with pytest.raises(ValueError, match="effort"):
        providers.make_vision_client(provider="glm", effort="medium", key_env="UNSET_TEST_KEY")


def test_claude_rejects_api_endpoint_without_constructing_transport(monkeypatch):
    from jev.play import teacher

    construct = Mock(side_effect=AssertionError("invalid endpoint started Claude"))
    monkeypatch.setattr(teacher, "ClaudeVisionClient", construct)
    with pytest.raises(ValueError, match="only to the GLM provider"):
        providers.make_vision_client(provider="claude", base_url="https://api.z.ai/api/paas/v4")
    construct.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_glm_selection_constructs_only_requested_transport(tmp_path, monkeypatch, explicit):
    from jev.play import glm, teacher

    fixture = tmp_path / "fixture.env"
    fixture.write_text(f"{KEY_ENV}=fixture-api-credential\n")
    monkeypatch.delenv(KEY_ENV, raising=False)
    construct = Mock(return_value=object())
    claude = Mock(side_effect=AssertionError("GLM selection also constructed Claude"))
    monkeypatch.setattr(glm, "GLMVisionClient", construct)
    monkeypatch.setattr(teacher, "ClaudeVisionClient", claude)
    options = ({"model": "glm-4.6v", "base_url": glm.BIGMODEL_BASE_URL} if explicit else {})
    result = providers.make_vision_client(provider="glm", env_file=fixture,
                                          key_env=KEY_ENV, **options)
    assert result is construct.return_value
    construct.assert_called_once_with(
        api_key="fixture-api-credential", model="glm-4.6v" if explicit else "glm-4.6v-flash",
        base_url=glm.BIGMODEL_BASE_URL if explicit else glm.ZAI_BASE_URL,
    )
    claude.assert_not_called()
