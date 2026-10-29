"""Actual saved synthetic bytes, hostile imported conclusions, no external actions."""

import json
import socket

import pytest
from pydantic import ValidationError
from test_primary import EXAMPLES, NOW, make_dossier, run
from test_qualification import FORM, POLICY
from test_reading_integrity import pdf_dossier

from lexradar.approval import send_client_message
from lexradar.decision import decide_production
from lexradar.primary.models import Citation, HTMLPosition, StaticSpan
from lexradar.primary.qualification import qualify_primary
from lexradar.verifier.offline import (
    LocalResearch,
    export_verification,
    source_range,
    verify_saved_assessment,
)
from lexradar.verifier.offline_cli import independent_main
from lexradar.verifier.offline_models import IndependentVerification


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Independent local verification must remain offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def setup(tmp_path, body=POLICY + FORM, **kwargs):
    root, data = make_dossier(tmp_path, body, **kwargs)
    primary = run(root)
    assessment = qualify_primary(primary)
    return root, data, primary, assessment


def verify(root, primary, assessment, **kwargs):
    result = verify_saved_assessment(root, primary, assessment, **kwargs)
    assert result.production_outcome == "HOLD"
    assert result.legal_status == "not_confirmed"
    assert not result.production_go_allowed
    assert not result.client_release_allowed and not result.automatic_send_allowed
    assert not result.legal_research_completed
    return result


def test_actual_artifacts_and_citations_reproduced(tmp_path):
    root, _, primary, assessment = setup(tmp_path)
    result = verify(root, primary, assessment)
    assert result.technical_fact_count > 0
    assert not result.contradiction_count
    assert all(f.technical_status == "verified_fact" for f in result.findings)
    assert not result.new_findings
    assert all(
        c.actual_sha256 == c.citation.artifact_sha256
        for f in result.findings
        for c in f.evidence_checks
    )


@pytest.mark.parametrize("mutation", ["replace", "remove", "hash"])
def test_original_tampering_never_verified(tmp_path, mutation):
    root, data, primary, assessment = setup(tmp_path)
    artifact = data.evidence[0].artifacts[0]
    path = root / artifact.path
    if mutation == "replace":
        path.write_text("<html>Forged</html>")
    elif mutation == "remove":
        path.unlink()
    else:
        artifact.sha256 = "0" * 64
        (root / "collection.json").write_text(data.model_dump_json())
    result = verify(root, primary, assessment)
    assert result.technical_fact_count == 0 and result.gaps
    assert all(f.technical_status == "insufficient_evidence" for f in result.findings)
    assert all(w.preparation_recommendation == "blocked" for w in result.commercial_work)


@pytest.mark.parametrize("mutation", ["line", "column", "quote", "hash", "description"])
def test_forged_imported_citation_or_fact(tmp_path, mutation):
    root, _, primary, assessment = setup(tmp_path, "<p>Fixture prefix</p></span>" + POLICY + FORM)
    fact = next(
        fact
        for f in assessment.legal_findings
        for fact in f.facts
        if any(c.static_span and c.quote for c in fact.citations)
    )
    citation = next(c for c in fact.citations if c.static_span and c.quote)
    if mutation == "line":
        citation.static_span.end_exclusive.line = 99999
    elif mutation == "column":
        citation.static_span.end_exclusive.column = 99999
    elif mutation == "quote":
        citation.quote = "Fabricated quote absent from original"
    elif mutation == "hash":
        citation.artifact_sha256 = "0" * 64
    else:
        fact.description = "An arbitrary assertion of illegal collection"
    result = verify(root, primary, assessment)
    assert fact.id not in {i for f in result.findings for i in f.independently_reproduced_fact_ids}
    assert result.gaps
    assert any(f.technical_status != "verified_fact" for f in result.findings)


@pytest.mark.parametrize(
    "texts,limit,scans",
    [
        (["Readable policy text"] * 12, 2, False),
        (["Readable policy text", "", "Other text"], 100, True),
        ([""], 100, True),
    ],
)
def test_pdf_actual_page_denominator_and_unread_parts(tmp_path, texts, limit, scans):
    root, _ = pdf_dossier(tmp_path, texts, pages=limit, scans=scans)
    primary = run(root)
    result = verify(root, primary, qualify_primary(primary))
    material = result.materials[0]
    assert material.known_total_parts == len(texts)
    assert len(material.read_parts) < material.known_total_parts
    assert material.skipped_parts and not material.text_coverage_complete
    assert material.visual_review_required and not material.visual_review_performed


def test_organizations_biography_and_roles_remain_unconfirmed(tmp_path):
    root, _, primary, assessment = setup(
        tmp_path,
        "<p>ООО «Синтетическая клиника»</p><p>Образование врача: ООО «Синтетическое обучение»</p>",
    )
    result = verify(root, primary, assessment)
    assert len([f for f in result.findings if f.source_finding.category == "A"]) == 2
    assert any("Biography" in note for f in result.findings for note in f.limitations)
    a = [f for f in result.findings if f.source_finding.category == "A"]
    assert (
        sum(
            any(
                c["context"] == "biography_education_partner_candidate"
                for c in f.organization_contexts
            )
            for f in a
        )
        == 1
    )
    assert not result.commercial_work
    assert all(
        not obj.identity_or_operator_confirmed
        for f in result.findings
        for obj in f.source_finding.objects
    )


def test_checkbox_absence_and_unread_policy_not_proof_or_paid_need(tmp_path):
    root, _, primary, assessment = setup(
        tmp_path, '<form><input name="phone"><a href="/policy.pdf">Политика</a></form>'
    )
    result = verify(root, primary, assessment)
    assert not result.commercial_work
    assert all(f.legal_status == "not_confirmed" for f in result.findings)
    assert result.gap_count > 0


def test_gtm_medflex_not_confirmed_transfer(tmp_path):
    root, _, primary, assessment = setup(
        tmp_path,
        '<a href="https://booking.medflex.ru/A">Запись</a>',
        head='<script src="https://www.googletagmanager.com/gtm.js"></script>',
    )
    result = verify(root, primary, assessment)
    assert {f.source_finding.category for f in result.findings} == {"E", "F"}
    assert all(f.legal_applicability == "unestablished" for f in result.findings)
    assert not result.commercial_work


def test_unverified_norm_revision_stays_unverified(tmp_path):
    root, _, primary, assessment = setup(tmp_path)
    result = verify(root, primary, assessment, registry_path=EXAMPLES / "legal-registry.json")
    candidates = [n for f in result.findings for n in f.norm_candidates]
    assert candidates and all(n["assessment"]["status"] != "current_confirmed" for n in candidates)
    assert all(f.normative_status == "normative_review_required" for f in result.findings)
    assert all(w.necessity_status == "not_confirmed" for w in result.commercial_work)


def test_forged_paid_candidate_is_not_inherited(tmp_path):
    root, _, primary, assessment = setup(tmp_path)
    assessment.commercial_work[0].work_type = "advertising_consent"
    result = verify(root, primary, assessment)
    row = result.commercial_work[0]
    assert row.factual_basis == "insufficient_evidence"
    assert row.preparation_recommendation == "blocked"
    assert row.new_document_needed == "unknown"


def test_stage_i_finds_removed_primary_facts(tmp_path):
    root, _, primary, assessment = setup(
        tmp_path,
        '<form><input name="phone"></form><a href="https://booking.medflex.ru/">Запись</a>',
    )
    assessment.legal_findings = [f for f in assessment.legal_findings if f.category != "E"]
    primary.services = []
    for observation in primary.observations:
        if observation.stage_id in {"08", "09"}:
            observation.description = "Imported report omitted the resource observation"
    result = verify(root, primary, assessment)
    assert any(f.category == "E" for f in result.new_findings)
    assert result.stage_i_input == "saved_dossier_and_registry_only"
    assert result.new_observations
    assert any(o.technical_status == "verified_fact" for o in result.new_observations)


def test_pdf_quote_bound_to_available_page_not_entire_document(tmp_path):
    root, data = pdf_dossier(tmp_path, ["Readable source"] * 12, pages=2)
    research = LocalResearch(
        root, registry=None, sector="unknown", as_of=NOW, max_input_bytes=2_000_000
    )
    e = data.evidence[0]
    cite = Citation(
        evidence_id=e.id,
        url=e.source,
        artifact_path=e.artifacts[0].path,
        artifact_sha256=e.artifacts[0].sha256,
        basis="reproduced_text",
        quote="Readable source",
        page=1,
    )
    assert research.check(cite).status == "partially_verified"
    cite.page = 12
    assert research.check(cite).status == "insufficient_evidence"


def test_missing_registry_does_not_discard_available_materials(tmp_path):
    root, _, primary, assessment = setup(tmp_path)
    result = verify(root, primary, assessment, registry_path=tmp_path / "missing.json")
    assert result.technical_fact_count > 0 and result.materials
    assert any("Norm registry" in gap for gap in result.gaps)


def test_source_change_during_comparison_stops_output(tmp_path, monkeypatch):
    root, data, primary, assessment = setup(tmp_path)
    original = LocalResearch.check

    def mutate(self, citation):
        checked = original(self, citation)
        (root / data.evidence[0].artifacts[0].path).write_text("Changed during comparison")
        return checked

    monkeypatch.setattr(LocalResearch, "check", mutate)
    with pytest.raises(ValueError):
        verify_saved_assessment(root, primary, assessment)


@pytest.mark.parametrize("separator", ["\r", "\f", "\u2028", "\u2029"])
def test_citation_coordinates_follow_htmlparser_not_splitlines(separator):
    raw = f"prefix{separator}target\r\nlast\n"
    span = StaticSpan(
        start=HTMLPosition(line=1, column=7),
        end_exclusive=HTMLPosition(line=1, column=13),
        status="complete",
    )
    assert source_range(raw, span) == "target"
    span = StaticSpan(
        start=HTMLPosition(line=2, column=0),
        end_exclusive=HTMLPosition(line=3, column=0),
        status="complete",
    )
    assert source_range(raw, span) == "last\n"


def test_reproducible_without_mutating_sources(tmp_path):
    root, _, primary, assessment = setup(tmp_path)
    before = (
        assessment.model_dump_json(),
        primary.model_dump_json(),
        (root / "collection.json").read_bytes(),
    )
    first = verify(root, primary, assessment)
    assert verify(root, primary, assessment).model_dump_json() == first.model_dump_json()
    assert before == (
        assessment.model_dump_json(),
        primary.model_dump_json(),
        (root / "collection.json").read_bytes(),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("production_outcome", "GO"),
        ("client_release_allowed", True),
        ("automatic_send_allowed", True),
        ("legal_research_completed", True),
    ],
)
def test_imported_verification_cannot_enable_release(tmp_path, field, value):
    root, _, primary, assessment = setup(tmp_path)
    payload = verify(root, primary, assessment).model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        IndependentVerification.model_validate(payload)


def test_exports_and_cli_keep_all_old_outputs(tmp_path):
    from lexradar.primary.cli import write_report

    root, _, primary, assessment = setup(tmp_path)
    old = tmp_path / "primary"
    write_report(primary, old, root)
    before = {p.name: p.read_bytes() for p in old.glob("*.json")}
    out = tmp_path / "verified"
    independent_main(
        [
            str(root),
            "--primary",
            str(old / "audit.json"),
            "--assessment",
            str(old / "internal-assessment.json"),
            "--output",
            str(out),
        ]
    )
    assert len(before) == 15 and before == {p.name: p.read_bytes() for p in old.glob("*.json")}
    assert len(list(out.glob("*.json"))) == 4
    for file in out.glob("*.json"):
        data = json.loads(file.read_text())
        assert data["audience"] == "internal_only" and not data["client_release_allowed"]
    with pytest.raises(PermissionError):
        send_client_message(
            decide_production(
                EXAMPLES / "analysis-dossier",
                EXAMPLES / "decision-finding.json",
                EXAMPLES / "legal-registry.json",
                imported_report=out / "independent-verification.json",
            )
        )
    with pytest.raises(ValueError):
        export_verification(verify(root, primary, assessment), root / "output", root)
