"""Fault injection for malformed provider accounting and unavailable background workers."""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai import tasks
from app.ai.client import AIError, OpenRouterClient, ResultProblem, Tool
from tests.conftest import FAKE_KEY, TEST_MODEL, tool_response

TOOL = Tool("return_thing", "Return a thing.", {"type": "object"})
MESSAGES = [{"role": "user", "content": "Synthetic request"}]


def positive(arguments):
    if arguments["value"] <= 0:
        raise ResultProblem(["value must be positive"])
    return arguments["value"]


def test_enormous_provider_accounting_cannot_crash_a_corrective_retry(fake_ai, ai_client):
    fake_ai.push(tool_response(TOOL.name, {"value": -1}, {"total_tokens": 10 ** 400}),
                 tool_response(TOOL.name, {"value": 2}, {"total_tokens": 150.0}))

    result = ai_client.structured("adversarial", MESSAGES, TOOL, positive)

    assert (result.value, result.attempts) == (2, 2)
    assert result.usage == {"total_tokens": 150.0}


def test_finite_accounting_cannot_overflow_when_retries_are_combined(fake_ai, ai_client):
    fake_ai.push(tool_response(TOOL.name, {"value": -1}, {"total_tokens": 1e308}),
                 tool_response(TOOL.name, {"value": 2}, {"total_tokens": 1e308}))

    result = ai_client.structured("adversarial", MESSAGES, TOOL, positive)

    assert result.value == 2
    assert result.usage == {}


@pytest.mark.parametrize("value", ["1e309", "-1e309", "NaN", "-5"])
def test_invalid_accounting_does_not_poison_a_valid_result(value):
    response = tool_response(TOOL.name, {"value": 2})
    response["usage"] = {"total_tokens": "REPLACE", "cost": 0.001}
    raw = json.dumps(response).replace('"REPLACE"', value)
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL,
                              transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)))

    result = client.structured("adversarial", MESSAGES, TOOL, positive)

    assert result.value == 2
    assert result.usage == {"cost": 0.001}
    json.dumps(result.usage, allow_nan=False)


def test_worker_start_failure_finishes_the_task_and_allows_a_new_attempt(monkeypatch):
    registry = tasks.AITasks(lambda: pytest.fail("failed worker opened a database"))

    def fail_start(_):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as fault:
        fault.setattr(tasks.threading.Thread, "start", fail_start)
        first = registry.start("adversarial", 1, lambda *_: None, lambda *_: None)

    assert first.state == "failed"
    assert first.finished_at is not None
    assert registry.running(1) == []
    with pytest.raises(AIError) as exc:
        first.result()
    assert exc.value.kind == "busy"
    assert "try again" in first.message().lower()
    monkeypatch.setattr(registry, "_run", lambda *_: "recovered")
    second = registry.start("adversarial", 1, lambda *_: None, lambda *_: None)
    assert second.id != first.id
    assert second.future.result(timeout=5) == "recovered"
    assert second.state == "done"
    registry.shutdown(timeout=0)
