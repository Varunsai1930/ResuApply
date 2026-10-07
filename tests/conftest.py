"""Shared fixtures. Every test gets its own temporary data folder and database.

All profile data here is synthetic (the same fictional candidate ResuSkill's tests use).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.ai.client import OpenRouterClient
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from tests.synthetic import DEMO_JOB, DEMO_REQUIREMENTS, SAMPLE_JOB, SAMPLE_PROFILE  # noqa: F401  (re-exported)

BASE_URL = "http://127.0.0.1:8000"

@pytest.fixture
def sample_profile() -> dict:
    return copy.deepcopy(SAMPLE_PROFILE)


@pytest.fixture
def settings(tmp_path) -> Settings:
    # No AI key, whatever the developer's environment says.
    return Settings(data_dir=tmp_path / "data", _env_file=None, openrouter_api_key=None)


@pytest.fixture
def session(settings) -> Iterator[Session]:
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        yield db
    engine.dispose()


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE_URL) as test_client:
        yield test_client


# ---------------------------------------------------------------- Milestone 2: requirements and AI

FAKE_KEY = "sk-or-test-not-a-real-key"
TEST_MODEL = "test/model:free"


def tool_response(name: str, arguments: dict, usage: dict | None = None) -> dict:
    """An OpenRouter chat completion whose answer is a call to ``name``."""
    return {
        "id": "gen-test",
        "model": TEST_MODEL,
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}, "finish_reason": "tool_calls"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


class FakeOpenRouter:
    """Stands in for OpenRouter. Queue responses; every request is recorded for inspection.

    A queued item is a dict (JSON body, status 200), a ``(status, body)`` tuple, or an
    exception instance to raise (e.g. ``httpx.ReadTimeout``).
    """

    def __init__(self):
        self.queue: list = []
        self.requests: list = []
        self.transport = httpx.MockTransport(self._handle)

    def push(self, *items) -> "FakeOpenRouter":
        self.queue.extend(items)
        return self

    def _handle(self, request):
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content)})
        if not self.queue:
            raise AssertionError("Unexpected OpenRouter request: no response queued")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    @property
    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests]

    def sent_text(self) -> str:
        """Every message sent so far, as one string."""
        return "\n".join(m.get("content") or "" for body in self.bodies for m in body["messages"])


@pytest.fixture
def fake_ai() -> FakeOpenRouter:
    return FakeOpenRouter()


@pytest.fixture
def ai_client(fake_ai):
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=fake_ai.transport)
    yield client
    client.close()


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"data_dir": tmp_path / "data", "openrouter_api_key": FAKE_KEY, "openrouter_model": TEST_MODEL,
              "openrouter_model_trust": "free"} | overrides
    return Settings(_env_file=None, **values)


@pytest.fixture
def ai_settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def ai_app_client(ai_settings, fake_ai) -> Iterator[TestClient]:
    with TestClient(create_app(ai_settings, ai_transport=fake_ai.transport), base_url=BASE_URL) as test_client:
        yield test_client
