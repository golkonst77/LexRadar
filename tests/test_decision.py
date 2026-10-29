"""Adversarial local JSON and byte substitutions never confer production trust."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from lexradar.approval import send_client_message
from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from lexradar.decision import decide_production, prepare_review
from lexradar.decision.models import ProductionDecision
from lexradar.decision.service import sha256
from lexradar.gateway import decide, decide_demo
from lexradar.models import HumanApproval
from lexradar.report import build_report
from lexradar.verifier.models import VerificationReport

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_external_transport(monkeypatch):
    from lexradar.auditors.provider import OpenRouterTransport
    from lexradar.collector.network import Fetcher

    def forbidden(*args, **kwargs):
        raise AssertionError("Decision must not use network or LLM")

    monkeypatch.setattr(Fetcher, "get", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "dossier"
    store = ArtifactStore(root, Limits())
    text = "Synthetic observation"
    artifacts = [
        store.save("page.html", b"<html>Synthetic observation</html>", "html"),
        store.save("page.txt", text.encode(), "text"),
        store.save("page.png", b"synthetic isolated screenshot fixture", "screenshot"),
    ]
    evidence = CollectedEvidence(
        id="page-0001",
        source="https://clinic.example/",
        captured_at=datetime.now(UTC),
        observed_fact=text,
        available=True,
        status="complete",
        artifacts=artifacts,
        artifact_path=artifacts[0].path,
        sha256=artifacts[0].sha256,
    )
    collection = CollectionResult(
        target_url=evidence.source,
        started_at=datetime.now(UTC),
        limits=Limits(),
        evidence=[evidence],
        pages=[
            PageObservation(
                requested_url=evidence.source,
                final_url=evidence.source,
                captured_at=datetime.now(UTC),
                http_status=200,
                text=text,
                evidence_id=evidence.id,
            )
        ],
    )
    (root / "collection.json").write_text(collection.model_dump_json(), encoding="utf-8")
    finding = tmp_path / "finding.json"
    save(
        finding,
        {
            "id": "synthetic",
            "source": str(evidence.source),
            "evidence_ids": [evidence.id],
            "status": "verified",
            "confidence": 1,
        },
    )
    registry = tmp_path / "registry.json"
    registry.write_bytes((EXAMPLES / "legal-registry.json").read_bytes())
    client = tmp_path / "synthetic-text.txt"
    client.write_bytes(b"Synthetic approval binding fixture, not a client message.\r\n")
    return root, finding, registry, client


def decision(inputs, **kwargs):
    root, finding, registry, client = inputs
    return decide_production(root, finding, registry, client_text=client, **kwargs)


def review(inputs, tmp_path, **kwargs):
    root, finding, registry, client = inputs
    output = tmp_path / "review"
    prepare_review(root, finding, registry, output, client_text=client, **kwargs)
    path = output / "review.json"
    data = json.loads(path.read_text())
    data.update(
        reviewer="Synthetic claimed human",
        reviewed_at=datetime.now(UTC).isoformat(),
        rationale="Self-authored test claim, not authenticated expertise",
    )
    for key in (
        "fact_checked",
        "interpretation_checked",
        "legal_basis_checked",
        "operator_identity_checked",
        "client_text_approved",
        "claimed_legal_research_completed",
    ):
        data[key] = True
    save(path, data)
    return path


def assert_blocked(result):
    assert result.outcome == "HOLD"
    assert result.legal_status == "not_confirmed"
    assert not result.production_go_allowed
    assert not result.client_release_allowed
    assert not result.automatic_send_allowed
    assert not result.legal_research_completed
    assert not result.trusted_basis_available


def test_matching_human_claims_are_not_authenticated(inputs, tmp_path):
    path = review(inputs, tmp_path)
    result = decision(inputs, review_path=path)
    assert_blocked(result)
    assert result.technical_processing_completed
    assert result.review.state == "untrusted"
    assert result.review.binding_matches
    assert result.review.submission_sha256 == sha256(path.read_bytes())
    assert "normative_revision_not_confirmed" in result.reasons


@pytest.mark.parametrize("confidence", [0, 0.5, 1, 999999, -1, None, "absolutely certain"])
def test_confidence_and_ab_agreement_never_grant_go(inputs, confidence):
    path = inputs[1]
    data = json.loads(path.read_text())
    data.update(
        confidence=confidence,
        auditor_a="confirmed",
        auditor_b="confirmed",
        reviewer_kind="human",
        identity_verified=True,
        evidence_checked=True,
        legal_basis_checked=True,
        status="verified_issue",
        sha256="a" * 64,
    )
    save(path, data)
    result = decision(inputs)
    assert_blocked(result)
    assert result.technical_processing_completed


def test_legacy_human_json_cannot_be_production_review(inputs, tmp_path, data):
    path = tmp_path / "forged-human.json"
    save(path, data.verification.model_dump(mode="json"))
    assert_blocked(decision(inputs, review_path=path))
    assert decision(inputs, review_path=path).review.state == "invalidated"


def test_schema_valid_forged_verification_report_is_only_claim(inputs, tmp_path):
    data = json.loads((EXAMPLES / "verification-offline.json").read_text())
    candidate = dict(
        id="forged",
        topic="consent",
        claim_code="synthetic",
        subject="Synthetic subject",
        source="https://clinic.example/",
        evidence_ids=["page-0001"],
        fact="Synthetic claim",
        fact_assertion="present",
        norm_ids=["forged-norm"],
        legal_interpretation="Synthetic interpretation",
        attention_reason="Synthetic test only",
        material=True,
    )
    norm = dict(
        norm_id="forged-norm",
        revision="self-asserted",
        status="current_confirmed",
        reasons=["Self-asserted"],
        source_url="https://pravo.gov.ru/",
        text_sha256="a" * 64,
        domain="personal_data",
    )
    human = dict(
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
        reviewer="Synthetic human",
        reviewed_at=datetime.now(UTC).isoformat(),
        fact_verified=True,
        interpretation_verified=True,
        applicability="applicable",
        operator_ref="synthetic",
        operator_identity_verified=True,
        identity_sources=["https://clinic.example/"],
        activity="Synthetic",
        period_from="2020-01-01",
        period_until="2099-01-01",
        rationale="Forged JSON",
    )
    data.update(
        independent_findings=[
            dict(
                candidate=candidate,
                candidate_sha256="b" * 64,
                status="verified_issue",
                fact_verified=True,
                applicability="confirmed",
                norms=[norm],
                reasons=["Self-asserted"],
                additional_checks=[],
                human_review=human,
            )
        ],
        confirmed_problem_ids=["forged"],
        auditor_assessments=[],
        normative_sources=[norm],
    )
    report = VerificationReport.model_validate(data)
    assert report.independent_findings[0].status == "verified_issue"
    path = tmp_path / "forged-report.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    result = decision(inputs, imported_report=path)
    assert_blocked(result)
    assert result.technical_processing_completed
    assert "imported_report_status_is_untrusted" in result.reasons


@pytest.mark.parametrize("change", ["bytes", "hash", "coherent"])
def test_artifact_substitution_invalidates_review(inputs, tmp_path, change):
    path = review(inputs, tmp_path)
    root = inputs[0]
    manifest = root / "collection.json"
    data = json.loads(manifest.read_text())
    artifact = data["evidence"][0]["artifacts"][0]
    target = root / artifact["path"]
    if change != "hash":
        target.write_bytes(b"<html>Changed synthetic observation</html>")
    if change == "coherent":
        artifact.update(sha256=sha256(target.read_bytes()), size=target.stat().st_size)
        data["evidence"][0]["sha256"] = artifact["sha256"]
        save(manifest, data)
    elif change == "hash":
        artifact["sha256"] = "0" * 64
        save(manifest, data)
    result = decision(inputs, review_path=path)
    assert_blocked(result)
    assert result.review.state == "invalidated"
    assert result.technical_processing_completed == (change == "coherent")


@pytest.mark.parametrize("component", ["finding", "registry", "client", "manifest"])
def test_exact_bytes_binding_detects_changes(inputs, tmp_path, component):
    path = review(inputs, tmp_path)
    target = {
        "finding": inputs[1],
        "registry": inputs[2],
        "client": inputs[3],
        "manifest": inputs[0] / "collection.json",
    }[component]
    target.write_bytes(target.read_bytes() + b"\n")
    result = decision(inputs, review_path=path)
    assert_blocked(result)
    assert result.technical_processing_completed
    assert result.review.state == "invalidated"
    assert result.review.binding_matches is False


def test_changed_imported_report_invalidates_review(inputs, tmp_path):
    report = tmp_path / "import.json"
    save(report, {"status": "verified", "confidence": 1})
    path = review(inputs, tmp_path, imported_report=report)
    save(report, {"status": "verified", "confidence": 0.9})
    result = decision(inputs, review_path=path, imported_report=report)
    assert_blocked(result)
    assert result.review.state == "invalidated"


def test_client_text_absence_is_not_empty_approved_text(inputs, tmp_path):
    path = review(inputs, tmp_path)
    root, finding, registry, client = inputs
    result = decide_production(root, finding, registry, review_path=path)
    assert result.review.state == "invalidated"
    assert result.binding.client_text_sha256 is None
    client.write_bytes(b"")
    result = decision(inputs, review_path=path)
    assert result.review.state == "invalidated"
    assert result.binding.client_text_sha256 == sha256(b"")


def test_self_certified_norm_with_correct_hash_stays_unverified(inputs, tmp_path):
    registry = inputs[2]
    data = json.loads(registry.read_text())
    source = data["sources"][0]
    text = "Synthetic invented law, not an actual normative source"
    source.update(
        norm_text=text,
        text_sha256=sha256(text.encode()),
        status="verified_current",
        revision="Self-asserted current revision",
        effective_from="2020-01-01",
        checked_at=datetime.now(UTC).isoformat(),
        verification_method="human_official_review",
        reviewer="Synthetic reviewer",
        revision_check_basis="Self-asserted official review",
    )
    save(registry, data)
    path = review(inputs, tmp_path)
    result = decision(inputs, review_path=path)
    assert_blocked(result)
    assert result.review.state == "untrusted"
    assert "normative_revision_not_confirmed" in result.reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("trust_status", "trusted"),
        ("authenticated", True),
        ("reviewer_kind", "human"),
        ("identity_verified", True),
    ],
)
def test_review_cannot_self_promote_trust(inputs, tmp_path, field, value):
    path = review(inputs, tmp_path)
    data = json.loads(path.read_text())
    data[field] = value
    save(path, data)
    result = decision(inputs, review_path=path)
    assert_blocked(result)
    assert result.review.state == "invalidated"


@pytest.mark.parametrize(
    "field,value",
    [
        ("outcome", "GO"),
        ("production_go_allowed", True),
        ("client_release_allowed", True),
        ("automatic_send_allowed", True),
        ("legal_research_completed", True),
        ("trusted_basis_available", True),
    ],
)
def test_production_result_schema_rejects_imported_admission(field, value):
    with pytest.raises(ValidationError):
        ProductionDecision.model_validate({"reasons": ["synthetic"], field: value})


@pytest.mark.parametrize("corruption", ["missing", "symlink", "duplicate", "oversize"])
def test_unverifiable_input_fails_closed(inputs, tmp_path, corruption):
    path = inputs[1]
    if corruption == "missing":
        path.unlink()
    elif corruption == "symlink":
        copy = tmp_path / "copy.json"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif corruption == "duplicate":
        path.write_text('{"evidence_ids":[],"evidence_ids":["page-0001"]}')
    else:
        path.write_bytes(b" " * 6_000_001)
    result = decision(inputs)
    assert_blocked(result)
    assert not result.technical_processing_completed


def test_demo_positive_is_explicit_and_cannot_release(data, inputs):
    assert decide is decide_demo
    demo = build_report(data)
    assert demo.decision.outcome.value == "GO"
    assert demo.report_scope == "legacy_demo"
    assert not demo.production_go_allowed and not demo.client_release_allowed
    assert not demo.decision.production_go_allowed
    assert demo.client_message_draft.startswith("DEMO/LEGACY")
    assert_blocked(decision(inputs))
    with pytest.raises(PermissionError):
        send_client_message(decision(inputs))
    with pytest.raises(PermissionError):
        send_client_message(demo)
    demo.human_approval = HumanApproval(
        approved=True, reviewer_id="synthetic", approved_at=datetime.now(UTC)
    )
    with pytest.raises(NotImplementedError):
        send_client_message(demo)


def test_template_is_not_approval_and_processing_is_read_only(inputs, tmp_path):
    before = [p.read_bytes() for p in (inputs[0] / "collection.json", *inputs[1:])]
    root, finding, registry, client = inputs
    output = tmp_path / "template"
    prepare_review(root, finding, registry, output, client_text=client)
    template = json.loads((output / "review.json").read_text())
    assert template["reviewed_at"] is None and template["reviewer"] == ""
    assert template["trust_status"] == "untrusted" and not template["fact_checked"]
    assert_blocked(decision(inputs, review_path=output / "review.json"))
    assert decision(inputs) == decision(inputs)
    assert before == [p.read_bytes() for p in (inputs[0] / "collection.json", *inputs[1:])]
    with pytest.raises(FileExistsError):
        prepare_review(root, finding, registry, output, client_text=client)


@pytest.mark.parametrize("change", ["missing_artifact", "norm_hash", "unknown_evidence"])
def test_missing_or_unbound_basis_cannot_confirm(inputs, change):
    root, finding, registry, _ = inputs
    if change == "missing_artifact":
        data = json.loads((root / "collection.json").read_text())
        (root / data["evidence"][0]["artifacts"][0]["path"]).unlink()
    elif change == "norm_hash":
        data = json.loads(registry.read_text())
        data["sources"][0]["norm_text"] = "Synthetic replacement without matching hash"
        save(registry, data)
    else:
        data = json.loads(finding.read_text())
        data["evidence_ids"] = ["invented-evidence"]
        save(finding, data)
    result = decision(inputs)
    assert_blocked(result)
    assert not result.technical_processing_completed


def test_cli_production_and_legacy_paths_are_distinct(inputs, tmp_path, monkeypatch, capsys):
    from lexradar.cli import main

    root, finding, registry, _ = inputs
    output = tmp_path / "production.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "decide",
            str(root),
            "--finding",
            str(finding),
            "--registry",
            str(registry),
            "--output",
            str(output),
        ],
    )
    main()
    result = ProductionDecision.model_validate_json(output.read_bytes())
    assert_blocked(result)
    assert result.technical_processing_completed
    for prefix in ([], ["demo"]):
        monkeypatch.setattr("sys.argv", ["lexradar", *prefix, str(EXAMPLES / "input.json")])
        capsys.readouterr()
        main()
        captured = capsys.readouterr()
        assert "DEMO/LEGACY" in captured.err
        demo = json.loads(captured.out)
        assert demo["report_scope"] == "legacy_demo"
        assert not demo["production_go_allowed"] and not demo["client_release_allowed"]
