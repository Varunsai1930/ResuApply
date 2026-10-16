"""Real socket reads for total deadlines; bounded response reads and failure recovery."""

from __future__ import annotations

import asyncio
import copy
import json
import multiprocessing
import os
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import anyio
import httpx
import pytest
from httpcore._backends.anyio import AnyIOStream

from app.ai import client as client_module
from app.ai import operations
from app.ai.client import AIError, OpenRouterClient
from app.models import AIRun
from app.services import jobs as job_service
from app.services.requirements import set_requirements
from tests.conftest import DEMO_JOB, DEMO_REQUIREMENTS, FAKE_KEY, TEST_MODEL, tool_response
from tests.test_ai_client import MESSAGES, TOOL


@contextmanager
def socket_provider(monkeypatch, body: bytes, *, pause: float, pieces: int = 20, headers_pause: float = 0):
    """HTTPX's real async HTTP transport, connected to an offline socketpair.

    Only connect_tcp is replaced. HTTP parsing, streaming, read timeouts and
    cancellation use HTTPcore/AnyIO's normal implementation. The server thread
    always exits and is joined, including if a client assertion fails.
    """
    left, right = socket.socketpair()
    right.settimeout(5)
    stop, disconnected = threading.Event(), threading.Event()
    sent = []

    def serve():
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = right.recv(65536)
                if not chunk:
                    disconnected.set()
                    return
                request += chunk
            headers, received = request.split(b"\r\n\r\n", 1)
            length = int(next(line for line in headers.split(b"\r\n")
                              if line.lower().startswith(b"content-length:")).split(b":")[1])
            while len(received) < length:
                chunk = right.recv(65536)
                if not chunk:
                    disconnected.set()
                    return
                received += chunk
            if stop.wait(headers_pause):
                return
            right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n")
            width = (len(body) + pieces - 1) // pieces
            for offset in range(0, len(body), width):
                if stop.wait(pause):
                    return
                right.sendall(body[offset:offset + width])
                sent.append(offset)
            if right.recv(1) == b"":
                disconnected.set()
        except ConnectionError:  # Windows reports a closed peer as ConnectionAbortedError (WinError 10053)
            disconnected.set()
        except OSError:
            if not stop.is_set():
                raise
        finally:
            right.close()

    class PairBackend:
        stream_socket = left

        async def connect_tcp(self, *args, **kwargs):
            return AnyIOStream(await anyio.abc.SocketStream.from_socket(left))

    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = PairBackend()
    monkeypatch.setattr(client_module, "API_URL", "http://synthetic.invalid/completions")
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield transport, disconnected, sent
    finally:
        stop.set()
        for sock in (left, right):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        thread.join(3)
        assert not thread.is_alive(), "synthetic provider did not stop"


def test_slow_drip_cancels_at_total_deadline_preserves_work_and_allows_retry(
    session, fake_ai, ai_client, monkeypatch,
):
    job = job_service.create(session, job_service.clean_input(**DEMO_JOB))
    set_requirements(session, job, copy.deepcopy(DEMO_REQUIREMENTS))
    valid = tool_response("return_requirements", {"requirements": copy.deepcopy(DEMO_REQUIREMENTS)})
    fake_ai.push(valid)
    previous = operations.extract_requirements(session, ai_client, job)
    before = (list(job.requirements), dict(job.overrides), job.revision, previous.run.result)
    with socket_provider(monkeypatch, json.dumps(valid).encode(), pause=0.025) as (transport, disconnected, sent):
        client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=transport, timeout=0.15)
        started = time.monotonic()
        with pytest.raises(AIError) as caught:
            operations.extract_requirements(session, client, job, force=True)
        elapsed = time.monotonic() - started
        assert caught.value.kind == "timeout"
        assert 0.10 <= elapsed < 0.45
        assert disconnected.wait(0.3), "the timed-out HTTP connection remained open"
        assert 0 < len(sent) < 20
        client.close()
    session.refresh(job)
    session.refresh(previous.run)
    assert (job.requirements, job.overrides, job.revision, previous.run.result) == before
    assert session.query(AIRun).count() == 1
    # The same operation lock can be acquired, and the old valid cache is usable.
    recovered = operations.extract_requirements(session, ai_client, job)
    assert recovered.from_cache and recovered.run.id == previous.run.id
    fake_ai.push(valid)
    assert operations.extract_requirements(session, ai_client, job, force=True).run is not None


def test_waiting_for_headers_is_cancelled_and_connection_closed(monkeypatch):
    body = json.dumps(tool_response("return_thing", {"value": 3})).encode()
    with socket_provider(monkeypatch, body, pause=0, headers_pause=0.3) as (transport, disconnected, sent):
        client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=transport, timeout=0.08)
        with pytest.raises(AIError) as caught:
            client.complete("header_wait", MESSAGES, TOOL)
        assert caught.value.kind == "timeout"
        assert disconnected.wait(0.4)
        assert sent == []


def test_complete_body_just_before_deadline_is_accepted(monkeypatch):
    body = json.dumps(tool_response("return_thing", {"value": 3})).encode()
    with socket_provider(monkeypatch, body, pause=0.07, pieces=3) as (transport, disconnected, sent):
        client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=transport, timeout=0.30)
        assert client.complete("finished_body", MESSAGES, TOOL).arguments == {"value": 3}
        assert disconnected.wait(0.2)
        assert len(sent) == 3


class TrackedBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("length", ["1025", "9" * 5000, "0" * 5000 + "1025"])
def test_oversized_announced_response_is_refused_without_reading(monkeypatch, length):
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1024)
    stream = TrackedBody([b"never read"])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-length": length}, stream=stream))
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=transport)
    with pytest.raises(AIError) as caught:
        client.structured("large_header", MESSAGES, TOOL, lambda arguments: arguments)
    assert caught.value.kind == "provider"
    assert stream.reads == 0 and stream.closed


def test_oversized_stream_stops_reading_and_is_closed_without_retry(monkeypatch):
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1024)
    stream = TrackedBody([b"x" * 600] * 20)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=httpx.MockTransport(respond))
    with pytest.raises(AIError) as caught:
        client.structured("large_body", MESSAGES, TOOL, lambda arguments: arguments)
    assert caught.value.kind == "provider"
    assert stream.reads == 2 and stream.closed
    assert len(requests) == 1


def test_response_at_size_limit_is_accepted(monkeypatch):
    body = json.dumps(tool_response("return_thing", {"value": 4})).encode()
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", len(body))
    stream = TrackedBody([body[:100], body[100:]])
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    ))
    assert client.complete("exact_limit", MESSAGES, TOOL).arguments == {"value": 4}
    assert stream.closed


def test_unexpected_compression_is_refused_before_decompression():
    stream = TrackedBody([b"unused compressed body"])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=httpx.MockTransport(respond))
    with pytest.raises(AIError) as caught:
        client.complete("compressed_body", MESSAGES, TOOL)
    assert caught.value.kind == "provider"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert stream.reads == 0 and stream.closed


def test_deadline_cancels_async_mock_and_waits_for_cleanup():
    cleaned = []

    async def respond(request):
        try:
            await asyncio.sleep(1)
            raise AssertionError("request continued after cancellation")
        finally:
            cleaned.append(True)

    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=httpx.MockTransport(respond), timeout=0.03)
    with pytest.raises(AIError) as caught:
        client.complete("cancelled_mock", MESSAGES, TOOL)
    assert caught.value.kind == "timeout" and cleaned == [True]


def test_connect_is_cancelled_and_cleaned_up_before_return(monkeypatch):
    cleaned = []

    class WaitingBackend:
        async def connect_tcp(self, *args, **kwargs):
            try:
                await asyncio.sleep(1)
                raise AssertionError("connection continued after cancellation")
            finally:
                cleaned.append(True)

    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = WaitingBackend()
    monkeypatch.setattr(client_module, "API_URL", "http://synthetic.invalid/completions")
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, transport=transport, timeout=0.03)
    with pytest.raises(AIError) as caught:
        client.complete("cancelled_connect", MESSAGES, TOOL)
    assert caught.value.kind == "timeout" and cleaned == [True]


def _worker_with_stalled_dns(body, headers, timeout, deadline, api_url, output, sender):
    """Spawned test helper: exercise the real worker with an uncancellable system resolver."""
    from unittest.mock import patch

    ready = body.pop("_dns_ready")

    def stalled_resolution(*args, **kwargs):
        ready.send(os.getpid())
        time.sleep(10)
        raise AssertionError("DNS should have been terminated with its worker")

    # The resolver stalls before any external connection. Do not patch connect:
    # Windows asyncio uses a loopback connection to construct its own self-pipe.
    with patch.dict(os.environ, {"NO_PROXY": "*"}), patch("socket.getaddrinfo", stalled_resolution):
        client_module._request_worker(body, headers, timeout, deadline, api_url, output, sender)


def _worker_with_windows_socketpair(body, headers, timeout, deadline, api_url, output, sender):
    """Exercise Windows' TCP socketpair fallback even when this test runs on POSIX."""
    from unittest.mock import patch

    with patch.object(socket, "socketpair", socket._fallback_socketpair):
        _worker_with_stalled_dns(body, headers, timeout, deadline, api_url, output, sender)


def _worker_over_socketpair(body, headers, timeout, deadline, api_url, output, sender):
    """Spawned test helper: swap connection establishment for the passed offline socket."""
    stream_socket = body.pop("_socket")

    class PairBackend:
        async def connect_tcp(self, *args, **kwargs):
            return AnyIOStream(await anyio.abc.SocketStream.from_socket(stream_socket))

    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = PairBackend()
    original_client = httpx.AsyncClient

    def socket_client(**kwargs):
        kwargs["transport"] = transport
        return original_client(**kwargs)

    from unittest.mock import patch

    with patch.object(httpx, "AsyncClient", socket_client):
        client_module._request_worker(body, headers, timeout, deadline, api_url, output, sender)


@pytest.mark.parametrize("windows_socketpair", [False, True], ids=["platform-socketpair", "windows-socketpair"])
def test_real_worker_total_deadline_terminates_stalled_dns_and_joins(monkeypatch, windows_socketpair):
    before = {process.pid for process in multiprocessing.active_children()}
    context = multiprocessing.get_context("spawn")
    ready_receiver, ready_sender = context.Pipe(duplex=False)
    worker = _worker_with_windows_socketpair if windows_socketpair else _worker_with_stalled_dns
    monkeypatch.setattr(client_module, "_request_worker", worker)
    monkeypatch.setattr(client_module, "API_URL", "https://synthetic.invalid/completions")
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, timeout=2)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client._request_in_process({"_dns_ready": ready_sender}, {}, deadline=started + 2)
        elapsed = time.monotonic() - started
        assert 1.8 <= elapsed < 3
        assert ready_receiver.poll(0.2), "the child never reached DNS; this would only test startup timeout"
        worker_pid = ready_receiver.recv()
        assert worker_pid not in {process.pid for process in multiprocessing.active_children()}
        assert {process.pid for process in multiprocessing.active_children()} == before
    finally:
        ready_receiver.close()
        ready_sender.close()


@pytest.mark.parametrize("pause,pieces,timeout,succeeds", [(0, 3, 3, True), (0.08, 40, 2, False)])
def test_real_worker_over_socketpair_returns_or_cancels_and_leaves_no_child(
    monkeypatch, pause, pieces, timeout, succeeds,
):
    before = {process.pid for process in multiprocessing.active_children()}
    body = json.dumps(tool_response("return_thing", {"value": 5})).encode()
    monkeypatch.setattr(client_module, "_request_worker", _worker_over_socketpair)
    with socket_provider(monkeypatch, body, pause=pause, pieces=pieces) as (transport, disconnected, sent):
        stream_socket = transport._pool._network_backend.stream_socket
        client = OpenRouterClient(FAKE_KEY, TEST_MODEL, timeout=timeout)
        deadline = time.monotonic() + timeout
        if succeeds:
            response = client._request_in_process({"_socket": stream_socket}, {}, deadline=deadline)
            assert response.json() == json.loads(body)
        else:
            with pytest.raises(TimeoutError):
                client._request_in_process({"_socket": stream_socket}, {}, deadline=deadline)
            assert 0 < len(sent) < pieces
        assert {process.pid for process in multiprocessing.active_children()} == before
        # The parent kept its test-only duplicate of the passed socket. Closing it
        # now lets the synthetic server observe that the child socket is gone too.
        stream_socket.close()
        assert disconnected.wait(0.3)


def test_worker_start_failure_returns_provider_error_and_closes_resources(monkeypatch):
    context = multiprocessing.get_context("spawn")
    pipes, closed = [], []

    def pipe(**kwargs):
        pair = context.Pipe(**kwargs)
        pipes.extend(pair)
        return pair

    class FailedProcess:
        pid = None

        def start(self):
            raise OSError("no process resources")

        def close(self):
            closed.append(True)

    fake_context = SimpleNamespace(Pipe=pipe, Process=lambda **kwargs: FailedProcess())
    monkeypatch.setattr(client_module, "multiprocessing", SimpleNamespace(get_context=lambda method: fake_context))
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL)
    with pytest.raises(AIError) as caught:
        client.complete("spawn_failure", MESSAGES, TOOL)
    assert caught.value.kind == "provider"
    assert closed == [True] and all(connection.closed for connection in pipes)


def _crashed_worker(*args):
    os._exit(7)


def test_worker_crash_returns_provider_error_and_is_joined(monkeypatch):
    before = {process.pid for process in multiprocessing.active_children()}
    monkeypatch.setattr(client_module, "_request_worker", _crashed_worker)
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, timeout=3)
    with pytest.raises(AIError) as caught:
        client.complete("worker_crash", MESSAGES, TOOL)
    assert caught.value.kind == "provider"
    assert {process.pid for process in multiprocessing.active_children()} == before


def _idle_worker(*args):
    time.sleep(10)


def test_interrupted_parent_terminates_and_joins_its_worker(monkeypatch):
    before = {process.pid for process in multiprocessing.active_children()}
    context = multiprocessing.get_context("spawn")
    pipes = []

    class InterruptedReceiver:
        def __init__(self, connection):
            self.connection = connection

        def poll(self, timeout):
            raise KeyboardInterrupt

        def close(self):
            self.connection.close()

    def pipe(**kwargs):
        receiver, sender = context.Pipe(**kwargs)
        pipes.extend((receiver, sender))
        return InterruptedReceiver(receiver), sender

    fake_context = SimpleNamespace(Pipe=pipe, Process=context.Process)
    monkeypatch.setattr(client_module, "multiprocessing", SimpleNamespace(get_context=lambda method: fake_context))
    monkeypatch.setattr(client_module, "_request_worker", _idle_worker)
    client = OpenRouterClient(FAKE_KEY, TEST_MODEL, timeout=3)
    with pytest.raises(KeyboardInterrupt):
        client.complete("interrupted_parent", MESSAGES, TOOL)
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert all(connection.closed for connection in pipes)
