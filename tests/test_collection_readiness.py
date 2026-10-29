"""Regression coverage for real-site reading gaps and offline OCR provenance."""

import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime

import pytest
from test_collector import pdf_bytes

from lexradar.batch import BatchOptions, rank
from lexradar.collector.models import Artifact, CollectedEvidence, CollectionResult, Limits
from lexradar.collector.ocr import run_ocr
from lexradar.collector.reading import extract_html
from lexradar.collector.runner import document_priority


def test_svg_does_not_withhold_subscription_suffix():
    text, reading = extract_html(
        b'<html><body>Before<svg><path d="M0 0"/><rect width="2"/></svg>'
        b"<form>Email subscription</form><script>SECRET</script></body></html>",
        1000,
    )
    assert "Email subscription" in text and "SECRET" not in text
    assert reading.extraction_state == "complete"


def test_self_closing_html_script_stays_ambiguous():
    text, reading = extract_html(b"<body>Before<script/>SECRET</script>After</body>", 1000)
    assert "SECRET" not in text and reading.extraction_state == "partial"


def test_legal_document_preference_is_stable_and_decodes_urls():
    assert document_priority("/upload/%D0%9F%D0%94%D0%BD.pdf") == 0
    assert document_priority("Список сторонних операторов /upload/random.pdf") == 1
    assert document_priority("Согласие /upload/random.pdf") == 1
    assert document_priority("Свидетельство ЕГРЮЛ /upload/random.pdf") == 2


def test_unread_document_cannot_be_low_priority_or_clean():
    row = rank(
        {},
        [],
        {
            "documents": [
                {
                    "evidence_id": "document-0001",
                    "requested_url": "https://fixture.example/policy.pdf",
                    "extraction_status": "visual_review_required",
                    "reading": {},
                }
            ]
        },
    )
    assert row["group"] == "waiting" and len(row["reading_gaps"]) == 1
    assert not row["paid_work_needed"] and row["legal_status"] == "not_confirmed"


def test_file_bounds_in_batch_options():
    assert BatchOptions(max_file_bytes=20_000_000).max_file_bytes == 20_000_000
    with pytest.raises(ValueError):
        BatchOptions(max_file_bytes=20_000_001)


@pytest.fixture
def ocr_dossier(tmp_path):
    root = tmp_path / "dossier"
    root.mkdir()
    raw = pdf_bytes(True)
    (root / "original.pdf").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    original = Artifact(
        path="original.pdf", sha256=digest, size=len(raw), kind="pdf", representation="original_pdf"
    )
    result = CollectionResult(
        target_url="https://fixture.example/",
        started_at=datetime.now(UTC),
        limits=Limits(),
        evidence=[
            CollectedEvidence(
                id="document-0001",
                source="https://fixture.example/policy.pdf",
                captured_at=datetime.now(UTC),
                observed_fact="Synthetic original",
                available=True,
                status="complete",
                artifacts=[original],
            )
        ],
    )
    (root / "collection.json").write_text(result.model_dump_json())
    return root


def test_ocr_rejects_changed_original_before_tools(ocr_dossier, tmp_path):
    (ocr_dossier / "original.pdf").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="mismatch"):
        run_ocr(ocr_dossier, "document-0001", tmp_path / "ocr")


def test_ocr_rejects_output_inside_source(ocr_dossier):
    with pytest.raises(ValueError, match="outside"):
        run_ocr(ocr_dossier, "document-0001", ocr_dossier / "ocr")


@pytest.mark.skipif(sys.platform != "linux", reason="OCR resource controls require Linux")
def test_real_offline_ocr_retains_page_image_text_and_source_hash(ocr_dossier, tmp_path):
    if not all(shutil.which(x) for x in ("tesseract", "pdftoppm", "pdfinfo")):
        pytest.skip("System OCR dependencies not installed")
    before = (ocr_dossier / "collection.json").read_bytes()
    output = tmp_path / "ocr"
    report = run_ocr(ocr_dossier, "document-0001", output, language="eng", timeout=60)
    assert report["ocr_complete"] and len(report["pages"]) == report["page_count"] == 1
    page = report["pages"][0]
    assert page["status"] == "ocr_text" and (output / page["text"]).read_text().strip()
    assert hashlib.sha256((output / page["image"]).read_bytes()).hexdigest() == page["image_sha256"]
    assert (ocr_dossier / "collection.json").read_bytes() == before
    assert not report["visual_examined"] and not report["client_release_allowed"]
    assert json.loads((output / "ocr.json").read_text())["source_sha256"] == report["source_sha256"]
