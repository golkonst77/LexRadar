import hashlib
import io
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from lexradar.collector import Limits, collect
from lexradar.collector.artifacts import verify_integrity
from lexradar.collector.models import CollectionResult
from lexradar.collector.network import (
    AddressPolicy,
    CollectionError,
    Fetcher,
    canonical_url,
    origin,
)


def pdf_bytes(text=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 20 Td (Synthetic public document) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FixturePolicy(AddressPolicy):
    """Test-only exact fixture host/port; unavailable through production CLI."""

    def __init__(self, port):
        self.port = port

    def resolve(self, host, port, *, timeout=None):
        if host != "127.0.0.1" or port != self.port:
            raise CollectionError("Not fixture server")
        return host


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/edge/"):
                case = self.path.rsplit("/", 1)[1]
                if case in {"301", "302", "303", "307", "308", "no-location"}:
                    self.send_response(302 if case == "no-location" else int(case))
                    if case != "no-location":
                        self.send_header("Location", "../policy#fragment")
                    self.end_headers()
                    return
                body = b"x" * (
                    1024
                    if case == "exact"
                    else 1025
                    if case in {"oversize", "unknown-large", "chunked-large"}
                    else 64
                )
                if case == "not-modified":
                    body = b""
                self.send_response(304 if case == "not-modified" else 200)
                self.send_header("Content-Type", "text/html")
                if case in {"chunked", "chunked-large", "ambiguous", "bad-chunk"}:
                    self.send_header("Transfer-Encoding", "chunked")
                if case not in {
                    "unknown",
                    "unknown-large",
                    "chunked",
                    "chunked-large",
                    "bad-chunk",
                }:
                    length = (
                        "-1"
                        if case == "negative"
                        else "oops"
                        if case == "invalid"
                        else str(len(body) + 1 if case == "truncated" else len(body))
                    )
                    self.send_header("Content-Length", length)
                    if case == "duplicate":
                        self.send_header("Content-Length", length)
                if case == "encoded":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if case in {"chunked", "chunked-large"}:
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                elif case == "bad-chunk":
                    self.wfile.write(b"invalid\r\n")
                else:
                    self.wfile.write(body)
                return
            if self.path == "/slow":
                time.sleep(0.3)
            if self.path in {"/redirect", "/blocked", "/loop"}:
                self.send_response(302)
                destination = {
                    "/redirect": "/policy",
                    "/blocked": "http://169.254.169.254/latest",
                    "/loop": "/loop",
                }[self.path]
                self.send_header("Location", destination)
                self.end_headers()
                return
            status = 404 if self.path == "/missing" else 200
            if self.path == "/":
                body = Path("tests/fixtures/collector/index.html").read_bytes()
            elif self.path.endswith(".pdf"):
                body = pdf_bytes(self.path == "/text.pdf")
            elif self.path == "/large":
                body = b"x" * 5000
            else:
                body = b"<html><title>Policy</title><body>Public synthetic text</body></html>"
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/pdf" if self.path.endswith(".pdf") else "text/html; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/"
    yield url, FixturePolicy(httpd.server_port), requests
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.fixture
def collected(server, tmp_path):
    url, policy, requests = server
    limits = Limits(max_pages=10)
    output = tmp_path / "audit"
    result = collect(url, output, limits, fetcher=Fetcher(limits, policy))
    return result, output, requests


def test_forms_entities_documents(collected):
    result, output, requests = collected
    page = result.pages[0]
    assert page.http_status == 200
    assert "Контакты" in page.title
    assert page.forms[0].purpose == "Запись на приём"
    fields = {f.name: f for f in page.forms[0].fields}
    assert fields["consent"].checked is False
    assert fields["name"].required
    assert page.forms[0].policy_links and page.forms[0].consent_links
    assert page.forms[0].screenshot
    assert len(result.entities) == 2
    assert [e.inn for e in result.entities] == ["0000000000", "1111111111"]
    assert all(not e.identity_verified for e in result.entities)
    assert any(u.endswith(".docx") for u in page.document_links)
    statuses = {str(d.requested_url).rsplit("/", 1)[1]: d for d in result.documents}
    assert "Synthetic public document" in statuses["text.pdf"].text
    assert statuses["scan.pdf"].extraction_status == "visual_review_required"
    assert statuses["scan.pdf"].text == ""
    assert all(e.available for e in result.evidence if e.id.startswith("document"))
    assert "/executed" not in requests
    assert not verify_integrity(result, output)
    assert CollectionResult.model_validate_json((output / "collection.json").read_text()) == result


def test_unavailable_redirects_and_duplicates(collected):
    result, _, requests = collected
    pages = {str(p.requested_url).rsplit("/", 1)[1]: p for p in result.pages}
    evidence = {e.id: e for e in result.evidence}
    assert pages["missing"].http_status == 404
    assert not evidence[pages["missing"].evidence_id].available
    assert "HTTP 404" in evidence[pages["missing"].evidence_id].unavailable_reason
    assert str(pages["redirect"].final_url).endswith("/policy")
    assert "Cross-origin" in evidence[pages["blocked"].evidence_id].unavailable_reason
    assert requests.count("/policy") <= 2  # Once direct, once redirect; fragment not crawled.
    assert len({str(p.requested_url) for p in result.pages}) == len(result.pages)


def test_integrity_and_missing_artifact(collected):
    result, output, _ = collected
    artifact = result.evidence[0].artifacts[0]
    path = output / artifact.path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
    path.write_bytes(b"tampered")
    assert any("integrity mismatch" in issue for issue in verify_integrity(result, output))
    path.unlink()
    assert any("missing" in issue for issue in verify_integrity(result, output))
    result.evidence[0].artifacts = []
    assert any("mandatory" in issue for issue in verify_integrity(result, output))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org",
        "http://user:pw@example.org",
        "http://example.org:bad",
        "http://example.org/\\evil",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(CollectionError):
        canonical_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "0.0.0.0",
        "224.0.0.1",
        "ff02::1",
    ],
)
def test_ssrf_addresses(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (ip, 80))])
    with pytest.raises(CollectionError):
        AddressPolicy().resolve("clinic.example", 80)


def test_mixed_dns_answers(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("8.8.8.8", 80)), (2, 1, 6, "", ("10.0.0.1", 80))],
    )
    with pytest.raises(CollectionError):
        AddressPolicy().resolve("clinic.example", 80)


def test_timeouts_limits_redirect_loop(server):
    url, policy, _ = server
    limits = Limits(timeout_seconds=0.1, max_file_bytes=1024)
    fetcher = Fetcher(limits, policy)
    for path in ("slow", "large", "loop", "blocked"):
        with pytest.raises(CollectionError):
            fetcher.get(url + path, origin(url))


def test_page_limit_and_no_gateway(server, tmp_path):
    url, policy, _ = server
    limits = Limits(max_pages=1, max_documents=0)
    result = collect(url, tmp_path / "limited", limits, fetcher=Fetcher(limits, policy))
    assert len(result.pages) == 1
    assert result.warnings
    assert "decision" not in result.model_dump()
    assert result.requires_independent_review


def test_missing_mandatory_screenshot(server, tmp_path, monkeypatch):
    from lexradar.collector.artifacts import ArtifactStore

    original = ArtifactStore.save

    def fail_screenshot(self, name, content, kind):
        if kind == "screenshot":
            raise CollectionError("Synthetic screenshot failure")
        return original(self, name, content, kind)

    monkeypatch.setattr(ArtifactStore, "save", fail_screenshot)
    url, policy, _ = server
    limits = Limits(max_pages=1, max_documents=0)
    result = collect(url, tmp_path / "failure", limits, fetcher=Fetcher(limits, policy))
    evidence = result.evidence[0]
    assert evidence.available and evidence.status == "partial"
    assert evidence.unavailable_reason is None
    assert any("screenshot failure" in error for error in evidence.collection_errors)
    assert {a.kind for a in evidence.artifacts} >= {"html", "text"}
    assert result.pages[0].forms and result.pages[0].document_links
    assert not verify_integrity(result, tmp_path / "failure")
    saved = CollectionResult.model_validate_json((tmp_path / "failure/collection.json").read_text())
    assert saved.evidence[0].status == "partial"
    assert "Partially" in saved.evidence[0].observed_fact


def test_timeout_page_record(server, tmp_path):
    url, policy, _ = server
    limits = Limits(timeout_seconds=0.1, max_pages=1)
    result = collect(url + "slow", tmp_path / "timeout", limits, fetcher=Fetcher(limits, policy))
    assert not result.evidence[0].available
    assert result.evidence[0].unavailable_reason
    assert result.pages[0].text is None


def test_total_download_budget(server):
    url, policy, _ = server
    limits = Limits(max_total_bytes=1024)
    fetcher = Fetcher(limits, policy)
    with pytest.raises(CollectionError, match="Total download"):
        fetcher.get(url, origin(url))


def test_dns_timeout(monkeypatch):
    def delayed(*args, **kwargs):
        time.sleep(0.1)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", delayed)
    with pytest.raises(CollectionError, match="DNS resolution timeout"):
        AddressPolicy(timeout=0.01).resolve("clinic.example", 443)


def test_pdf_extraction_failure(tmp_path):
    from lexradar.collector.pdf import extract_pdf

    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF- malformed synthetic fixture")
    text, status, reason = extract_pdf(path, 10, 3)
    assert text is None and status == "failed" and reason


def test_proxy_is_not_bypassed(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    url = "https://clinic.example/"
    with pytest.raises(CollectionError, match="proxy configured"):
        Fetcher(Limits()).get(url, origin(url))


def test_collection_example():
    result = CollectionResult.model_validate_json(Path("examples/collection.json").read_text())
    assert result.requires_independent_review
    assert not result.evidence[0].available


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1",
        "http://@clinic.example",
        "http://clinic.example:0",
        "http://clinic.example:65536",
        "http://clinic.example/\npath",
        "http://clinic.example/\x7f",
        "http://[fe80::1%25eth0]/",
        "http://%31%32%37.0.0.1",
        "http://clinic.example/%zz",
        "http://./",
    ],
)
def test_url_edge_rejections(url):
    with pytest.raises(CollectionError):
        canonical_url(url)


def test_url_canonicalization():
    assert canonical_url("HTTPS://CLINIC.EXAMPLE.:443#section") == "https://clinic.example/"
    assert canonical_url("https://клиника.example/политика?q=да") == (
        "https://xn--80apagcdp.example/%D0%BF%D0%BE%D0%BB%D0%B8%D1%82%D0%B8%D0%BA%D0%B0"
        "?q=%D0%B4%D0%B0"
    )
    assert origin("https://clinic.example:444") != origin("https://clinic.example")


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_relative_redirects(server, code):
    url, policy, _ = server
    response = Fetcher(Limits(), policy).get(url + f"edge/{code}", origin(url))
    assert response.url == url + "policy" and response.status == 200


@pytest.mark.parametrize(
    "case",
    [
        "negative",
        "invalid",
        "duplicate",
        "ambiguous",
        "truncated",
        "encoded",
        "bad-chunk",
        "no-location",
        "oversize",
        "unknown-large",
        "chunked-large",
    ],
)
def test_response_framing_and_limits(server, case):
    url, policy, _ = server
    with pytest.raises(CollectionError):
        Fetcher(Limits(max_file_bytes=1024), policy).get(url + "edge/" + case, origin(url))


@pytest.mark.parametrize("case", ["unknown", "chunked", "exact"])
def test_bounded_response_modes(server, case):
    url, policy, _ = server
    fetcher = Fetcher(Limits(max_file_bytes=1024), policy)
    response = fetcher.get(url + "edge/" + case, origin(url))
    assert len(response.body) == (1024 if case == "exact" else 64)
    assert fetcher.received == len(response.body)


def test_shared_budget_and_zero_redirects(server):
    url, policy, requests = server
    fetcher = Fetcher(Limits(max_total_bytes=1024), policy)
    fetcher.get(url + "edge/exact", origin(url))
    count = len(requests)
    with pytest.raises(CollectionError, match="Total download"):
        fetcher.get(url + "policy", origin(url))
    assert len(requests) == count
    with pytest.raises(CollectionError, match="Redirect limit"):
        Fetcher(Limits(max_redirects=0), policy).get(url + "redirect", origin(url))


def test_rebinding_on_redirect_is_blocked(monkeypatch):
    from unittest.mock import MagicMock

    from lexradar.collector import network

    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    answers = iter(["8.8.8.8", "127.0.0.1"])
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (next(answers), 80))]
    )
    connection = MagicMock()
    connection.getresponse.return_value.status = 302
    connection.getresponse.return_value.getheader.return_value = "/next"
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(network, "PinnedHTTP", factory)
    url = "http://clinic.example/"
    with pytest.raises(CollectionError, match="Non-public"):
        Fetcher(Limits()).get(url, origin(url))
    assert factory.call_count == 1
    assert factory.call_args.args[2] == "8.8.8.8"


def test_pinned_socket_uses_ip_not_hostname(monkeypatch):
    from unittest.mock import MagicMock

    from lexradar.collector.network import PinnedHTTP

    connect = MagicMock()
    monkeypatch.setattr(socket, "create_connection", connect)
    connection = PinnedHTTP("clinic.example", 80, "8.8.8.8", 1)
    connection.connect()
    connect.assert_called_once_with(("8.8.8.8", 80), 1)


@pytest.mark.parametrize("host", ["localhost", "a.localhost", "clinic.local", "clinic.internal"])
def test_local_hostnames_blocked_without_dns(monkeypatch, host):
    def unexpected(*args, **kwargs):
        pytest.fail("Local hostname must be blocked before DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    with pytest.raises(CollectionError, match="Local hostname"):
        AddressPolicy().resolve(host, 80)


def test_artifact_representations(collected):
    from lexradar.collector.models import SCREENSHOT_NOTICE

    result, _, _ = collected
    for evidence in result.evidence:
        if evidence.available:
            assert evidence.status == "complete"
        for artifact in evidence.artifacts:
            if artifact.kind in {"screenshot", "form_screenshot"}:
                assert artifact.representation == "isolated_screenshot"
                assert artifact.notice == SCREENSHOT_NOTICE
            elif artifact.kind == "html":
                assert artifact.representation == "original_html"
            elif artifact.kind == "text":
                assert artifact.representation == "extracted_text"


@pytest.mark.parametrize("kind", ["html", "text", "form_screenshot"])
def test_independent_artifact_failure(server, tmp_path, monkeypatch, kind):
    from lexradar.collector.artifacts import ArtifactStore

    save = ArtifactStore.save

    def failing(self, name, content, artifact_kind):
        if artifact_kind == kind:
            raise OSError("Synthetic disk failure")
        return save(self, name, content, artifact_kind)

    monkeypatch.setattr(ArtifactStore, "save", failing)
    url, policy, _ = server
    limits = Limits(max_pages=1, max_documents=0)
    root = tmp_path / kind
    result = collect(url, root, limits, fetcher=Fetcher(limits, policy))
    evidence = result.evidence[0]
    assert evidence.available and evidence.status == "partial"
    assert evidence.unavailable_reason is None and evidence.collection_errors
    assert "screenshot" in {a.kind for a in evidence.artifacts}
    assert result.pages[0].forms and result.pages[0].document_links
    assert not verify_integrity(result, root)


def test_browser_screenshot_exception_retains_dossier_and_crawl(server, tmp_path, monkeypatch):
    from playwright.sync_api import Error, Page

    def fail(*args, **kwargs):
        raise Error("Synthetic browser screenshot exception")

    monkeypatch.setattr(Page, "screenshot", fail)
    url, policy, _ = server
    limits = Limits(max_pages=10)
    root = tmp_path / "browser-error"
    result = collect(url, root, limits, fetcher=Fetcher(limits, policy))
    assert len(result.pages) > 1 and result.documents
    evidence = result.evidence[0]
    assert evidence.available and evidence.status == "partial"
    assert {a.kind for a in evidence.artifacts} >= {"html", "text", "form_screenshot"}
    html = next(a for a in evidence.artifacts if a.kind == "html")
    assert (root / html.path).read_bytes() == Path(
        "tests/fixtures/collector/index.html"
    ).read_bytes()
    assert not verify_integrity(result, root)


def test_browser_navigation_exception_keeps_original_html(server, tmp_path, monkeypatch):
    from playwright.sync_api import Error, Page

    def fail(*args, **kwargs):
        raise Error("Synthetic rendering failure")

    monkeypatch.setattr(Page, "goto", fail)
    url, policy, _ = server
    limits = Limits(max_pages=1)
    result = collect(url, tmp_path / "render-error", limits, fetcher=Fetcher(limits, policy))
    evidence = result.evidence[0]
    assert evidence.status == "partial" and evidence.available
    assert [a.representation for a in evidence.artifacts] == ["original_html"]
    assert result.pages[0].text is None
    assert "rendering failure" in evidence.collection_errors[0]


def test_screenshot_notice_required(collected):
    from pydantic import ValidationError

    from lexradar.collector.models import Artifact

    result, _, _ = collected
    screenshot = next(a for a in result.evidence[0].artifacts if a.kind == "screenshot")
    raw = screenshot.model_dump()
    raw["notice"] = None
    with pytest.raises(ValidationError):
        Artifact.model_validate(raw)


def test_inconsistent_availability_rejected(collected):
    from pydantic import ValidationError

    result, _, _ = collected
    raw = result.model_dump()
    raw["evidence"][0]["status"] = "unavailable"
    with pytest.raises(ValidationError):
        CollectionResult.model_validate(raw)


def test_non_success_status_not_marked_complete(server, tmp_path):
    url, policy, _ = server
    limits = Limits(max_pages=1)
    result = collect(
        url + "edge/not-modified", tmp_path / "304", limits, fetcher=Fetcher(limits, policy)
    )
    assert result.pages[0].http_status == 304
    assert result.evidence[0].status == "unavailable"


def test_dns_failure_and_empty_answers(monkeypatch):
    def failure(*args, **kwargs):
        raise socket.gaierror("Synthetic DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", failure)
    with pytest.raises(CollectionError, match="DNS resolution failed"):
        AddressPolicy().resolve("clinic.example", 80)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
    with pytest.raises(CollectionError, match="Non-public"):
        AddressPolicy().resolve("clinic.example", 80)


@pytest.mark.parametrize("case", ["unknown-large", "chunked-large"])
def test_streaming_total_budget(server, case):
    url, policy, _ = server
    fetcher = Fetcher(Limits(max_total_bytes=1024), policy)
    with pytest.raises(CollectionError, match="Total download"):
        fetcher.get(url + "edge/" + case, origin(url))
    assert fetcher.received <= 1025  # One extra byte detects exceeding the limit.


def test_tls_keeps_hostname_verification(monkeypatch):
    from unittest.mock import MagicMock

    from lexradar.collector import network

    transport_socket = MagicMock()
    monkeypatch.setattr(socket, "create_connection", MagicMock(return_value=transport_socket))
    context = MagicMock()
    monkeypatch.setattr(network.ssl, "create_default_context", MagicMock(return_value=context))
    connection = network.PinnedHTTPS("clinic.example", 443, "8.8.8.8", 1)
    connection.connect()
    context.wrap_socket.assert_called_once_with(transport_socket, server_hostname="clinic.example")
