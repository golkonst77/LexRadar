import json
from decimal import Decimal

import pytest
from test_offline_mvp_048 import dossier, no_network, ready  # noqa: F401

from lexradar.auditors.models import ProviderDiagnostics, RequestLog, Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.prompts import messages_for as pair_messages
from lexradar.auditors.provider import HTTPReply, ProviderError, ProviderReply
from lexradar.offline_mvp import profile as pair_profile
from lexradar.single_auditor import DeepSeekAdapter, messages_for, profile


@pytest.mark.parametrize("single", [True, False])
@pytest.mark.parametrize("phase", ["open", "read"])
def test_safe_diagnostics_and_retry(ready, tmp_path, phase, single):  # noqa: F811
    config = (profile() if single else pair_profile()).model_copy(update={"retries": 1})

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
            label = "A" if settings.model == config.auditor_a.model else "B"
            return ProviderReply(json.dumps(ready[1][label]), Usage())

    provider = Provider()
    report = analyze(
        ready[0],
        tmp_path / "out",
        config,
        provider=provider,
        message_builder=messages_for if single else pair_messages,
        sleep=lambda _: None,
    )
    assert report.completed is (not single)
    assert provider.calls == len(report.logs) == (1 if single else 3)
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
        assert len(records) == (1 if single else 3)
        assert all(record["provider_diagnostics"] is None for record in records[1:])
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
    adapter.config.retries = 1
    with pytest.raises(ProviderError, match="unexpected_single"):
        adapter.complete(adapter.config.auditor_a, [])
    assert profile().timeout_seconds == 120
    with pytest.raises(ValueError):
        profile(timeout_seconds=60)


def test_mvp_120_and_budget_retry(ready, tmp_path):  # noqa: F811
    from test_auditors import approve_packet

    class Broken:
        calls = 0

        def complete(self, settings, messages):
            self.calls += 1
            raise ProviderError("transport_error", retryable=True)

    config = profile(timeout_seconds=120)
    provider = Broken()
    report = analyze(
        ready[0],
        tmp_path / "paid-fake",
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approve_packet(ready[0]),
        provider=provider,
        message_builder=messages_for,
        mvp_no_screening=True,
        sleep=lambda _: None,
    )
    assert provider.calls == len(report.logs) == 1
    assert not report.completed
    assert report.logs[0].status == "transport_error"
    assert report.budget_charged_usd <= report.budget_limit_usd == Decimal("0.50")
    assert not report.automatic_send_allowed
    run = json.loads((tmp_path / "paid-fake/run.json").read_text())
    assert run["timeout_seconds"] == 120
