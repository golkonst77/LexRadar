# ruff: noqa: F811
import json
from decimal import Decimal

import pytest
from test_offline_mvp_048 import dossier, no_network, ready  # noqa: F401, F811

from lexradar.auditors.budget import Budget, BudgetExceeded
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.models import Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import HTTPReply, ProviderError, ProviderReply
from lexradar.offline_mvp import FakeOpenRouterProposal, bind_review, review_matches
from lexradar.single_auditor import MODEL, DeepSeekAdapter, messages_for, profile, run


@pytest.fixture(autouse=True)
def current_cookie_response(ready):
    ready[1]["A"]["cookie_analytics_review"] = {
        "status": "NOT_VERIFIED", "rationale": "Synthetic fixture; cookie behavior not examined"
    }


def test_one_finding_go_without_qwen(ready, tmp_path):
    root, responses = ready
    a = responses["A"]
    a["findings"][0]["proposed_work"] = ["legal_documents", "webmaster_instructions"]
    # Missing coverage elsewhere does not invalidate the supported finding.
    a["coverage"][0]["status"] = "insufficient_evidence"
    calls = []

    class Provider:
        def complete(self, settings, messages):
            calls.append(settings.model)
            assert "cookie_analytics_review" in messages[0]["content"]
            return ProviderReply(json.dumps(a), Usage())

    result = run(root, tmp_path / "go", a, provider=Provider())
    assert calls == [MODEL]
    assert result["commercial"]["outcome"] == "GO"
    assert len(result["analysis"]["runs"]) == 1
    assert not result["send_allowed"]
    review = bind_review(tmp_path / "go/client", "reviewer")
    assert review_matches(tmp_path / "go/client", review)
    assert not review.authenticated


@pytest.mark.parametrize(
    "change",
    [
        "quote",
        "extra_quote",
        "url",
        "id",
        "no_work",
        "immaterial",
        "absent",
        "no_findings",
        "bad_schema",
    ],
)
def test_hold_no_proposal(ready, tmp_path, change):
    root, responses = ready
    a = responses["A"]
    f = a["findings"][0]
    f["proposed_work"] = ["legal_documents"]
    if change == "quote":
        f["text_grounding"][0]["exact_quote"] = "invented"
    elif change == "extra_quote":
        f["text_grounding"].append({"evidence_id": "page-0001", "exact_quote": "invented"})
    elif change == "url":
        f["source"] = "https://invented.example/"
    elif change == "id":
        f["evidence_ids"] = ["invented"]
    elif change == "no_work":
        f["proposed_work"] = []
    elif change == "immaterial":
        f["material"] = False
    elif change == "absent":
        f["fact_assertion"] = "absent"
    elif change == "no_findings":
        a["findings"] = []
    else:
        a = {}
    fake = FakeOpenRouterProposal()
    result = run(root, tmp_path / "hold", a, proposal_provider=fake)
    assert result["commercial"]["outcome"] == "HOLD"
    assert fake.calls == 0
    assert not (tmp_path / "hold/client").exists()


def test_integrity(ready, tmp_path):
    root, responses = ready
    (root / "artifacts/page-0001.html").write_text("tampered")
    with pytest.raises(ValueError):
        run(root, tmp_path / "bad", responses["A"])


def test_retry_limit(ready, tmp_path):
    class Broken:
        calls = 0

        def complete(self, settings, messages):
            self.calls += 1
            raise ProviderError("http_429", retryable=True)

    provider = Broken()
    report = analyze(
        ready[0],
        tmp_path / "retry",
        profile(),
        provider=provider,
        message_builder=messages_for,
        sleep=lambda _: None,
    )
    assert provider.calls == 1
    assert not report.completed and len(report.runs) == 1


def test_budget_before_request(ready):
    settings = profile().auditor_a
    messages, _, _ = messages_for("A", load_packet(ready[0], 200000))
    with pytest.raises(BudgetExceeded):
        Budget(Decimal("0.000001")).reserve(settings, messages)


@pytest.mark.parametrize("failure", [None, "http", "model", "truncated", "tokens", "size"])
def test_real_adapter_fake_transport(ready, monkeypatch, failure):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-050")

    class Transport:
        def post(self, payload, key, timeout, max_bytes):
            data = json.loads(payload)
            assert data["model"] == MODEL
            assert data["max_tokens"] == 28000
            assert not data["provider"]["allow_fallbacks"]
            assert data["provider"]["max_price"] == {"prompt": 1, "completion": 2}
            assert timeout == 120 and max_bytes == 500000
            body = {
                "model": MODEL if failure != "model" else "qwen/fallback",
                "choices": [
                    {
                        "finish_reason": "length" if failure == "truncated" else "stop",
                        "message": {"content": json.dumps(ready[1]["A"])},
                    }
                ],
                "usage": {"completion_tokens": 28001 if failure == "tokens" else 100},
            }
            return HTTPReply(
                429 if failure == "http" else 200,
                b"x" * 500001 if failure == "size" else json.dumps(body).encode(),
            )

    adapter = DeepSeekAdapter(transport=Transport())
    messages, _, _ = messages_for("A", load_packet(ready[0], 200000))
    if failure:
        with pytest.raises(ProviderError):
            adapter.complete(profile().auditor_a, messages)
    else:
        assert adapter.complete(profile().auditor_a, messages).content
    with pytest.raises(PermissionError):
        analyze(ready[0], ready[0].parent / "blocked", profile(), provider=adapter)


def test_direct_export_cannot_bypass_hold(ready, tmp_path):
    from lexradar.auditors.models import AnalysisReport
    from lexradar.offline_mvp import export_proposal
    from lexradar.single_auditor import checked_report, commercial

    result = run(ready[0], tmp_path / "hold", ready[1]["A"])
    report = AnalysisReport.model_validate(result["analysis"])
    fake = FakeOpenRouterProposal()
    assert (
        export_proposal(
            ready[0], tmp_path / "bypass", report, fake, decision=commercial, recheck=checked_report
        )["status"]
        == "not_requested"
    )
    assert fake.calls == 0
    report.evidence_packet_sha256 = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        commercial(report, load_packet(ready[0], 200000))


def test_adapter_input_limit_before_transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-050")

    class Forbidden:
        def post(self, *args):
            pytest.fail("No transport after input limit")

    adapter = DeepSeekAdapter(transport=Forbidden())
    with pytest.raises(ProviderError, match="input_token_bound"):
        adapter.complete(profile().auditor_a, [{"role": "user", "content": "x" * 240000}])


def test_paid_budget_prevents_transport(ready, tmp_path):
    from test_auditors import approve_packet

    class Forbidden:
        def complete(self, *args):
            pytest.fail("No provider after budget exhaustion")

    config = profile()
    config.max_budget_usd = Decimal("0.000001")
    report = analyze(
        ready[0],
        tmp_path / "budget",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approve_packet(ready[0]),
        provider=Forbidden(),
        message_builder=messages_for,
    )
    assert report.runs[0].status == "budget_exceeded"
    assert not report.logs[0].request_sent
