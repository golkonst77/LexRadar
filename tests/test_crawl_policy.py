"""Only local HTTP fixtures, virtual pacing and mocked DNS; no public-site requests."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeCrawlClock
from pydantic import ValidationError
from test_collector import FixturePolicy, pdf_bytes

from lexradar.collector import CrawlOptions, Limits, collect
from lexradar.collector.crawl import CrawlSession
from lexradar.collector.network import AddressPolicy, CollectionError, Fetcher, origin
from lexradar.collector.robots import USER_AGENT, parse_robots


@pytest.fixture
def web():
    routes = {"/robots.txt": (200, "text/plain", b"User-agent: LexRadar\nDisallow:\n")}
    requests, active, maximum = [], 0, 0
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                requests.append((self.path, self.headers.get("User-Agent"), time.monotonic()))
                status, content_type, body = routes.get(
                    self.path, (200, "text/html", b"<html>Public synthetic text.</html>")
                )
                if isinstance(body, float):
                    time.sleep(body)
                    body = b"Public synthetic text."
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", body.decode())
                    body = b""
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                with lock:
                    active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    yield url, FixturePolicy(server.server_port), routes, requests, lambda: maximum
    server.shutdown()
    server.server_close()
    thread.join()


def session(web, root, *, options=None, limits=None, clock=None):
    url, policy, *_ = web
    return CrawlSession(
        Fetcher(limits or Limits(), policy), url, root, options, clock=clock or FakeCrawlClock()
    )


def events(s):
    return [json.loads(line) for line in s.journal.read_text().splitlines()]


@pytest.mark.parametrize(
    "body,path,allowed",
    [
        (b"User-agent: *\nDisallow: /private\n", "/private/file", False),
        (
            b"User-agent: *\nDisallow: /private\nAllow: /private/public\n",
            "/private/public/file",
            True,
        ),
        (b"User-agent: *\nDisallow: /same\nAllow: /same\n", "/same", True),
        (b"User-agent: *\nDisallow: /*.pdf$\n", "/policy.pdf", False),
        (b"User-agent: *\nDisallow: /*.pdf$\n", "/policy.pdf?x=1", True),
        (b"User-agent: *\nDisallow: /x?secret=*\n", "/x?secret=yes", False),
        (b"User-agent: LexRadar\nDisallow: /p%61th\n", "/path", False),
        (b"User-agent: LexRadar\nDisallow: /path\n", "/%70ath", False),
        (b"User-agent: LexRadar\nDisallow: /encoded%2Fpath\n", "/encoded%2fpath", False),
        (b"User-agent: LexRadar\nDisallow: /case\n", "/Case", True),
        (b"User-agent: OtherBot\nDisallow: /\n", "/", True),
        (b"User-agent: *\nDisallow: /\n\nUser-agent: lExRaDaR\nDisallow:\n", "/", True),
        (b"User-agent: LexRadar/0.2\nDisallow: /\n", "/", False),
        (b"User-agent: LexRadar\nDisallow: /\n", "/", False),
        (b"User-agent: LexRadar\nDisallow:\n", "/", True),
        (b"", "/", True),
        (b"# No rules\n", "/", True),
        (
            "User-agent: LexRadar\nDisallow: /закрыто\n".encode(),
            "/%D0%B7%D0%B0%D0%BA%D1%80%D1%8B%D1%82%D0%BE",
            False,
        ),
    ],
)
def test_robots_matching(body, path, allowed):
    assert parse_robots(body).allows("https://fixture.example" + path) is allowed


def test_groups_merged_specific_agent_and_largest_delay():
    rules = parse_robots(
        b"User-agent: *\nDisallow: /\nCrawl-delay: 99\n\n"
        b"User-agent: LexRadar\nDisallow: /a\nCrawl-delay: 2.5\n\n"
        b"User-agent: LexRadar\nUser-agent: Other\nDisallow: /b\nCrawl-delay: 4\n"
    )
    assert not rules.allows("https://fixture.example/a")
    assert not rules.allows("https://fixture.example/b")
    assert rules.allows("https://fixture.example/c")
    assert rules.crawl_delay == 4 and rules.selected_agent == "LexRadar"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Not robots</html>",
        b"Disallow: /private",
        b"User-agent:\nDisallow: /",
        b"User-agent: LexRadar\nCrawl-delay: NaN",
        b"User-agent: LexRadar\nCrawl-delay: -1",
        b"User-agent: LexRadar\nCrawl-delay: 100000",
        b"User-agent: LexRadar\nCrawl-delay: oops",
        b"User-agent: LexRadar\nDisallow: malformed",
        b"User-agent: LexRadar\nDisallow: /bad%zz",
        b"\xff",
        b"User-agent: LexRadar\nDisallow: /\x00",
        b"Sitemap: https://fixture.example/map",
        b"x" * 500001,
    ],
)
def test_ambiguous_robots_fails_closed(body):
    with pytest.raises(ValueError):
        parse_robots(body)


@pytest.mark.parametrize("delay,expected", [(0, 2), (1, 2), (2, 2), (3.5, 3.5)])
def test_pacing_includes_robots_and_every_request(web, tmp_path, delay, expected):
    url, _, routes, requests, maximum = web
    routes["/robots.txt"] = (
        200,
        "text/plain",
        f"User-agent: LexRadar\nCrawl-delay: {delay}\nDisallow:\n".encode(),
    )
    clock = FakeCrawlClock()
    s = session(web, tmp_path, clock=clock)
    s.get(url, origin(url))
    s.get(url + "policy", origin(url))
    assert clock.sleeps == [expected, expected]
    summary = s.finish()
    assert summary["requests_sent"] == summary["request_attempts"] == 3
    assert [r[0] for r in requests] == ["/robots.txt", "/", "/policy"]
    assert {r[1] for r in requests} == {USER_AGENT}
    assert maximum() == 1
    assert sum(e["event"] == "robots_received" for e in events(s)) == 1
    assert (tmp_path / "crawl/robots-response.txt").read_bytes() == routes["/robots.txt"][2]


def test_real_pacing_clock_on_local_server(web, tmp_path):
    class RealClock:
        now = staticmethod(time.monotonic)
        sleep = staticmethod(time.sleep)

    url, _, _, requests, _ = web
    s = session(web, tmp_path, options=CrawlOptions(min_delay_seconds=0.1), clock=RealClock())
    s.get(url, origin(url))
    assert requests[1][2] - requests[0][2] >= 0.09


@pytest.mark.parametrize(
    "status,mime,body",
    [
        (404, "text/plain", b"Not found"),
        (403, "text/plain", b"Forbidden"),
        (429, "text/plain", b"Slow down"),
        (500, "text/plain", b"Error"),
        (200, "text/html", b"<html>Wrong response</html>"),
        (200, "text/plain", b"Not valid robots"),
        (200, "text/plain", b"\xff"),
        (302, "text/plain", b"/private"),
    ],
)
def test_robots_unavailable_blocks_all_content_no_retry(web, tmp_path, status, mime, body):
    url, _, routes, requests, _ = web
    routes["/robots.txt"] = (status, mime, body)
    s = session(web, tmp_path)
    for target in (url, url + "policy"):
        with pytest.raises(CollectionError):
            s.get(target, origin(url))
    assert [r[0] for r in requests] == ["/robots.txt"]
    assert s.finish()["stop_reason"] == "robots_unavailable"
    assert any(e["event"] == "robots_unavailable" and e["policy"] == "deny_all" for e in events(s))


@pytest.mark.parametrize("case", ["timeout", "network", "oversize"])
def test_robots_fetch_errors_are_not_permission(web, tmp_path, monkeypatch, case):
    url, _, routes, requests, _ = web
    routes["/robots.txt"] = (200, "text/plain", 0.25 if case == "timeout" else b"x" * 1025)
    limits = Limits(timeout_seconds=0.1, max_file_bytes=1024)
    s = session(web, tmp_path, limits=limits)
    if case == "network":

        def failure(*args, **kwargs):
            raise OSError("Synthetic connection error")

        monkeypatch.setattr("lexradar.collector.network.PinnedHTTP.connect", failure)
    with pytest.raises(CollectionError):
        s.get(url, origin(url))
    assert s.rules is None and s.finish()["robots_status"] == "unavailable"
    assert all(r[0] == "/robots.txt" for r in requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_target_is_checked_before_request(web, tmp_path, status):
    url, _, routes, requests, _ = web
    routes["/robots.txt"] = (200, "text/plain", b"User-agent: LexRadar\nDisallow: /private\n")
    routes["/start"] = (status, "text/html", b"/private")
    s = session(web, tmp_path)
    with pytest.raises(CollectionError, match="robots_disallow"):
        s.get(url + "start", origin(url))
    assert [r[0] for r in requests] == ["/robots.txt", "/start"]
    assert s.finish()["requests_sent"] == 2
    assert any(e["event"] == "url_denied" and e["url"] == url + "private" for e in events(s))


def test_disallowed_initial_url_and_pdf_are_never_sent(web, tmp_path):
    url, _, routes, requests, _ = web
    routes["/robots.txt"] = (
        200,
        "text/plain",
        b"User-agent: LexRadar\nDisallow: /private\nDisallow: /*.pdf$\n",
    )
    s = session(web, tmp_path)
    for path in ("private", "policy.pdf"):
        with pytest.raises(CollectionError, match="robots_disallow"):
            s.get(url + path, origin(url))
    s.get(url, origin(url))
    assert [r[0] for r in requests] == ["/robots.txt", "/"]


def test_allowed_redirects_paced_and_counted(web, tmp_path):
    url, _, routes, requests, _ = web
    routes["/a"] = (302, "text/html", b"/b")
    routes["/b"] = (307, "text/html", b"/c")
    s = session(web, tmp_path)
    response = s.get(url + "a", origin(url))
    assert response.url == url + "c"
    assert [r[0] for r in requests] == ["/robots.txt", "/a", "/b", "/c"]
    assert s.clock.sleeps == [2, 2, 2] and s.finish()["requests_sent"] == 4


@pytest.mark.parametrize(
    "target", ["http://169.254.169.254/latest", "http://127.0.0.1:1/", "https://fixture.example/"]
)
def test_cross_origin_redirect_is_never_followed(web, tmp_path, target):
    url, _, routes, requests, _ = web
    routes["/start"] = (302, "text/html", target.encode())
    s = session(web, tmp_path)
    with pytest.raises(CollectionError, match="Cross-origin"):
        s.get(url + "start", origin(url))
    assert [r[0] for r in requests] == ["/robots.txt", "/start"]
    assert any(e["event"] == "navigation_blocked" for e in events(s))


def test_request_limit_includes_redirects_and_robots(web, tmp_path):
    url, _, routes, requests, _ = web
    routes["/a"] = (302, "text/html", b"/b")
    routes["/b"] = (302, "text/html", b"/c")
    s = session(web, tmp_path, options=CrawlOptions(max_requests=3))
    with pytest.raises(CollectionError, match="request_count_limit"):
        s.get(url + "a", origin(url))
    assert [r[0] for r in requests] == ["/robots.txt", "/a", "/b"]
    summary = s.finish()
    assert summary["request_attempts"] == 3 and summary["stop_reason"] == "request_count_limit"


def test_duration_stops_before_wait_larger_than_budget(web, tmp_path):
    url, _, routes, requests, _ = web
    routes["/robots.txt"] = (200, "text/plain", b"User-agent: LexRadar\nCrawl-delay: 9\n")
    s = session(web, tmp_path, options=CrawlOptions(max_duration_seconds=3))
    with pytest.raises(CollectionError, match="crawl_duration_limit"):
        s.get(url, origin(url))
    assert [r[0] for r in requests] == ["/robots.txt"] and s.clock.sleeps == []
    assert s.finish()["stop_reason"] == "crawl_duration_limit"


def test_dns_rechecked_for_every_http_hop(web, tmp_path):
    url, policy, _, requests, _ = web

    class ChangedPolicy(FixturePolicy):
        def __init__(self, port):
            super().__init__(port)
            self.calls = 0

        def resolve(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise CollectionError("Non-public address blocked")
            return super().resolve(*args, **kwargs)

    s = CrawlSession(
        Fetcher(Limits(), ChangedPolicy(policy.port)), url, tmp_path, clock=FakeCrawlClock()
    )
    with pytest.raises(CollectionError, match="Non-public"):
        s.get(url, origin(url))
    assert [r[0] for r in requests] == ["/robots.txt"]
    assert s.requests == 2 and s.sent == 1


def test_proxy_guard_not_bypassed_and_ssrf_dns_still_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")

    def forbidden(*args, **kwargs):
        raise AssertionError("No network connection permitted")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    s = CrawlSession(
        Fetcher(Limits()), "https://fixture.example/", tmp_path, clock=FakeCrawlClock()
    )
    with pytest.raises(CollectionError, match="robots_unavailable"):
        s.get("https://fixture.example/", origin("https://fixture.example/"))
    assert s.sent == 0
    assert any("proxy configured" in e.get("reason", "") for e in events(s))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(CollectionError, match="Non-public"):
        AddressPolicy().resolve("fixture.example", 443)


@pytest.mark.parametrize(
    "path",
    [
        "a/../private",
        "%2e%2e/private",
        "a/%2E/private",
        "a%2Fprivate",
        "a%5Cprivate",
        "a%252Fprivate",
    ],
)
def test_ambiguous_paths_are_not_robot_bypasses(web, tmp_path, path):
    url, _, _, requests, _ = web
    s = session(web, tmp_path)
    with pytest.raises(CollectionError, match="Ambiguous path"):
        s.get(url + path, origin(url))
    assert not requests


def test_collect_retains_dossier_and_journal_on_request_stop(web, tmp_path):
    url, policy, routes, requests, _ = web
    routes["/"] = (
        200,
        "text/html",
        b'<html>Home<a href="/policy">Policy</a><a href="/text.pdf">PDF</a></html>',
    )
    routes["/text.pdf"] = (200, "application/pdf", pdf_bytes(True))
    out = tmp_path / "audit"
    limits = Limits(max_pages=10, max_documents=10)
    result = collect(
        url,
        out,
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=CrawlOptions(max_requests=2),
    )
    assert len(requests) == 2 and [r[0] for r in requests] == ["/robots.txt", "/"]
    assert result.evidence[0].available
    assert not result.documents and result.warnings
    assert (out / "collection.json").is_file()
    summary = json.loads((out / "crawl/summary.json").read_text())
    assert summary["stop_reason"] == "request_count_limit"
    assert "decision" not in result.model_dump()


def test_collect_partial_screenshot_failure_still_preserves_html(web, tmp_path, monkeypatch):
    from playwright.sync_api import Error

    def screenshot_error(*args, **kwargs):
        raise Error("Synthetic screenshot failure")

    monkeypatch.setattr("playwright.sync_api.Page.screenshot", screenshot_error)
    url, policy, *_ = web
    limits = Limits(max_pages=1, max_documents=0)
    out = tmp_path / "partial"
    result = collect(url, out, limits, fetcher=Fetcher(limits, policy))
    assert result.evidence[0].available and result.evidence[0].status == "partial"
    assert {a.kind for a in result.evidence[0].artifacts} == {"html", "text"}
    assert (out / "crawl/summary.json").is_file()


def test_browser_startup_error_is_journaled(web, tmp_path, monkeypatch):
    from playwright.sync_api import Error

    monkeypatch.setattr(
        "playwright.sync_api.BrowserType.launch",
        lambda *a, **k: (_ for _ in ()).throw(Error("Synthetic browser failure")),
    )
    url, policy, *_ = web
    out = tmp_path / "failure"
    with pytest.raises(Error):
        collect(url, out, fetcher=Fetcher(Limits(), policy))
    assert json.loads((out / "crawl/summary.json").read_text())["stop_reason"] == "collector_error"
    assert not (out / "collection.json").exists()


def test_limits_no_disable_flag_and_defaults():
    assert CrawlOptions().min_delay_seconds == 2
    for data in (
        {"min_delay_seconds": 0},
        {"max_requests": 0},
        {"max_duration_seconds": 0},
        {"user_agent": "OtherBot"},
        {"ignore_robots": True},
    ):
        with pytest.raises(ValidationError):
            CrawlOptions.model_validate(data)


def test_total_duration_clamps_inflight_http_timeout(web, tmp_path):
    class RealClock:
        now = staticmethod(time.monotonic)
        sleep = staticmethod(time.sleep)

    url, _, routes, requests, _ = web
    routes["/slow"] = (200, "text/html", 0.5)
    s = session(
        web,
        tmp_path,
        options=CrawlOptions(min_delay_seconds=0.1, max_duration_seconds=0.25),
        limits=Limits(timeout_seconds=10),
        clock=RealClock(),
    )
    began = time.monotonic()
    with pytest.raises(CollectionError, match="crawl_duration_limit"):
        s.get(url + "slow", origin(url))
    assert time.monotonic() - began < 0.45
    assert [r[0] for r in requests] == ["/robots.txt", "/slow"]
    assert s.finish()["stop_reason"] == "crawl_duration_limit"


def test_pacing_does_not_consume_http_io_timeout(web, tmp_path):
    url, *_ = web
    s = session(web, tmp_path, limits=Limits(timeout_seconds=0.1))
    assert s.get(url, origin(url)).status == 200
    assert s.clock.sleeps == [2]


def test_content_network_error_stops_additional_requests(web, tmp_path, monkeypatch):
    url, *_ = web
    s = session(web, tmp_path)
    s.get(url, origin(url))

    def failure(*args, **kwargs):
        raise OSError("Synthetic connection reset")

    monkeypatch.setattr("lexradar.collector.network.PinnedHTTP.connect", failure)
    with pytest.raises(CollectionError, match="Fetch failed"):
        s.get(url + "other", origin(url))
    attempts = s.requests
    with pytest.raises(CollectionError, match="network_error"):
        s.get(url + "next", origin(url))
    assert s.requests == attempts and s.finish()["stop_reason"] == "network_error"


def test_crawl_sidecars_do_not_change_v03_packet_hash(tmp_path):
    import shutil
    from pathlib import Path

    from lexradar.auditors.evidence import load_packet

    root = tmp_path / "dossier"
    shutil.copytree(Path("examples/analysis-dossier"), root)
    before = load_packet(root, 200000)
    (root / "crawl").mkdir()
    (root / "crawl/summary.json").write_text('{"stop_reason":"queue_exhausted"}')
    after = load_packet(root, 200000)
    assert before.sha256 == after.sha256
    assert before.manifest_sha256 == after.manifest_sha256


def test_options_snapshot_cannot_change_mid_crawl(web, tmp_path):
    url, *_ = web
    options = CrawlOptions(max_requests=2)
    s = session(web, tmp_path, options=options)
    s.get(url, origin(url))
    options.max_requests = 100
    with pytest.raises(CollectionError, match="request_count_limit"):
        s.get(url + "next", origin(url))
    assert s.requests == 2


def test_total_byte_budget_includes_robots_and_stops_further_requests(web, tmp_path):
    url, _, routes, requests, _ = web
    routes["/"] = (200, "text/html", b"x" * 1024)
    s = session(web, tmp_path, limits=Limits(max_total_bytes=1024))
    with pytest.raises(CollectionError, match="Total download"):
        s.get(url, origin(url))
    count = s.requests
    with pytest.raises(CollectionError, match="download_byte_limit"):
        s.get(url + "next", origin(url))
    assert s.requests == count and s.finish()["stop_reason"] == "download_byte_limit"
    assert [r[0] for r in requests] == ["/robots.txt", "/"]
