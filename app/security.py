"""Request guards for a local, single-user app.

- Only loopback Host headers are accepted (configured in ``main``), which blocks DNS rebinding.
- State-changing requests from another site are refused. A page on any website could
  otherwise post a hidden form to 127.0.0.1 and change the profile or a job.
- No page may be shown inside another site's frame. A framed page's own buttons post from
  the app's origin, so the check above can't stop a site that tricks the user into clicking
  them (clickjacking).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SameOriginMiddleware:
    """Reject unsafe requests whose Origin (or Referer) is a different host than the app's."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            source = headers.get("origin") or headers.get("referer")
            # Browsers always send Origin on form posts; non-browser clients (tests, curl) may send neither.
            try:
                refused = source is not None and (source == "null" or urlsplit(source).netloc != headers.get("host"))
            except ValueError:  # Malformed IPv6 hosts and other invalid URL authorities.
                refused = True
            if refused:
                response = PlainTextResponse("Cross-site request refused.", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


FRAME_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
]


class NoFramingMiddleware:
    """Tell browsers never to show any of the app's responses inside a frame."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *FRAME_HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HeadAsGetMiddleware:
    """Answer HEAD like GET, without the body.

    Routes are declared for GET only, so HEAD (used by link checkers and some browsers) was
    refused with 405. A HEAD request now runs the GET route and gets its status and headers.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)
