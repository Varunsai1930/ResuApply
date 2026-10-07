"""Request guards for a local, single-user app.

- Only loopback Host headers are accepted (configured in ``main``), which blocks DNS rebinding.
- State-changing requests from another site are refused. A page on any website could
  otherwise post a hidden form to 127.0.0.1 and change the profile or a job.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
            if source is not None and (source == "null" or urlsplit(source).netloc != headers.get("host")):
                response = PlainTextResponse("Cross-site request refused.", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
