"""OpenRouter-only transport. No implicit credentials, redirects, fallback or unsafe error logs."""

import http.client
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from .models import REQUIRED_TOPICS, AnalysisConfig, ModelSettings, Usage

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPReply:
    status: int
    body: bytes


class Transport(Protocol):
    def post(self, payload: bytes, key: str, timeout: float, max_bytes: int) -> HTTPReply: ...


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenRouterTransport:
    def post(self, payload: bytes, key: str, timeout: float, max_bytes: int) -> HTTPReply:
        request = Request(
            ENDPOINT,
            data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            deadline = time.monotonic() + timeout
            with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProviderError("transport_timeout", retryable=True)
                    if response.fp is not None:
                        response.fp.raw._sock.settimeout(remaining)
                    chunk = response.read1(min(65536, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ProviderError("response_size_exceeded")
                return HTTPReply(response.status, bytes(body))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            return HTTPReply(status, b"")  # Do not persist error bodies or headers.
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError("transport_error", retryable=True) from exc


@dataclass(frozen=True)
class ProviderReply:
    content: str
    usage: Usage


class Provider(Protocol):
    def complete(
        self, settings: ModelSettings, messages: list[dict[str, str]]
    ) -> ProviderReply: ...


class OpenRouterProvider:
    def __init__(self, config: AnalysisConfig, transport: Transport | None = None):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key or any(c.isspace() for c in key):
            raise ProviderError("missing_or_invalid_api_key")
        if any(key in settings.model for settings in (config.auditor_a, config.auditor_b)):
            raise ProviderError("secret_in_configuration")
        self._key = key
        self.config = config
        self.transport = transport or OpenRouterTransport()

    def complete(self, settings: ModelSettings, messages: list[dict[str, str]]) -> ProviderReply:
        payload = json.dumps(
            {
                "model": settings.model,
                "messages": messages,
                "max_tokens": settings.max_output_tokens,
                "temperature": 0,
                "stream": False,
                "n": 1,
                "response_format": {"type": "json_object"},
                "provider": {
                    "allow_fallbacks": False,
                    "require_parameters": True,
                    "max_price": {
                        "prompt": float(settings.prompt_price_cap),
                        "completion": float(settings.completion_price_cap),
                    },
                },
            },
            ensure_ascii=False,
        ).encode()
        if self._key in payload.decode():
            raise ProviderError("secret_in_materials")
        reply = self.transport.post(
            payload, self._key, self.config.timeout_seconds, self.config.max_response_bytes
        )
        if not 200 <= reply.status < 300:
            raise ProviderError(
                f"http_{reply.status}",
                retryable=reply.status == 429 or reply.status in {500, 502, 503, 504},
            )
        if len(reply.body) > self.config.max_response_bytes:
            raise ProviderError("response_size_exceeded")
        try:
            body = reply.body.decode("utf-8")
            if self._key in body:
                raise ProviderError("secret_echo_rejected")
            data = json.loads(body)
            if self._key in json.dumps(data, ensure_ascii=False):
                raise ProviderError("secret_echo_rejected")
            if data.get("error"):
                raise ProviderError("provider_error", retryable=True)
            if data.get("model") != settings.model:
                raise ProviderError("unexpected_model_no_fallback")
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "end_turn"}:
                raise ProviderError("incomplete_completion")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ProviderError("invalid_completion")
            if self._key in content:
                raise ProviderError("secret_echo_rejected")
            try:
                decoded_content = json.loads(content)
            except ValueError:
                decoded_content = None
            if self._key in json.dumps(decoded_content, ensure_ascii=False):
                raise ProviderError("secret_echo_rejected")
            usage_data = data.get("usage") or {}
            usage = Usage.model_validate(
                {
                    "prompt_tokens": usage_data.get("prompt_tokens"),
                    "completion_tokens": usage_data.get("completion_tokens"),
                    "total_tokens": usage_data.get("total_tokens"),
                    "cost_usd": usage_data.get("cost"),
                }
            )
            return ProviderReply(content, usage)
        except (
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            ValidationError,
        ) as exc:
            raise ProviderError("invalid_provider_envelope") from exc


class OfflineProvider:
    """Schema-complete demonstrator, not an AI or simulated legal conclusion."""

    def complete(self, settings: ModelSettings, messages: list[dict[str, str]]) -> ProviderReply:
        auditor = "A" if "Auditor label: A" in messages[0]["content"] else "B"
        result = {
            "auditor": auditor,
            "findings": [],
            "coverage": [
                {
                    "topic": topic.value,
                    "status": "insufficient_evidence",
                    "limitations": ["Offline demonstration; no AI/legal assessment performed"],
                }
                for topic in sorted(REQUIRED_TOPICS)
            ],
            "limitations": ["Offline mode: placeholders only; not an actual AI audit"],
        }
        return ProviderReply(json.dumps(result), Usage(cost_usd=Decimal(0)))
