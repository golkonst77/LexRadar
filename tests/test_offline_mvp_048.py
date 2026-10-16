import copy
import json
import socket
from threading import Barrier

import pytest
from test_auditors import auditor_response, dossier, finding  # noqa: F401

from lexradar.auditors.evidence import load_packet
from lexradar.auditors.models import Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import OpenRouterTransport, ProviderReply
from lexradar.collector.reading import extract_html
from lexradar.offline_mvp import (
    PAIR,
    FakeOpenRouterProposal,
    bind_review,
    commercial,
    export_proposal,
    profile,
    review_matches,
    run,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)


@pytest.fixture
def ready(dossier):  # noqa: F811
    root, data = dossier
    body = (root / data.evidence[0].artifacts[0].path).read_bytes()
    data.pages[0].text, data.pages[0].reading = extract_html(body, 100000)
    (root / "collection.json").write_text(data.model_dump_json())
    responses = {}
    for label in ("A", "B"):
        f = finding()
        f.update(
            fact=data.pages[0].text,
            fact_assertion="present",
            examination_scope="text_excerpt",
            text_grounding=[{"evidence_id": "page-0001", "exact_quote": data.pages[0].text}],
        )
        responses[label] = auditor_response(label, [f])
        for c in responses[label]["coverage"]:
            c["status"] = "reviewed"
    return root, responses


def test_synthetic_go_pdf_and_review(ready, tmp_path):
    root, responses = ready
    fake = FakeOpenRouterProposal()
    result = run(root, tmp_path / "go", responses, pdf=True, proposal_provider=fake)
    assert result["commercial"]["outcome"] == "GO", result
    assert fake.calls == 1
    assert result["legal_production_status"] == "HOLD" and not result["send_allowed"]
    client = tmp_path / "go/client"
    from pypdf import PdfReader

    assert "Synthetic public form" in PdfReader(client / "proposal.pdf").pages[0].extract_text()
    review = bind_review(client, "synthetic reviewer")
    assert review_matches(client, review) and not review.authenticated
    (client / "proposal.html").write_text("substitution")
    assert not review_matches(client, review)


@pytest.mark.parametrize("change", ["missing_b", "bad_a", "quote", "coverage", "conflict"])
def test_hold_no_generation_or_client_files(ready, tmp_path, change):
    root, responses = ready
    if change == "missing_b":
        del responses["B"]
    elif change == "bad_a":
        responses["A"] = {"malformed": True}
    elif change == "quote":
        responses["A"]["findings"][0]["text_grounding"][0]["exact_quote"] = "invented"
    elif change == "coverage":
        responses["B"]["coverage"][0]["status"] = "insufficient_evidence"
    else:
        responses["B"]["findings"][0]["fact_assertion"] = "absent"
    fake = FakeOpenRouterProposal()
    result = run(root, tmp_path / ("run-" + change), responses, proposal_provider=fake)
    assert result["commercial"]["outcome"] == "HOLD"
    assert fake.calls == 0
    assert not (tmp_path / ("run-" + change) / "client").exists()
    assert not list((tmp_path / ("run-" + change)).rglob("proposal.*"))


def test_parallel_independent_context_even_on_error(ready, tmp_path):
    root, responses = ready
    barrier = Barrier(2)
    calls = {}

    class Provider:
        def complete(self, settings, messages):
            label = "A" if settings.model == PAIR[0] else "B"
            calls[label] = copy.deepcopy(messages)
            barrier.wait(timeout=5)  # deterministic overlap; sequential execution fails
            if label == "A":
                messages[1]["content"] = "A_RESULT_LEAK"
                raise RuntimeError("synthetic provider failure")
            return ProviderReply(json.dumps(responses[label]), Usage())

    report = analyze(root, tmp_path / "parallel", profile(), provider=Provider(), parallel=True)
    assert [r.status for r in report.runs] == ["provider_error", "success"]
    assert calls["A"][1] == calls["B"][1]
    assert calls["A"][0] != calls["B"][0]
    assert len(calls["B"]) == 2 and "A_RESULT_LEAK" not in str(calls["B"])
    assert len({log.evidence_packet_sha256 for log in report.logs}) == 1


@pytest.mark.parametrize("tamper", ["packet", "analysis", "finding", "price", "law", "quote"])
def test_invalid_proposal_never_exported(ready, tmp_path, tamper):
    root, responses = ready

    class Bad:
        def generate(self, request):
            if tamper == "packet":
                request["packet_sha256"] = "0" * 64
            elif tamper == "analysis":
                request["analysis_sha256"] = "0" * 64
            elif tamper == "finding":
                request["finding_ids"] = ["unrelated"]
            else:
                request[tamper] = "invented"
            return request

    result = run(root, tmp_path / tamper, responses, proposal_provider=Bad())
    assert result["proposal"]["status"] == "blocked"
    assert not (tmp_path / tamper / "client").exists()


def test_direct_export_hold_and_hash_substitution(ready, tmp_path):
    root, responses = ready
    result = run(root, tmp_path / "source", responses)
    from lexradar.auditors.models import AnalysisReport

    report = AnalysisReport.model_validate(result["analysis"])
    fake = FakeOpenRouterProposal()
    report.runs[1].status = "provider_error"
    report.completed = False
    assert export_proposal(root, tmp_path / "bypass", report, fake)["status"] == "not_requested"
    assert fake.calls == 0 and not (tmp_path / "bypass").exists()
    report.evidence_packet_sha256 = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        commercial(report, load_packet(root, 200000))


def test_missing_qwen_is_rejected(ready, tmp_path):
    root, responses = ready
    result = run(root, tmp_path / "source", responses)
    from lexradar.auditors.models import AnalysisReport

    report = AnalysisReport.model_validate(result["analysis"])
    report.runs[1].model = PAIR[0]
    with pytest.raises(ValueError, match="Qwen"):
        commercial(report, load_packet(root, 200000))


@pytest.mark.parametrize("change", ["partial", "legacy", "extra_quote", "warning"])
def test_incomplete_or_extra_quote_hold(ready, tmp_path, change):
    root, responses = ready
    from lexradar.collector.models import CollectionResult

    data = CollectionResult.model_validate_json((root / "collection.json").read_text())
    if change == "partial":
        data.evidence[0].collection_errors = ["synthetic missing page"]
        data.evidence[0].status = "partial"
    elif change == "legacy":
        data.pages[0].reading.extraction_state = "unknown"
    elif change == "warning":
        data.warnings = ["collection truncated"]
    else:
        responses["A"]["findings"][0]["text_grounding"].append(
            {"evidence_id": "page-0001", "exact_quote": "invented extra assertion"}
        )
    (root / "collection.json").write_text(data.model_dump_json())
    fake = FakeOpenRouterProposal()
    result = run(root, tmp_path / ("run-" + change), responses, proposal_provider=fake)
    assert result["commercial"]["outcome"] == "HOLD"
    assert fake.calls == 0 and not (tmp_path / ("run-" + change) / "client").exists()


@pytest.mark.parametrize("change", ["dossier", "provider_error", "mutate_ids", "pdf_failure"])
def test_generation_errors_leave_no_documents(ready, tmp_path, change):
    root, responses = ready

    class Bad:
        def generate(self, request):
            if change == "provider_error":
                raise RuntimeError("synthetic error")
            if change == "dossier":
                path = root / "collection.json"
                value = json.loads(path.read_text())
                value["warnings"] = ["changed during generation"]
                path.write_text(json.dumps(value))
            if change == "mutate_ids":
                request["finding_ids"].append("invented")
            return request

    result = run(
        root,
        tmp_path / ("run-" + change),
        responses,
        proposal_provider=Bad(),
        pdf=change == "pdf_failure",
        font_path=tmp_path / "missing.ttf",
    )
    assert result["proposal"]["status"] == "blocked"
    assert not (tmp_path / ("run-" + change) / "client").exists()


@pytest.mark.parametrize("filename", ["proposal.json", "proposal.html", "proposal.pdf"])
def test_review_checks_actual_bytes_not_manifest(ready, tmp_path, filename):
    root, responses = ready
    run(root, tmp_path / "go", responses, pdf=True)
    client = tmp_path / "go/client"
    review = bind_review(client, "test")
    (client / filename).write_bytes(b"replaced bytes")
    (client / "sha256.json").write_text(json.dumps({filename: review.document_sha256}))
    assert not review_matches(client, review)


def test_cli_runs_recorded_pair(ready, tmp_path, monkeypatch, capsys):
    from lexradar.offline_mvp import main

    root, responses = ready
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(responses))
    output = tmp_path / "cli"
    monkeypatch.setattr("sys.argv", ["offline_mvp", str(root), str(path), str(output), "--pdf"])
    main()
    assert json.loads(capsys.readouterr().out)["commercial"]["outcome"] == "GO"
    assert (output / "client/proposal.pdf").is_file()


def test_parallel_paid_is_disabled(ready, tmp_path):
    with pytest.raises(PermissionError):
        analyze(
            ready[0],
            tmp_path / "paid",
            profile(),
            mode="openrouter",
            parallel=True,
            allow_external_transfer=True,
        )
    assert not (tmp_path / "paid").exists()
