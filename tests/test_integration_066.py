"""All providers in these tests are local; credentials and network are forbidden."""

import json
import socket
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from lexradar.auditors.budget import BudgetExceeded
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.models import AnalysisReport, Usage
from lexradar.auditors.provider import OpenRouterTransport, ProviderError, ProviderReply
from lexradar.batch_paid_guard import CappedProvider
from lexradar.batch_qualification import analysis_digest, qualify
from lexradar.evidence_review import review
from lexradar.mvp_batch import run
from lexradar.mvp_demo import create_inputs
from lexradar.saved_audit import verify
from lexradar.single_auditor import MODEL, profile


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("External requests and credentials are forbidden")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)


@pytest.fixture
def real():
    dossier = Path("runs-054/doktstom-improved/dossier")
    source = Path("runs-064/doktstom-deepseek-chat-001/analysis.json")
    assert dossier.exists() and source.exists(), "Required real saved acceptance artifacts missing"
    return load_packet(dossier, 200000), AnalysisReport.model_validate_json(source.read_bytes())


def test_real_csv_to_hold_and_resume(tmp_path, real, monkeypatch):
    packet, analysis = real
    decision = verify(tmp_path / "real")
    assert analysis.completed and decision["outcome"] == "HOLD"
    output = tmp_path / "real/doktstom-real"
    assert not (output / "proposal.html").exists()
    reviewed = json.loads((output / "evidence-review.json").read_text())
    assert reviewed["packet_sha256"] == packet.sha256
    assert [f["assessment"] for f in reviewed["findings"]] == ["PARTIAL", "PARTIAL"]
    assert all(q["exact_match"] for f in reviewed["findings"] for q in f["quotes"])
    assert "HOLD" in (output / "report.html").read_text()
    manifest = run(
        tmp_path / "real/saved-input.csv",
        tmp_path / "real/real-batch",
        mode="saved",
        max_sites=1,
        resume=True,
    )
    row = manifest["rows"][0]
    assert row["cost_usd"] == row["budget_charge_usd"] == "0"
    assert row["historical_cost_usd"] == "0.0043350435"
    assert not manifest["DEMO"] and not manifest["automatic_send_allowed"]


def test_real_hash_and_second_auditor_rejected(tmp_path, real):
    packet, analysis = real
    analysis.evidence_packet_sha256 = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        review(analysis, packet, tmp_path)
    analysis.evidence_packet_sha256 = packet.sha256
    analysis.runs[0].model = "qwen/qwen-plus"
    with pytest.raises(ValueError, match="Single DeepSeek A"):
        review(analysis, packet, tmp_path)


def test_csv_five_rows_html_and_no_repeated_analysis(tmp_path, monkeypatch):
    import lexradar.mvp_batch as batch

    source = create_inputs(tmp_path / "inputs")
    root = tmp_path / "batch"
    state = run(source, root)
    assert [r.get("qualification") for r in state["rows"][:4]] == ["GO", "GO", "HOLD", "HOLD"]
    assert state["rows"][4]["status"] == "SKIPPED"
    assert state["rows"][3]["status"] == "ERROR"
    for row in state["rows"]:
        if row.get("qualification") != "GO":
            assert not row.get("proposal")

    def forbidden(*args, **kwargs):
        pytest.fail("Completed rows must not run again")

    monkeypatch.setattr(batch, "analyze", forbidden)
    assert run(source, root, resume=True)["rows"] == state["rows"]
    assert len(list(root.rglob("proposal.html"))) == 2
    assert len(list(root.rglob("evidence-review.json"))) == 3


@pytest.mark.parametrize("budget", ["NaN", "Infinity", "-1", "0", "0.51", "no"])
def test_invalid_total_budget_before_output(tmp_path, budget):
    with pytest.raises(ValueError):
        run(tmp_path / "absent.csv", tmp_path / "output", max_total_budget_usd=budget)
    assert not (tmp_path / "output").exists()


def test_call_cap_and_durable_reservation(tmp_path):
    calls = []

    class Local:
        def complete(self, settings, messages):
            record = json.loads((tmp_path / "paid-attempt.json").read_text())
            assert record["state"] == "request_may_have_been_sent"
            calls.append(settings.model)
            raise ProviderError("offline simulated timeout", retryable=True)

    provider = CappedProvider(Local(), tmp_path, 1, "0.50")
    with pytest.raises(ProviderError, match="simulated timeout"):
        provider.complete(profile().auditor_a, [{"content": "test"}])
    with pytest.raises(ProviderError, match="max_ai_calls"):
        provider.complete(profile().auditor_a, [{"content": "test"}])
    assert calls == [MODEL]
    assert provider.budget.charged > 0


def test_budget_stops_before_local_provider(tmp_path):
    class Local:
        def complete(self, settings, messages):
            pytest.fail("Budget must stop before provider")

    provider = CappedProvider(Local(), tmp_path, 2, "0.000001")
    with pytest.raises(BudgetExceeded):
        provider.complete(profile().auditor_a, [{"content": "test"}])
    assert not (tmp_path / "paid-attempt.json").exists()


def test_legal_review_binding_and_production_go(tmp_path):
    inputs = create_inputs(tmp_path / "inputs")
    state = run(inputs, tmp_path / "offline")
    site = tmp_path / "offline" / state["rows"][0]["directory"]
    packet = load_packet(site / "dossier", 200000)
    analysis = AnalysisReport.model_validate_json((site / "analysis/analysis.json").read_bytes())
    assert qualify(analysis, packet, production=True) == ([], ["independent_legal_review_required"])
    path = tmp_path / "local-review.json"
    data = dict(
        packet_sha256=packet.sha256,
        analysis_sha256=analysis_digest(analysis),
        reviewer="DEMO reviewer",
        reviewed_at=datetime.now(UTC).isoformat(),
        findings=[
            dict(
                finding_id="demo-f1",
                factual_defect_verified=True,
                legal_applicability_verified=True,
                serviceable=True,
                reason="DEMO: only a synthetic legal review fixture, no real legal conclusion",
                verified_legal_sources=["DEMO local test source"],
            )
        ],
    )
    path.write_text(json.dumps(data))
    assert qualify(analysis, packet, production=True, legal_review=path) == (["demo-f1"], [])
    data["packet_sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="binding"):
        qualify(analysis, packet, production=True, legal_review=path)


def test_paid_completed_resume_and_uncertain_attempt(tmp_path, monkeypatch):
    import lexradar.mvp_batch as batch

    inputs = create_inputs(tmp_path / "inputs")
    packet = load_packet(inputs.parent / "dossier-0", 200000)
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="DEMO",
                approved_at=datetime.now(UTC).isoformat(),
            )
        )
    )
    calls = []

    class Local:
        def complete(self, settings, messages):
            calls.append(settings.model)
            return ProviderReply(
                (inputs.parent / "response-0.json").read_text(), Usage(cost_usd=Decimal("0.001"))
            )

    monkeypatch.setattr(batch, "DeepSeekAdapter", Local)
    options = dict(
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    root = tmp_path / "paid"
    state = run(inputs, root, **options)
    assert state["rows"][0]["qualification"] == "HOLD"
    assert run(inputs, root, resume=True, **options)["rows"] == state["rows"]
    assert calls == [MODEL]
    state["rows"][0]["status"] = "pending"
    (root / "manifest.json").write_text(json.dumps(state))
    with pytest.raises(PermissionError, match="uncertain"):
        run(inputs, root, resume=True, **options)
    assert calls == [MODEL]


def test_zero_ai_calls_never_constructs_provider(tmp_path, monkeypatch):
    import lexradar.mvp_batch as batch

    inputs = create_inputs(tmp_path / "inputs")
    packet = load_packet(inputs.parent / "dossier-0", 200000)
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="DEMO",
                approved_at=datetime.now(UTC).isoformat(),
            )
        )
    )

    def forbidden():
        pytest.fail("Do not initialize provider without a call allowance")

    monkeypatch.setattr(batch, "DeepSeekAdapter", forbidden)
    state = run(
        inputs,
        tmp_path / "zero",
        mode="paid",
        max_sites=1,
        max_ai_calls=0,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    assert state["rows"][0]["status"] == "NEEDS_APPROVAL"
    assert not list((tmp_path / "zero").rglob("paid-attempt.json"))


def test_quote_xss_is_escaped_and_evidence_is_bound(tmp_path):
    import hashlib
    import html

    from lexradar.collector.models import CollectionResult
    from lexradar.collector.reading import extract_html

    inputs = create_inputs(tmp_path / "inputs")
    dossier = inputs.parent / "dossier-0"
    manifest = dossier / "collection.json"
    data = CollectionResult.model_validate_json(manifest.read_bytes())
    quote = '<script>alert("quote")</script><img src=x onerror=alert(1)>'
    body = ("<html><body>" + html.escape(quote) + "</body></html>").encode()
    text, reading = extract_html(body, 100000)
    for artifact in data.evidence[0].artifacts:
        if artifact.kind in {"html", "text"}:
            raw = body if artifact.kind == "html" else text.encode()
            (dossier / artifact.path).write_bytes(raw)
            artifact.sha256 = hashlib.sha256(raw).hexdigest()
            artifact.size = len(raw)
    data.evidence[0].sha256 = data.evidence[0].artifacts[0].sha256
    data.pages[0].text, data.pages[0].reading = text, reading
    manifest.write_text(data.model_dump_json())
    response = inputs.parent / "response-0.json"
    result = json.loads(response.read_bytes())
    result["findings"][0]["fact"] = text
    result["findings"][0]["text_grounding"][0]["exact_quote"] = text
    response.write_text(json.dumps(result))
    state = run(inputs, tmp_path / "escaped")
    page = (tmp_path / "escaped" / state["rows"][0]["proposal"]).read_text()
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page
    assert "size:A4" in page and "@media print" in page


def test_saved_production_go_requires_local_review_without_new_spend(tmp_path, monkeypatch):
    import csv

    import lexradar.mvp_batch as batch

    inputs = create_inputs(tmp_path / "inputs")
    packet = load_packet(inputs.parent / "dossier-0", 200000)
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="DEMO",
                approved_at=datetime.now(UTC).isoformat(),
            )
        )
    )
    calls = []

    class Local:
        def complete(self, settings, messages):
            calls.append(settings.model)
            return ProviderReply(
                (inputs.parent / "response-0.json").read_text(), Usage(cost_usd=Decimal("0.001"))
            )

    monkeypatch.setattr(batch, "DeepSeekAdapter", Local)
    paid = run(
        inputs,
        tmp_path / "local-paid",
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    site = tmp_path / "local-paid" / paid["rows"][0]["directory"]
    template = json.loads((site / "legal-review-template.json").read_bytes())
    template.update(
        reviewer="DEMO reviewer",
        reviewed_at=datetime.now(UTC).isoformat(),
        findings=[
            dict(
                finding_id="demo-f1",
                factual_defect_verified=True,
                legal_applicability_verified=True,
                serviceable=True,
                reason="DEMO synthetic test review, not a legal conclusion",
                verified_legal_sources=["DEMO local fixture"],
            )
        ],
    )
    legal = tmp_path / "legal.json"
    legal.write_text(json.dumps(template))
    source = tmp_path / "saved.csv"
    with source.open("w", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=["url", "company_name", "email", "dossier", "analysis", "legal_review"]
        )
        writer.writeheader()
        writer.writerow(
            dict(
                url="https://demo-0.example/",
                company_name="DEMO reviewed",
                email="",
                dossier=str(site / "dossier"),
                analysis=str(site / "analysis/analysis.json"),
                legal_review=str(legal),
            )
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Saved mode must not invoke analyze or initialize a provider")

    monkeypatch.setattr(batch, "analyze", forbidden)
    monkeypatch.setattr(batch, "DeepSeekAdapter", forbidden)
    state = run(source, tmp_path / "reviewed", mode="saved", max_sites=1)
    row = state["rows"][0]
    assert row["status"] == "SUCCESS" and row["qualification"] == "GO"
    assert row["cost_usd"] == "0" and row["ai_calls"] == 0
    assert calls == [MODEL]
    assert (tmp_path / "reviewed" / row["proposal"]).exists()
    assert (tmp_path / "reviewed" / row["directory"] / "independent-legal-review.json").exists()
    assert (
        run(source, tmp_path / "reviewed", mode="saved", max_sites=1, resume=True)["rows"]
        == state["rows"]
    )


def test_120_second_profile_uses_only_a_and_one_attempt(tmp_path, monkeypatch):
    import lexradar.mvp_batch as batch

    inputs = create_inputs(tmp_path / "inputs")
    packet = load_packet(inputs.parent / "dossier-0", 200000)
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="DEMO",
                approved_at=datetime.now(UTC).isoformat(),
            )
        )
    )
    calls = []

    class Local:
        def __init__(self, *, timeout_seconds):
            assert timeout_seconds == 120

        def complete(self, settings, messages):
            calls.append(settings.model)
            raise ProviderError("local timeout", retryable=True)

    monkeypatch.setattr(batch, "DeepSeekAdapter", Local)
    state = run(
        inputs,
        tmp_path / "timeout",
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
        timeout_seconds=120,
        max_ai_calls=1,
    )
    row = state["rows"][0]
    assert calls == [MODEL] and row["ai_calls"] == 1
    assert row["status"] == "ERROR" and row["cost_usd"] == "unknown"
    assert Decimal(row["budget_charge_usd"]) <= Decimal("0.50")
    assert not list((tmp_path / "timeout").rglob("proposal.html"))


def test_corrupted_saved_ledger_stops_before_output(tmp_path):
    import shutil

    audit = tmp_path / "audit"
    shutil.copytree("runs-064/doktstom-deepseek-chat-001", audit)
    (audit / "requests.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="ledger"):
        verify(tmp_path / "blocked", audit=audit)
    assert not (tmp_path / "blocked").exists()
