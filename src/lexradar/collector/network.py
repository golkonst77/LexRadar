"""Bounded GET transport with DNS pinning and explicit redirect validation."""

import http.client
import ipaddress
import os
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

from .models import Limits
from .robots import USER_AGENT


class CollectionError(Exception):
    pass


def canonical_url(url: str) -> str:
    if "\\" in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise CollectionError("Invalid URL characters")
    try:
        url = urldefrag(url)[0]
        p = urlsplit(url)
        if (
            p.scheme not in {"http", "https"}
            or not p.hostname
            or p.username is not None
            or p.password is not None
        ):
            raise CollectionError("Only credential-free HTTP(S) URLs are allowed")
        port = p.port
        if port == 0:
            raise CollectionError("Invalid port")
        if "%" in p.hostname:
            raise CollectionError("Encoded or scoped hostnames are blocked")
        host = p.hostname.encode("idna").decode().lower().rstrip(".")
        if not host:
            raise CollectionError("Empty hostname")
        if ":" in host:
            host = f"[{host}]"
        netloc = host if port in (None, 80 if p.scheme == "http" else 443) else f"{host}:{port}"
        if re.search(r"%(?![0-9a-fA-F]{2})", p.path + p.query):
            raise CollectionError("Malformed percent escape")
        return urlunsplit(
            (
                p.scheme,
                netloc,
                quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                quote(p.query, safe="/%?:@!$&'()*+,;=-._~"),
                "",
            )
        )
    except (ValueError, UnicodeError) as exc:
        raise CollectionError("Invalid URL") from exc


def origin(url: str) -> tuple[str, str, int]:
    p = urlsplit(canonical_url(url))
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class AddressPolicy:
    def __init__(self, timeout: float = 5):
        self.timeout = timeout

    def resolve(self, host: str, port: int, *, timeout: float | None = None) -> str:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise CollectionError("Local hostname blocked")
        answers = queue.Queue(maxsize=1)

        def lookup():
            try:
                answers.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                answers.put(exc)

        threading.Thread(target=lookup, daemon=True).start()
        try:
            addresses = answers.get(
                timeout=min(self.timeout, timeout) if timeout is not None else self.timeout
            )
        except queue.Empty as exc:
            raise CollectionError("DNS resolution timeout") from exc
        if isinstance(addresses, OSError):
            raise CollectionError("DNS resolution failed") from addresses
        ips = sorted({item[4][0] for item in addresses})
        if not ips or any(
            not ipaddress.ip_address(ip).is_global
            or ipaddress.ip_address(ip).is_multicast
            or ipaddress.ip_address(ip).is_reserved
            for ip in ips
        ):
            raise CollectionError("Non-public address blocked")
        return ips[0]


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)


class PinnedHTTPS(PinnedHTTP):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


@dataclass
class Response:
    url: str
    status: int
    content_type: str
    body: bytes


class Fetcher:
    def __init__(self, limits: Limits, policy: AddressPolicy | None = None):
        self.limits = limits
        self.policy = policy or AddressPolicy(limits.timeout_seconds)
        self.received = 0

    def get(
        self,
        url: str,
        allowed_origin: tuple[str, str, int],
        *,
        before_request=None,
        on_event=None,
        allow_redirects=True,
        max_file_bytes=None,
    ) -> Response:
        if self.received >= self.limits.max_total_bytes:
            raise CollectionError("Total download limit exceeded")
        file_limit = min(self.limits.max_file_bytes, max_file_bytes or self.limits.max_file_bytes)
        deadline = time.monotonic() + self.limits.timeout_seconds
        current = canonical_url(url)
        for step in range(self.limits.max_redirects + 1):
            if origin(current) != allowed_origin:
                if on_event:
                    on_event("navigation_blocked", current)
                raise CollectionError("Cross-origin navigation blocked")
            if before_request:
                began = time.monotonic()
                remaining_crawl = before_request(current)
                # Pacing is charged to the crawl deadline, not to the HTTP I/O timeout.
                deadline += time.monotonic() - began
                if remaining_crawl is not None:
                    deadline = min(deadline, time.monotonic() + remaining_crawl)
            p = urlsplit(current)
            host, port = p.hostname, origin(current)[2]
            # Do not silently bypass an environment's mandatory egress proxy.
            if type(self.policy) is AddressPolicy and any(
                os.environ.get(k)
                for k in (
                    "HTTPS_PROXY",
                    "HTTP_PROXY",
                    "https_proxy",
                    "http_proxy",
                    "ALL_PROXY",
                    "all_proxy",
                )
            ):
                raise CollectionError("Pinned transport requires direct egress; proxy configured")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError("Request timeout")
            ip = self.policy.resolve(host, port, timeout=remaining)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError("Request timeout")
            connection = (PinnedHTTPS if p.scheme == "https" else PinnedHTTP)(
                host, port, ip, remaining
            )
            try:
                connection.request(
                    "GET",
                    urlunsplit(("", "", p.path or "/", p.query, "")),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Encoding": "identity",
                    },
                )
                if on_event:
                    on_event("request_sent", current)
                response = connection.getresponse()
                if on_event:
                    on_event("http_response", current, response.status)
                if response.status in {301, 302, 303, 307, 308}:
                    if not allow_redirects:
                        raise CollectionError("Robots redirect refused; target not requested")
                    location = response.getheader("Location")
                    if not location or step == self.limits.max_redirects:
                        raise CollectionError("Redirect limit or missing Location")
                    current = canonical_url(urljoin(current, location))
                    if on_event:
                        on_event("redirect_target", current)
                    continue
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise CollectionError("Encoded response unsupported")
                lengths = response.headers.get_all("Content-Length", [])
                transfer = response.getheader("Transfer-Encoding")
                if len(lengths) > 1 or (lengths and transfer):
                    raise CollectionError("Ambiguous response framing")
                if transfer and transfer.lower() != "chunked":
                    raise CollectionError("Unsupported transfer encoding")
                declared = lengths[0] if lengths else None
                if declared is not None and not re.fullmatch(r"[0-9]+", declared):
                    raise CollectionError("Invalid Content-Length")
                if declared is not None and int(declared) > file_limit:
                    raise CollectionError("File size limit exceeded")
                if declared is not None and int(declared) > (
                    self.limits.max_total_bytes - self.received
                ):
                    raise CollectionError("Total download limit exceeded")
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CollectionError("Request timeout")
                    if response.fp is not None:
                        response.fp.raw._sock.settimeout(remaining)
                    chunk = response.read1(
                        min(
                            65536,
                            file_limit + 1 - len(body),
                            self.limits.max_total_bytes + 1 - self.received,
                        )
                    )
                    if not chunk:
                        break
                    body.extend(chunk)
                    self.received += len(chunk)
                    if len(body) > file_limit:
                        raise CollectionError("File size limit exceeded")
                    if self.received > self.limits.max_total_bytes:
                        raise CollectionError("Total download limit exceeded")
                if declared and len(body) != int(declared):
                    raise CollectionError("Incomplete response body")
                return Response(
                    current, response.status, response.getheader("Content-Type", ""), bytes(body)
                )
            except (OSError, ValueError, http.client.HTTPException) as exc:
                raise CollectionError(f"Fetch failed: {type(exc).__name__}") from exc
            finally:
                connection.close()
        raise CollectionError("Redirect limit exceeded")
