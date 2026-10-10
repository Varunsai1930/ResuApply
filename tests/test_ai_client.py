"""The OpenRouter client: request shape, error handling, corrective retry and logging."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.ai.client import API_URL, TIMEOUT_SECONDS, AIError, OpenRouterClient, ResultProblem, Tool
from tests.conftest import FAKE_KEY, TEST_MODEL, tool_response

TOOL = Tool("return_thing", "Return a thing.", {"type": "object", "properties": {"value": {"type": "integer"}}})
MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "Candidate secret narrative"}]


def positive(arguments: dict) -> int:
    value = arguments.get("value")
    if not isinstance(value, int) or value <= 0:
        raise ResultProblem(["value must be a positive integer"])
    return value


def test_request_offers_one_forced_data_only_tool(fake_ai, ai_client):
    fake_ai.push(tool_response("return_thing", {"value": 3}))
    result = ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert (result.value, result.attempts) == (3, 1)
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    request = fake_ai.requests[0]
    assert request["url"] == API_URL
    assert request["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
    body = request["body"]
    assert body["model"] == TEST_MODEL
    assert [t["function"]["name"] for t in body["tools"]] == ["return_thing"]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "return_thing"}}
    assert "response_format" not in body


def test_timeout_is_ninety_seconds():
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL)
    assert TIMEOUT_SECONDS == 90.0
    assert client._http.timeout.read == 90.0
    client.close()


def test_json_in_message_text_is_accepted(fake_ai, ai_client):
    fake_ai.push({"choices": [{"message": {"content": 'Here you go:\n```json\n{"value": 7}\n```'}}]})
    assert ai_client.structured("test_op", MESSAGES, TOOL, positive).value == 7


def test_one_corrective_retry_for_invalid_output(fake_ai, ai_client):
    fake_ai.push(tool_response("return_thing", {"value": -1}), tool_response("return_thing", {"value": 2}))
    result = ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert (result.value, result.attempts) == (2, 2)
    assert result.usage["total_tokens"] == 300
    retry = fake_ai.bodies[1]["messages"]
    assert retry[:2] == MESSAGES
    assert retry[2] == {"role": "assistant", "content": '{"value": -1}'}
    assert "value must be a positive integer" in retry[3]["content"]


def test_malformed_output_is_retried_once(fake_ai, ai_client):
    fake_ai.push({"choices": [{"message": {"content": "I cannot help with that."}}]},
                 tool_response("return_thing", {"value": 5}))
    assert ai_client.structured("test_op", MESSAGES, TOOL, positive).value == 5
    assert "did not call the return_thing tool" in fake_ai.bodies[1]["messages"][-1]["content"]


@pytest.mark.parametrize("response", [
    {"choices": None},
    {"choices": [None]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": "invalid"}]},
    {"choices": [{"message": {"tool_calls": {"function": {}}}}]},
    {"choices": [{"message": {"tool_calls": [None]}}]},
    {"choices": [{"message": {"tool_calls": [{"function": "invalid"}]}}]},
    tool_response("return_thing", {"value": 3}) | {"usage": [150]},
    tool_response("return_thing", {"value": 3}) | {"usage": "invalid"},
])
def test_malformed_envelopes_get_a_corrective_retry(fake_ai, ai_client, response):
    fake_ai.push(response, tool_response("return_thing", {"value": 5}))
    result = ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert (result.value, result.attempts) == (5, 2)
    assert len(fake_ai.requests) == 2
    assert "previous answer was rejected" in fake_ai.bodies[1]["messages"][-1]["content"]


def test_repeated_malformed_envelopes_raise_an_ai_error(fake_ai, ai_client):
    malformed = {"choices": [{"message": None}]}
    fake_ai.push(malformed, malformed)
    with pytest.raises(AIError) as exc:
        ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert exc.value.kind == "invalid"
    assert len(fake_ai.requests) == 2


@pytest.mark.parametrize("raw", [
    '{"value":' + "9" * 5000 + "}",
    '{"value":' + "[" * 10000 + "0" + "]" * 10000 + "}",
], ids=["oversized-integer", "excessive-nesting"])
@pytest.mark.parametrize("recover", [True, False])
def test_json_decoder_limits_are_handled_as_invalid_output(fake_ai, ai_client, raw, recover):
    malformed = tool_response("return_thing", {})
    malformed["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = raw
    fake_ai.push(malformed, tool_response("return_thing", {"value": 4}) if recover else malformed)
    if recover:
        result = ai_client.structured("test_op", MESSAGES, TOOL, positive)
        assert (result.value, result.attempts) == (4, 2)
    else:
        with pytest.raises(AIError) as exc:
            ai_client.structured("test_op", MESSAGES, TOOL, positive)
        assert exc.value.kind == "invalid"
    assert len(fake_ai.requests) == 2


def test_two_invalid_answers_raise_with_the_last_answer(fake_ai, ai_client):
    fake_ai.push(tool_response("return_thing", {"value": 0}), tool_response("return_thing", {"value": -5}))
    with pytest.raises(AIError) as exc:
        ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert exc.value.kind == "invalid"
    assert exc.value.last_arguments == {"value": -5}
    assert len(fake_ai.requests) == 2


def test_pydantic_errors_count_as_invalid_output(fake_ai, ai_client):
    from pydantic import BaseModel

    class Thing(BaseModel):
        value: int

    fake_ai.push(tool_response("return_thing", {"value": "lots"}), tool_response("return_thing", {"value": 4}))
    assert ai_client.structured("test_op", MESSAGES, TOOL, lambda a: Thing.model_validate(a).value).value == 4
    assert "value" in fake_ai.bodies[1]["messages"][-1]["content"]


@pytest.mark.parametrize("response, kind", [
    ((401, {"error": {"code": 401, "message": "No auth credentials found"}}), "auth"),
    ((402, {"error": {"code": 402, "message": "Insufficient credits"}}), "quota"),
    ((429, {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}}), "quota"),
    ((404, {"error": {"code": 404, "message": "No endpoints found that support tool use"}}), "provider"),
    ((500, {"error": {"code": 500, "message": "Internal"}}), "provider"),
    ((502, "<html>bad gateway</html>"), "provider"),
    ({"error": {"code": 429, "message": "Upstream rate limited"}}, "quota"),
    (httpx.ReadTimeout("timed out"), "timeout"),
    (httpx.ConnectError("no route"), "provider"),
])
def test_provider_failures_are_not_retried(fake_ai, ai_client, response, kind):
    fake_ai.push(response)
    with pytest.raises(AIError) as exc:
        ai_client.structured("test_op", MESSAGES, TOOL, positive)
    assert exc.value.kind == kind
    assert len(fake_ai.requests) == 1


def test_no_request_without_a_key(fake_ai):
    client = OpenRouterClient("  ", TEST_MODEL, transport=fake_ai.transport)
    with pytest.raises(AIError) as exc:
        client.structured("test_op", MESSAGES, TOOL, positive)
    assert exc.value.kind == "not_configured"
    assert fake_ai.requests == []


def test_logs_never_contain_the_key_or_content(fake_ai, ai_client, caplog):
    caplog.set_level(logging.DEBUG)
    fake_ai.push(tool_response("return_thing", {"value": -1}), tool_response("return_thing", {"value": 9}))
    ai_client.structured("test_op", MESSAGES, TOOL, positive)
    fake_ai.push((401, {"error": {"code": 401, "message": "bad key"}}))
    with pytest.raises(AIError):
        ai_client.structured("test_op", MESSAGES, TOOL, positive)
    text = caplog.text
    assert "op=test_op" in text and "outcome=ok" in text and "outcome=auth" in text
    for secret in (FAKE_KEY, "Candidate secret narrative", "rules", "positive integer"):
        assert secret not in text
