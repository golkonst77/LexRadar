"""Offline guards for the single authorized experiment."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from lexradar.auditors.budget import Budget, BudgetExceeded
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.preflight import authorize
from lexradar.auditors.provider import HTTPReply, OpenRouterProvider, ProviderError
from lexradar.experiment_069 import PACKET, OnePostTransport, experiment_profile, messages_for
from lexradar.single_auditor import messages_for as original_messages

DOSSIER = Path("runs-054/doktstom-improved/dossier")


class Sink:
    def __init__(self):
        self.calls = 0

    def post(self, payload, key, timeout, max_bytes):
        self.calls += 1
        raise ProviderError(
            "transport_error",
            retryable=True,
            diagnostics={"phase": "open", "kind": "network", "received_bytes": 0},
        )


def test_packet_prompt_budget():
    config = experiment_profile()
    packet = load_packet(DOSSIER, config.max_input_bytes)
    assert packet.sha256 == PACKET
    assert authorize(packet, Path("runs-058/packet-approval.json"), mvp_no_screening=True).approved
    messages, version, digest = messages_for("A", packet)
    assert version == "single-a-v2" and len(digest) == 64
    assert messages[1]["content"] == original_messages("A", packet)[0][1]["content"]
    assert original_messages("A", packet)[1] == "single-a-v1"
    assert config.retries == 0 and config.timeout_seconds == 120
    assert config.max_budget_usd == Decimal("0.10")
    assert Budget(config.max_budget_usd).reserve(config.auditor_a, messages) < Decimal("0.10")
    with pytest.raises(BudgetExceeded):
        Budget(Decimal("0.001")).reserve(config.auditor_a, messages)


def test_no_second_post_even_after_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    config = experiment_profile()
    sink = Sink()
    transport = OnePostTransport(sink)
    provider = OpenRouterProvider(config, transport=transport)
    messages = messages_for("A", load_packet(DOSSIER, config.max_input_bytes))[0]
    with pytest.raises(ProviderError, match="transport_error"):
        provider.complete(config.auditor_a, messages)
    with pytest.raises(ProviderError, match="experiment_call_limit"):
        provider.complete(config.auditor_a, messages)
    assert sink.calls == 1


def test_changed_packet_never_sent(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    config = experiment_profile()
    sink = Sink()
    transport = OnePostTransport(sink)
    provider = OpenRouterProvider(config, transport=transport)
    messages = messages_for("A", load_packet(DOSSIER, config.max_input_bytes))[0]
    messages[1]["content"] += "x"
    with pytest.raises(ProviderError, match="experiment_request_mismatch"):
        provider.complete(config.auditor_a, messages)
    assert sink.calls == 0


def test_provider_capture_before_normalization(monkeypatch, tmp_path):
    from lexradar.audit_capture import Capture

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    original = ' {"invalid_schema": true} \r\n'

    class Reply:
        def post(self, *args):
            return HTTPReply(
                200,
                json.dumps(
                    {
                        "model": experiment_profile().auditor_a.model,
                        "choices": [{"finish_reason": "stop", "message": {"content": original}}],
                        "usage": {"cost": 0.001},
                    }
                ).encode(),
            )

    provider = OpenRouterProvider(experiment_profile(), transport=OnePostTransport(Reply()))
    provider.audit_capture = Capture(tmp_path / "capture")
    messages = messages_for("A", load_packet(DOSSIER, 200000))[0]
    reply = provider.complete(experiment_profile().auditor_a, messages)
    assert reply.content == original
    assert (tmp_path / "capture/original.txt").read_bytes() == original.encode()


def test_experiment_screening_gate(tmp_path, monkeypatch):
    from lexradar.auditors.orchestrator import analyze
    from lexradar.auditors.provider import OfflineProvider

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    report = analyze(
        DOSSIER,
        tmp_path / "analysis",
        experiment_profile(),
        provider=OfflineProvider(),
        message_builder=messages_for,
        mvp_no_screening=True,
    )
    assert len(report.logs) == 1 and report.runs[0].prompt_version == "single-a-v2"
    assert not report.logs[0].request_sent


def test_paid_path_one_failure_and_honest_export(tmp_path, monkeypatch):
    from lexradar.audit_review import export
    from lexradar.auditors.orchestrator import analyze

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    sink = Sink()
    config = experiment_profile()
    provider = OpenRouterProvider(config, transport=OnePostTransport(sink))
    report = analyze(
        DOSSIER,
        tmp_path / "analysis",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=Path("runs-058/packet-approval.json"),
        provider=provider,
        mvp_no_screening=True,
        message_builder=messages_for,
    )
    assert sink.calls == 1 and len(report.logs) == 1 and not report.completed
    assert report.logs[0].usage.cost_usd is None
    assert report.budget_charged_usd <= Decimal("0.10")
    assert not (tmp_path / "analysis/capture-A-1/original.txt").exists()
    export(tmp_path / "analysis", tmp_path / "review", dossier=DOSSIER)
    assert "ORIGINAL_NOT_AVAILABLE" in (tmp_path / "review/01-deepseek-original.txt").read_text()

    summary = (tmp_path / "review/CHATGPT-REVIEW.md").read_text()
    assert "Ответ модели не получен" in summary and "Исходный ответ утрачен" not in summary


def test_module_entry_uses_canonical_main(monkeypatch):
    import runpy

    import lexradar.experiment_069 as experiment

    calls = []
    monkeypatch.setattr(experiment, "main", lambda: calls.append("canonical"))
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        runpy.run_module("lexradar.experiment_069", run_name="__main__")
    assert calls == ["canonical"]
