"""HTTPResponse over memory with a stalled peer; no sockets or wall-clock waits."""

import http.client
import io
import json
from unittest.mock import MagicMock

import pytest

from lexradar.auditors import provider
from lexradar.single_auditor import DeepSeekAdapter


class StalledPeer(io.RawIOBase):
    def __init__(self, wire):
        self.wire = io.BytesIO(wire)
        self._sock = self
        self.stalls = 0

    def readable(self):
        return True

    def readinto(self, target):
        data = self.wire.read(len(target))
        if not data:
            self.stalls += 1
            raise TimeoutError("synthetic peer remains open")
        target[: len(data)] = data
        return len(data)

    def settimeout(self, timeout):
        assert 0 < timeout <= 120

    def makefile(self, *args):
        return io.BufferedReader(self)


def install(monkeypatch, headers, body):
    peer = StalledPeer(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + body)
    response = http.client.HTTPResponse(peer)
    response.begin()
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(provider, "build_opener", lambda *args: opener)
    return peer


@pytest.mark.parametrize("framing", ["length", "chunked", "unframed", "sse", "sse_done"])
def test_440_bytes_then_stall(monkeypatch, framing):
    body = b" " * 440
    headers = b"Content-Length: 441"
    if framing == "chunked":
        headers = b"Transfer-Encoding: chunked"
        body = b"1b8\r\n" + body + b"\r\n"  # Missing terminating zero chunk.
    elif framing == "unframed":
        headers = b"Connection: keep-alive"
    elif framing in {"sse", "sse_done"}:
        headers = b"Content-Type: text/event-stream"
        prefix = b": keepalive\n\n" if framing == "sse" else b"data: [DONE]\n\n"
        body = prefix + b" " * (440 - len(prefix))
    peer = install(monkeypatch, headers, body)
    with pytest.raises(provider.ProviderError) as caught:
        provider.OpenRouterTransport().post(b"{}", "fake", 120, 500000)
    assert caught.value.diagnostics == {
        "phase": "read",
        "kind": "timeout",
        "http_status": 200,
        "received_bytes": 440,
    }
    assert peer.stalls == 1


@pytest.mark.parametrize("framing", ["length", "chunked", "chunked_and_length"])
def test_complete_framing_does_not_wait_for_connection_close(monkeypatch, framing):
    content = b" " * 440
    if framing == "length":
        headers, body = b"Content-Length: 440", content
    else:
        headers = b"Transfer-Encoding: chunked"
        if framing == "chunked_and_length":
            headers += b"\r\nContent-Length: 999"
        body = b"1b8\r\n" + content + b"\r\n0\r\n\r\n"
    peer = install(monkeypatch, headers, body)
    assert provider.OpenRouterTransport().post(b"{}", "fake", 120, 500000).body == content
    assert peer.stalls == 0


def test_single_deepseek_requests_non_streaming_and_rejects_sse(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-061-key")

    class Transport:
        def post(self, payload, key, timeout, max_bytes):
            request = json.loads(payload)
            assert request["stream"] is False
            assert request["provider"]["allow_fallbacks"] is False
            assert request["max_tokens"] == 4000
            assert timeout == 120
            return provider.HTTPReply(200, b"data: [DONE]\n\n")

    adapter = DeepSeekAdapter(Transport(), timeout_seconds=120)
    with pytest.raises(provider.ProviderError, match="invalid_provider_envelope"):
        adapter.complete(adapter.config.auditor_a, [{"role": "user", "content": "fixture"}])
