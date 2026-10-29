"""Real HTTP framing with local in-memory input; no network or paid calls."""

import http.client
import io
from unittest.mock import MagicMock

import pytest

from lexradar.auditors import provider


class Socket:
    def __init__(self, wire):
        self.stream = io.BufferedReader(io.BytesIO(wire))
        # Match the socket file shape used by HTTPResponse in production.
        self.stream.raw._sock = self
        self.timeouts = []

    def makefile(self, *args):
        return self.stream

    def settimeout(self, timeout):
        assert not self.stream.closed, "Socket touched after response EOF"
        self.timeouts.append(timeout)


def response(monkeypatch, wire):
    sock = Socket(wire)
    reply = http.client.HTTPResponse(sock)
    reply.begin()
    opener = MagicMock()
    opener.open.return_value = reply
    monkeypatch.setattr(provider, "build_opener", lambda *args: opener)
    return sock, opener


@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_complete_response(monkeypatch, framing):
    content = b' \n{"choices": []}'
    if framing == "length":
        wire = b"Content-Length: " + str(len(content)).encode() + b"\r\n\r\n" + content
    elif framing == "chunked":
        wire = b"Transfer-Encoding: chunked\r\n\r\n"
        wire += b"2\r\n \n\r\n" + b"f\r\n" + content[2:] + b"\r\n0\r\n\r\n"
    else:
        wire = b"Connection: close\r\n\r\n" + content
    _, opener = response(monkeypatch, b"HTTP/1.1 200 OK\r\n" + wire)
    result = provider.OpenRouterTransport().post(b"{}", "fake", 5, 100)
    assert result.body == content
    assert opener.open.call_count == 1


def test_response_size_limit(monkeypatch):
    response(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n1234")
    with pytest.raises(provider.ProviderError, match="response_size_exceeded"):
        provider.OpenRouterTransport().post(b"{}", "fake", 5, 3)


def test_safe_diagnostic(monkeypatch):
    opener = MagicMock()
    opener.open.side_effect = TimeoutError("secret prompt credential")
    monkeypatch.setattr(provider, "build_opener", lambda *args: opener)
    with pytest.raises(provider.ProviderError) as error:
        provider.OpenRouterTransport().post(b"{}", "fake", 5, 100)
    assert error.value.diagnostics == {
        "phase": "open",
        "kind": "timeout",
        "http_status": None,
        "received_bytes": 0,
    }
    assert "secret" not in str(error.value.diagnostics)
    assert opener.open.call_count == 1


@pytest.mark.parametrize(
    "headers,body,received",
    [
        (b"Content-Length: 20", b"short", 5),
        (b"Content-Length: 20", b"", 0),
        (b"Transfer-Encoding: chunked", b"5\r\nshort\r\n", 5),
    ],
)
def test_truncated_body_is_transport_failure(monkeypatch, headers, body, received):
    response(monkeypatch, b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + body)
    with pytest.raises(provider.ProviderError) as error:
        provider.OpenRouterTransport().post(b"{}", "fake", 5, 100)
    assert error.value.code == "transport_error"
    assert error.value.retryable is True
    assert error.value.diagnostics == {
        "phase": "read",
        "kind": "incomplete_read",
        "http_status": 200,
        "received_bytes": received,
    }


@pytest.mark.parametrize(
    "body,received",
    [
        (b"", 0),
        (b"a\r\nshort", 5),
        (b"5\r\nshort", 5),
        (b"5\r\nshort\r\ngarbage\r\n", 5),
    ],
)
def test_chunked_truncation_and_invalid_size(monkeypatch, body, received):
    response(monkeypatch, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    with pytest.raises(provider.ProviderError) as error:
        provider.OpenRouterTransport().post(b"{}", "fake", 5, 100)
    assert error.value.code == "transport_error"
    assert error.value.retryable
    assert error.value.diagnostics == {
        "phase": "read",
        "kind": "incomplete_read",
        "http_status": 200,
        "received_bytes": received,
    }


@pytest.mark.parametrize("content", [b"", b"short"])
def test_stdlib_read1_does_not_raise_on_short_content_length(content):
    reply = http.client.HTTPResponse(
        Socket(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n" + content)
    )
    reply.begin()
    assert reply.read1(100) == content
    assert reply.read1(100) == b""
    assert reply.isclosed()
    assert reply.length == 20 - len(content)


@pytest.mark.parametrize(
    "error,kind",
    [
        (TimeoutError("secret"), "timeout"),
        (provider.URLError(TimeoutError("secret")), "timeout"),
        (OSError("secret"), "network"),
        (http.client.BadStatusLine("secret"), "http_protocol"),
    ],
)
def test_read_errors_are_safe_and_retryable(monkeypatch, error, kind):
    sock, opener = response(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
    reply = opener.open.return_value
    monkeypatch.setattr(reply, "read1", MagicMock(side_effect=[b"short", error]))
    with pytest.raises(provider.ProviderError) as caught:
        provider.OpenRouterTransport().post(b"secret", "secret", 5, 100)
    assert caught.value.code == "transport_error"
    assert caught.value.retryable
    assert caught.value.diagnostics == {
        "phase": "read",
        "kind": kind,
        "http_status": 200,
        "received_bytes": 5,
    }
    assert "secret" not in str(caught.value)
    assert sock.stream.closed


def test_deadline_after_partial_read(monkeypatch):
    _, opener = response(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
    monkeypatch.setattr(opener.open.return_value, "read1", MagicMock(return_value=b"short"))
    monkeypatch.setattr(provider.time, "monotonic", MagicMock(side_effect=[0, 1, 6]))
    with pytest.raises(provider.ProviderError) as caught:
        provider.OpenRouterTransport().post(b"{}", "fake", 5, 100)
    assert caught.value.code == "transport_timeout"
    assert caught.value.retryable
    assert caught.value.diagnostics == {
        "phase": "read",
        "kind": "deadline",
        "http_status": 200,
        "received_bytes": 5,
    }


@pytest.mark.parametrize("status", [200, 204])
def test_empty_complete_response(monkeypatch, status):
    response(monkeypatch, f"HTTP/1.1 {status} OK\r\nContent-Length: 0\r\n\r\n".encode())
    assert provider.OpenRouterTransport().post(b"{}", "fake", 5, 100).body == b""


def test_http_error_body_is_closed_and_not_returned(monkeypatch):
    body = io.BytesIO(b"secret")
    error = provider.HTTPError(provider.ENDPOINT, 429, "secret", {}, body)
    opener = MagicMock()
    opener.open.side_effect = error
    monkeypatch.setattr(provider, "build_opener", lambda *args: opener)
    assert provider.OpenRouterTransport().post(b"{}", "fake", 5, 100) == provider.HTTPReply(
        429, b""
    )
    assert body.closed
