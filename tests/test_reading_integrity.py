"""Actual synthetic PDF bytes and hostile manifest claims, with all external transports blocked."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_auditors import auditor_response

from lexradar.approval import send_client_message
from lexradar.auditors.evidence import DossierError, load_packet
from lexradar.auditors.validator import validate_result
from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import (
    CollectedEvidence,
    CollectionResult,
    DocumentObservation,
    Limits,
    PageObservation,
)
from lexradar.collector.pdf import extract_inventory, legacy_result
from lexradar.collector.reading import ReadingState, extract_html
from lexradar.collector.visual_review import VisualObservation, verify_sidecar, write_inventory
from lexradar.decision import decide_production
from lexradar.decision.service import sha256
from lexradar.quality.fixtures import pdf_bytes
from lexradar.verifier.materials import completeness, examined, inventory
from lexradar.verifier.models import IndependentResult, Material


@pytest.fixture(autouse=True)
def block_external(monkeypatch):
    from lexradar.auditors.provider import OpenRouterTransport
    from lexradar.collector.network import Fetcher

    def blocked(*args, **kwargs):
        raise AssertionError("Reading workflows must not use external transports")

    monkeypatch.setattr(Fetcher, "get", blocked)
    monkeypatch.setattr(OpenRouterTransport, "post", blocked)


def save_manifest(root, data):
    (root / "collection.json").write_text(data.model_dump_json(indent=2), encoding="utf-8")


def pdf_dossier(tmp_path, texts, *, pages=100, per_page=100_000, total=1_000_000):
    root = tmp_path / "dossier"
    store = ArtifactStore(root, Limits(max_pdf_pages=pages))
    artifact = store.save("synthetic.pdf", pdf_bytes(texts), "pdf")
    extracted = extract_inventory(root / artifact.path, pages, 10, per_page, total)
    text, status, reason = legacy_result(extracted)
    evidence = CollectedEvidence(
        id="document-0001",
        source="https://clinic.example/synthetic.pdf",
        captured_at=datetime.now(UTC),
        observed_fact="Synthetic PDF fixture, no legal conclusion",
        available=True,
        status="complete",
        artifacts=[artifact],
        artifact_path=artifact.path,
        sha256=artifact.sha256,
    )
    document = DocumentObservation(
        requested_url=evidence.source,
        evidence_id=evidence.id,
        text=text,
        extraction_status=status,
        extraction_reason=reason,
        reading=extracted.reading,
        page_texts=extracted.page_texts,
    )
    data = CollectionResult(
        target_url="https://clinic.example/",
        started_at=datetime.now(UTC),
        limits=store.limits,
        evidence=[evidence],
        documents=[document],
    )
    save_manifest(root, data)
    return root, data


def material_for(root):
    return inventory(load_packet(root, 2_000_000), root)[0]


def acknowledge(material, numbers):
    result = IndependentResult(
        materials=[
            dict(
                evidence_id=material.evidence_id,
                text_examined=bool(numbers),
                pages_examined=numbers,
                limitation="Synthetic text review only",
            )
        ]
    )
    return examined([material], result)[0]


def test_twelve_pages_only_eight_extracted(tmp_path):
    root, _ = pdf_dossier(tmp_path, ["Synthetic page"] * 12, pages=8)
    material = material_for(root)
    assert material.page_count == 12 and material.text_page_count == 8
    assert len(material.reading.pages) == 12
    assert [p.extraction_state for p in material.reading.pages[8:]] == ["unread"] * 4
    reviewed = acknowledge(material, list(range(1, 9)))
    assert reviewed.examination == "partial_text"
    summary = completeness([reviewed])
    assert summary.examined_pdf_text_pages == 8 and summary.known_pdf_pages == 12
    assert not summary.legal_research_completed


def test_all_page_numbers_do_not_hide_one_truncated_page(tmp_path):
    root, _ = pdf_dossier(tmp_path, ["Short text"] * 11 + ["Long text " * 10], per_page=20)
    material = material_for(root)
    assert material.text_page_count == 12
    reviewed = acknowledge(material, list(range(1, 13)))
    assert reviewed.examined_pages == 12 and reviewed.fully_examined_text_pages == 11
    assert reviewed.examination == "partial_text"
    assert material.reading.pages[-1].truncated
    assert completeness([reviewed]).fully_examined_pdf_text_pages == 11
    assert completeness([reviewed]).pdf_full_text_review_fraction == 11 / 12
    assert completeness([reviewed]).pdf_page_denominator_complete


@pytest.mark.parametrize("per_page,total", [(7, 1_000_000), (100_000, 17), (7, 17)])
def test_page_and_total_text_caps_are_explicit(tmp_path, per_page, total):
    root, _ = pdf_dossier(tmp_path, ["Synthetic document text"] * 3, per_page=per_page, total=total)
    material = material_for(root)
    assert material.reading.text_truncated
    assert material.reading.extraction_state == "partial"
    assert not material.reading.text_coverage_complete
    assert material.reading.supplied_chars <= total
    assert all(p.supplied_chars <= per_page for p in material.reading.pages)
    assert all(p.original_chars > p.supplied_chars for p in material.reading.pages)


def test_mixed_text_and_scan_not_whole_text_review(tmp_path):
    root, _ = pdf_dossier(tmp_path, ["Readable synthetic page", "", "Another page"])
    material = material_for(root)
    assert material.reading.extraction_state == "complete"
    assert material.reading.pages[1].extraction_state == "no_text"
    assert not material.reading.text_coverage_complete
    assert material.visual_review_required
    reviewed = acknowledge(material, [1, 3])
    assert reviewed.examination == "partial_text"
    with pytest.raises(ValueError, match="unread"):
        acknowledge(material, [1, 2, 3])


def test_fully_extracted_and_acknowledged_is_not_legal_audit(tmp_path):
    root, _ = pdf_dossier(tmp_path, ["One synthetic page", "Another synthetic page"])
    material = acknowledge(material_for(root), [1, 2])
    assert material.reading.text_coverage_complete
    assert material.examination == "text_only"
    assert not material.reading.visual_examined and not material.legal_research_completed
    assert not completeness([material]).legal_research_completed
    assert not completeness([material]).complete_website_audit


@pytest.mark.parametrize("change", ["bytes", "missing", "corrupt", "manifest_text", "page_text"])
def test_pdf_original_and_derivative_integrity(tmp_path, change):
    root, data = pdf_dossier(tmp_path, ["Synthetic public text"])
    artifact = data.evidence[0].artifacts[0]
    path = root / artifact.path
    if change in {"bytes", "corrupt"}:
        path.write_bytes(b"%PDF- corrupted synthetic bytes")
    elif change == "missing":
        path.unlink()
    elif change == "manifest_text":
        data.documents[0].text = "Invented lawless processing allegation"
        save_manifest(root, data)
    else:
        data.documents[0].page_texts[1] = "Invented page excerpt"
        save_manifest(root, data)
    if change in {"bytes", "corrupt", "missing"}:
        with pytest.raises(DossierError, match="integrity"):
            load_packet(root, 2_000_000)
    else:
        packet = load_packet(root, 2_000_000)
        document = packet.data.documents[0]
        assert document.text is None and document.page_texts == {}
        assert document.reading.provenance == "mismatch"
        assert not material_for(root).text_available
        assert "Invented" not in packet.payload


def test_corrupt_original_with_updated_manifest_cannot_reproduce(tmp_path):
    root, data = pdf_dossier(tmp_path, ["Synthetic public text"])
    artifact = data.evidence[0].artifacts[0]
    raw = b"%PDF- synthetic malformed data"
    (root / artifact.path).write_bytes(raw)
    artifact.sha256, artifact.size = sha256(raw), len(raw)
    data.evidence[0].sha256 = artifact.sha256
    save_manifest(root, data)
    packet = load_packet(root, 2_000_000)
    assert packet.data.documents[0].reading.provenance == "failed"
    assert packet.data.documents[0].text is None


@pytest.mark.parametrize(
    "page,assertion,scope,expected",
    [
        (1, "present", "text_excerpt", "potential"),
        (2, "present", "text_excerpt", "unverifiable"),
        (1, "absent", "whole_document", "unverifiable"),
        (None, "present", "text_excerpt", "unverifiable"),
    ],
)
def test_pdf_quotes_and_negative_claims_are_scope_bound(tmp_path, page, assertion, scope, expected):
    root, _ = pdf_dossier(tmp_path, ["Synthetic quoted page", "Different readable page", ""])
    response = auditor_response("A")
    response["findings"][0].update(
        source="https://clinic.example/synthetic.pdf",
        evidence_ids=["document-0001"],
        fact="Synthetic quoted page",
        fact_assertion=assertion,
        examination_scope=scope,
        search_scope="whole_document",
        fact_supported=True,
        text_grounding=[
            dict(evidence_id="document-0001", exact_quote="Synthetic quoted page", page=page)
        ],
    )
    result, _ = validate_result(json.dumps(response), "A", load_packet(root, 2_000_000))
    finding = result.findings[0]
    assert finding.status == expected
    assert finding.fact_supported == (expected == "potential")
    assert finding.normative_basis[0].actuality_status == "unverified"
    assert finding.limitations


def test_html_truncation_preserves_original_and_safe_legacy(tmp_path):
    root = tmp_path / "html"
    store = ArtifactStore(root, Limits())
    html = b"<html><body>Public synthetic text with additional words</body></html>"
    text, reading = extract_html(html, 12)
    artifacts = [
        store.save("page.html", html, "html"),
        store.save("page.txt", text.encode(), "text"),
        store.save("page.png", b"synthetic screenshot fixture", "screenshot"),
    ]
    evidence = CollectedEvidence(
        id="page-0001",
        source="https://clinic.example/",
        captured_at=datetime.now(UTC),
        observed_fact="Synthetic HTML",
        available=True,
        status="complete",
        artifacts=artifacts,
        artifact_path=artifacts[0].path,
        sha256=artifacts[0].sha256,
    )
    page = PageObservation(
        requested_url=evidence.source,
        captured_at=datetime.now(UTC),
        evidence_id=evidence.id,
        text=text,
        reading=reading,
    )
    data = CollectionResult(
        target_url=evidence.source,
        started_at=datetime.now(UTC),
        limits=Limits(),
        evidence=[evidence],
        pages=[page],
    )
    save_manifest(root, data)
    packet = load_packet(root, 2_000_000)
    state = packet.data.pages[0].reading
    assert state.original_saved and state.text_truncated and state.provenance == "reproduced"
    assert state.original_chars > state.supplied_chars == 12
    assert (root / artifacts[0].path).read_bytes() == html
    raw = json.loads((root / "collection.json").read_text())
    del raw["pages"][0]["reading"]
    (root / "collection.json").write_text(json.dumps(raw))
    legacy = load_packet(root, 2_000_000).data.pages[0].reading
    assert legacy.extraction_state == "unknown" and not legacy.text_coverage_complete


def test_legacy_pdf_missing_metadata_stays_unknown(tmp_path):
    root, _ = pdf_dossier(tmp_path, ["Synthetic text"])
    manifest = root / "collection.json"
    data = json.loads(manifest.read_text())
    del data["documents"][0]["reading"]
    del data["documents"][0]["page_texts"]
    manifest.write_text(json.dumps(data))
    material = material_for(root)
    assert material.text_available
    assert material.reading.extraction_state == "unknown"
    assert acknowledge(material, [1]).examination == "partial_text"


@pytest.mark.parametrize("wrong", ["sha", "page", "future", "none"])
def test_visual_journal_bound_untrusted_and_read_only(tmp_path, wrong):
    root, data = pdf_dossier(tmp_path, [""])
    original = data.evidence[0].artifacts[0]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    template = tmp_path / "template"
    receipt = write_inventory(root, template, evidence_id="document-0001", page=1)
    assert receipt["state"] == "pending"
    submission = json.loads((template / "observations.json").read_text())
    submission.update(
        viewed=True,
        reviewer_id="Self-declared synthetic reviewer",
        viewed_at=datetime.now(UTC).isoformat(),
        note="Synthetic view claim",
        observations=["Self-declared synthetic observation, not a legal conclusion"],
    )
    if wrong == "sha":
        submission["original_sha256"] = "a" * 64
    elif wrong == "page":
        submission["page"] = 2
    elif wrong == "future":
        submission["viewed_at"] = "2099-01-01T00:00:00Z"
    path = tmp_path / "submitted.json"
    path.write_text(json.dumps(submission))
    output = tmp_path / "recorded"
    receipt = write_inventory(root, output, submission=path)
    assert (output / "submission.json").read_bytes() == path.read_bytes()
    assert receipt["submission_sha256"] == sha256(path.read_bytes())
    assert receipt["state"] == ("declared_untrusted" if wrong == "none" else "invalidated")
    assert receipt["trust_status"] == "untrusted"
    assert not receipt["legal_research_completed"] and not receipt["production_go_allowed"]
    assert receipt["inventory_sha256"] == sha256((output / "inventory.json").read_bytes())
    assert receipt["observations_sha256"] == sha256((output / "observations.json").read_bytes())
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not material_for(root).reading.visual_examined
    assert original.sha256 == sha256((root / original.path).read_bytes())
    finding = tmp_path / "finding.json"
    finding.write_text(
        json.dumps(
            {
                "source": str(data.evidence[0].source),
                "evidence_ids": ["document-0001"],
                "viewed": True,
            }
        )
    )
    registry = Path(__file__).parents[1] / "examples/legal-registry.json"
    decision = decide_production(root, finding, registry, imported_report=output / "receipt.json")
    assert decision.outcome == "HOLD" and not decision.client_release_allowed
    with pytest.raises(PermissionError):
        send_client_message(decision)


def test_visual_submission_cannot_self_authenticate():
    with pytest.raises(ValidationError):
        VisualObservation(
            evidence_id="synthetic", original_sha256="a" * 64, page=1, trust_status="trusted"
        )


def test_original_directory_not_used_for_sidecar(tmp_path):
    root, _ = pdf_dossier(tmp_path, [""])
    with pytest.raises(ValueError, match="outside"):
        write_inventory(root, root / "journal")


def test_no_metadata_does_not_default_to_full_reading():
    state = ReadingState()
    assert state.extraction_state == "unknown" and state.text_truncated is None
    assert not state.text_coverage_complete and not state.legal_research_completed
    material = Material(
        evidence_id="synthetic",
        url="https://clinic.example/",
        type="pdf",
        collection_status="partial",
    )
    assert material.examination == "not_examined"


@pytest.mark.parametrize(
    "page,assertion,scope,expected",
    [
        (1, "present", "text_excerpt", "potential_issue"),
        (2, "present", "text_excerpt", "insufficient_evidence"),
        (1, "absent", "whole_document", "insufficient_evidence"),
    ],
)
def test_verifier_rechecks_page_and_negative_scope(tmp_path, page, assertion, scope, expected):
    from dataclasses import replace

    from lexradar.auditors.models import REQUIRED_TOPICS
    from lexradar.verifier.cli import default_config
    from lexradar.verifier.packets import prepare_independent
    from lexradar.verifier.rules import normalize_independent

    root, _ = pdf_dossier(tmp_path, ["Synthetic quoted page", "Another supplied page", ""])
    config = default_config()
    registry = Path(__file__).parents[1] / "examples/legal-registry.json"
    prepared = prepare_independent(root, registry, config)
    result = IndependentResult.model_validate(
        {
            "directions": [
                dict(topic=t.value, status="text_reviewed", limitation="Synthetic excerpt only")
                for t in REQUIRED_TOPICS
            ],
            "materials": [
                dict(
                    evidence_id="document-0001",
                    text_examined=True,
                    pages_examined=[1, 2],
                    limitation="Unexamined scan page excluded",
                )
            ],
            "findings": [
                dict(
                    id="synthetic",
                    topic="privacy_policy",
                    claim_code="synthetic",
                    subject="Synthetic document scope",
                    source="https://clinic.example/synthetic.pdf",
                    evidence_ids=["document-0001"],
                    fact="Synthetic quoted page",
                    fact_assertion=assertion,
                    examination_scope=scope,
                    search_scope="whole_document",
                    norm_ids=["152-template"],
                    legal_interpretation="Unverified synthetic hypothesis",
                    attention_reason="Synthetic regression only",
                    fragments=[
                        dict(evidence_id="document-0001", text="Synthetic quoted page", page=page)
                    ],
                )
            ],
        }
    )
    prepared = replace(prepared, materials=examined(prepared.materials, result))
    finding = normalize_independent(result, prepared, config, datetime.now(UTC))[0]
    assert finding.status == expected and not finding.fact_verified
    assert all(n.status != "current_confirmed" for n in finding.norms)


def test_html_decoding_loss_not_complete():
    _, state = extract_html(b"<body>synthetic \xff</body>", 100)
    assert state.extraction_state == "partial"
    assert not state.text_coverage_complete
    assert any("decoding loss" in text for text in state.limitations)


def test_independent_visual_claim_does_not_authorize_page_reading():
    with pytest.raises(ValidationError):
        ReadingState(visual_examined=True, legal_research_completed=True)


def test_sidecar_tampering_and_stale_source_are_detected(tmp_path):
    root, data = pdf_dossier(tmp_path, ["Synthetic text"])
    output = tmp_path / "sidecar"
    write_inventory(root, output, evidence_id="document-0001", page=1)
    assert verify_sidecar(root, output)["trust_status"] == "untrusted"
    observations = output / "observations.json"
    before = observations.read_bytes()
    observations.write_bytes(before + b"\n")
    with pytest.raises(ValueError, match="hash"):
        verify_sidecar(root, output)
    observations.write_bytes(before)
    data.documents[0].extraction_reason = "Synthetic manifest changed after viewing"
    save_manifest(root, data)
    with pytest.raises(ValueError, match="Stale"):
        verify_sidecar(root, output)
