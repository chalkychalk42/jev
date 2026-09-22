import json

from tools.check_synthetic_transport import payload, redacted_detail


def test_synthetic_transport_does_not_include_repo_game_or_user_context():
    document = json.loads(payload())
    text = document["message"]["content"][1]["text"]
    assert "programmatically_generated_public_example" in text
    assert all(value not in text for value in ("ForeverV2", "NAS", "Young Wolf", "Northshire", "C:\\"))


def test_diagnostic_redacts_credentials_email_urls_and_long_identifiers():
    raw = ("request failed for private@example.invalid authorization: Bearer abc123 "
           "api_key='short-secret' sk-ant-test-secret https://example.invalid/?token=secret "
           "LongValue012345678901234567890123456789")
    clean = redacted_detail(raw)
    assert "request failed" in clean
    assert all(secret not in clean for secret in ("private@example.invalid", "abc123", "short-secret",
                                                 "sk-ant-test-secret", "token=secret", "LongValue"))
    assert len(redacted_detail("x " * 400)) <= 300
