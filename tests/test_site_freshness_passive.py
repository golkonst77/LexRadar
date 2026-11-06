"""Freshness regression checks: synthetic responses only, no sockets or remote API."""

import json
from datetime import UTC, datetime

import pytest
from pypdf import PdfWriter

from lexradar.auditors.evidence import load_packet
from lexradar.collector.crawl import CrawlOptions
from lexradar.collector.freshness import policy_signals, site_freshness
from lexradar.collector.models import CollectionResult, Limits
from lexradar.collector.network import Response
from lexradar.collector.pdf import extract_inventory
from lexradar.collector.runner import collect


@pytest.mark.parametrize("footer,year", [
    ("© 2018–2024", 2024), ("© 2024", 2024), ("Copyright 2024", 2024),
    ("&copy; <span>2018</span>—<span>2024</span>", 2024),
    ("© 2018-2024", 2024), ("©\n2018–\n2024", 2024), ("Компания основана в 2018 году", None),
    ("Адрес: дом 2024", None), ("Сайт создан в 2024", None),
    ("© 2018–2024 © 2023", None), ("© 2024–2018", None),
    ("© 2024–", None), ("© 20240", None), ("© 2099", None),
    ("Разработка сайта © 2024 WebStudio", None),
    ("<script>© 2024</script>", None),
    ("<article>Copyright 2024</article>", None),
    ("<address>Copyright 2024</address>", None),
])
def test_footer_cases(footer, year):
    result = site_freshness(f"<main>© 2020</main><footer>{footer}</footer>".encode())
    assert result.footer_year == year
    assert result.policy_priority == ("high" if year and year < datetime.now(UTC).year else None)


def test_unknown_and_headers():
    assert site_freshness(b"<article>Copyright 2024</article>").footer_year is None
    assert site_freshness(b"<footer>Copyright 2024").footer_year is None
    site_freshness(b"<![broken]><footer>Copyright 2024</footer>")  # Version-dependent recovery.
    assert site_freshness(b'<div class="footer" id>Copyright 2024</div>').footer_year == 2024
    result = site_freshness(b"", "not a date", 'W/"2024"', "https://example.test/")
    assert result.http_last_modified == "not a date"  # Retain raw claim for diagnosis.
    assert [s.kind for s in result.signals] == ["http_etag"]
    assert result.signals[0].confidence == "opaque_validator"
    result = site_freshness(b"", "Wed, 01 Jan 2020 00:00:00 GMT")
    assert result.signals[0].value == "2020-01-01T00:00:00+00:00"


def test_policy_dates():
    text = "Редакция от 05.11.2024. Утверждена 31.02.2024. Основана в 2001."
    signals = policy_signals(text, "https://example.test/privacy", "page-0002")
    assert [(s.kind, s.value, s.evidence_id) for s in signals] == [
        ("policy_declared_date", "2024-11-05", "page-0002"),
    ]
    assert policy_signals("Дата неизвестна", "url", "id") == []
    assert policy_signals("Last updated: 2025-10-01", "url", "id")[0].value == "2025-10-01"


class ReplayFetcher:
    synthetic = True

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, url, origin, **kwargs):
        self.requests.append(url)
        return self.routes[url]


def replay(tmp_path, *, enabled=True, status=200, content_type="text/html", body=None):
    url = "https://example.test/"
    home = body if body is not None else (
        '<footer>© 2018–2024</footer><a href="/consent">Consent</a>'
        '<a href="/privacy">Privacy policy</a><a href="/misc">Other</a>'
    ).encode()
    fetcher = ReplayFetcher({
        url: Response(url + "landing", status, content_type, home,
                      "Wed, 01 Jan 2020 00:00:00 GMT", '"abc"'),
        url + "privacy": Response(url + "privacy", 200, "text/html",
                                   '<h1>Privacy policy</h1><p>Редакция от 01.02.2024</p>'.encode()),
        url + "consent": Response(url + "consent", 200, "text/html", b"Consent",
                                   etag='"consent-validator"'),
        url + "misc": Response(url + "misc", 200, "text/html", b"Other"),
    })
    result = collect(url, tmp_path, Limits(max_pages=3, max_documents=0), fetcher=fetcher,
                     static_only=True, screening_mode=True,
                     crawl_options=CrawlOptions(ignore_robots=True,
                                                site_freshness_enabled=enabled))
    return result, fetcher


def test_passive_collection_priority_packet_and_rollback(tmp_path):
    result, fetcher = replay(tmp_path / "on")
    assert len(fetcher.requests) == 3
    assert any(s.kind == "http_etag" and s.value == '"consent-validator"'
               and s.evidence_id is not None for s in result.site_freshness.signals)
    assert fetcher.requests[1].endswith("/privacy")
    assert result.site_freshness.signal_source.endswith("/landing")
    assert result.site_freshness.processing_seconds >= 0
    assert any(s.kind == "policy_declared_date" for s in result.site_freshness.signals)
    packet = load_packet(tmp_path / "on", 200000)
    assert str(packet.data.pages[0].requested_url).endswith("/privacy")
    payload = json.loads(packet.payload)
    assert payload["untrusted_collector_data"]["pages"][0]["requested_url"].endswith("/privacy")
    assert "never proof of a violation" in " ".join(payload["analysis_limitations"])
    baseline, old_fetcher = replay(tmp_path / "off", enabled=False)
    assert baseline.site_freshness is None
    assert len(old_fetcher.requests) == len(fetcher.requests)
    # Pre-freshness dossier and the first implementation's minimal format both remain readable.
    raw = json.loads((tmp_path / "off" / "collection.json").read_text())
    del raw["site_freshness"]
    assert CollectionResult.model_validate(raw).site_freshness is None
    raw["site_freshness"] = {"footer_year": 2024, "http_etag": '"old"'}
    assert CollectionResult.model_validate(raw).site_freshness.footer_year == 2024
    (tmp_path / "off" / "collection.json").write_text(json.dumps(raw))
    assert load_packet(tmp_path / "off", 200000).data.site_freshness.http_etag == '"old"'
    del raw["site_freshness"]
    (tmp_path / "off" / "collection.json").write_text(json.dumps(raw))
    assert "site_freshness" not in json.loads(load_packet(tmp_path / "off", 200000).payload)[
        "untrusted_collector_data"
    ]


@pytest.mark.parametrize("status,ctype,body", [
    (403, "text/html", b"<footer>Copyright 2024</footer>"),
    (200, "application/pdf", b"<footer>Copyright 2024</footer>"),
    (200, "text/html", b""),
])
def test_non_html_failed_empty(tmp_path, status, ctype, body):
    result, fetcher = replay(tmp_path / "run", status=status, content_type=ctype, body=body)
    assert result.site_freshness.footer_year is None
    assert len(fetcher.requests) == 1


def test_pdf_metadata_uses_existing_worker(tmp_path):
    path = tmp_path / "policy.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/CreationDate": "D:20240102030405Z", "/ModDate": "invalid"})
    writer.write(path)
    result = extract_inventory(path, 2, 10, metadata_dates=True)
    assert result.metadata_dates == {"pdf_creation_date": "2024-01-02T03:04:05+00:00"}
    assert extract_inventory(path, 2, 10).metadata_dates == {}


def test_cli_disable_flag(monkeypatch, tmp_path):
    from lexradar.cli import main

    class Collected(Exception):
        pass

    def capture(*args, **kwargs):
        assert kwargs["crawl_options"].site_freshness_enabled is False
        raise Collected

    monkeypatch.setattr("lexradar.collector.collect", capture)
    monkeypatch.setattr("sys.argv", ["lexradar", "collect", "https://example.test/",
                                     "--output", str(tmp_path), "--no-site-freshness"])
    with pytest.raises(Collected):
        main()


def test_html_parser_failure_is_unknown(monkeypatch):
    def fail(*args):
        raise AssertionError("malformed marked section")

    monkeypatch.setattr("lexradar.collector.freshness._FooterParser.feed", fail)
    assert site_freshness(b"malformed").footer_year is None


def test_policy_pdf_keeps_download_budget_and_metadata(tmp_path):
    import io

    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/ModDate": "D:20240203040506Z"})
    writer.write(output)
    url = "https://example.test/"
    fetcher = ReplayFetcher({
        url: Response(url, 200, "text/html", (
            '<footer>© 2024</footer><a href="/price.pdf">Prices</a>'
            '<a href="/privacy.pdf">Privacy policy</a>'
        ).encode()),
        url + "privacy.pdf": Response(url + "privacy.pdf", 200, "application/pdf", output.getvalue()),
    })
    result = collect(url, tmp_path / "pdf", Limits(max_pages=1, max_documents=1),
                     fetcher=fetcher, static_only=True,
                     crawl_options=CrawlOptions(ignore_robots=True))
    assert fetcher.requests == [url, url + "privacy.pdf"]
    assert len(result.documents) == 1
    signal = next(s for s in result.site_freshness.signals if s.kind == "pdf_modification_date")
    assert signal.evidence_id == "document-0001"
    assert signal.source == url + "privacy.pdf"
    assert signal.confidence == "document_declared"


def test_policy_date_claims_are_bounded():
    text = " ".join(f"Revision 01.01.{year}" for year in range(2000, 2050))
    assert len(policy_signals(text, "url", "id")) == 16
    assert len(policy_signals("Revision 01.01.2024 " * 100, "url", "id")) == 1


@pytest.mark.parametrize("encoding,status", [("gzip", 200), ("deflate", 200), ("gzip", 403)])
def test_real_fetcher_headers_redirect_compression_without_sockets(monkeypatch, encoding, status):
    import gzip
    import http.client
    import zlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from test_transport_061 import StalledPeer

    from lexradar.collector.network import Fetcher, origin

    html = '<footer>© 2024</footer>'.encode()
    body = gzip.compress(html) if encoding == "gzip" else zlib.compress(html)
    if status == 403:
        body = b"malformed encoded error body"

    def response(status_line, headers, content):
        peer = StalledPeer(
            b"HTTP/1.1 " + status_line + b"\r\n" + headers + b"\r\n\r\n" + content
        )
        result = http.client.HTTPResponse(peer)
        result.begin()
        return result

    redirect = response(b"302 Found", b"Location: /final\r\nContent-Length: 0", b"")
    final = response(str(status).encode() + b" Result", (
        "Content-Type: text/html\r\nContent-Encoding: " + encoding
        + "\r\nContent-Length: " + str(len(body))
        + '\r\nLast-Modified: Wed, 01 Jan 2020 00:00:00 GMT\r\nETag: "final"'
    ).encode(), body)
    connection = MagicMock()
    connection.getresponse.side_effect = [redirect, final]
    monkeypatch.setattr("lexradar.collector.network.PinnedHTTPS", lambda *args: connection)
    fetcher = Fetcher(Limits(), SimpleNamespace(resolve=lambda *args, **kwargs: "192.0.2.1"))
    result = fetcher.get("https://example.test/", origin("https://example.test/"))
    assert result.url == "https://example.test/final"
    assert result.etag == '"final"'
    assert result.last_modified == "Wed, 01 Jan 2020 00:00:00 GMT"
    assert result.body == (html if status == 200 else b"")
    assert [call.args[0] for call in connection.request.call_args_list] == ["GET", "GET"]
    assert site_freshness(result.body).footer_year == (2024 if status == 200 else None)
    redirect.close()
    final.close()
