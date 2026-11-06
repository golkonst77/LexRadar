"""Synthetic, offline coverage for independent facts and the one-request operator path."""

import copy
import io
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_approved_proposal_084 import mask_fields
from test_auditors import approve_packet
from test_offline_mvp_048 import dossier, no_network, ready  # noqa: F401

from lexradar import client_proposal
from lexradar.audit_progress import Progress, waiting
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.grounding import check_quote
from lexradar.auditors.models import AIAuditorResult, TextGrounding, Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import HTTPReply, ProviderError, ProviderReply
from lexradar.auditors.validator import validate_result
from lexradar.batch import snapshot
from lexradar.batch_qualification import analysis_digest
from lexradar.collector import CrawlOptions, Limits, collect
from lexradar.collector.network import Response
from lexradar.cookie_inspection import SERVICES, inspect_saved
from lexradar.finding_assessment import CommercialReview, FindingReview, evaluate
from lexradar.operator_audit import audit_main, finalize, prepare
from lexradar.single_auditor import DeepSeekAdapter, messages_for, profile


@pytest.fixture
def case(ready, tmp_path):  # noqa: F811
    root, responses = ready
    raw = copy.deepcopy(responses["A"])
    raw["cookie_analytics_review"] = dict(status="NOT_VERIFIED", rationale="Synthetic only")
    raw["findings"][0]["proposed_work"] = ["legal_documents"]
    raw["findings"][0]["legal_position"] = "possible_issue"
    packet = load_packet(root, 200000)
    report = analyze(
        root,
        tmp_path / "analysis",
        profile(),
        message_builder=messages_for,
        provider=SimpleNamespace(complete=lambda *_: ProviderReply(json.dumps(raw), Usage())),
    )
    assert report.completed
    return root, packet, AIAuditorResult.model_validate(raw), report


def review_for(packet, result, report, **overrides):
    fields = dict(
        finding_id=result.findings[0].id,
        quote_indices=[0],
        factual_scope="The exact synthetic source text was independently examined.",
        risk_reason="A concrete documented configuration needs targeted examination.",
        deliverables=["Review the consent workflow and deliver webmaster instructions"],
        services=["webmaster_instructions"],
        factual_scope_verified=True,
        service_need_verified=True,
    )
    fields.update(overrides)
    return CommercialReview(
        packet_sha256=packet.sha256,
        analysis_sha256=analysis_digest(report),
        reviewer="Synthetic operator",
        reviewed_at=datetime.now(UTC),
        findings=[FindingReview(**fields)],
    )


def test_all_quotes_independently_match(case):
    _, packet, result, report = case
    result.findings[0].text_grounding.append(result.findings[0].text_grounding[0].model_copy())
    row = evaluate(result, packet, report)["findings"][0]
    assert row["fact_status"] == "CONFIRMED"
    assert [c["status"] for c in row["quotes"]] == ["MATCH", "MATCH"]
    assert not row["legal_violation_proven"]


def test_one_bad_quote_does_not_destroy_other_evidence(case):
    _, packet, result, report = case
    result.findings[0].text_grounding.append(
        TextGrounding(evidence_id="page-0001", exact_quote="invented second quote")
    )
    normalized, _ = validate_result(result.model_dump_json(), "A", packet)
    assert normalized.findings[0].fact_supported
    row = evaluate(result, packet, report)["findings"][0]
    assert row["confirmed_quote_indices"] == [0]
    assert row["quotes"][1]["status"] == "NOT_FOUND"
    decision = evaluate(result, packet, report, review_for(packet, result, report))
    assert decision["outcome"] == "GO"
    with pytest.raises(ValueError, match="matching quotations"):
        evaluate(result, packet, report, review_for(packet, result, report, quote_indices=[1]))


def test_wrong_source_preserves_actual_url_and_repeated_notices(case):
    _, packet, result, report = case
    quote = result.findings[0].text_grounding[0].model_copy()
    quote.evidence_id = "wrong-page"
    checked = check_quote(packet, quote)
    assert checked["status"] == "WRONG_SOURCE"
    assert checked["matches"][0]["source"] == str(packet.data.evidence[0].source)
    # Repeat a known, reproduced notice in a second page of this in-memory fixture.
    data = copy.deepcopy(packet.data)
    evidence = data.evidence[0].model_copy(
        update={"id": "page-0002", "source": data.evidence[0].source}
    )
    page = data.pages[0].model_copy(update={"evidence_id": "page-0002"})
    data.evidence.append(evidence)
    data.pages.append(page)
    from dataclasses import replace

    repeated = replace(packet, data=data)
    q = result.findings[0].text_grounding[0]
    assert check_quote(repeated, q)["status"] == "MATCH"
    assert len(check_quote(repeated, q)["matches"]) == 2
    data.evidence[1].source = "https://clinic.example/second"
    result.findings[0].evidence_ids.append("page-0002")
    result.findings[0].text_grounding.append(q.model_copy(update={"evidence_id": "page-0002"}))
    normalized, _ = validate_result(result.model_dump_json(), "A", repeated)
    assert normalized.findings[0].fact_supported
    rows = evaluate(result, repeated, report)["findings"]
    assert len(rows[0]["source_urls"]) == 2


def test_no_quote_no_go_and_paraphrase_is_partial(case):
    _, packet, result, report = case
    f = result.findings[0]
    f.fact = "A paraphrase, never a verbatim quote"
    assert evaluate(result, packet, report)["findings"][0]["fact_status"] == "PARTIAL"
    f.text_grounding[0].exact_quote = "not in any source"
    row = evaluate(result, packet, report)["findings"][0]
    assert row["fact_status"] == "UNSUPPORTED"
    assert row["classification"] == "unsubstantiated_assumption"
    assert not row["commercial_candidate"]


def test_risk_is_candidate_not_automatic_go(case):
    _, packet, result, report = case
    decision = evaluate(result, packet, report)
    row = decision["findings"][0]
    assert row["classification"] == "confirmed_fact_with_legal_risk"
    assert row["commercial_candidate"] and not row["legal_violation_proven"]
    assert decision["outcome"] == "HOLD"


def test_positive_fact_cannot_be_sold_as_a_defect(case):
    _, packet, result, report = case
    result.findings[0].legal_position = "no_issue"
    decision = evaluate(result, packet, report, review_for(packet, result, report))
    assert decision["outcome"] == "HOLD"
    assert decision["findings"][0]["classification"] == "positive_fact"


def test_bound_operator_review_go_and_approved_template(case, tmp_path):
    _, packet, result, report = case
    output = tmp_path / "final"
    output.mkdir()
    path = tmp_path / "review.json"
    path.write_text(review_for(packet, result, report).model_dump_json())
    before = client_proposal.APPROVED_TEMPLATE.read_bytes()
    decision = finalize(
        report, result, packet, output, company="Synthetic clinic", review_path=path, replayed=True
    )
    assert decision["outcome"] == "GO"
    page = (output / "proposal.html").read_text()
    assert "Нарушение не установлено" in page
    assert "webmaster instructions" in page
    assert client_proposal.APPROVED_TEMPLATE.read_bytes() == before
    masked = mask_fields(page)
    expected = before.decode().replace("LexRadar · КП v2.2", "LexRadar · КП v2.4", 1)
    assert masked == mask_fields(expected)
    assert json.loads((output / "summary.json").read_text())["new_ai_cost_usd"] == "0"


def test_review_hash_tampering_and_unproven_violation_rejected(case):
    _, packet, result, report = case
    review = review_for(packet, result, report)
    review.packet_sha256 = "0" * 64
    with pytest.raises(ValueError, match="binding"):
        evaluate(result, packet, report, review)
    review = review_for(packet, result, report, legal_violation_verified=True)
    with pytest.raises(ValueError, match="legal proof"):
        evaluate(result, packet, report, review)


def test_reviewed_counter_evidence_is_distinct(case):
    _, packet, result, report = case
    result.findings[0].fact_assertion = "absent"
    review = review_for(packet, result, report, contradiction=result.findings[0].text_grounding[0])
    decision = evaluate(result, packet, report, review)
    assert decision["findings"][0]["fact_status"] == "CONTRADICTED"
    assert decision["outcome"] == "HOLD"


@pytest.mark.parametrize("failure", ["transport_error", "model_refusal"])
def test_failure_never_retries_and_cost_is_uncertain(case, tmp_path, failure):
    root, packet, _, _ = case

    class Broken:
        calls = 0

        def complete(self, *_):
            self.calls += 1
            raise ProviderError(failure, retryable=True)

    broken = Broken()
    progress_root = tmp_path / "progress"
    progress_root.mkdir()
    progress = Progress(progress_root)
    report = analyze(
        root,
        tmp_path / "paid-fake",
        profile(),
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approve_packet(root),
        provider=broken,
        message_builder=messages_for,
        progress=progress,
    )
    assert evaluate(None, packet, report)["reasons"][0] == "auditor_failed"
    assert broken.calls == 1 and not report.completed
    assert report.budget_charged_usd > 0 and report.logs[0].usage.cost_usd is None
    events = [json.loads(x) for x in (progress_root / "events.jsonl").read_text().splitlines()]
    assert any(e.get("cost_uncertain") is True for e in events)


def test_missing_key_and_budget_stop_before_network(case, monkeypatch, tmp_path):
    root, _, _, _ = case
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="missing_or_invalid_api_key"):
        DeepSeekAdapter()
    config = profile().model_copy(update={"max_budget_usd": Decimal("0.000001")})
    provider = SimpleNamespace(complete=lambda *_: pytest.fail("Must not send"))
    report = analyze(
        root,
        tmp_path / "no-budget",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approve_packet(root),
        provider=provider,
        message_builder=messages_for,
    )
    assert report.runs[0].status == "budget_exceeded"
    assert not report.logs[0].request_sent


def test_heartbeat_is_passive_and_stops(tmp_path):
    progress = Progress(tmp_path, stream=io.StringIO())
    with waiting(progress, interval=0.01):
        time.sleep(0.045)
    text = (tmp_path / "events.jsonl").read_text()
    assert text.count("waiting_for_response") >= 2
    time.sleep(0.025)
    assert (tmp_path / "events.jsonl").read_text() == text


def test_no_browser_is_reported_without_automatic_fallback(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PermissionError("Do not log secret-person-payload")

    monkeypatch.setattr("lexradar.operator_audit.collect", unavailable)
    out = tmp_path / "browser-unavailable"
    with pytest.raises(SystemExit):
        audit_main(["https://clinic.example/", "--output", str(out)])
    assert json.loads((out / "error.json").read_text())["outcome"] == "HOLD"
    assert "secret-person-payload" not in (out / "events.jsonl").read_text()


def test_prepare_only_and_cookie_unknown(case, tmp_path):
    root, packet, _, _ = case
    before = snapshot(root)
    out = tmp_path / "prepared"
    audit_main(
        ["https://clinic.example/", "--output", str(out), "--dossier", str(root), "--cookie-check"]
    )
    assert load_packet(out / "dossier", 200000).sha256 == packet.sha256
    assert snapshot(root) == before
    assert not (out / "audit").exists()
    cookie = inspect_saved(root)
    assert cookie["dynamic_status"] == "NOT_VERIFIED"
    assert all(v == "NOT_VERIFIED" for v in cookie["dynamic_checks"].values())
    for service, text in [
        ("yandex_metrika", "https://mc.yandex.ru/metrika/tag.js"),
        ("google_analytics", "https://www.google-analytics.com/analytics.js"),
        ("google_tag_manager", "GTM-ABC123"),
        ("other_analytics", "https://www.clarity.ms/tag/abc"),
    ]:
        assert SERVICES[service].search(text)


def test_model_refusal_envelope_and_capture(case, tmp_path, monkeypatch):
    from lexradar.audit_capture import Capture

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-102-key")
    body = dict(
        model="openai/gpt-5-mini",
        choices=[dict(finish_reason="stop", message=dict(content=None, refusal="Cannot comply"))],
    )
    adapter = DeepSeekAdapter(
        SimpleNamespace(post=lambda *_: HTTPReply(200, json.dumps(body).encode()))
    )
    adapter.audit_capture = Capture(tmp_path / "refusal")
    with pytest.raises(ProviderError, match="model_refusal"):
        adapter.complete(profile().auditor_a, [{"role": "user", "content": "synthetic"}])
    assert (tmp_path / "refusal/provider-envelope.json").is_file()


def test_duplicate_navigation_does_not_hide_pdf(tmp_path, monkeypatch):
    from test_collector import pdf_bytes

    from lexradar.collector.crawl import Clock

    monkeypatch.setattr(Clock, "sleep", lambda *_: None)

    class Fetcher:
        synthetic = True

        def get(self, url, allowed_origin, **kwargs):
            kwargs["before_request"](url)
            kwargs["on_event"]("request_sent", url)
            if url.endswith("robots.txt"):
                return Response(url, 200, "text/plain", b"User-agent: *\nDisallow:\n")
            if url.endswith(".pdf"):
                return Response(url, 200, "application/pdf", pdf_bytes(text=True))
            return Response(
                url,
                200,
                "text/html",
                (
                    "<html><body>"
                    + '<a href="/privacy">privacy</a>' * 250
                    + '<a href="/consent.pdf">consent</a></body></html>'
                ).encode(),
            )

    result = collect(
        "https://clinic.example/",
        tmp_path / "dedup",
        Limits(max_pages=1, max_documents=1, max_links_per_page=2),
        fetcher=Fetcher(),
        crawl_options=CrawlOptions(min_delay_seconds=0.1),
        static_only=True,
    )
    assert len(result.documents) == 1
    assert str(result.documents[0].requested_url).endswith("/consent.pdf")


def test_whole_page_selection_prefers_legal_materials(tmp_path):
    from lexradar.collector.artifacts import ArtifactStore
    from lexradar.collector.models import CollectedEvidence, CollectionResult, PageObservation
    from lexradar.collector.reading import extract_html

    root = tmp_path / "large-source"
    limits = Limits()
    store = ArtifactStore(root, limits)
    pages, evidence = [], []
    now = datetime.now(UTC)
    for index, slug in enumerate(("ordinary", "politika", "consent"), 1):
        eid = f"page-{index:04}"
        url = f"https://clinic.example/{slug}"
        body = f"<html><body>{slug} ".encode() + b"x" * 80000 + b"</body></html>"
        text, reading = extract_html(body, 100000)
        artifacts = [
            store.save(f"{eid}.html", body, "html"),
            store.save(f"{eid}.txt", text.encode(), "text"),
            store.save(f"{eid}.png", b"synthetic", "screenshot"),
        ]
        evidence.append(
            CollectedEvidence(
                id=eid,
                source=url,
                captured_at=now,
                observed_fact="Synthetic source",
                available=True,
                status="complete",
                artifacts=artifacts,
                artifact_path=artifacts[0].path,
                sha256=artifacts[0].sha256,
            )
        )
        pages.append(
            PageObservation(
                requested_url=url,
                final_url=url,
                http_status=200,
                captured_at=now,
                text=text,
                reading=reading,
                evidence_id=eid,
            )
        )
    collection = CollectionResult(
        target_url="https://clinic.example/",
        started_at=now,
        limits=limits,
        pages=pages,
        evidence=evidence,
    )
    (root / "collection.json").write_text(collection.model_dump_json())
    before = snapshot(root)
    output = tmp_path / "prepared-large"
    output.mkdir()
    packet = prepare(root, output / "dossier")
    assert len(packet.payload.encode()) <= 200000
    assert {e.id for e in packet.data.evidence} == {"page-0002", "page-0003"}
    assert snapshot(root) == before
    assert json.loads((output / "selection.json").read_text())["excluded"] == ["page-0001"]


def test_paid_cli_confirmation_one_mock_call_and_no_implicit_go(case, tmp_path, monkeypatch):
    root, packet, raw, _ = case
    confirmation = "РАЗРЕШАЮ ОДИН ЗАПРОС 0.50 USD " + packet.sha256
    reader = io.StringIO("Synthetic operator\n" + confirmation + "\n")

    class TTY:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, text):
            pass

        def flush(self):
            pass

        def readline(self):
            return reader.readline()

    calls = []

    def complete(*args):
        calls.append(args)
        return ProviderReply(
            raw.model_dump_json(),
            Usage(prompt_tokens=20, completion_tokens=30, total_tokens=50, cost_usd="0.02"),
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-102-only")
    monkeypatch.setattr("lexradar.operator_audit.open", lambda *_: TTY(), raising=False)
    monkeypatch.setattr(
        "lexradar.operator_audit.DeepSeekAdapter", lambda: SimpleNamespace(complete=complete)
    )
    out = tmp_path / "confirmed-paid-fake"
    audit_main(
        [
            "https://clinic.example/",
            "--output",
            str(out),
            "--dossier",
            str(root),
            "--paid",
            "--allow-external-transfer",
        ]
    )
    assert len(calls) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert summary["new_ai_cost_usd"] == "0.02" and summary["outcome"] == "HOLD"
    assert not (out / "proposal.html").exists()
    events = (out / "events.jsonl").read_text()
    for stage in (
        "request_sending",
        "response_received",
        "structure_and_evidence_check_started",
        "commercial_assessment_and_report_completed",
        "finished",
    ):
        assert stage in events
    assert "synthetic-102-only" not in events
    with pytest.raises(SystemExit):
        audit_main(["https://clinic.example/", "--output", str(out)])


def test_paid_cli_missing_key_and_cancellation_never_send(case, tmp_path, monkeypatch):
    root, _, _, _ = case
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "lexradar.operator_audit.DeepSeekAdapter", lambda: pytest.fail("No provider")
    )
    with pytest.raises(SystemExit):
        audit_main(
            [
                "https://clinic.example/",
                "--output",
                str(tmp_path / "no-key"),
                "--dossier",
                str(root),
                "--paid",
                "--allow-external-transfer",
            ]
        )
    assert not (tmp_path / "no-key/audit").exists()


def test_replay_preserves_history_without_provider(case, tmp_path, monkeypatch):
    root, _, _, original_report = case
    monkeypatch.setattr(
        "lexradar.operator_audit.DeepSeekAdapter", lambda: pytest.fail("No provider")
    )
    original = tmp_path / "analysis"
    before = snapshot(original)
    out = tmp_path / "replayed"
    audit_main(
        [
            "https://clinic.example/",
            "--output",
            str(out),
            "--dossier",
            str(root),
            "--saved-audit",
            str(original),
        ]
    )
    assert snapshot(original) == before
    assert json.loads((out / "summary.json").read_text())["new_ai_calls"] == 0
    assert (out / "audit/historical-analysis.json").read_bytes() == (
        original / "analysis.json"
    ).read_bytes()
