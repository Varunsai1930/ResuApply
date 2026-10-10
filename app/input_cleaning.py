"""Remove invisible control characters from every submitted form value.

Text pasted from documents can carry NUL bytes and other control characters. They are invisible
on screen but end up in titles, answers, notes and the profile, and from there in what is
printed or submitted. Every form in the app is posted URL-encoded, so cleaning the request
body here covers all of them in one place:

- tab, newline and carriage return are kept;
- vertical tab and form feed (line breaks in some word processors) become newlines;
- every other control character (Unicode category Cc, including NUL and DEL) is removed.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import parse_qsl, urlencode

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_KEEP = {"\t", "\n", "\r"}
_LINE_BREAKS = {"\x0b": "\n", "\x0c": "\n"}


def clean_text(value: str) -> str:
    """``value`` without control characters, except tab, newline and carriage return."""
    if value.isprintable():
        return value
    return "".join(_LINE_BREAKS.get(ch, ch) for ch in value
                   if ch in _KEEP or ch in _LINE_BREAKS or unicodedata.category(ch) != "Cc")


class ControlCharacterMiddleware:
    """Rewrite URL-encoded form bodies so no route ever sees a control character."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            await self.app(scope, receive, send)
            return

        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                await self.app(scope, _replay([message]), send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        # Parsing bytes handles both percent-escapes and raw UTF-8, as Starlette's own parser does.
        pairs = [(k.decode("utf-8", "replace"), v.decode("utf-8", "replace"))
                 for k, v in parse_qsl(body, keep_blank_values=True)]
        cleaned = urlencode([(clean_text(k), clean_text(v)) for k, v in pairs], encoding="utf-8").encode("ascii")

        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        scope["headers"].append((b"content-length", str(len(cleaned)).encode()))
        await self.app(scope, _replay([{"type": "http.request", "body": cleaned, "more_body": False}]), send)


def _replay(messages: list[Message]) -> Receive:
    queue = list(messages)

    async def receive() -> Message:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    return receive
