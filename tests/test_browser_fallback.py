"""Compatibility retry uses the real Fetcher entry point without network access."""

from email.message import Message
from types import SimpleNamespace

import pytest

from lexradar.collector import browser_transport, network
from lexradar.collector.models import Limits


@pytest.mark.parametrize(
    "status,encoding,body,retry",
    [(406, "identity", b"", True), (415, "gzip", b"", True),
     (505, "identity", b"", True), (403, "br", b"", False),
     (200, "br", b"", True), (200, "gzip", b"bad gzip", True),
     (200, "deflate", b"", True), (200, "identity", b"ok", False)],
)
def test_fetcher_compatibility_retry(monkeypatch, status, encoding, body, retry):
    url = "https://clinic.example/"
    headers = Message()
    headers["Content-Encoding"] = encoding
    headers["Content-Type"] = "text/html"
    chunks = iter([body, b""])
    response = SimpleNamespace(
        status=status, headers=headers, getheader=headers.get, fp=None,
        read1=lambda size: next(chunks),
    )
    closed = []
    connection = SimpleNamespace(
        request=lambda *a, **kw: None, getresponse=lambda: response,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(network, "PinnedHTTPS", lambda *a: connection)
    fetcher = network.Fetcher(Limits(), SimpleNamespace(resolve=lambda *a, **kw: "8.8.8.8"))
    expected = network.Response(url, 200, "text/html", b"browser")
    attempts, events, calls = [], [], []

    def browser_get(actual, target, allowed, **kwargs):
        assert closed and actual is fetcher and target == url
        assert allowed == network.origin(url)
        assert kwargs["max_redirects"] == fetcher.limits.max_redirects
        assert kwargs["max_file_bytes"] == 100
        kwargs["before_request"](target)
        calls.append(target)
        return expected

    monkeypatch.setattr(browser_transport, "browser_get", browser_get)
    result = fetcher.get(
        url, network.origin(url), before_request=lambda u: attempts.append(u),
        on_event=lambda *args: events.append(args), max_file_bytes=100,
    )
    assert bool(calls) is retry
    assert len(attempts) == (2 if retry else 1)
    assert any(e[0] == "browser_fallback" for e in events) is retry
    if retry:
        assert result is expected
    else:
        assert result.status == status
