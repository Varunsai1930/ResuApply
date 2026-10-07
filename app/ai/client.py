"""OpenRouter Chat Completions client over HTTPX.

Each request offers exactly one tool, whose only job is to return data in a fixed JSON
schema, and forces the model to call it. The tool never triggers an action: its arguments
are parsed and validated locally with Pydantic and the ResuSkill rules. Free endpoints do
not enforce ``response_format``, which is why a tool is used.

Logs record the operation, model, attempt, outcome, duration and token counts. They never
contain the API key, prompts, responses or any candidate or job text.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

import httpx

logger = logging.getLogger("resuapply.ai")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 90.0
T = TypeVar("T")

ErrorKind = Literal["not_configured", "auth", "quota", "timeout", "provider", "invalid", "busy"]


class AIError(Exception):
    """A user-facing AI failure. Existing work is never touched when this is raised."""

    def __init__(self, kind: ErrorKind, message: str, last_arguments: dict | None = None,
                 problems: list[str] | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        # For "invalid": the model's final answer and why it was rejected, so a caller can
        # hand it to the user for correction instead of discarding it.
        self.last_arguments = last_arguments
        self.problems = problems or []


class ResultProblem(Exception):
    """Raised by a validator: the model's result was well-formed JSON but broke a rule."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema of the arguments


@dataclass
class Completion:
    arguments: dict
    usage: dict = field(default_factory=dict)


@dataclass
class StructuredResult:
    value: object
    usage: dict
    attempts: int


class OpenRouterClient:
    def __init__(self, api_key: str | None, model: str, transport: httpx.BaseTransport | None = None,
                 timeout: float = TIMEOUT_SECONDS):
        self._api_key = (api_key or "").strip()
        self.model = model
        self._http = httpx.Client(timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ one request

    def complete(self, operation: str, messages: list[dict], tool: Tool, attempt: int = 1) -> Completion:
        """Send one request that must answer by calling ``tool``. Returns its parsed arguments."""
        if not self.configured:
            raise AIError("not_configured", "AI is not set up. Add OPENROUTER_API_KEY to .env and restart the app.")
        body = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.parameters,
            }}],
            "tool_choice": {"type": "function", "function": {"name": tool.name}},
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": "http://127.0.0.1",
            "X-Title": "ResuApply",
        }
        started = time.monotonic()
        outcome = "ok"
        usage: dict = {}
        try:
            try:
                response = self._http.post(API_URL, json=body, headers=headers)
            except httpx.TimeoutException:
                raise AIError("timeout", "The model took longer than 90 seconds. Your work is unchanged; try again.") from None
            except httpx.HTTPError:
                raise AIError("provider", "Could not reach OpenRouter. Check your connection; your work is unchanged.") from None
            data = self._json(response)
            self._raise_for_error(response.status_code, data)
            usage = _usage(data)
            return Completion(_arguments(data, tool.name), usage)
        except AIError as exc:
            outcome = exc.kind
            raise
        finally:
            logger.info(
                "ai request op=%s model=%s attempt=%d outcome=%s duration_ms=%d tokens=%s",
                operation, self.model, attempt, outcome, (time.monotonic() - started) * 1000,
                usage.get("total_tokens", "-"),
            )

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if response.status_code >= 400:
                return {}
            raise AIError("provider", "OpenRouter returned a response that isn't JSON. Try again later.")
        return data

    @staticmethod
    def _raise_for_error(status: int, data: dict) -> None:
        error = data.get("error")
        if status < 400 and not error:
            return
        code = status if status >= 400 else (error.get("code") if isinstance(error, dict) else None)
        detail = error.get("message") if isinstance(error, dict) else None
        detail = f" ({str(detail)[:200]})" if detail else ""
        if code in (401, 403):
            raise AIError("auth", f"OpenRouter rejected the API key{detail}. Check OPENROUTER_API_KEY in .env.")
        if code == 402:
            raise AIError("quota", f"OpenRouter reports insufficient credits{detail}. Your work is unchanged.")
        if code == 429:
            raise AIError("quota", f"The model's rate limit or free quota was reached{detail}. Try again later.")
        if code == 408:
            raise AIError("timeout", "The model timed out. Your work is unchanged; try again.")
        if code == 404:
            raise AIError("provider", f"OpenRouter can't serve this model with tool calling{detail}. "
                                      "Check OPENROUTER_MODEL; changing it is your decision.")
        raise AIError("provider", f"The model provider returned an error{detail}. Your work is unchanged; try again.")

    # ------------------------------------------------------------ with one corrective retry

    def structured(self, operation: str, messages: list[dict], tool: Tool,
                   validate: Callable[[dict], T]) -> StructuredResult:
        """Ask for a result and validate it, with one corrective retry for malformed or invalid output.

        ``validate`` turns the tool arguments into the final value and raises
        ``ResultProblem`` (or a Pydantic ``ValidationError``) when they break a rule.
        """
        conversation = list(messages)
        total: dict = {}
        for attempt in (1, 2):
            try:
                completion = self.complete(operation, conversation, tool, attempt)
            except AIError as exc:
                if exc.kind != "invalid" or attempt == 2:
                    raise
                problems, previous = [exc.message], ""
            else:
                total = _add_usage(total, completion.usage)
                try:
                    return StructuredResult(validate(completion.arguments), total, attempt)
                except ResultProblem as exc:
                    problems = exc.problems
                except ValueError as exc:  # pydantic.ValidationError is a ValueError
                    problems = _pydantic_problems(exc)
                previous = json.dumps(completion.arguments, ensure_ascii=False)[:20000]
                if attempt == 2:
                    raise AIError("invalid", "The model's answer failed validation twice, so nothing was saved. "
                                             "Try again, or enter the information yourself.",
                                  last_arguments=completion.arguments, problems=problems) from None
            logger.info("ai result rejected op=%s attempt=%d problems=%d", operation, attempt, len(problems))
            if previous:
                conversation.append({"role": "assistant", "content": previous})
            conversation.append({"role": "user", "content": (
                "Your previous answer was rejected for these reasons:\n- " + "\n- ".join(problems[:20])
                + f"\nCall the {tool.name} tool again with a complete, corrected result."
            )})
        raise AssertionError("unreachable")


def _arguments(data: dict, tool_name: str) -> dict:
    """The tool call's arguments, or a JSON object the model put in its text instead."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise AIError("invalid", "The response had no message.") from None
    raw = None
    for call in message.get("tool_calls") or []:
        function = (call or {}).get("function") or {}
        if function.get("name") in (tool_name, None):
            raw = function.get("arguments")
            break
    if raw is None:
        content = message.get("content")
        if isinstance(content, str):
            match = re.search(r"\{.*\}", content, re.DOTALL)
            raw = match.group(0) if match else None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise AIError("invalid", f"The model did not call the {tool_name} tool.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise AIError("invalid", f"The {tool_name} arguments were not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise AIError("invalid", f"The {tool_name} arguments must be a JSON object.")
    return parsed


def _usage(data: dict) -> dict:
    usage = data.get("usage") or {}
    keep = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
    return {k: usage[k] for k in keep if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool)}


def _add_usage(total: dict, usage: dict) -> dict:
    out = dict(total)
    for key, value in usage.items():
        out[key] = out.get(key, 0) + value
    return out


def _pydantic_problems(exc: ValueError) -> list[str]:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [str(exc)[:300]]
    return [f"{'.'.join(str(p) for p in e.get('loc', ())) or 'result'}: {e.get('msg')}" for e in errors()][:20]
