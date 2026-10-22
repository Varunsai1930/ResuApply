"""OpenRouter Chat Completions client over HTTPX.

Each request offers exactly one tool, whose only job is to return data in a fixed JSON
schema, and forces the model to call it. The tool never triggers an action: its arguments
are parsed and validated locally with Pydantic and the ResuSkill rules. Free endpoints do
not enforce ``response_format``, which is why a tool is used.

Logs record the operation, model, attempt, outcome, duration and token counts. They never
contain the API key, prompts, responses or any candidate or job text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import multiprocessing
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal, TypeVar

import httpx

logger = logging.getLogger("resuapply.ai")

API_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 90.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
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
    def __init__(self, api_key: str | None, model: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT_SECONDS):
        self._api_key = (api_key or "").strip()
        self.model = model
        self._transport = transport
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        # Every request closes its own client, including on timeout. There is no
        # event-loop-bound connection pool to share between the sync route threads.
        pass

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
            "Accept-Encoding": "identity",
        }
        started = time.monotonic()
        outcome = "ok"
        usage: dict = {}
        try:
            try:
                deadline = started + self._timeout
                if self._transport is None:
                    response = self._request_in_process(body, headers, deadline=deadline)
                else:
                    response = asyncio.run(self._request(body, headers, deadline=deadline))
            except (TimeoutError, httpx.TimeoutException):
                raise AIError("timeout", f"The model took longer than {self._timeout:g} seconds. "
                                        "Your work is unchanged; try again.") from None
            except (httpx.HTTPError, OSError):
                raise AIError("provider", "The AI request could not finish. Check your connection and local storage; "
                                          "your work is unchanged.") from None
            data = self._json(response)
            self._raise_for_error(response.status_code, data)
            usage = _usage(data)
            if time.monotonic() - started >= self._timeout:
                raise AIError("timeout", f"The model took longer than {self._timeout:g} seconds. "
                                        "Your work is unchanged; try again.")
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

    def _request_in_process(self, body: dict, headers: dict, *, deadline: float) -> httpx.Response:
        """Own the real network worker, including a system DNS lookup that cannot be cancelled.

        Only a small completion/error message crosses the pipe. The bounded body
        is read from a private temporary file after the worker has closed HTTPX,
        so reading a partially sent response cannot defeat the parent's deadline.
        The worker receives request data in memory, never shell arguments or logs.
        """
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="resuapply-ai-") as directory:
            output = Path(directory) / "response"
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_request_worker,
                                      args=(body, headers, self._timeout, deadline, API_URL, str(output), sender), daemon=True)
            try:
                process.start()
                sender.close()
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not receiver.poll(remaining):
                    raise TimeoutError
                try:
                    message = json.loads(receiver.recv_bytes(maxlength=4096))
                except (EOFError, OSError, ValueError):
                    raise AIError("provider", "OpenRouter's request could not finish. "
                                              "Your work is unchanged; try again.") from None
                # A returned response is usable only after its worker is gone.
                process.join(timeout=max(0, deadline - time.monotonic()))
                if process.is_alive() or time.monotonic() >= deadline:
                    raise TimeoutError
                if "error" in message:
                    raise AIError(message["error"], message["message"])
                with output.open("rb") as stored:
                    content = stored.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise self._response_too_large()
                return httpx.Response(message["status"], content=content)
            finally:
                sender.close()
                receiver.close()
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=0.25)
                    if process.is_alive():
                        process.kill()
                    process.join()
                process.close()

    async def _request(self, body: dict, headers: dict, *, deadline: float,
                       api_url: str | None = None) -> httpx.Response:
        """Read a bounded response under one cancellable deadline for the whole exchange.

        ``complete`` stays synchronous, but the socket work is asynchronous so the
        deadline cancels a blocked connect/read instead of leaving a worker behind.
        A request owns its client and closes it before returning to the route thread.
        ``MockTransport`` implements the same async transport API for offline tests.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                async with client.stream("POST", api_url or API_URL, json=body, headers=headers) as response:
                    # Ask for identity above and refuse unexpected compression rather
                    # than decompress an unbounded body before checking its size.
                    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                        raise AIError("provider", "OpenRouter returned an unsupported compressed response. "
                                                  "Your work is unchanged; try again.")
                    length = response.headers.get("content-length", "")
                    digits = length.lstrip("0") or "0"
                    if length.isascii() and length.isdigit() and (
                        len(digits) > len(str(MAX_RESPONSE_BYTES)) or int(digits) > MAX_RESPONSE_BYTES
                    ):
                        raise self._response_too_large()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunk) > MAX_RESPONSE_BYTES - len(content):
                            raise self._response_too_large()
                        content.extend(chunk)
                    return httpx.Response(response.status_code, headers=response.headers, content=bytes(content))

    @staticmethod
    def _response_too_large() -> AIError:
        return AIError("provider", f"OpenRouter's response was over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB. "
                                   "Your work is unchanged; try again.")

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except (ValueError, RecursionError):
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
                   validate: Callable[[dict], T], before_attempt: Callable[[], None] | None = None) -> StructuredResult:
        """Ask for a result and validate it, with one corrective retry for malformed or invalid output.

        ``validate`` turns the tool arguments into the final value and raises
        ``ResultProblem`` (or a Pydantic ``ValidationError``) when they break a rule.
        ``before_attempt`` rechecks permission and input freshness before each outbound request.
        """
        conversation = list(messages)
        total: dict = {}
        for attempt in (1, 2):
            if before_attempt is not None:
                before_attempt()
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
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIError("invalid", "The response had no message.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AIError("invalid", "The response message must be an object.")
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise AIError("invalid", "The response tool_calls must be a list.")
    raw = None
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise AIError("invalid", "Each tool call must contain a function object.")
        function = call["function"]
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
    except (ValueError, RecursionError):
        raise AIError("invalid", f"The {tool_name} arguments were not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise AIError("invalid", f"The {tool_name} arguments must be a JSON object.")
    return parsed


def _valid_usage_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a JSON integer can exceed the range of a float
        return False


def _usage(data: dict) -> dict:
    usage = data.get("usage")
    if usage is None:
        return {}
    if not isinstance(usage, dict):
        raise AIError("invalid", "The response usage must be an object.")
    keep = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
    # Accounting is optional metadata; unusable numbers must not discard a valid answer
    # or reach persistence/logging as NaN, infinity or an overflowing retry total.
    return {k: usage[k] for k in keep if _valid_usage_number(usage.get(k))}


def _add_usage(total: dict, usage: dict) -> dict:
    out = dict(total)
    for key, value in usage.items():
        combined = out.get(key, 0) + value
        if _valid_usage_number(combined):
            out[key] = combined
        else:
            out.pop(key, None)
    return out


def _pydantic_problems(exc: ValueError) -> list[str]:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [str(exc)[:300]]
    return [f"{'.'.join(str(p) for p in e.get('loc', ())) or 'result'}: {e.get('msg')}" for e in errors()][:20]


def _request_worker(body: dict, headers: dict, timeout: float, deadline: float, api_url: str,
                    output: str, sender: Connection) -> None:
    """Spawnable network-only worker: no candidate models, sessions or persistence."""
    try:
        client = OpenRouterClient(None, "", timeout=timeout)
        response = asyncio.run(client._request(body, headers, deadline=deadline, api_url=api_url))
        with open(output, "xb") as stored:
            stored.write(response.content)
        message = {"status": response.status_code}
    except (TimeoutError, httpx.TimeoutException):
        message = {"error": "timeout", "message": f"The model took longer than {timeout:g} seconds. "
                                                   "Your work is unchanged; try again."}
    except AIError as exc:
        message = {"error": exc.kind, "message": exc.message[:300]}
    except Exception:
        message = {"error": "provider", "message": "Could not reach OpenRouter. Check your connection; "
                                                    "your work is unchanged."}
    try:
        sender.send_bytes(json.dumps(message).encode("utf-8"))
    finally:
        sender.close()
