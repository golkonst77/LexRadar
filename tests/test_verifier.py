import copy
import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from test_auditors import RecordingProvider, approve_packet, auditor_response, save_dossier
from test_auditors import dossier as dossier

from lexradar.auditors.models import REQUIRED_TOPICS, AnalysisConfig, Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import HTTPReply, OpenRouterProvider, ProviderError, ProviderReply
from lexradar.collector.models import (
    Artifact,
    CollectedEvidence,
    DocumentObservation,
    EntityObservation,
)
from lexradar.models import Signal
from lexradar.verifier.cli import default_config
from lexradar.verifier.materials import completeness, examined
from lexradar.verifier.models import (
    ComparisonResult,
    HumanFindingReview,
    IndependentResult,
    LegalSource,
    SourceRegistry,
    VerificationReport,
)
from lexradar.verifier.orchestrator import finish, preflight, start
from lexradar.verifier.packets import canonical, prepare_independent
from lexradar.verifier.registry import assess, digest, load_registry
from lexradar.verifier.rules import candidate_hash, normalize_independent


@pytest.fixture
def config():
    cfg = default_config()
    cfg.verifier.model = "test/verifier"
    cfg.max_backoff_seconds = 0
    return cfg


@pytest.fixture
def registry(tmp_path):
    source = LegalSource(
        id="test-law",
        domain="personal_data",
        act_name="SYNTHETIC TEST ONLY",
        act_number="152-ФЗ",
        provision="test provision",
        revision="synthetic-r1",
        effective_from=date(2020, 1, 1),
        source_url="https://pravo.gov.ru/synthetic-fixture",
        norm_text="Synthetic public obligation",
        text_sha256=digest("Synthetic public obligation"),
        status="unverified",
        provenance="Synthetic fixture; not real legislation",
    )
    path = tmp_path / "registry.json"
    path.write_text(SourceRegistry(sources=[source]).model_dump_json())
    return path


def self_asserted_registry(path):
    reg, _ = load_registry(path)
    source = reg.sources[0]
    source.status = "verified_current"
    source.verification_method = "human_official_review"
    source.reviewer = "synthetic-reviewer"
    source.revision_check_basis = "Synthetic fixture only; no live source checked"
    source.checked_at = datetime.now(UTC)
    path.write_text(reg.model_dump_json())
    return source


def candidate():
    return {
        "id": "v-new",
        "topic": "privacy_policy",
        "claim_code": "synthetic_public_obligation",
        "subject": "synthetic scope",
        "source": "https://clinic.example/",
        "evidence_ids": ["page-0001"],
        "fact": "Synthetic public form",
        "fact_assertion": "present",
        "examination_scope": "text_excerpt",
        "fragments": [{"evidence_id": "page-0001", "text": "Synthetic public form", "page": 1}],
        "norm_ids": ["test-law"],
        "operator_ref": "entity-0001",
        "legal_interpretation": "Synthetic hypothesis requiring legal review",
        "attention_reason": "Concrete synthetic text deserves independent review",
        "additional_checks": ["Review public text and applicability"],
        "status": "potential_issue",
    }


def independent_reply(packet, findings=None):
    return {
        "findings": [candidate()] if findings is None else findings,
        "materials": [
            {
                "evidence_id": m["evidence_id"],
                "text_examined": m["text_available"],
                "pages_examined": list(map(int, m["page_texts"])) if m["text_available"] else [],
                "limitation": "Text only; not visual or legal confirmation",
            }
            for m in packet["materials"]
        ],
        "directions": [
            {"topic": t.value, "status": "text_reviewed", "limitation": "Only supplied text scope"}
            for t in sorted(REQUIRED_TOPICS)
        ],
        "limitations": ["Synthetic mocked verifier"],
    }


class Scripted:
    def __init__(self, findings=None, statuses=None):
        self.calls = []
        self.findings = findings
        self.statuses = statuses or {}

    def complete(self, settings, messages):
        self.calls.append(copy.deepcopy(messages))
        packet = json.loads(messages[1]["content"])
        if packet["stage"] == "independent":
            result = independent_reply(packet, self.findings)
        else:
            result = {
                "assessments": [
                    {
                        "auditor": r["auditor"],
                        "finding_id": f["id"],
                        "status": self.statuses.get(f["claim_code"], "verified_issue"),
                        "reason": "Synthetic comparison hypothesis",
                        "matched_independent_ids": [],
                    }
                    for r in packet["untrusted_auditor_results"]
                    for f in r["findings"]
                ],
                "limitations": [],
            }
        return ProviderReply(json.dumps(result), Usage(cost_usd=Decimal("0.001")))


def prior_analysis(dossier, config, tmp_path, responses=None):
    # v0.3 contract, using mocks only; packet hashes bind the exact Collector dossier.
    analyze(
        dossier[0],
        tmp_path / "ab",
        AnalysisConfig.model_validate(
            config.model_dump(exclude={"verifier", "legal_recheck_days"})
        ),
        provider=RecordingProvider(responses),
    )
    return tmp_path / "ab/analysis.json"


def prepared_review(dossier, registry, config, findings=None):
    prepared = prepare_independent(dossier[0], registry, config)
    result = IndependentResult.model_validate(
        independent_reply(json.loads(prepared.packet.payload), findings)
    )
    return replace(prepared, materials=examined(prepared.materials, result)), result


def human_review(prepared, result, **changes):
    data = {
        "packet_sha256": prepared.packet.sha256,
        "candidate_sha256": candidate_hash(result.findings[0]),
        "reviewer": "synthetic-reviewer",
        "reviewed_at": datetime.now(UTC),
        "fact_verified": True,
        "interpretation_verified": True,
        "applicability": "applicable",
        "operator_ref": "entity-0001",
        "operator_identity_verified": True,
        "identity_sources": ["https://clinic.example/public-identity"],
        "activity": "Synthetic activity",
        "period_from": date(2020, 1, 1),
        "period_until": date(2099, 1, 1),
        "rationale": "Synthetic human confirmation of exact claim/operator/activity/period",
    }
    data.update(changes)
    return HumanFindingReview.model_validate(data)


def entities(dossier):
    root, data = dossier
    data.entities = [
        EntityObservation(
            source="https://clinic.example/", raw_text="Synthetic entity A", name="Entity A"
        ),
        EntityObservation(
            source="https://clinic.example/", raw_text="Synthetic entity B", name="Entity B"
        ),
    ]
    save_dossier(root, data)


def approve_verifier(prepared, path):
    path.write_text(
        json.dumps(
            {
                "packet_sha256": prepared.packet.sha256,
                "approved": True,
                "contents_reviewed": True,
                "reviewer": "synthetic-reviewer",
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    return path


def test_two_stage_independence_and_new_finding(dossier, registry, config, tmp_path):
    provider = Scripted()
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=provider)
    blind = copy.deepcopy(provider.calls[0])
    assert (one / "independent.json").is_file()
    assert "untrusted_auditor_results" not in blind[1]["content"]
    assert "separate_consent_present" not in canonical(blind)
    analysis = prior_analysis(dossier, config, tmp_path)
    report = finish(
        dossier[0], registry, one, analysis, tmp_path / "two", config, provider=provider
    )
    second = json.loads(provider.calls[1][1]["content"])
    assert {r["auditor"] for r in second["untrusted_auditor_results"]} == {"A", "B"}
    assert len(provider.calls[1]) == 2 and provider.calls[0] == blind
    assert (
        report.completed
        and report.independent_findings[0].candidate.origin == "independent_verifier"
    )
    assert report.independent_findings[0].candidate.claim_code == "synthetic_public_obligation"
    assert report.independent_findings[0].status == "potential_issue"
    assert not report.automatic_go_allowed and not report.automatic_send_allowed
    assert not report.full_legal_compliance_established


@pytest.mark.parametrize(
    "signal",
    [Signal.PDF_SCAN, Signal.NO_CHECKBOX, Signal.ANALYTICS_SCRIPT, Signal.DOCUMENT_UNAVAILABLE],
)
def test_technical_signals_are_not_violations(dossier, registry, config, signal):
    f = candidate()
    f.update(signal=signal.value, status="verified_issue")
    prepared, result = prepared_review(dossier, registry, config, [f])
    finding = normalize_independent(result, prepared, config, datetime.now(UTC))[0]
    assert finding.status == "rejected"
    assert any("not proof" in reason for reason in finding.reasons)


@pytest.mark.parametrize("status", ["verified_issue", "potential_issue"])
def test_unverified_norm_never_confirms(dossier, registry, config, status):
    entities(dossier)
    f = candidate()
    f["status"] = status
    prepared, result = prepared_review(dossier, registry, config, [f])
    review = human_review(prepared, result)
    finding = normalize_independent(result, prepared, config, datetime.now(UTC), [review])[0]
    assert finding.status == "potential_issue" and finding.norms[0].status == "unverified"


def test_complete_finding_review_cannot_replace_source_authentication(dossier, registry, config):
    entities(dossier)
    self_asserted_registry(registry)
    prepared, result = prepared_review(dossier, registry, config)
    assert (
        normalize_independent(result, prepared, config, datetime.now(UTC))[0].status
        == "potential_issue"
    )
    review = human_review(prepared, result)
    finding = normalize_independent(result, prepared, config, datetime.now(UTC), [review])[0]
    assert finding.status == "potential_issue" and finding.applicability == "confirmed"
    assert finding.norms[0].status == "unverified"
    assert not dossier[1].entities[0].identity_verified


@pytest.mark.parametrize(
    "change",
    [
        {"operator_identity_verified": False},
        {"fact_verified": False},
        {"interpretation_verified": False},
        {"applicability": "unknown"},
    ],
)
def test_incomplete_human_review_cannot_confirm(dossier, registry, config, change):
    entities(dossier)
    self_asserted_registry(registry)
    prepared, result = prepared_review(dossier, registry, config)
    review = human_review(prepared, result, **change)
    finding = normalize_independent(result, prepared, config, datetime.now(UTC), [review])[0]
    assert finding.status != "verified_issue"


def test_inapplicable_norm_rejected_for_concrete_entity(dossier, registry, config):
    entities(dossier)
    self_asserted_registry(registry)
    prepared, result = prepared_review(dossier, registry, config)
    review = human_review(prepared, result, applicability="not_applicable")
    finding = normalize_independent(result, prepared, config, datetime.now(UTC), [review])[0]
    assert finding.status == "rejected" and finding.applicability == "not_applicable"
    assert any("inapplicable" in reason for reason in finding.reasons)


def test_wrong_operator_and_changed_finding_approval_rejected(dossier, registry, config):
    entities(dossier)
    prepared, result = prepared_review(dossier, registry, config)
    with pytest.raises(ValueError, match="operator"):
        normalize_independent(
            result,
            prepared,
            config,
            datetime.now(UTC),
            [human_review(prepared, result, operator_ref="entity-0002")],
        )
    review = human_review(prepared, result)
    result.findings[0].legal_interpretation = "Changed hypothesis"
    with pytest.raises(ValueError, match="stale"):
        normalize_independent(result, prepared, config, datetime.now(UTC), [review])


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"verification_method": "llm"}, "unverified"),
        ({"source_url": "https://commercial.example/law"}, "unverified"),
        ({"source_url": "https://pravo.gov.ru.evil.example/law"}, "unverified"),
        ({"checked_at": datetime.now(UTC) - timedelta(days=31)}, "unverified"),
        ({"checked_at": datetime.now(UTC) + timedelta(days=1)}, "unverified"),
        ({"status": "unavailable"}, "unavailable"),
        ({"effective_from": date(2099, 1, 1)}, "unverified"),
        ({"effective_until": date(2021, 1, 1)}, "unverified"),
    ],
)
def test_norm_currency_is_not_model_assertion(registry, change, expected):
    source = self_asserted_registry(registry)
    for key, value in change.items():
        setattr(source, key, value)
    assert assess(source, datetime.now(UTC).date(), datetime.now(UTC), 30).status == expected


def test_distinct_revisions_and_historical_period(registry):
    current = self_asserted_registry(registry)
    old = current.model_copy(
        update={"id": "old", "revision": "synthetic-old", "effective_until": date(2021, 1, 1)}
    )
    assert assess(old, date(2020, 1, 1), datetime.now(UTC), 30).status == "unverified"
    assert assess(old, datetime.now(UTC).date(), datetime.now(UTC), 30).status == "unverified"
    assert assess(current, datetime.now(UTC).date(), datetime.now(UTC), 30).revision != old.revision


def test_registry_integrity_and_medical_domains(registry):
    reg, _ = load_registry(registry)
    reg.sources[0].norm_text = "Tampered synthetic text"
    registry.write_text(reg.model_dump_json())
    with pytest.raises(ValueError, match="hash"):
        load_registry(registry)
    with pytest.raises(ValidationError):
        reg.sources[0].domain = "medical"


def add_scan(dossier):
    root, data = dossier
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    body = io.BytesIO()
    writer.write(body)
    path = root / "artifacts/scan.pdf"
    path.write_bytes(body.getvalue())
    import hashlib

    artifact = Artifact(
        path="artifacts/scan.pdf",
        sha256=hashlib.sha256(body.getvalue()).hexdigest(),
        size=len(body.getvalue()),
        kind="pdf",
        representation="original_pdf",
    )
    data.evidence.append(
        CollectedEvidence(
            id="doc-scan",
            source="https://clinic.example/scan.pdf",
            captured_at=datetime.now(UTC),
            observed_fact="Synthetic PDF",
            available=True,
            status="complete",
            artifacts=[artifact],
            artifact_path=artifact.path,
            sha256=artifact.sha256,
        )
    )
    data.documents.append(
        DocumentObservation(
            requested_url="https://clinic.example/scan.pdf",
            evidence_id="doc-scan",
            text="",
            extraction_status="visual_review_required",
        )
    )
    save_dossier(root, data)


def test_scan_inventory_and_no_fake_examination(dossier, registry, config):
    add_scan(dossier)
    prepared = prepare_independent(dossier[0], registry, config)
    pdf = prepared.materials[1]
    assert pdf.page_count == 2 and pdf.text_page_count == 0 and pdf.visual_review_required
    raw = independent_reply(json.loads(prepared.packet.payload), [])
    raw["materials"][1].update(text_examined=True, pages_examined=[1, 2])
    with pytest.raises(ValueError, match="unavailable"):
        examined(prepared.materials, IndependentResult.model_validate(raw))


def test_partial_pdf_quote_and_unread_page(dossier, registry, config, monkeypatch):
    add_scan(dossier)
    from hashlib import sha256

    from lexradar.collector.pdf import extract_inventory
    from lexradar.quality.fixtures import pdf_bytes

    root, data = dossier
    artifact = data.evidence[-1].artifacts[0]
    content = pdf_bytes(["Synthetic quoted page", ""])
    (root / artifact.path).write_bytes(content)
    artifact.sha256, artifact.size = sha256(content).hexdigest(), len(content)
    data.evidence[-1].sha256 = artifact.sha256
    extracted = extract_inventory(root / artifact.path, 100, 10)
    data.documents[0].text = extracted.text
    data.documents[0].reading = extracted.reading
    data.documents[0].page_texts = extracted.page_texts
    save_dossier(root, data)
    f = candidate()
    f.update(
        source="https://clinic.example/scan.pdf",
        evidence_ids=["doc-scan"],
        fact="Synthetic quoted page",
        fragments=[{"evidence_id": "doc-scan", "text": "Synthetic quoted page", "page": 1}],
    )
    prepared, result = prepared_review(dossier, registry, config, [f])
    finding = normalize_independent(result, prepared, config, datetime.now(UTC))[0]
    assert finding.status == "potential_issue"
    assert (
        prepared.materials[1].examined_pages == 1
        and prepared.materials[1].examination == "partial_text"
    )
    result.findings[0].fragments[0].page = 2
    assert (
        normalize_independent(result, prepared, config, datetime.now(UTC))[0].status
        == "insufficient_evidence"
    )
    result.materials[1].pages_examined = [1, 2]
    with pytest.raises(ValueError, match="unread"):
        examined(prepared.materials, result)


def test_no_findings_and_completeness_never_full_compliance(dossier, registry, config, tmp_path):
    report = start(dossier[0], registry, tmp_path / "one", config, provider=Scripted(findings=[]))
    assert report.completeness.text_review_fraction == 1
    assert (
        not report.completeness.complete_website_audit
        and not report.full_legal_compliance_established
    )
    assert not report.confirmed_problem_ids
    assert completeness([]).text_review_fraction is None


@pytest.mark.parametrize(
    "code",
    [
        "missing_checkbox_illegal",
        "pdf_scan_illegal",
        "google_analytics_illegal",
        "operator_name_confirms_identity",
    ],
)
def test_comparison_rejects_false_positive(dossier, registry, config, tmp_path, code):
    raw_a, raw_b = auditor_response("A"), auditor_response("B")
    for raw in [raw_a, raw_b]:
        raw["findings"][0]["claim_code"] = code
    analysis = prior_analysis(dossier, config, tmp_path, [raw_a, raw_b])
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=Scripted())
    report = finish(
        dossier[0], registry, one, analysis, tmp_path / "two", config, provider=Scripted()
    )
    assert all(a.status == "rejected" and a.reasons for a in report.auditor_assessments)
    assert report.auditor_assessments[1].duplicate_of == "A:f1"


def test_transmission_needs_exact_new_approval(dossier, registry, config, tmp_path):
    provider = Scripted()
    with pytest.raises(PermissionError):
        start(
            dossier[0],
            registry,
            tmp_path / "one",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            provider=provider,
        )
    # A valid v0.3 approval must not authorize the extended Stage I packet.
    with pytest.raises(PermissionError):
        start(
            dossier[0],
            registry,
            tmp_path / "one",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=approve_packet(dossier[0]),
            provider=provider,
        )
    assert not provider.calls


def test_stage_two_needs_separate_approval(dossier, registry, config, tmp_path):
    provider = Scripted()
    review = tmp_path / "approval.json"
    prepared = prepare_independent(dossier[0], registry, config)
    approve_verifier(prepared, review)
    one = tmp_path / "one"
    start(
        dossier[0],
        registry,
        one,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=review,
        provider=provider,
    )
    analysis = prior_analysis(dossier, config, tmp_path)
    with pytest.raises(PermissionError):
        finish(
            dossier[0],
            registry,
            one,
            analysis,
            tmp_path / "two",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=review,
            provider=provider,
        )
    assert len(provider.calls) == 1
    preflight(
        dossier[0], registry, tmp_path / "review-two", config, stage_one=one, analysis_path=analysis
    )
    second_review = tmp_path / "review-two/approval.json"
    raw = json.loads(second_review.read_text())
    raw.update(
        approved=True,
        contents_reviewed=True,
        reviewer="synthetic-reviewer",
        approved_at=datetime.now(UTC).isoformat(),
    )
    second_review.write_text(json.dumps(raw))
    report = finish(
        dossier[0],
        registry,
        one,
        analysis,
        tmp_path / "two",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=second_review,
        provider=provider,
    )
    assert report.completed and len(provider.calls) == 2
    assert report.stage_one_packet_sha256 != report.stage_two_packet_sha256
    assert report.budget_charged_usd == Decimal("0.002")


def test_comparison_preflight_sensitive_auditor_text_blocks(dossier, registry, config, tmp_path):
    one = tmp_path / "one"
    provider = Scripted()
    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    start(
        dossier[0],
        registry,
        one,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=provider,
    )
    raw_a, raw_b = auditor_response("A"), auditor_response("B")
    raw_a["findings"][0]["legal_interpretation"] = "Synthetic patient diagnosis"
    analysis = prior_analysis(dossier, config, tmp_path, [raw_a, raw_b])
    preflight(
        dossier[0], registry, tmp_path / "review", config, stage_one=one, analysis_path=analysis
    )
    record = json.loads((tmp_path / "review/approval.json").read_text())
    record.update(
        approved=True,
        contents_reviewed=True,
        reviewer="synthetic-reviewer",
        approved_at=datetime.now(UTC).isoformat(),
    )
    (tmp_path / "review/approval.json").write_text(json.dumps(record))
    with pytest.raises(PermissionError, match="sensitive"):
        finish(
            dossier[0],
            registry,
            one,
            analysis,
            tmp_path / "two",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=tmp_path / "review/approval.json",
            provider=provider,
        )
    assert len(provider.calls) == 1


def test_blind_preflight_cannot_take_auditor_path(dossier, registry, config, tmp_path):
    with pytest.raises(ValueError, match="must not read"):
        preflight(
            dossier[0],
            registry,
            tmp_path / "review",
            config,
            analysis_path=tmp_path / "nonexistent",
        )


def test_persisted_independent_tamper_blocks_comparison(dossier, registry, config, tmp_path):
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=Scripted())
    analysis = prior_analysis(dossier, config, tmp_path)
    raw = json.loads((one / "independent.json").read_text())
    raw["findings"][0]["fact"] = "Changed"
    (one / "independent.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="changed"):
        finish(dossier[0], registry, one, analysis, tmp_path / "two", config, provider=Scripted())


def test_budget_shared_across_stages(dossier, registry, config, tmp_path):
    config.max_budget_usd = Decimal("0.000001")
    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    provider = Scripted()
    report = start(
        dossier[0],
        registry,
        tmp_path / "one",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=provider,
    )
    assert report.run_status == "budget_exceeded" and not provider.calls
    assert not report.logs[0].request_sent


def test_no_api_key_in_verifier_journal(dossier, registry, config, tmp_path, monkeypatch):
    marker = "synthetic-secret-test-marker"
    monkeypatch.setenv("OPENROUTER_API_KEY", marker)
    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")

    class Transport:
        def post(self, payload, key, timeout, max_bytes):
            assert key == marker and marker not in payload.decode()
            packet = json.loads(payload)["messages"][1]["content"]
            body = {
                "model": config.verifier.model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(independent_reply(json.loads(packet)))},
                    }
                ],
            }
            return HTTPReply(200, json.dumps(body).encode())

    one = tmp_path / "one"
    start(
        dossier[0],
        registry,
        one,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=OpenRouterProvider(config, Transport()),
    )
    assert all(marker not in p.read_text() for p in one.iterdir())


def test_offline_cli_and_v03_compatibility(dossier, registry, config, tmp_path, monkeypatch):
    from lexradar.cli import main

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    one, two = tmp_path / "one", tmp_path / "two"
    monkeypatch.setattr(
        "sys.argv",
        ["lexradar", "verify", str(dossier[0]), "--registry", str(registry), "--output", str(one)],
    )
    main()
    analysis = prior_analysis(dossier, config, tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "verify-compare",
            str(dossier[0]),
            "--registry",
            str(registry),
            "--stage-one",
            str(one),
            "--analysis",
            str(analysis),
            "--output",
            str(two),
        ],
    )
    main()
    report = VerificationReport.model_validate_json((two / "verification.json").read_text())
    assert report.completed and not any(log.request_sent for log in report.logs)
    assert not report.automatic_go_allowed and not report.automatic_send_allowed


def test_conflicting_revisions_fail_closed(registry):
    from lexradar.verifier.registry import assess_registry

    source = self_asserted_registry(registry)
    other = source.model_copy(update={"id": "overlapping", "revision": "different-revision"})
    results = assess_registry(
        SourceRegistry(sources=[source, other]), datetime.now(UTC).date(), datetime.now(UTC), 30
    )
    assert all(n.status == "unverified" for n in results.values())
    assert all(any("Conflicting" in r for r in n.reasons) for n in results.values())


def test_retired_revision_claim_cannot_authenticate_historical_period(registry):
    source = self_asserted_registry(registry)
    source.effective_until = date(2021, 1, 1)
    source.status = "repealed"
    assert assess(source, date(2020, 1, 1), datetime.now(UTC), 30).status == "unverified"
    assert assess(source, datetime.now(UTC).date(), datetime.now(UTC), 30).status == "unverified"


def test_llm_cannot_create_verified_legal_source(dossier, registry, config, tmp_path):
    f = candidate()
    f["norm_ids"] = ["invented-law"]
    report = start(dossier[0], registry, tmp_path / "one", config, provider=Scripted([f]))
    assert report.run_status == "invalid_evidence_response" and not report.independent_findings
    assert report.logs[0].status == "invalid_evidence_response"


def test_partial_screenshot_failure_keeps_verified_text_scope(dossier, registry, config):
    root, data = dossier
    data.evidence[0].status = "partial"
    data.evidence[0].collection_errors = ["Screenshot failed"]
    data.evidence[0].artifacts.pop()
    save_dossier(root, data)
    prepared, result = prepared_review(dossier, registry, config)
    finding = normalize_independent(result, prepared, config, datetime.now(UTC))[0]
    assert finding.status == "potential_issue" and prepared.materials[0].text_available
    assert "Screenshot failed" in prepared.materials[0].reasons


def test_pdf_inventory_timeout_is_not_violation(dossier, registry, config, monkeypatch):
    import subprocess

    add_scan(dossier)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic", 1)

    monkeypatch.setattr("lexradar.collector.pdf.subprocess.run", timeout)
    prepared = prepare_independent(dossier[0], registry, config)
    assert prepared.materials[1].page_count is None
    assert not prepared.materials[1].text_available
    assert any("unavailable" in r for r in prepared.materials[1].reasons)


def test_unknown_cost_reservation_carried_to_stage_two(dossier, registry, config, tmp_path):
    from lexradar.auditors.budget import Budget
    from lexradar.verifier.runtime import messages_for

    class UnknownCost(Scripted):
        def complete(self, settings, messages):
            response = super().complete(settings, messages)
            return ProviderReply(response.content, Usage())

    prepared = prepare_independent(dossier[0], registry, config)
    messages = messages_for("independent", prepared.packet)[0]
    reservation = Budget(Decimal(100)).reserve(config.verifier, messages)
    config.max_budget_usd = reservation + Decimal("0.000001")
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    provider = UnknownCost()
    one = tmp_path / "one"
    start(
        dossier[0],
        registry,
        one,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=provider,
    )
    analysis = prior_analysis(dossier, config, tmp_path)
    preflight(
        dossier[0], registry, tmp_path / "review", config, stage_one=one, analysis_path=analysis
    )
    path = tmp_path / "review/approval.json"
    raw = json.loads(path.read_text())
    raw.update(
        approved=True,
        contents_reviewed=True,
        reviewer="synthetic-reviewer",
        approved_at=datetime.now(UTC).isoformat(),
    )
    path.write_text(json.dumps(raw))
    report = finish(
        dossier[0],
        registry,
        one,
        analysis,
        tmp_path / "two",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=path,
        provider=provider,
    )
    assert report.run_status == "budget_exceeded" and len(provider.calls) == 1
    assert report.budget_charged_usd == reservation
    assert not report.logs[-1].request_sent


def test_bounded_retry_and_safe_error_journal(dossier, registry, config, tmp_path):
    class Transient:
        def __init__(self):
            self.calls = 0

        def complete(self, settings, messages):
            self.calls += 1
            raise ProviderError("http_429", retryable=True)

    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    provider = Transient()
    report = start(
        dossier[0],
        registry,
        tmp_path / "one",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=provider,
    )
    assert provider.calls == config.retries + 1
    assert not report.completed and all(log.status == "http_429" for log in report.logs)


def test_api_key_echo_never_saved(dossier, registry, config, tmp_path, monkeypatch):
    marker = "synthetic-secret-echo-marker"
    monkeypatch.setenv("OPENROUTER_API_KEY", marker)

    class Echo:
        def post(self, payload, key, timeout, max_bytes):
            return HTTPReply(200, json.dumps({"echo": marker}).encode())

    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    output = tmp_path / "one"
    report = start(
        dossier[0],
        registry,
        output,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=OpenRouterProvider(config, Echo()),
    )
    assert report.run_status == "secret_echo_rejected"
    assert all(marker not in p.read_text() for p in output.iterdir())


def test_stage_one_failure_never_reads_auditor_report(dossier, registry, config, tmp_path):
    bad = candidate()
    bad["evidence_ids"] = ["unknown"]
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=Scripted([bad]))
    with pytest.raises(ValueError, match="not successful"):
        finish(dossier[0], registry, one, tmp_path / "nonexistent-ab", tmp_path / "two", config)


def test_comparison_cannot_omit_one_auditor(dossier, registry, config, tmp_path):
    from lexradar.verifier.packets import load_analysis
    from lexradar.verifier.rules import compare_results

    prepared, result = prepared_review(dossier, registry, config)
    analysis = load_analysis(prior_analysis(dossier, config, tmp_path), prepared.packet)
    with pytest.raises(ValueError, match="every A/B"):
        compare_results(
            ComparisonResult(assessments=[]), analysis, [], prepared, config, datetime.now(UTC)
        )


def test_verified_problem_cannot_be_injected_in_report(dossier, registry, config, tmp_path):
    report = start(dossier[0], registry, tmp_path / "one", config)
    data = report.model_dump()
    data["confirmed_problem_ids"] = ["invented-confirmation"]
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(data)
    data = report.model_dump()
    data["automatic_go_allowed"] = True
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(data)
    data = report.model_dump()
    data["automatic_send_allowed"] = True
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(data)


@pytest.mark.parametrize(
    "basis_act,expected", [("152-ФЗ", "potential_issue"), ("invented-act", "potential_issue")]
)
def test_auditor_confirmation_cannot_borrow_unverified_basis(
    dossier, registry, config, tmp_path, basis_act, expected
):
    from lexradar.verifier.packets import load_analysis
    from lexradar.verifier.rules import compare_results

    entities(dossier)
    self_asserted_registry(registry)
    prepared, independent = prepared_review(dossier, registry, config)
    review = human_review(prepared, independent)
    own = normalize_independent(independent, prepared, config, datetime.now(UTC), [review])
    raw_a, raw_b = auditor_response("A"), auditor_response("B")
    template = candidate()
    for raw in [raw_a, raw_b]:
        f = raw["findings"][0]
        for key in [
            "topic",
            "claim_code",
            "subject",
            "fact",
            "fact_assertion",
            "examination_scope",
            "legal_interpretation",
        ]:
            f[key] = template[key]
        f["text_grounding"] = [{"evidence_id": "page-0001", "exact_quote": f["fact"]}]
        f["normative_basis"][0].update(act_id=basis_act, provision="test provision")
    analysis = load_analysis(
        prior_analysis(dossier, config, tmp_path, [raw_a, raw_b]), prepared.packet
    )
    proposals = ComparisonResult(
        assessments=[
            {
                "auditor": label,
                "finding_id": "f1",
                "status": "verified_issue",
                "reason": "Synthetic claim comparison",
                "matched_independent_ids": ["v-new"],
            }
            for label in ["A", "B"]
        ]
    )
    results = compare_results(proposals, analysis, own, prepared, config, datetime.now(UTC))
    assert all(a.status == expected for a in results)
    assert all(not a.verified_independent_ids for a in results)


def test_finding_requires_declared_normative_basis(dossier, registry, config, tmp_path):
    f = candidate()
    f["norm_ids"] = []
    report = start(dossier[0], registry, tmp_path / "one", config, provider=Scripted([f]))
    assert report.run_status == "invalid_response" and not report.independent_findings


def test_verifier_model_cannot_leak_key_into_log(dossier, registry, config, tmp_path, monkeypatch):
    marker = "synthetic-config-secret-marker"
    monkeypatch.setenv("OPENROUTER_API_KEY", marker)
    config.verifier.model = marker
    prepared = prepare_independent(dossier[0], registry, config)
    approval = approve_verifier(prepared, tmp_path / "approval.json")
    with pytest.raises(ProviderError, match="secret_in_configuration"):
        start(
            dossier[0],
            registry,
            tmp_path / "one",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=approval,
        )
    assert not (tmp_path / "one").exists()


def test_wrong_auditor_source_blocks_comparison(dossier, registry, config, tmp_path):
    from lexradar.verifier.packets import load_analysis

    prepared, _ = prepared_review(dossier, registry, config)
    path = prior_analysis(dossier, config, tmp_path)
    raw = json.loads(path.read_text())
    raw["runs"][0]["result"]["findings"][0]["source"] = "https://wrong.example/"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="Unsupported auditor source"):
        load_analysis(path, prepared.packet)


def test_potential_index_includes_unresolved_auditor_hypothesis(
    dossier, registry, config, tmp_path
):
    raw_a, raw_b = auditor_response("A"), auditor_response("B")
    for raw in [raw_a, raw_b]:
        raw["findings"][0].update(
            fact_assertion="present",
            examination_scope="text_excerpt",
            fact="Synthetic public form",
            text_grounding=[{"evidence_id": "page-0001", "exact_quote": "Synthetic public form"}],
        )
    analysis = prior_analysis(dossier, config, tmp_path, [raw_a, raw_b])
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=Scripted(findings=[]))
    report = finish(
        dossier[0], registry, one, analysis, tmp_path / "two", config, provider=Scripted()
    )
    assert report.potential_problem_ids == ["A:f1"]
    assert (
        report.auditor_assessments[0].original_finding.normative_basis[0].actuality_status
        == "unverified"
    )
    assert report.auditor_assessments[0].additional_checks
    assert report.auditor_assessments[1].duplicate_of == "A:f1"


def test_opposite_auditor_assertions_are_not_duplicates(dossier, registry, config, tmp_path):
    raw_a, raw_b = auditor_response("A"), auditor_response("B")
    raw_a["findings"][0].update(
        fact_assertion="present",
        examination_scope="text_excerpt",
        fact="Synthetic public form",
        text_grounding=[{"evidence_id": "page-0001", "exact_quote": "Synthetic public form"}],
    )
    raw_b["findings"][0]["fact_assertion"] = "absent"
    analysis = prior_analysis(dossier, config, tmp_path, [raw_a, raw_b])
    one = tmp_path / "one"
    start(dossier[0], registry, one, config, provider=Scripted(findings=[]))
    report = finish(
        dossier[0], registry, one, analysis, tmp_path / "two", config, provider=Scripted()
    )
    assert all(a.duplicate_of is None for a in report.auditor_assessments)
    assert all(
        any("contradiction" in reason for reason in a.reasons) for a in report.auditor_assessments
    )
    assert report.potential_problem_ids == ["A:f1"]


def test_forged_registry_json_with_correct_hash_never_trusted(registry):
    # Serialized JSON, not a mock trust provider: every legacy verification field is filled.
    source = self_asserted_registry(registry)
    source.norm_text = "FAKE synthetic norm: every form requires an imaginary checkbox"
    source.text_sha256 = digest(source.norm_text)
    source.provenance = "Invented claim of official acquisition"
    registry.write_text(SourceRegistry(sources=[source]).model_dump_json())
    loaded, _ = load_registry(registry)
    assessment = assess(loaded.sources[0], datetime.now(UTC).date(), datetime.now(UTC), 30)
    assert assessment.status == "unverified"
    assert any("authenticated" in reason for reason in assessment.reasons)


def source_confirmation(submission):
    from lexradar.verifier.attestation import ReviewerConfirmation

    return ReviewerConfirmation(
        binding_sha256=submission.binding_sha256,
        reviewer="synthetic-independent-reviewer",
        reviewed_at=datetime.now(UTC),
        independent_of_record_author=True,
        official_origin_checked=True,
        revision_and_period_checked=True,
        acquisition_reference="Synthetic reference; not authenticated",
        rationale="Synthetic claimed independent review",
    )


def test_complete_separate_attestation_is_still_unverified(registry):
    from lexradar.verifier.attestation import AttestationSubmission, submit

    source = self_asserted_registry(registry)
    draft = submit(source)
    completed = submit(source, source_confirmation(draft))
    assert completed.status == "unverified" and not completed.trusted
    assert completed.origin_authentication == completed.reviewer_authentication == "unsupported"
    assert assess(source, datetime.now(UTC).date(), datetime.now(UTC), 30).status == "unverified"
    forged = json.loads(completed.model_dump_json())
    forged.update(status="current_confirmed", trusted=True)
    with pytest.raises(ValidationError):
        AttestationSubmission.model_validate(forged)


@pytest.mark.parametrize(
    "field,value",
    [
        ("norm_text", "Different synthetic norm"),
        ("source_url", "https://publication.pravo.gov.ru/another-synthetic-record"),
        ("revision", "different-revision"),
        ("effective_from", date(2022, 1, 1)),
        ("effective_until", date(2090, 1, 1)),
        ("provision", "other synthetic provision"),
    ],
)
def test_changed_attestation_binding_rejects_confirmation(registry, field, value):
    from lexradar.verifier.attestation import submit

    source = self_asserted_registry(registry)
    confirmation = source_confirmation(submit(source))
    setattr(source, field, value)
    if field == "norm_text":
        source.text_sha256 = digest(source.norm_text)
    with pytest.raises(ValueError, match="another source"):
        submit(source, confirmation)


@pytest.mark.parametrize(
    "field",
    ["independent_of_record_author", "official_origin_checked", "revision_and_period_checked"],
)
def test_attestation_requires_independent_specific_confirmation(registry, field):
    from lexradar.verifier.attestation import submit

    source = self_asserted_registry(registry)
    confirmation = source_confirmation(submit(source))
    setattr(confirmation, field, False)
    with pytest.raises(ValueError):
        submit(source, confirmation)


def test_attestation_cli_preserves_registry_and_never_grants_trust(registry, tmp_path, monkeypatch):
    from lexradar.cli import main

    self_asserted_registry(registry)
    original = registry.read_bytes()
    output = tmp_path / "source-request"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "attest-source",
            "--registry",
            str(registry),
            "--source-id",
            "test-law",
            "--output",
            str(output),
        ],
    )
    main()
    draft = json.loads((output / "submission.json").read_text())
    assert draft["status"] == "unverified" and not draft["trusted"]
    assert digest((output / "norm-text.txt").read_text()) == draft["binding"]["text_sha256"]
    assert not json.loads((output / "confirmation.json").read_text())[
        "independent_of_record_author"
    ]
    assert registry.read_bytes() == original
    from lexradar.verifier.attestation import submit

    confirmation = source_confirmation(submit(load_registry(registry)[0].sources[0]))
    (output / "confirmation.json").write_text(confirmation.model_dump_json())
    completed = tmp_path / "source-submission"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "attest-source",
            "--registry",
            str(registry),
            "--source-id",
            "test-law",
            "--confirmation",
            str(output / "confirmation.json"),
            "--output",
            str(completed),
        ],
    )
    main()
    artifact = json.loads((completed / "submission.json").read_text())
    assert (
        artifact["confirmation"] and artifact["status"] == "unverified" and not artifact["trusted"]
    )
    assert registry.read_bytes() == original


def test_attestation_cannot_be_injected_as_registry_trust(registry):
    source = self_asserted_registry(registry)
    payload = json.loads(SourceRegistry(sources=[source]).model_dump_json())
    payload["trusted_confirmation"] = {"status": "current_confirmed", "authenticated": True}
    registry.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        load_registry(registry)


@pytest.mark.parametrize("tamper", [False, True])
def test_source_cards_reach_independent_and_comparison_with_reasons(
    dossier, config, tmp_path, tamper
):
    from test_legal_sources import RAW, payload

    original = tmp_path / "original.txt"
    original.write_bytes(RAW)
    card = payload(
        id="test-law", act_number="152-ФЗ", domain="personal_data", provision="test provision"
    )
    path = tmp_path / "cards.json"
    path.write_text(json.dumps({"schema_version": "0.4", "sources": [], "cards": [card]}))
    if tamper:
        original.write_bytes(b"Synthetic substitution")
    provider = Scripted()
    first = start(dossier[0], path, tmp_path / "stage-one", config, provider=provider)
    assert first.run_status == "awaiting_comparison" and first.independent_findings
    finding = first.independent_findings[0]
    assert finding.status != "verified_issue"
    assert finding.norms[0].status == "unverified"
    assert any("authentication unsupported" in r for r in finding.reasons)
    assert finding.norms[0].card_assessment.technical_integrity == (
        "invalid" if tamper else "consistent"
    )
    independent_packet = json.loads(provider.calls[0][1]["content"])
    assert "untrusted_auditor_results" not in independent_packet
    responses = [auditor_response(label) for label in ("A", "B")]
    for response in responses:
        response["findings"][0]["normative_basis"][0].update(
            act_id="152-ФЗ", provision="test provision"
        )
    analysis = prior_analysis(dossier, config, tmp_path, responses)
    final = finish(
        dossier[0],
        path,
        tmp_path / "stage-one",
        analysis,
        tmp_path / "stage-two",
        config,
        provider=Scripted(),
    )
    assert final.completed
    assert all(a.status != "verified_issue" for a in final.auditor_assessments)
    assert any(a.normative_assessments for a in final.auditor_assessments)
    assert not final.automatic_go_allowed and not final.automatic_send_allowed


def test_source_original_change_after_stage_i_blocks_comparison(dossier, config, tmp_path):
    from test_legal_sources import RAW, payload

    original = tmp_path / "original.txt"
    original.write_bytes(RAW)
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "0.4",
                "sources": [],
                "cards": [payload(id="test-law", domain="personal_data", act_number="152-ФЗ")],
            }
        )
    )
    start(dossier[0], path, tmp_path / "stage-one", config, provider=Scripted())
    analysis = prior_analysis(dossier, config, tmp_path)
    original.write_bytes(b"Substituted after independent preparation")
    provider = Scripted()
    with pytest.raises(ValueError, match="snapshot changed"):
        finish(
            dossier[0],
            path,
            tmp_path / "stage-one",
            analysis,
            tmp_path / "stage-two",
            config,
            provider=provider,
        )
    assert not provider.calls
