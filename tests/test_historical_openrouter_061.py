"""Offline comparison with the preserved pre-compact OpenRouter implementation."""

import http.client
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lexradar.auditors import provider
from lexradar.single_auditor import DeepSeekAdapter, profile

SNAPSHOT = Path(__file__).parent / "fixtures/openrouter_061/transport_ccc08c7.py.txt"


class MemoryPeer(io.BytesIO):
    def __init__(self, wire):
        super().__init__(wire)
        self._sock = self

    def makefile(self, *args):
        return io.BufferedReader(self)

    def settimeout(self, timeout):
        assert 0 < timeout <= 120


class TracedResponse(http.client.HTTPResponse):
    def __init__(self, wire):
        super().__init__(MemoryPeer(wire))
        self.chunks = []
        self.begin()

    def read1(self, size):
        chunk = super().read1(size)
        self.chunks.append(chunk)
        return chunk


@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
def test_single_a_matches_historical_request_and_response_reader(monkeypatch, framing):
    # No real environment credential is read by either implementation.
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-history-061")
    content = '{"auditor":"A","findings":[]}'
    body = (
        b" " * 440
        + json.dumps(
            {
                "model": "deepseek/deepseek-chat",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {
                    "prompt_tokens": 29868,
                    "completion_tokens": 1625,
                    "total_tokens": 31493,
                    "cost": "0.0093596607",
                },
            }
        ).encode()
    )
    if framing == "length":
        headers = f"Content-Length: {len(body)}".encode()
        encoded = body
    elif framing == "chunked":
        headers = b"Transfer-Encoding: chunked"
        encoded = (
            b"".join(
                f"{len(part):x}\r\n".encode() + part + b"\r\n" for part in (body[:440], body[440:])
            )
            + b"0\r\n\r\n"
        )
    else:
        headers, encoded = b"Connection: close", body
    wire = b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + encoded
    messages = [{"role": "user", "content": "Synthetic protocol comparison"}]
    payload = json.dumps(
        {
            "model": "deepseek/deepseek-chat",
            "messages": messages,
            "max_tokens": 4000,
            "temperature": 0,
            "stream": False,
            "n": 1,
            "response_format": {"type": "json_object"},
            "provider": {
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": 1.0, "completion": 2.0},
            },
        },
        ensure_ascii=False,
    ).encode()
    namespace = dict(vars(provider))
    exec(compile(SNAPSHOT.read_text(), str(SNAPSHOT), "exec"), namespace)
    historical_response = TracedResponse(wire)
    old_opener = MagicMock()
    old_opener.open.return_value = historical_response
    namespace["build_opener"] = lambda *args: old_opener
    old_provider = namespace["OpenRouterProvider"](
        profile(timeout_seconds=120, model="deepseek/deepseek-chat")
    )
    old_reply = old_provider.complete(old_provider.config.auditor_a, messages)

    current_response = TracedResponse(wire)
    current_opener = MagicMock()
    current_opener.open.return_value = current_response
    monkeypatch.setattr(provider, "build_opener", lambda *args: current_opener)
    adapter = DeepSeekAdapter(timeout_seconds=120, model="deepseek/deepseek-chat")
    assert type(adapter.transport) is provider.OpenRouterTransport
    reply = adapter.complete(adapter.config.auditor_a, messages)
    old_request = old_opener.open.call_args.args[0]
    new_request = current_opener.open.call_args.args[0]
    assert old_request.full_url == new_request.full_url == provider.ENDPOINT
    assert old_request.method == new_request.method == "POST"
    assert old_request.header_items() == new_request.header_items()
    assert old_request.data == new_request.data == payload
    assert old_reply.content == content
    assert b"".join(historical_response.chunks) == body
    assert reply.content == content
    assert str(reply.usage.cost_usd) == "0.0093596607"
    # The old loop performs one extra empty read after Content-Length is consumed.
    assert [c for c in historical_response.chunks if c] == [c for c in current_response.chunks if c]
    assert b"".join(current_response.chunks) == body
    settings = profile(timeout_seconds=120, model="deepseek/deepseek-chat")
    assert settings.max_budget_usd == profile().max_budget_usd == 0.50
    assert settings.max_response_bytes == 500000
    assert settings.auditor_a.max_output_tokens == 4000
    assert settings.retries == 0
