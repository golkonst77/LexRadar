"""Synthetic offline tests for the isolated manual import, without real site data."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_primary import make_dossier

from lexradar.auditors.evidence import DossierError, load_packet
from lexradar.auditors.preflight import authorize, screen
from lexradar.collector.artifacts import verify_integrity
from lexradar.single_auditor import messages_for, profile

spec = importlib.util.spec_from_file_location(
    "prepare_054", Path(__file__).resolve().parents[1] / "scripts/prepare_doktstom_054.py"
)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network/API forbidden in offline import tests")

    monkeypatch.setattr("socket.getaddrinfo", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("socket.socket.connect_ex", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)


def test_repairs_exact_tokens_and_preserves_policy_and_excludes_script():
    body = (
        '<html><body><p>Начало</p></span>\n<div class"broken">'
        "<p>Политика обработки данных</p><script>SECRET</script></div></body></html>"
    ).encode()
    derived, text, reading, edits, inspection = importer.normalize(body, "https://clinic.example/")
    assert derived == body.replace(b"</span>", b"").replace(b'<div class"broken">', b"<div>")
    assert len(edits) == 2 and inspection["status"] == "partial"
    assert "Политика обработки данных" in text and "SECRET" not in text
    assert reading.extraction_state == "complete"
    for edit in edits:
        assert body.decode()[edit["offset"] : edit["end_exclusive"]] == edit["original"]


@pytest.mark.parametrize("body", [b"<div><span></div>", b"<script>SECRET", b"\xff"])
def test_ambiguous_or_lossy_sources_fail_closed(body):
    with pytest.raises((ValueError, UnicodeError)):
        importer.normalize(body, "https://clinic.example/")


def test_import_roundtrip_screening_and_source_immutability(tmp_path):
    source, _ = make_dossier(
        tmp_path,
        '<p>Начало</p></span><div id="policy"><p>Политика обработки данных</p>'
        "<p>Контакт: private@example.org</p></div>",
    )
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "improved"
    metrics = importer.prepare(source, output, datetime(2026, 10, 16, tzinfo=UTC))
    assert before == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert before == {
        p.relative_to(output / "original-dossier"): p.read_bytes()
        for p in (output / "original-dossier").rglob("*")
        if p.is_file()
    }
    root = output / "dossier"
    packet = load_packet(root, profile().max_input_bytes)
    assert packet.payload == (output / "packet.json").read_text()
    assert packet.sha256 == metrics["packet_sha256"]
    assert not verify_integrity(packet.data, root)
    page = packet.data.pages[0]
    assert page.origin.kind == "manual_html_import"
    assert page.origin.http_response_at is None and page.http_status is None
    assert page.reading.provenance == "reproduced"
    assert page.origin.original_capture_at != page.captured_at
    assert "Политика обработки данных" in page.text
    assert messages_for("A", packet)[0][1]["content"] == packet.payload
    assert any(s["category"] == "email" for s in screen(packet))
    with pytest.raises(PermissionError, match="Potential sensitive"):
        authorize(packet, None)
    with pytest.raises(FileExistsError):
        importer.prepare(source, output)
    html = root / "artifacts/page-0001.html"
    html.write_bytes(html.read_bytes() + b"changed")
    with pytest.raises(DossierError, match="integrity"):
        load_packet(root, profile().max_input_bytes)


def test_source_directory_cannot_be_output(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        importer.prepare(tmp_path, tmp_path / "child")
