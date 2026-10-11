"""A stand-in for Greenhouse's Job Board API, so no test touches the network. Fictional content only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_job.json"
POSTING_URL = "https://job-boards.greenhouse.io/examplerobotics/jobs/4012345"
API_URL = "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/4012345?questions=true"


def posting() -> dict:
    """The recorded posting (fictional), as a fresh copy to change."""
    return copy.deepcopy(json.loads(FIXTURE.read_text()))


class FakeGreenhouse:
    """Answers each request with the next queued item and records the request.

    An item is a dict (sent as JSON with status 200), an ``httpx.Response``, or an exception to raise.
    """

    def __init__(self, *items):
        self.queue = list(items)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError(f"Unexpected Greenhouse request: {request.url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


class Clock:
    """A stand-in for the import's clock (greenhouse._clock): reads the times given, then stays at the last.

    Only the import's own reads of the time come here; anything else that reads time.monotonic is unaffected.
    """

    def __init__(self, *times: float):
        self.times = list(times) or [0.0]

    def __call__(self) -> float:
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]
