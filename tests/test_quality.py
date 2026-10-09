"""Reference arithmetic, complete local pipeline, failure gates and pilot permissions."""

import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from lexradar.auditors.budget import Budget, BudgetExceeded
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.models import AnalysisReport, ModelSettings
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.preflight import authorize
from lexradar.auditors.provider import OpenRouterTransport
from lexradar.collector.network import CollectionError, Fetcher, origin
from lexradar.quality.cli import quality_main
from lexradar.quality.evaluator import aggregate, match, measure, ratio
from lexradar.quality.fixtures import FixtureFetcher, ReplayProvider
from lexradar.quality.models import (
    ExpertLabel,
    Metric,
    Prediction,
    Reference,
)
from lexradar.quality.models import (
    TestReport as QualityReport,
)
from lexradar.quality.pilot import PilotConfig, TargetApproval, pilot
from lexradar.quality.report import evidence_link, labels_for, load_report, write_html
from lexradar.quality.runner import benchmark
from lexradar.verifier.cli import default_config
from lexradar.verifier.models import VerificationReport
from lexradar.verifier.orchestrator import start
from lexradar.verifier.registry import digest

SUITE = Path(__file__).parent / "benchmarks"
CASES = sorted(p.parent.name for p in SUITE.glob("*/reference.json"))


def forbidden(*args, **kwargs):
    raise AssertionError("External transport must not be used by an offline benchmark")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("quality") / "run"
    # Guard both public collection and paid API transport while exercising real Playwright.
    from unittest.mock import patch

    with (
        patch.object(Fetcher, "get", forbidden),
        patch.object(OpenRouterTransport, "post", forbidden),
    ):
        report = benchmark(SUITE, root)
    return root, report


@pytest.mark.parametrize("case_id", CASES)
def test_reference_scenarios(run, case_id):
    root, report = run
    case = next(c for c in report.cases if c.id == case_id)
    assert case.checks and all(c.passed for c in case.checks)
    assert case.models == ["offline/a", "offline/b", "offline/verifier"]
    assert len(case.prompts) == 4
    assert case.api_cost_usd == "0"
    assert all(p.status != "verified_issue" for p in case.predictions)
    assert not report.automatic_go_allowed and not report.automatic_send_allowed
    actual = VerificationReport.model_validate_json(
        (root / "cases" / case_id / "verification/verification.json").read_text()
    )
    assert not actual.confirmed_problem_ids
    assert all(n.status == "unverified" for n in actual.normative_sources)
    for paths in case.evidence_links.values():
        for path in paths:
            assert (root / path).is_file()


def test_suite_totals_and_intentional_quality_errors(run):
    _, report = run
    assert len(report.cases) == 19
    assert report.overall_status == "manual_review_required"
    expected = {
        "precision": (4, 7),
        "recall": (4, 6),
        "false_positive_rate": (3, 13),
        "false_negative_rate": (2, 6),
        "factual_detection": (5, 6),
        "evidence_coverage": (22, 23),
        "normative_verification_coverage": (0, 19),
        "operator_identification_accuracy": (0, 1),
        "applicability_accuracy": (0, 1),
        "completeness_of_material_review": (21, 24),
        "disagreement_resolution_rate": (0, 1),
        "confirmed_legal_recall": (0, 6),
    }
    for name, (n, d) in expected.items():
        assert report.metrics[name] == ratio(n, d)
    missed = next(c for c in report.cases if c.id == "13-both-miss")
    assert missed.found == ["13-both-miss-unit"]
    assert next(c for c in report.cases if c.id == "17-prices").missed
    assert next(c for c in report.cases if c.id == "08-wrong-operator").false_positives


def test_partial_failure_retains_original_html_and_text(run):
    root, _ = run
    packet = load_packet(root / "cases/14-screenshot-error/dossier", 2_000_000)
    e = packet.data.evidence[0]
    assert e.available and e.status == "partial" and e.unavailable_reason is None
    assert {a.representation for a in e.artifacts} == {"original_html", "extracted_text"}
    assert any("screenshot" in error for error in e.collection_errors)


def test_mixed_pdf_not_whole_document(run):
    root, _ = run
    r = VerificationReport.model_validate_json(
        (root / "cases/11-mixed-pdf/verification/verification.json").read_text()
    )
    m = next(m for m in r.materials if m.type == "pdf")
    assert m.page_count == 2 and m.examined_page_numbers == [1]
    assert m.examination == "partial_text" and m.visual_review_required
    assert r.independent_findings[0].status == "insufficient_evidence"
    assert any("unread" in reason for reason in r.independent_findings[0].reasons)


def test_independence_and_prompt_injection(run):
    root, _ = run
    folder = root / "cases/15-injection"
    ab = AnalysisReport.model_validate_json((folder / "auditors/analysis.json").read_text())
    stage = json.loads((folder / "stage-one/stage-one.json").read_text())
    raw = (folder / "stage-one/independent.json").read_text()
    assert stage["result_sha256"] == digest(
        json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert [r.auditor for r in ab.runs] == ["A", "B"]
    assert ab.runs[0].model != ab.runs[1].model
    assert all(not log.request_sent for log in ab.logs)
    assert not ab.automatic_go_allowed


def reference(units=None):
    ref = Reference.model_validate_json((SUITE / "13-both-miss/reference.json").read_text())
    if units is not None:
        ref.units = units
    return ref


def prediction(**changes):
    unit = reference().units[0]
    return Prediction.model_validate(
        {
            "id": "actual",
            "topic": unit.topic,
            "claim_code": unit.claim_code,
            "evidence_ids": unit.evidence_ids,
            "asserted": True,
            "status": "potential_issue",
            "fact_grounded": True,
            "norm_verified": False,
            "applicability": "unestablished",
            "reasons": ["Exact synthetic excerpt."],
            **changes,
        }
    )


@pytest.mark.parametrize("n,d", [(0, 0), (0, 3), (1, 3), (3, 3)])
def test_metric_arithmetic(n, d):
    r = ratio(n, d)
    assert r.value == (n / d if d else None)
    assert r.status == ("measured" if d else "not_applicable")


def test_invalid_metric_is_rejected():
    with pytest.raises(ValidationError):
        Metric(numerator=1, denominator=2, value=0.9, status="measured")
    with pytest.raises(ValidationError):
        ratio(2, 1)


def test_classification_hand_calculation():
    ref = reference()
    pos = ref.units[0]
    units = [
        pos.model_copy(update={"id": "p1", "claim_code": "p1"}),
        pos.model_copy(update={"id": "p2", "claim_code": "p2"}),
        pos.model_copy(update={"id": "n1", "claim_code": "n1", "expected_problem": False}),
        pos.model_copy(update={"id": "n2", "claim_code": "n2", "expected_problem": False}),
    ]
    ref.units = units
    preds = [prediction(id="f1", claim_code="p1"), prediction(id="f2", claim_code="n1")]
    _, _, found, missed, fp, counts, metrics = measure(ref, preds)
    assert counts == {"tp": 1, "tn": 1, "fp": 1, "fn": 1, "extra_fp": 0}
    assert found == ["p1"] and missed == ["p2"] and len(fp) == 1
    for name in ("precision", "recall", "false_positive_rate", "false_negative_rate"):
        assert metrics[name].value == 0.5


def test_ambiguous_matches_do_not_earn_true_positive():
    ref = reference()
    matches, manual, _ = match(ref, [prediction(id="a"), prediction(id="b")])
    assert not matches and manual == ["a", "b"]
    result = measure(ref, [prediction(id="a"), prediction(id="b")])
    assert result[5]["tp"] == 0 and result[5]["fn"] == 1
    # Factual grounding is measured independently of disputed legal classification.
    assert result[6]["factual_detection"].value == 1


def test_ambiguous_reference_labels_are_manual():
    ref = reference()
    ref.units = [ref.units[0], ref.units[0].model_copy(update={"id": "other"})]
    assert match(ref, [prediction()]) == ({}, ["actual"], [])


def test_same_text_or_code_wrong_evidence_cannot_match():
    ref = reference()
    matches, _, unmatched = match(ref, [prediction(evidence_ids=["other-evidence"])])
    assert not matches and unmatched == ["actual"]
    result = measure(ref, [prediction(evidence_ids=["other-evidence"])])
    assert result[5]["extra_fp"] == 1 and result[6]["precision"].value == 0
    assert result[6]["false_positive_rate"].status == "not_applicable"


def test_normalized_codes_exact_evidence_match():
    matches, _, _ = match(
        reference(), [prediction(claim_code=" UNLIMITED_RETENTION ", topic="PRIVACY_POLICY")]
    )
    assert list(matches) == ["13-both-miss-unit"]


def test_empty_universe_and_aggregate():
    ref = reference([])
    result = measure(ref, [])
    assert all(m.status == "not_applicable" for m in result[6].values())
    assert aggregate([]) == {
        name: ratio(0, 0)
        for name in ("precision", "recall", "false_positive_rate", "false_negative_rate")
    }


def replay_suite(tmp_path, run, case_id="01-correct-policy"):
    destination = tmp_path / "suite" / case_id
    destination.mkdir(parents=True)
    for name in ("reference.json", "registry.json", "responses.json"):
        shutil.copyfile(SUITE / case_id / name, destination / name)
    shutil.copytree(run[0] / "cases" / case_id / "dossier", destination / "dossier")
    return destination.parent, destination


def test_offline_replay_reuses_saved_dossier_without_collector(run, tmp_path, monkeypatch):
    suite, _ = replay_suite(tmp_path, run)
    monkeypatch.setattr("lexradar.quality.runner.collect", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)
    out = tmp_path / "out"
    result = benchmark(suite, out, mode="replay")
    assert result.overall_status == "passed" and result.cases[0].mode == "replay"
    assert result.cases[0].dossier_sha256 == run[1].cases[0].dossier_sha256
    assert result.cases[0].evidence_links


@pytest.mark.parametrize("fault", ["missing", "corrupt", "mandatory_missing", "symlink"])
def test_replay_missing_or_corrupt_artifact_is_failed(run, tmp_path, fault):
    suite, folder = replay_suite(tmp_path, run)
    path = folder / "dossier/artifacts/page-0001.html"
    if fault == "corrupt":
        path.write_text("Changed public evidence")
    elif fault == "mandatory_missing":
        manifest = folder / "dossier/collection.json"
        data = json.loads(manifest.read_text())
        data["evidence"][0]["artifacts"] = [
            a for a in data["evidence"][0]["artifacts"] if a["kind"] != "screenshot"
        ]
        manifest.write_text(json.dumps(data))
    else:
        path.unlink()
        if fault == "symlink":
            path.symlink_to(folder / "responses.json")
    result = benchmark(suite, tmp_path / "out", mode="replay")
    assert result.overall_status == "failed"
    assert not result.cases[0].predictions
    assert any(not c.passed for c in result.cases[0].checks)


@pytest.mark.parametrize(
    "stage,fault",
    [
        ("A", "provider_error"),
        ("B", "provider_error"),
        ("independent", "provider_error"),
        ("comparison", "provider_error"),
        ("A", "unknown_evidence"),
        ("independent", "unknown_evidence"),
        ("independent", "injection"),
        ("comparison", "injection"),
    ],
)
def test_provider_and_injection_errors_cannot_succeed(run, tmp_path, stage, fault):
    suite, folder = replay_suite(tmp_path, run)
    path = folder / "responses.json"
    data = json.loads(path.read_text())
    if fault == "provider_error":
        data[stage] = {"provider_error": "http_503"}
    elif fault == "unknown_evidence":
        data[stage]["findings"][0]["evidence_ids"] = ["not-in-dossier"]
    else:
        data[stage]["automatic_go_allowed"] = True
    path.write_text(json.dumps(data))
    result = benchmark(suite, tmp_path / "out", mode="replay")
    assert result.overall_status == "failed"
    assert not result.automatic_go_allowed
    assert any(not c.passed for c in result.cases[0].checks)


def test_preflight_permission_and_changed_packet_cannot_send(run, tmp_path):
    root = run[0] / "cases/01-correct-policy/dossier"
    packet = load_packet(root, 2_000_000)
    with pytest.raises(PermissionError):
        authorize(packet, None)
    path = tmp_path / "approval.json"
    path.write_text(
        json.dumps(
            {
                "packet_sha256": "a" * 64,
                "approved": True,
                "contents_reviewed": True,
                "reviewer": "synthetic-reviewer",
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    with pytest.raises(PermissionError):
        authorize(packet, path)
    provider = ReplayProvider(json.loads((SUITE / "01-correct-policy/responses.json").read_text()))
    config = default_config()
    with pytest.raises(PermissionError):
        start(
            root,
            SUITE / "01-correct-policy/registry.json",
            tmp_path / "stage",
            config,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=path,
            provider=provider,
        )
    assert provider.calls == []
    with pytest.raises(PermissionError):
        analyze(root, tmp_path / "ab", config, mode="openrouter", provider=provider)


def test_budget_reservation_stops_before_transport():
    budget = Budget(Decimal("0.000001"))
    settings = ModelSettings(
        model="test/model", prompt_price_cap=100, completion_price_cap=100, max_output_tokens=2000
    )
    with pytest.raises(BudgetExceeded):
        budget.reserve(settings, [{"role": "user", "content": "synthetic"}])


def test_fixture_transport_cannot_reach_other_origins():
    fetcher = FixtureFetcher({"routes": {"/": {"html": "synthetic"}}})
    for url in ("http://127.0.0.1/", "https://other.example/", "https://benchmark.example:444/"):
        with pytest.raises(CollectionError):
            fetcher.get(url, origin(url))
    assert not fetcher.calls


def pilot_config():
    c = PilotConfig(enabled=True, target_url="https://benchmark.example/")
    c.approval = TargetApproval(
        config_sha256=c.binding_sha256(),
        reviewer="synthetic-reviewer",
        reviewed_at=datetime.now(UTC),
        public_collection_approved=True,
    )
    return c


@pytest.mark.parametrize(
    "fault",
    [
        "default_disabled",
        "flag_missing",
        "approval_missing",
        "changed_target",
        "changed_budget",
        "general_flag_only",
    ],
)
def test_pilot_requires_specific_permissions_and_journals(tmp_path, monkeypatch, fault):
    monkeypatch.setattr("lexradar.quality.pilot.collect", forbidden)
    c = pilot_config()
    allow = True
    if fault == "default_disabled":
        c = PilotConfig(target_url="https://benchmark.example/")
    elif fault == "flag_missing":
        allow = False
    elif fault in {"approval_missing", "general_flag_only"}:
        c.approval = None
    elif fault == "changed_target":
        c.target_url = "https://other.example/"
    elif fault == "changed_budget":
        c.max_api_budget_usd = "0.5"
    out = tmp_path / "pilot"
    with pytest.raises(PermissionError):
        pilot(c, out, allow_public_collection=allow)
    events = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events] == ["requested", "permission_denied"]
    assert all(not e["external_llm_transfer"] for e in events)


def test_authorized_pilot_only_collects_with_existing_limits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("lexradar.quality.pilot.collect", lambda *args: calls.append(args))
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)
    c = pilot_config()
    out = tmp_path / "pilot"
    pilot(c, out, allow_public_collection=True)
    assert len(calls) == 1 and calls[0][0] == str(c.target_url) and calls[0][2] == c.limits
    assert "SHA-256" in (out / "next-steps.txt").read_text()
    assert "collection_finished_requires_review" in (out / "runs.jsonl").read_text()


def test_pilot_limits_and_error_journal(tmp_path, monkeypatch):
    c = pilot_config()
    c.limits.max_pages = 11
    c.approval.config_sha256 = c.binding_sha256()
    with pytest.raises(ValueError):
        pilot(c, tmp_path / "limits", allow_public_collection=True)
    c.limits.max_pages = 5
    c.approval.config_sha256 = c.binding_sha256()

    def failure(*args):
        raise CollectionError("Synthetic SSRF denial")

    monkeypatch.setattr("lexradar.quality.pilot.collect", failure)
    with pytest.raises(CollectionError):
        pilot(c, tmp_path / "failure", allow_public_collection=True)
    assert "collection_failed" in (tmp_path / "failure/runs.jsonl").read_text()


def test_expert_annotations_exact_report_and_dossier_binding(run, tmp_path):
    root, report = run
    case = next(c for c in report.cases if c.id == "08-wrong-operator")
    _, sha = load_report(root)
    label = ExpertLabel(
        report_sha256=sha,
        dossier_sha256=case.dossier_sha256,
        case_id=case.id,
        finding_id=case.predictions[0].id,
        reviewer="Synthetic expert",
        reviewed_at=datetime.now(UTC),
        status="unrelated_operator",
        rationale="Reference identifies other entity.",
    )
    path = tmp_path / "expert.json"
    path.write_text(json.dumps([label.model_dump(mode="json")]))
    assert labels_for(root, path) == [label]
    assert not label.authenticated
    label.dossier_sha256 = "a" * 64
    path.write_text(json.dumps([label.model_dump(mode="json")]))
    with pytest.raises(ValueError):
        labels_for(root, path)
    label.dossier_sha256 = case.dossier_sha256
    label.report_sha256 = "b" * 64
    path.write_text(json.dumps([label.model_dump(mode="json")]))
    with pytest.raises(ValueError):
        labels_for(root, path)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/",
        "javascript:alert(1)",
        "cases/a/../../secret",
        "/etc/passwd",
        "cases/a\\evil",
    ],
)
def test_html_rejects_unsafe_links(path):
    with pytest.raises(ValueError):
        evidence_link(path)


def test_report_html_escapes_untrusted_strings(run, tmp_path):
    root, r = run
    report = r.model_copy(deep=True)
    report.limitations.append('<script>alert("test")</script>')
    report.cases[0].manual_review.append("<img src=x onerror=alert(1)>")
    (tmp_path / "test-report.json").write_text(report.model_dump_json())
    path = write_html(tmp_path)
    text = path.read_text()
    assert "<script>" not in text and "<img src=" not in text
    assert "&lt;script&gt;" in text and "Content-Security-Policy" in text
    assert "not_applicable" in text or r.metrics
    assert "JavaScript" in text
    assert (root / "test-report.html").is_file()


def test_cli_and_backcompat_schemas(run, capsys, tmp_path):
    root, _ = run
    quality_main("quality-report", [str(root), "--format", "html"])
    assert "test-report.html" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        quality_main(
            "benchmark", ["--suite", str(tmp_path / "missing"), "--output", str(tmp_path / "out")]
        )
    assert exc.value.code == 2
    QualityReport.model_validate_json((root / "test-report.json").read_text())
    # No modifications to existing v0.1-v0.4 schemas; full previous regression suite also runs.
    from lexradar.models import AuditInput
    from lexradar.report import build_report

    assert build_report(AuditInput.model_validate_json(Path("examples/input.json").read_text()))


def test_report_schema_forbids_go_and_sending(run):
    data = run[1].model_dump(mode="json")
    for field in (
        "automatic_go_allowed",
        "automatic_send_allowed",
        "full_legal_compliance_established",
    ):
        bad = {**data, field: True}
        with pytest.raises(ValidationError):
            QualityReport.model_validate(bad)


def test_replay_preserves_recording_model_metadata(run, tmp_path):
    suite, folder = replay_suite(tmp_path, run)
    metadata = {
        "models": {"A": "recorded/model-a", "B": "recorded/model-b"},
        "prompts": {"A": "recorded-v1"},
        "original_api_cost_usd": "0.012",
    }
    (folder / "metadata.json").write_text(json.dumps(metadata))
    r = benchmark(suite, tmp_path / "out", mode="replay")
    assert r.cases[0].recording_metadata.models == metadata["models"]
    assert r.cases[0].recording_metadata.original_api_cost_usd == Decimal("0.012")
    assert r.cases[0].recording_metadata_sha256
    assert r.cases[0].api_cost_usd == "0"
    assert "recorded/model-a" in (tmp_path / "out/test-report.html").read_text()


def test_sensitive_packet_blocked_even_with_exact_human_approval(run, tmp_path):
    from dataclasses import replace

    packet = load_packet(run[0] / "cases/19-special-data/dossier", 2_000_000)
    assert "Diagnosis" in packet.payload
    # Empty synthetic health form alone causes conservative screening, never a guarantee of absence.
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "packet_sha256": packet.sha256,
                "approved": True,
                "contents_reviewed": True,
                "reviewer": "synthetic-reviewer",
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    with pytest.raises(PermissionError, match="sensitive"):
        authorize(packet, approval)
    changed = replace(packet, payload=packet.payload + " ", sha256=digest(packet.payload + " "))
    with pytest.raises(PermissionError):
        authorize(changed, approval)


def test_quality_report_rejects_forged_success_or_metrics(run):
    data = run[1].model_dump(mode="json")
    data["overall_status"] = "passed"
    with pytest.raises(ValidationError):
        QualityReport.model_validate(data)
    data = run[1].model_dump(mode="json")
    data["metrics"]["precision"] = ratio(1, 1).model_dump()
    with pytest.raises(ValidationError):
        QualityReport.model_validate(data)


def test_report_hash_uses_exact_bytes_including_crlf(run, tmp_path):
    from hashlib import sha256

    content = (run[0] / "test-report.json").read_bytes().replace(b"\n", b"\r\n")
    (tmp_path / "test-report.json").write_bytes(content)
    _, actual = load_report(tmp_path)
    assert actual == sha256(content).hexdigest()
