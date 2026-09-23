import asyncio
import base64
import json

import httpx
import pytest

from jev.play import glm, tutor
from jev.play.glm import BIGMODEL_BASE_URL, ZAI_BASE_URL, GLMVisionClient
from jev.play.teacher import VisionTeacher

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
KEY = "synthetic-api-key-for-tests"


def reply(**changes):
    return {"observation_id": "screen-1", "action": "observe",
            "why": "Read the visible scene before moving.", **changes}


def reply_schema():
    return tutor.schema(tutor.menu({}, {}))


def completion(**changes):
    return {"model": "glm-4.6v-flash-served", "choices": [
        {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(reply())}}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45,
                  "prompt_tokens_details": {"cached_tokens": 100}}, **changes}


def ask(client, *, timeout_s=2, image=PNG, schema=None, prompt="private observation prompt"):
    return asyncio.run(client.ask_image(prompt, image, json_schema=schema or reply_schema(),
                                        timeout_s=timeout_s))


def mock_http(monkeypatch, handler):
    """Exercise real AsyncClient lifecycles with no reachable network transport."""
    created = []
    options = []
    transport_options = []
    transports = []
    original_client = httpx.AsyncClient

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    def make_transport(**kwargs):
        transport_options.append(kwargs)
        transport = Transport(handler)
        transports.append(transport)
        return transport

    def make_client(**kwargs):
        options.append(kwargs)
        client = original_client(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(glm.httpx, "AsyncHTTPTransport", make_transport)
    monkeypatch.setattr(glm.httpx, "AsyncClient", make_client)
    return created, options, transports, transport_options


@pytest.mark.parametrize("base_url", [ZAI_BASE_URL, BIGMODEL_BASE_URL + "/"])
def test_real_png_payload_and_verified_tutor_result(monkeypatch, base_url):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    clients, options, transports, transport_options = mock_http(monkeypatch, handler)
    client = GLMVisionClient(api_key=KEY, base_url=base_url)
    prompt = 'A private prompt with "quotes", `backticks`, $(shell) and 雪'
    result = ask(client, prompt=prompt)
    assert result.ok
    assert result.model == "glm-4.6v-flash-served"
    assert (result.tokens_in, result.tokens_out) == (123, 45)
    assert json.loads(result.text)["action"] == "observe"
    assert result.latency_ms > 0
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == base_url.rstrip("/") + "/chat/completions"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer " + KEY
    assert request.headers["Accept-Encoding"] == "identity"
    payload = json.loads(request.content)
    content = payload["messages"][1]["content"]
    image_url = content[0]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert base64.b64decode(image_url.partition(",")[2]) == PNG
    assert content[1] == {"type": "text", "text": prompt}
    system = payload["messages"][0]["content"]
    assert json.loads(system.split("JSON Schema:\n", 1)[1]) == reply_schema()
    assert payload["max_tokens"] == 1024
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["stream"] is False
    assert "tools" not in payload and "response_format" not in payload
    assert KEY not in request.content.decode()
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert transport_options == [{"retries": 0, "trust_env": False}]
    assert clients[0].is_closed and transports[0].closed


@pytest.mark.parametrize("url", ["https://example.invalid/api/paas/v4",
                                  "http://api.z.ai/api/paas/v4",
                                  "https://api.z.ai.evil.invalid/api/paas/v4",
                                  "https://user@api.z.ai/api/paas/v4",
                                  ZAI_BASE_URL + "?secret=" + KEY,
                                  "https://api.z.ai/api/coding/paas/v4",
                                  "https://api.z.ai:443/api/paas/v4"])
def test_only_exact_official_https_endpoints_are_allowed(url):
    with pytest.raises(ValueError) as exc:
        GLMVisionClient(api_key=KEY, base_url=url)
    assert url not in str(exc.value) and KEY not in str(exc.value)


@pytest.mark.parametrize("key", ["", " leading", "trailing ", "bad\r\nheader", "秘密"])
def test_bad_credentials_are_rejected_without_echo(key):
    with pytest.raises(ValueError, match="GLM API key"):
        GLMVisionClient(api_key=key)


def test_preflight_is_local_and_does_not_claim_verified_auth(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted a network connection")

    monkeypatch.setattr(glm.httpx, "AsyncClient", forbidden)
    report = GLMVisionClient(api_key=KEY).preflight()
    assert report["ok"] and report["credential_present"]
    assert not report["authentication_verified"]
    assert not report["model_call_made"] and not report["vision_roundtrip_verified"]
    assert KEY not in json.dumps(report)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500, 503])
def test_http_failure_never_echoes_response_or_retries(monkeypatch, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=KEY + " private observation prompt " + str(PNG),
                              headers={"Location": "https://other-provider.invalid/steal"})

    clients, _, transports, _ = mock_http(monkeypatch, handler)
    result = ask(GLMVisionClient(api_key=KEY))
    assert result.status == "transport"
    assert str(status) in result.detail
    assert KEY not in repr(result) and "private observation prompt" not in repr(result)
    assert len(requests) == 1, "the transport itself never retries"
    assert (result.retry_after_s is not None) == (status >= 500)
    assert clients[0].is_closed and transports[0].closed


@pytest.mark.parametrize(("code", "transient"), [
    ("1305", True), ("1302", True), (1305, True), ("1113", False), ("1311", False),
    ("not-a-code", False), (None, False)])
def test_only_documented_transient_provider_codes_carry_a_retry_hint(monkeypatch, code, transient):
    def handler(request):
        return httpx.Response(429, json={"error": {"code": code, "message": KEY + " private"}})

    mock_http(monkeypatch, handler)
    result = ask(GLMVisionClient(api_key=KEY))
    assert result.status == "transport"
    assert (result.retry_after_s == glm.RETRY_AFTER_S) is transient
    assert ("transient" in result.detail) is transient
    if code in ("1305", 1305, "1113"):
        assert f"code {code}" in result.detail
    assert "not-a-code" not in result.detail
    assert KEY not in repr(result) and "private" not in repr(result)


def test_network_exception_is_redacted_and_not_retried(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError(KEY + request.content.decode(), request=request)

    clients, _, _, _ = mock_http(monkeypatch, handler)
    result = ask(GLMVisionClient(api_key=KEY))
    assert result.status == "transport"
    assert KEY not in repr(result) and "private observation prompt" not in repr(result)
    assert len(requests) == 1 and clients[0].is_closed


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadline_does_not_connect(monkeypatch, timeout):
    clients, _, _, _ = mock_http(monkeypatch, lambda _: pytest.fail("connected"))
    result = ask(GLMVisionClient(api_key=KEY), timeout_s=timeout)
    assert result.status == "timeout" and not clients


@pytest.mark.parametrize("image", [b"not-png", PNG + b"x" * glm.MAX_IMAGE_BYTES])
def test_invalid_or_oversized_image_does_not_connect(monkeypatch, image):
    clients, _, _, _ = mock_http(monkeypatch, lambda _: pytest.fail("connected"))
    result = ask(GLMVisionClient(api_key=KEY), image=image)
    assert result.status == "transport" and not clients


def test_oversized_prompt_and_schema_do_not_connect(monkeypatch):
    clients, _, _, _ = mock_http(monkeypatch, lambda _: pytest.fail("connected"))
    client = GLMVisionClient(api_key=KEY)
    assert ask(client, prompt="x" * glm.MAX_REQUEST_BYTES).status == "transport"
    assert ask(client, schema=["not-an-object"]).status == "transport"
    assert ask(client, schema={"description": "x" * glm.MAX_SCHEMA_BYTES}).status == "transport"
    assert not clients


def test_the_menu_schema_is_guidance_and_the_reply_text_is_left_to_the_tutor(monkeypatch):
    """One contract, validated in one place: the transport only transports."""
    schema = reply_schema()
    requests = []

    def handler(request):
        requests.append(request)
        doc = completion()
        doc["choices"][0]["message"]["content"] = json.dumps(reply(action="shell"))
        return httpx.Response(200, json=doc)

    mock_http(monkeypatch, handler)
    result = ask(GLMVisionClient(api_key=KEY), schema=schema)
    assert result.ok and json.loads(result.text)["action"] == "shell"
    system = json.loads(requests[0].content)["messages"][0]["content"]
    assert system.startswith(tutor.SYSTEM_PROMPT)
    assert json.loads(system.split("JSON Schema:\n", 1)[1]) == schema


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"error": "' + KEY.encode() + b'"}',
                                  b'{"choices": [], "choices": []}'])
def test_malformed_server_reply_is_redacted(body):
    result = GLMVisionClient(api_key=KEY).classify_response(body)
    assert result.status == "transport"
    assert KEY not in repr(result) and result.text is None


@pytest.mark.parametrize("change", [
    {"finish_reason": "length"}, {"finish_reason": "tool_calls"},
    {"message": {"role": "assistant", "content": json.dumps(reply()),
                 "tool_calls": [{"function": {"name": "shell"}}]}},
])
def test_truncated_or_tool_replies_cannot_supply_text(change):
    doc = completion()
    doc["choices"][0].update(change)
    result = GLMVisionClient(api_key=KEY).classify_response(json.dumps(doc).encode())
    assert result.status == "rejected" and result.text is None
    assert (result.tokens_in, result.tokens_out) == (123, 45)


@pytest.mark.parametrize(("content", "status"), [
    (json.dumps(reply(action="move_forward", seconds=999)), "invalid"),
    (json.dumps(reply(action="shell")), "invalid"),
    ('{"why": "a", "why": "b"}', "invalid"),
    ("I think I should observe first.", "invalid"),
    ("```json\n" + json.dumps(reply()) + "\n```", "ok"),
    (json.dumps(reply(untrusted="ignored extra key")), "ok"),
])
def test_reply_text_is_validated_once_by_the_tutor(monkeypatch, content, status):
    doc = completion()
    doc["choices"][0]["message"]["content"] = content
    mock_http(monkeypatch, lambda _: httpx.Response(200, json=doc))
    teacher = VisionTeacher(GLMVisionClient(api_key=KEY))
    result = asyncio.run(teacher.decide({"id": "screen-1"}, PNG, controls={}))
    assert result.status == status
    assert (result.action is not None) is (status == "ok")
    assert KEY not in repr(result)


def test_refusal_and_empty_text_are_abstentions():
    client = GLMVisionClient(api_key=KEY)
    for message in ({"role": "assistant", "content": ""},
                    {"role": "assistant", "content": None, "reasoning_content": "private"},
                    {"role": "assistant", "content": json.dumps(reply()), "refusal": KEY}):
        result = client.classify_response(json.dumps(completion(choices=[{
            "finish_reason": "stop", "message": message}])).encode())
        assert result.status == "abstained" and result.text is None
        assert KEY not in repr(result) and "private" not in repr(result)


def test_missing_metadata_is_unknown_and_cached_tokens_are_not_added_twice():
    client = GLMVisionClient(api_key=KEY)
    result = client.classify_response(json.dumps(completion(model=None, usage={})).encode())
    assert result.ok and result.model == client.model_name
    assert result.tokens_in is None and result.tokens_out is None
    for value in (True, -1, "123", 1.5):
        result = client.classify_response(json.dumps(completion(
            usage={"prompt_tokens": value, "completion_tokens": value})).encode())
        assert result.tokens_in is None and result.tokens_out is None
    result = client.classify_response(json.dumps(completion()).encode())
    assert result.tokens_in == 123


def test_oversized_response_stops_reading_and_closes(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.read += 1
                yield b"x" * (64 * 1024)

        async def aclose(self):
            self.closed = True

    stream = Stream()
    clients, _, _, _ = mock_http(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    result = ask(GLMVisionClient(api_key=KEY))
    assert result.status == "transport" and "byte limit" in result.detail
    assert stream.read == 5 and stream.closed and clients[0].is_closed


@pytest.mark.parametrize("cancel", [False, True])
def test_deadline_and_cancellation_close_inflight_response(monkeypatch, cancel):
    async def run():
        entered = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"

            async def aclose(self):
                self.closed = True

        stream = Stream()
        clients, _, transports, _ = mock_http(
            monkeypatch, lambda _: httpx.Response(200, stream=stream))
        client = GLMVisionClient(api_key=KEY)
        task = asyncio.create_task(client.ask_image(
            "private", PNG, json_schema=reply_schema(), timeout_s=1 if cancel else 0.04))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "timeout"
        assert stream.closed and clients[0].is_closed and transports[0].closed

    asyncio.run(run())


def test_vision_teacher_preserves_actual_model_and_rejects_stale_observation(monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(200, json=completion()))
    teacher = VisionTeacher(GLMVisionClient(api_key=KEY))
    result = asyncio.run(teacher.decide({"id": "different-screen"}, PNG, controls={}))
    assert result.status == "stale" and not result.ok
    assert result.actual_model == "glm-4.6v-flash-served"
    assert result.requested_model == "glm-api:glm-4.6v-flash"


def test_an_unavailable_action_is_named_in_the_diagnostic(monkeypatch):
    doc = completion()
    doc["choices"][0]["message"]["content"] = json.dumps(reply(action="interact_unit", x=0.5,
                                                               y=0.5))
    mock_http(monkeypatch, lambda _: httpx.Response(200, json=doc))
    result = asyncio.run(VisionTeacher(GLMVisionClient(api_key=KEY)).decide(
        {"id": "screen-1", "values": {"ui.modal": True}}, PNG, controls={}))
    assert result.status == "invalid" and result.action is None
    assert "'interact_unit' is not available" in result.detail
    assert len(result.calls) == 2, "one re-ask, then the rejection stands"
    assert (result.tokens_in, result.tokens_out) == (246, 90)
