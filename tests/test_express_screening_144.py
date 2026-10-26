"""Express screening uses only local HTTP fixtures and never calls an auditor."""

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from lexradar.collector import CrawlOptions, Limits, collect
from lexradar.collector.network import AddressPolicy, CollectionError, Fetcher
from lexradar.mvp_batch import run


class FixturePolicy(AddressPolicy):
    def __init__(self, port):
        self.port = port

    def resolve(self, host, port, *, timeout=None):
        if host != "127.0.0.1" or port != self.port:
            raise CollectionError("Not fixture server")
        return host


@pytest.fixture
def site():
    routes = {"/robots.txt": (200, "text/plain", b"User-agent: LexRadar\nDisallow:\n", {})}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            status, content_type, body, headers = routes.get(
                self.path,
                (200, "text/html; charset=utf-8", b"<html><body>Fixture page</body></html>", {}),
            )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/"
    yield url, FixturePolicy(httpd.server_port), routes, requests
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def options():
    return CrawlOptions(min_delay_seconds=0.1, max_requests=50, max_duration_seconds=30)


def test_screening_prioritizes_relevant_pages_and_never_exceeds_seven(site, tmp_path):
    url, policy, routes, _ = site
    routes["/"] = (
        200,
        "text/html",
        b"<a href='/misc-a'>Other</a><a href='/privacy'>Policy</a>"
        b"<a href='/cookie'>Cookie preferences</a><a href='/contacts'>Contacts</a>"
        b"<a href='/booking'>Online booking</a><a href='/consent'>Consent</a>"
        b"<a href='/booking-two'>Book appointment</a><a href='/contacts-two'>Contact details</a>"
        b"<a href='/privacy-two'>Privacy policy</a><a href='/cookie-two'>Cookie choices</a>"
        b"<a href='/misc-b'>More</a><a href='/misc-c'>More 2</a>",
        {},
    )
    limits = Limits(max_pages=40, max_documents=0)
    result = collect(
        url,
        tmp_path / "cap",
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    requested_paths = [urlsplit(str(page.requested_url)).path for page in result.pages]
    assert len(result.pages) == 7
    assert result.limits.max_pages == 7
    assert [p.rsplit("/", 1)[-1] for p in requested_paths[1:7]] == [
        "booking",
        "booking-two",
        "contacts",
        "contacts-two",
        "privacy",
        "privacy-two",
    ]
    summary = json.loads((tmp_path / "cap/crawl/summary.json").read_text())
    assert summary["pages_attempted"] == 7
    assert summary["pages_html_captured"] == 7
    assert summary["page_limit"] == 7 and summary["target_pages"] == 5

    routes["/"] = (
        200,
        "text/html",
        b"".join(f"<a href='/plain-{i}'>Other {i}</a>".encode() for i in range(8)),
        {},
    )
    target = collect(
        url,
        tmp_path / "target",
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    target_summary = json.loads((tmp_path / "target/crawl/summary.json").read_text())
    assert len(target.pages) == 5
    assert target_summary["stop_reason"] == "screening_target_reached"


def test_screening_records_static_signals_and_stops_early(site, tmp_path):
    url, policy, routes, _ = site
    routes["/"] = (
        200,
        "text/html",
        b"<script src='https://mc.yandex.ru/metrika/tag.js'></script>"
        b"<form><input type='tel' name='phone' aria-label='Phone'></form>"
        b"<a href='/privacy'>Privacy policy</a>"
        b"<a href='/hidden' style='display:none'>Privacy policy hidden</a>"
        b"<a href='/booking'>Booking</a>"
        b"<a href='/contacts'>Contacts</a><a href='/cookie'>Cookie settings</a>"
        b"<a href='/faq1'>FAQ</a><a href='/faq2'>FAQ</a><a href='/faq3'>FAQ</a>"
        b"<button id='cookie-settings'>" + "Настроить cookies".encode() + b"</button>",
        {},
    )
    limits = Limits(max_pages=7, max_documents=0)
    output = tmp_path / "early"
    result = collect(
        url,
        output,
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    home = result.pages[0]
    assert len(result.pages) == 1
    assert home.forms[0].fields[0].type == "tel"
    policy_link = next(link for link in home.relevant_links if link.kind == "privacy_policy")
    assert policy_link.visible_in_static_html
    hidden_policy = next(link for link in home.relevant_links if not link.visible_in_static_html)
    assert hidden_policy.kind == "privacy_policy"
    assert home.static_analytics_hints == ["yandex_metrika"]
    assert home.cookie_text_observed
    assert any("Настроить cookies" in value for value in home.consent_management_controls)
    summary = json.loads((output / "crawl/summary.json").read_text())
    assert summary["stop_reason"] == "screening_signal_found"
    assert summary["pages_attempted"] == 1 and summary["static_signal_found"]
    assert any("not a legal finding" in warning for warning in result.warnings)


def test_screening_keeps_robots_denials_fail_closed(site, tmp_path):
    url, policy, routes, requests = site
    routes["/robots.txt"] = (
        200,
        "text/plain",
        b"User-agent: LexRadar\nDisallow: /\n",
        {},
    )
    limits = Limits(max_pages=7, max_documents=0)
    result = collect(
        url,
        tmp_path / "robots",
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    assert requests == ["/robots.txt"]
    assert not any(e.artifacts for e in result.evidence)
    assert result.evidence[0].collection_errors == ["robots_disallow"]


def test_screening_preserves_missing_location_failure(site, tmp_path):
    url, policy, routes, requests = site
    routes["/"] = (
        200,
        "text/html",
        b"<a href='/redirect'>Contacts</a>",
        {},
    )
    routes["/redirect"] = (302, "text/html", b"", {})
    limits = Limits(max_pages=7, max_documents=0)
    result = collect(
        url,
        tmp_path / "redirect",
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    assert requests == ["/robots.txt", "/", "/redirect"]
    assert any("missing Location" in error for e in result.evidence for error in e.collection_errors)
    assert json.loads((tmp_path / "redirect/crawl/summary.json").read_text())["pages_attempted"] == 2


def test_screening_checks_cross_origin_redirect_policy(site, tmp_path):
    url, policy, routes, requests = site
    routes["/"] = (200, "text/html", b"<a href='/redirect'>Contacts</a>", {})
    routes["/redirect"] = (
        302,
        "text/html",
        b"",
        {"Location": "https://outside.invalid/landing"},
    )
    limits = Limits(max_pages=7, max_documents=0)
    result = collect(
        url,
        tmp_path / "cross-origin",
        limits,
        fetcher=Fetcher(limits, policy),
        crawl_options=options(),
        static_only=True,
        screening_mode=True,
    )
    assert requests == ["/robots.txt", "/", "/redirect"]
    assert any(
        "robots_unavailable" in error
        for evidence in result.evidence
        for error in evidence.collection_errors
    )


def test_prepare_batch_reports_page_counts_and_static_cookie_inventory(site, tmp_path, monkeypatch):
    from lexradar import collector
    from lexradar.auditors.provider import OpenRouterTransport

    url, policy, routes, _ = site
    routes["/"] = (
        200,
        "text/html",
        b"<script src='https://mc.yandex.ru/metrika/tag.js'></script>"
        b"<form><input type='email' name='email' aria-label='Email'></form>"
        b"<a href='/privacy'>Privacy policy</a><a href='/booking'>Booking</a>"
        b"<a href='/contacts'>Contacts</a><a href='/cookie'>Cookie choices</a>"
        b"<a href='/faq'>FAQ</a><button>Reject cookies</button>",
        {},
    )
    real_collect = collector.collect

    def fixture_collect(target, output, limits, **kwargs):
        kwargs["crawl_options"] = options()
        return real_collect(
            target,
            output,
            limits,
            fetcher=Fetcher(limits, policy),
            **kwargs,
        )

    monkeypatch.setattr(collector, "collect", fixture_collect)

    def forbidden_ai(*args, **kwargs):
        raise AssertionError("express prepare must not call an AI provider")

    monkeypatch.setattr(OpenRouterTransport, "post", forbidden_ai)
    source = tmp_path / "input.csv"
    with source.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["url", "company_name", "email"])
        writer.writeheader()
        writer.writerow({"url": url, "company_name": "Fixture", "email": ""})
    output = tmp_path / "batch"
    state = run(
        source,
        output,
        mode="prepare",
        max_sites=1,
        max_ai_calls=0,
        allow_site_collection=True,
    )
    row = state["rows"][0]
    assert state["plan"]["collection_profile"] == "express-screening-144"
    assert row["status"] == "SUCCESS" and row["qualification"] == "HOLD", {
        key: row.get(key)
        for key in ("status", "qualification", "reason", "html_pages_attempted", "html_pages_captured")
    }
    assert row["html_pages_attempted"] == 1 and row["html_pages_captured"] == 1
    assert not row.get("proposal")
    collection = json.loads(
        (output / row["directory"] / "dossier/collection.json").read_text()
    )
    assert collection["pages"][0]["static_analytics_hints"] == ["yandex_metrika"]
    assert collection["pages"][0]["cookie_text_observed"]
    assert collection["pages"][0]["consent_management_controls"]
    csv_row = next(csv.DictReader((output / "results.csv").open(encoding="utf-8-sig")))
    assert csv_row["html_pages_attempted"] == "1"
    assert "HTML pages attempted/captured: 1/1" in (output / "report.md").read_text()
