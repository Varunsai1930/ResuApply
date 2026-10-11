"""A stand-in for Greenhouse's Job Board API, so no test touches the network. Fictional content only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpcore
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


class TickingClock:
    """A clock that only moves when a fake network read takes time."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SlowNetwork(httpcore.NetworkBackend):
    """A network whose reads take the given (fake) seconds, honouring each read's timeout as a socket does.

    ``reads`` is a list of (seconds, bytes); when it runs out, reads wait forever (until their timeout).
    """

    def __init__(self, clock: TickingClock, reads: list[tuple[float, bytes]]):
        self.clock, self.reads, self.timeouts = clock, list(reads), []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.timeouts.append(("connect", timeout))
        return _SlowStream(self)

    def sleep(self, seconds):
        self.clock.now += seconds


class _SlowStream(httpcore.NetworkStream):
    def __init__(self, network: SlowNetwork):
        self.network = network

    def read(self, max_bytes, timeout=None):
        self.network.timeouts.append(("read", timeout))
        seconds, data = self.network.reads.pop(0) if self.network.reads else (float("inf"), b"")
        if timeout is not None and seconds > timeout:
            self.network.clock.now += timeout
            raise httpcore.ReadTimeout("timed out")
        self.network.clock.now += seconds
        return data

    def write(self, buffer, timeout=None):
        self.network.timeouts.append(("write", timeout))

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.network.timeouts.append(("tls", timeout))
        return self

    def get_extra_info(self, info):
        return None


def http_response(body: bytes) -> bytes:
    """The bytes of an HTTP answer with this JSON body, as Greenhouse would send them."""
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\n\r\n" + body)
