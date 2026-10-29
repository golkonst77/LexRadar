"""Complete local URL crawl and commercial draft; no sockets or paid transport."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lexradar.commercial_preview import safe_link
from lexradar.mvp import DEMO_URL, run

FIXTURE = Path(__file__).resolve().parents[1] / "examples/mvp-site"


def test_complete_offline_mvp(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network or paid transport attempted")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", forbidden)
    result = run(
        DEMO_URL,
        tmp_path / "run",
        fixture=FIXTURE,
        static_only=True,
        as_of=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert result["status"] == "success"
    assert result["pages"] == 2 and result["documents"] == 1
    root = tmp_path / "run"
    assert not list(root.rglob("approval.json"))
    analysis = json.loads((root / "analysis/compact-analysis.json").read_text())
    assert analysis["status"] == "success" and analysis["simulated"]
    assert not analysis["request_sent"] and analysis["budget_charge_usd"] == "0"
    assert analysis["production_outcome"] == "HOLD"
    html = (root / "proposal.html").read_text()
    assert "ИП Исполнитель" in html and "checkbox: 0" in html
    assert "policy.html" in html and "terms.pdf" in html
    assert "СИНТЕТИЧЕСКОЕ ДЕМО" in html and "Потенциальные риски" in html
    assert "за подготовку" in html and "вебмастер" in html
    hashes = json.loads((root / "sha256.json").read_text())
    assert "proposal.html" in hashes and "dossier/artifacts/document-0001.pdf" in hashes
    with pytest.raises(FileExistsError):
        run(DEMO_URL, root, fixture=FIXTURE, static_only=True)


def test_fixture_cannot_impersonate_public_site(tmp_path):
    with pytest.raises(ValueError):
        run("https://example.com/", tmp_path / "run", fixture=FIXTURE, static_only=True)
    assert not (tmp_path / "run").exists()


def test_proposal_links_escape_untrusted_evidence():
    assert "href" not in safe_link("javascript:alert(1)")
    assert "<script>" not in safe_link("https://example.com/", "<script>attack</script>")


def test_collection_failure_retains_artifacts(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Chromium unavailable")

    monkeypatch.setattr("lexradar.mvp.collect", unavailable)
    with pytest.raises(RuntimeError):
        run(DEMO_URL, tmp_path / "run", fixture=FIXTURE)
    report = json.loads((tmp_path / "run/run.json").read_text())
    assert report["status"] == "failed" and report["cost_usd"] == "0"
    assert not (tmp_path / "run/proposal.html").exists()


def test_replay_and_unicode_pdf_are_offline(tmp_path, monkeypatch):
    from pypdf import PdfReader

    original = tmp_path / "original"
    run(DEMO_URL, original, fixture=FIXTURE, static_only=True)

    def forbidden(*args, **kwargs):
        pytest.fail("Replay attempted collection or external transport")

    monkeypatch.setattr("lexradar.mvp.collect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", forbidden)
    result = run(DEMO_URL, tmp_path / "replay", dossier=original / "dossier", pdf=True)
    assert result["status"] == "success" and result["pdf_generated"]
    root = tmp_path / "replay"
    text = "\n".join(page.extract_text() for page in PdfReader(root / "audit.pdf").pages)
    assert "Юридические нарушения не установлены" in text
    assert "Повторный анализ сохранённого досье" in text
    assert "HOLD / not_confirmed" in text
    assert "ПОВТОРНЫЙ АНАЛИЗ" in (root / "proposal.html").read_text()
    assert not list(root.rglob("*approval*"))
    assert not list(root.rglob("*ledger*"))
    hashes = json.loads((root / "sha256.json").read_text())
    assert "audit.pdf" in hashes and "audit.html" in hashes
    # Corrupted evidence is rejected before creating a result or invoking collector.
    (original / "dossier/artifacts/page-0001.html").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        run(DEMO_URL, tmp_path / "rejected", dossier=original / "dossier")
    assert not (tmp_path / "rejected").exists()


def test_replay_rejects_wrong_site(tmp_path):
    original = tmp_path / "original"
    run(DEMO_URL, original, fixture=FIXTURE, static_only=True)
    with pytest.raises(ValueError, match="URL mismatch"):
        run("https://example.org/", tmp_path / "rejected", dossier=original / "dossier")
    assert not (tmp_path / "rejected").exists()
