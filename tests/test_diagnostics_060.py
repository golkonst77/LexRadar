import json
from decimal import Decimal

import pytest
from test_offline_mvp_048 import dossier, no_network, ready  # noqa: F401

from lexradar.auditors.models import ProviderDiagnostics, RequestLog, Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import HTTPReply, ProviderError, ProviderReply
from lexradar.single_auditor import DeepSeekAdapter, messages_for, profile


@pytest.mark.parametrize("phase", ["open", "read"])
def test_safe_diagnostics_and_retry(ready, tmp_path, phase):  # noqa: F811
    class Provider:
        calls = 0

        def complete(self, settings, messages):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    "transport_error",
                    retryable=True,
                    diagnostics={
                        "phase": phase,
                        "kind": "timeout",
                        "http_status": 200,
                        "received_bytes": 17,
                        "headers": "secret-person-payload",
                    },
                ) from ValueError("secret-person-payload")
            return ProviderReply(json.dumps(ready[1]["A"]), Usage())

    report = analyze(
        ready[0],
        tmp_path / "out",
        profile(),
        provider=Provider(),
        message_builder=messages_for,
        sleep=lambda _: None,
    )
    assert report.completed
    assert not report.automatic_send_allowed
    for records in [
        json.loads((tmp_path / "out/analysis.json").read_text())["logs"],
        [json.loads(line) for line in (tmp_path / "out/requests.jsonl").read_text().splitlines()],
    ]:
        assert records[0]["provider_diagnostics"] == {
            "phase": phase,
            "kind": "timeout",
            "http_status": 200,
            "received_bytes": 17,
        }
        assert records[1]["provider_diagnostics"] is None
        assert "secret-person-payload" not in json.dumps(records)
    old = report.logs[0].model_dump()
    del old["provider_diagnostics"]
    assert RequestLog.model_validate(old).provider_diagnostics is None


def test_diagnostics_reject_unsafe_values():
    safe = ProviderDiagnostics.sanitized(
        {
            "phase": "secret",
            "kind": "secret",
            "http_status": "secret",
            "received_bytes": True,
            "body": "secret",
        }
    )
    assert safe is None


@pytest.mark.parametrize("timeout", [45, 120])
def test_timeout_profile_and_transport(monkeypatch, timeout):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-060-key")

    class Transport:
        def post(self, payload, key, actual_timeout, max_bytes):
            assert actual_timeout == timeout
            return HTTPReply(429, b"")

    adapter = DeepSeekAdapter(Transport(), timeout_seconds=timeout)
    assert adapter.config == profile(timeout_seconds=timeout)
    assert adapter.config.max_budget_usd == Decimal("0.50")
    assert adapter.config.max_input_bytes == profile().max_input_bytes == 200_000
    with pytest.raises(ProviderError, match="http_429"):
        adapter.complete(adapter.config.auditor_a, [{"role": "user", "content": "fixture"}])
    adapter.config.retries = 0
    with pytest.raises(ProviderError, match="unexpected_single"):
        adapter.complete(adapter.config.auditor_a, [])
    assert profile().timeout_seconds == 45
    with pytest.raises(ValueError):
        profile(timeout_seconds=60)


def test_mvp_120_and_budget_retry(ready, tmp_path):  # noqa: F811
    from test_auditors import approve_packet

    class Broken:
        def complete(self, settings, messages):
            raise ProviderError("transport_error", retryable=True)

    config = profile(timeout_seconds=120)
    report = analyze(
        ready[0],
        tmp_path / "paid-fake",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approve_packet(ready[0]),
        provider=Broken(),
        message_builder=messages_for,
        mvp_no_screening=True,
        sleep=lambda _: None,
    )
    assert len(report.logs) == 2
    assert report.budget_charged_usd <= report.budget_limit_usd == Decimal("0.50")
    assert not report.automatic_send_allowed
    run = json.loads((tmp_path / "paid-fake/run.json").read_text())
    assert run["timeout_seconds"] == 120
