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

    def resolve(self, host, port):
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
    assert not result.evidence[0].available
    assert "screenshot failure" in result.evidence[0].unavailable_reason
    assert result.evidence[0].artifacts  # Partial HTML remains reviewable.


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
