"""One sequential request budget, immutable robots snapshot, and a durable crawl journal."""

import hashlib
import json
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import Field

from ..models import Model
from .network import CollectionError, canonical_url, origin
from .robots import MAX_ROBOTS_BYTES, USER_AGENT, octets, parse_robots


class CrawlOptions(Model):
    min_delay_seconds: float = Field(default=2, ge=0.1, le=60, allow_inf_nan=False)
    max_requests: int = Field(default=64, ge=1, le=1000)
    max_duration_seconds: float = Field(default=300, ge=0.1, le=3600, allow_inf_nan=False)


class Clock:
    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


class CrawlSession:
    def __init__(self, fetcher, url, output, options=None, *, clock=None):
        self.fetcher, self.origin = fetcher, origin(url)
        self.options = (options or CrawlOptions()).model_copy(deep=True)
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.last_attempt = None
        self.requests = self.sent = 0
        self.stop_reason = None
        self.rules = None
        self.robots_checked = False
        self.robots_status = "not_checked"
        self.denied = self.blocked = 0
        self.output = output / "crawl"
        self.output.mkdir()
        self.journal = self.output / "requests.jsonl"
        self.event("started", options=self.options.model_dump(), user_agent=USER_AGENT)

    def event(self, event, **values):
        with self.journal.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "time": datetime.now(UTC).isoformat(),
                        "event": event,
                        "request_attempts": self.requests,
                        "requests_sent": self.sent,
                        **values,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            self.event("stopped", reason=reason)
        raise CollectionError(reason)

    def remaining(self):
        remaining = self.options.max_duration_seconds - (self.clock.now() - self.started)
        if remaining <= 0:
            self.stop("crawl_duration_limit")
        return remaining

    def check(self):
        if self.stop_reason:
            raise CollectionError(self.stop_reason)
        self.remaining()
        if self.requests >= self.options.max_requests:
            self.stop("request_count_limit")

    def _before(self, url, *, robots=False):
        url = canonical_url(url)
        if origin(url) != self.origin:
            self.blocked += 1
            self.event("url_denied", url=url, reason="cross_origin")
            raise CollectionError("Cross-origin navigation blocked")
        path = octets(urlsplit(url).path).lower()
        if any(v in path for v in ("%2f", "%5c", "%25")) or any(
            v in {".", ".."} for v in path.split("/")
        ):
            self.blocked += 1
            self.event("url_denied", url=url, reason="ambiguous_path")
            raise CollectionError("Ambiguous path blocked before robots matching")
        self.check()
        if not robots:
            self._robots()
            if not self.rules.allows(url):
                self.denied += 1
                self.blocked += 1
                self.event("url_denied", url=url, reason="robots_disallow")
                raise CollectionError("robots_disallow")
            self.event("url_allowed", url=url, reason="robots_allow")
        delay = max(self.options.min_delay_seconds, self.rules.crawl_delay if self.rules else 0)
        wait = (
            max(0, delay - (self.clock.now() - self.last_attempt))
            if self.last_attempt is not None
            else 0
        )
        self.event("delay", url=url, interval_seconds=delay, wait_seconds=wait)
        if wait >= self.remaining():
            self.stop("crawl_duration_limit")
        if wait:
            self.clock.sleep(wait)
        self.check()
        self.requests += 1
        self.last_attempt = self.clock.now()
        self.event("request_attempt", url=url, purpose="robots" if robots else "content")
        return self.remaining()

    def _network_event(self, event, url, status=None):
        if event == "request_sent":
            self.sent += 1
        if event == "navigation_blocked":
            self.blocked += 1
        self.event(event, url=url, http_status=status)

    def _robots(self):
        if self.robots_checked:
            if self.rules is None:
                self.stop("robots_unavailable")
            return
        self.robots_checked = True
        scheme, host, port = self.origin
        host = f"[{host}]" if ":" in host else host
        authority = host if port == (443 if scheme == "https" else 80) else f"{host}:{port}"
        url = f"{scheme}://{authority}/robots.txt"
        try:
            response = self.fetcher.get(
                url,
                self.origin,
                before_request=lambda u: self._before(u, robots=True),
                on_event=self._network_event,
                allow_redirects=False,
                max_file_bytes=MAX_ROBOTS_BYTES,
            )
            if response.status != 200 or response.url != url:
                raise CollectionError(f"Robots HTTP {response.status}; no permission established")
            # Retain real received bytes; this is a crawl-policy artifact, not legal evidence.
            (self.output / "robots-response.txt").write_bytes(response.body)
            if response.content_type.split(";", 1)[0].strip().lower() != "text/plain":
                raise ValueError("Robots Content-Type is not text/plain")
            self.rules = parse_robots(response.body)
            self.robots_status = "received"
            self.event(
                "robots_received",
                url=url,
                http_status=response.status,
                sha256=hashlib.sha256(response.body).hexdigest(),
                selected_agent=self.rules.selected_agent,
                crawl_delay=self.rules.crawl_delay,
                artifact="crawl/robots-response.txt",
            )
        except (CollectionError, ValueError, OSError) as exc:
            self.robots_status = "unavailable"
            self.event("robots_unavailable", url=url, reason=str(exc)[:300], policy="deny_all")
            self.stop("robots_unavailable")

    def get(self, url, allowed_origin):
        if allowed_origin != self.origin:
            raise CollectionError("Session origin mismatch")
        try:
            return self.fetcher.get(
                url, allowed_origin, before_request=self._before, on_event=self._network_event
            )
        except CollectionError as exc:
            self.event("fetch_rejected_or_failed", url=url, reason=str(exc)[:300])
            if self.clock.now() - self.started >= self.options.max_duration_seconds:
                self.stop("crawl_duration_limit")
            if str(exc).startswith("Total download limit"):
                self.stop_reason = "download_byte_limit"
                self.event("stopped", reason=self.stop_reason)
            elif str(exc).startswith(("Fetch failed:", "Request timeout", "DNS resolution")):
                self.stop_reason = "network_error"
                self.event("stopped", reason=self.stop_reason)
            raise

    def finish(self, reason="queue_exhausted"):
        if (
            self.stop_reason is None
            and self.clock.now() - self.started >= self.options.max_duration_seconds
        ):
            self.stop_reason = "crawl_duration_limit"
            self.event("stopped", reason=self.stop_reason)
        reason = self.stop_reason or (
            "queue_exhausted_with_denials"
            if reason == "queue_exhausted" and self.blocked
            else reason
        )
        summary = {
            "stop_reason": reason,
            "robots_status": self.robots_status,
            "robots_unavailable_policy": "deny_all",
            "user_agent": USER_AGENT,
            "request_attempts": self.requests,
            "requests_sent": self.sent,
            "robots_denied_urls": self.denied,
            "blocked_urls": self.blocked,
            "elapsed_seconds": max(0, self.clock.now() - self.started),
            "options": self.options.model_dump(),
        }
        self.event("finished", **summary)
        (self.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
