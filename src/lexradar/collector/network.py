"""Bounded GET transport with DNS pinning and explicit redirect validation."""

import http.client
import ipaddress
import os
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from .models import Limits


class CollectionError(Exception):
    pass


def canonical_url(url: str) -> str:
    url = urldefrag(url)[0]
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise CollectionError("Only credential-free HTTP(S) URLs are allowed")
    if "\\" in url or any(ord(c) < 33 for c in url):
        raise CollectionError("Invalid URL characters")
    try:
        port = p.port
    except ValueError as exc:
        raise CollectionError("Invalid port") from exc
    host = p.hostname.encode("idna").decode().lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, 80 if p.scheme == "http" else 443) else f"{host}:{port}"
    return urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))


def origin(url: str) -> tuple[str, str, int]:
    p = urlsplit(canonical_url(url))
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class AddressPolicy:
    def __init__(self, timeout: float = 5):
        self.timeout = timeout

    def resolve(self, host: str, port: int) -> str:
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
            addresses = answers.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise CollectionError("DNS resolution timeout") from exc
        if isinstance(addresses, OSError):
            raise CollectionError("DNS resolution failed") from addresses
        ips = sorted({item[4][0] for item in addresses})
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
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

    def get(self, url: str, allowed_origin: tuple[str, str, int]) -> Response:
        if self.received >= self.limits.max_total_bytes:
            raise CollectionError("Total download limit exceeded")
        deadline = time.monotonic() + self.limits.timeout_seconds
        current = canonical_url(url)
        for step in range(self.limits.max_redirects + 1):
            if origin(current) != allowed_origin:
                raise CollectionError("Cross-origin navigation blocked")
            p = urlsplit(current)
            host, port = p.hostname, origin(current)[2]
            # Do not silently bypass an environment's mandatory egress proxy.
            if type(self.policy) is AddressPolicy and any(
                os.environ.get(k)
                for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
            ):
                raise CollectionError("Pinned transport requires direct egress; proxy configured")
            ip = self.policy.resolve(host, port)
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
                        "User-Agent": "LexRadar/0.2 (bounded evidence collector)",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or step == self.limits.max_redirects:
                        raise CollectionError("Redirect limit or missing Location")
                    current = canonical_url(urljoin(current, location))
                    continue
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise CollectionError("Encoded response unsupported")
                declared = response.getheader("Content-Length")
                if declared and int(declared) > self.limits.max_file_bytes:
                    raise CollectionError("File size limit exceeded")
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CollectionError("Request timeout")
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, self.limits.max_file_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    self.received += len(chunk)
                    if len(body) > self.limits.max_file_bytes:
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
