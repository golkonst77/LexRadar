"""Paid pilot gates tested only with synthetic saved data and fake transport."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_compact_pipeline import block_network  # noqa: F401
from test_primary import NOW, make_dossier

from lexradar.auditors.compact_pipeline import analyze_compact, main
from lexradar.auditors.models import AnalysisConfig, ModelSettings
from lexradar.auditors.preflight import authorize, screen
from lexradar.auditors.provider import CompactFakeTransport, HTTPReply, ProviderError
from lexradar.primary.compact import prepare

SETTINGS = ModelSettings(
    model="deepseek/deepseek-v3.2",
    prompt_price_cap="0.259",
    completion_price_cap="0.80",
    max_output_tokens=2000,
)
CONFIG = AnalysisConfig(
    single_auditor=True,
    auditor_a=SETTINGS,
    auditor_b=SETTINGS,
    max_budget_usd="0.05",
    retries=0,
)


@pytest.fixture(autouse=True)
def profile_clock(monkeypatch):
    class VerifiedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, tzinfo=UTC)

    monkeypatch.setattr("lexradar.auditors.compact_profile.datetime", VerifiedDate)


@pytest.fixture
def pilot(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Public company information</p>")
    packet = prepare(root, as_of=NOW)
    assert screen(packet) == []
    approval = tmp_path / "approved.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="synthetic-reviewer",
                approved_at=NOW.isoformat(),
            )
        )
    )
    return root, approval


def run(pilot, tmp_path, **kwargs):
    root, approval = pilot
    return analyze_compact(
        root,
        tmp_path / "output",
        CONFIG,
        as_of=NOW,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        ledger=tmp_path / "ledger.json",
        **kwargs,
    )


def test_dry_run_no_network_or_ledger(pilot, tmp_path):
    report = run(pilot, tmp_path, dry_run=True)
    assert report["status"] == "dry_run_ready"
    assert not report["request_sent"] and report["budget_charge_usd"] == "0"
    assert report["preview"]["reserve_usd"]
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "ledger.json.lock").exists()


def test_missing_permission(pilot, tmp_path):
    with pytest.raises(PermissionError):
        analyze_compact(pilot[0], tmp_path / "out", CONFIG, as_of=NOW, mode="openrouter")


def test_wrong_sha_and_changed_source(pilot):
    root, approval = pilot
    data = json.loads(approval.read_text())
    data["packet_sha256"] = "0" * 64
    approval.write_text(json.dumps(data))
    with pytest.raises(PermissionError):
        authorize(prepare(root, as_of=NOW), approval)


@pytest.mark.parametrize("value", ["user@example.test", "+7 999 123 45 67", "ВИЧ", "паспорт"])
def test_screening_cannot_be_bypassed(pilot, value):
    root, approval = pilot
    packet = prepare(root, as_of=NOW)
    # Sensitive evidence in the pooled dictionary is expanded and screened.
    wire = json.loads(packet.payload)
    wire["body"]["new"] = value
    wire["keys"]["new"] = "test_evidence"
    from lexradar.primary.compact import canonical, digest

    payload = canonical(wire)
    packet = replace(packet, payload=payload, sha256=digest(payload))
    data = json.loads(approval.read_text())
    data["packet_sha256"] = packet.sha256
    approval.write_text(json.dumps(data))
    with pytest.raises(PermissionError, match="sensitive"):
        authorize(packet, approval)


@pytest.mark.parametrize("variant", ["known", "unknown", "timeout", "fallback", "overrun"])
def test_fake_paid_transport(pilot, tmp_path, monkeypatch, variant):
    calls = []

    def post(self, payload, key, timeout, max_bytes):
        request = json.loads(payload)
        calls.append(request)
        assert request["reasoning"] == {"enabled": False}
        assert request["provider"]["only"] == ["siliconflow/fp8"]
        assert request["provider"]["allow_fallbacks"] is False
        assert request["provider"]["require_parameters"] is True
        assert request["provider"]["max_price"] == {"prompt": 0.259, "completion": 0.80}
        assert request["response_format"] == {"type": "json_object"}
        assert request["max_tokens"] == 2000
        if variant == "timeout":
            raise ProviderError("transport_timeout", retryable=True)
        reply = CompactFakeTransport().post(payload, key, timeout, max_bytes)
        data = json.loads(reply.body)
        if variant == "unknown":
            data["usage"] = {}
        if variant == "overrun":
            data["usage"]["cost"] = 0.51
        if variant == "fallback":
            data["model"] = "other/model"
        return HTTPReply(200, json.dumps(data).encode())

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", post)
    report = run(pilot, tmp_path)
    assert len(calls) == 1
    assert report["production_outcome"] == "HOLD"
    assert report["legal_status"] == "not_confirmed"
    text = (tmp_path / "output/compact-analysis.json").read_text()
    assert "synthetic-test-key" not in text
    if variant == "known":
        assert report["status"] == "success"
    else:
        assert report["budget_state"] in {"unknown", "overrun"}
        second = analyze_compact(
            pilot[0],
            tmp_path / "again",
            CONFIG,
            as_of=NOW,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=pilot[1],
            ledger=tmp_path / "ledger.json",
            dry_run=True,
        )
        assert second["status"] == "dry_run_blocked"
        assert len(calls) == 1


def test_cli_dry_run(pilot, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(CONFIG.model_dump_json())
    main(
        [
            str(pilot[0]),
            "--output",
            str(tmp_path / "cli"),
            "--config",
            str(config),
            "--as-of",
            NOW.isoformat(),
            "--mode",
            "openrouter",
            "--dry-run",
            "--ledger",
            str(tmp_path / "ledger"),
            "--approval",
            str(pilot[1]),
            "--allow-external-transfer",
        ]
    )
    assert "dry_run_ready; HOLD; openrouter" in capsys.readouterr().out


def test_unverified_profile_and_expiry(pilot, tmp_path, monkeypatch):
    from lexradar.auditors.compact_profile import validate_profile

    for update in ({"retries": 1}, {"single_auditor": False}, {"max_budget_usd": 0.06}):
        with pytest.raises(PermissionError, match="Unverified"):
            validate_profile(CONFIG.model_copy(update=update))
    for update in (
        {"prompt_price_cap": "0.26"},
        {"completion_price_cap": "0.38"},
        {"max_output_tokens": 147457},
    ):
        with pytest.raises(PermissionError, match="Unverified"):
            validate_profile(
                CONFIG.model_copy(update={"auditor_a": SETTINGS.model_copy(update=update)})
            )
    monkeypatch.setattr(
        "lexradar.auditors.compact_profile.VERIFIED_UNTIL", datetime(2000, 1, 1, tzinfo=UTC)
    )
    report = run(pilot, tmp_path, dry_run=True)
    assert report["status"] == "dry_run_blocked"


def test_exhausted_budget_dry_run(pilot, tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "clinic.example": {
                    "limit": "0.05",
                    "entries": [{"id": "spent", "charge": "0.05", "state": "known"}],
                }
            }
        )
    )
    before = ledger.read_bytes()
    assert run(pilot, tmp_path, dry_run=True)["status"] == "dry_run_blocked"
    assert ledger.read_bytes() == before


def test_reserve_system_user_output():
    from decimal import Decimal

    from lexradar.auditors.budget import Budget

    messages = [{"content": "system"}, {"content": "user"}]
    estimate = Budget(Decimal("0.05")).reserve(SETTINGS, messages)
    assert estimate == (Decimal(1034) * Decimal("0.259") + 2000 * Decimal("0.80")) / 1000000


def test_unverified_model(pilot, tmp_path):
    config = CONFIG.model_copy(
        update={"auditor_a": SETTINGS.model_copy(update={"model": "other/model"})}
    )
    with pytest.raises(PermissionError, match="Unverified"):
        analyze_compact(
            pilot[0],
            tmp_path / "invalid",
            config,
            as_of=NOW,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=pilot[1],
            ledger=tmp_path / "ledger",
        )
