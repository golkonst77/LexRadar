"""Synthetic compact AI acceptance; all network access forbidden."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from test_primary import NOW

from lexradar.auditors.budget import BudgetExceeded
from lexradar.auditors.compact_pipeline import analyze_compact, messages_for_compact
from lexradar.auditors.compact_validator import validate_compact
from lexradar.auditors.models import AnalysisConfig, ModelSettings, Usage
from lexradar.auditors.provider import CompactFakeTransport, OpenRouterProvider
from lexradar.auditors.site_budget import SiteBudget
from lexradar.auditors.validator import ResultError
from lexradar.primary.compact import prepare

ROOT = Path(__file__).resolve().parents[1] / "examples/primary-audit/dossier"
SETTINGS = ModelSettings(
    model="synthetic/model", prompt_price_cap="1", completion_price_cap="1", max_output_tokens=128
)
CONFIG = AnalysisConfig(auditor_a=SETTINGS, auditor_b=SETTINGS, max_budget_usd="0.05")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network forbidden")

    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_offline_success_and_repeat(tmp_path):
    report = analyze_compact(ROOT, tmp_path / "one", CONFIG, as_of=NOW)
    assert report["status"] == "success"
    assert report["result"]["observations"]
    assert report["production_outcome"] == "HOLD"
    assert report["legal_status"] == "not_confirmed"
    assert not report["request_sent"] and report["budget_charge_usd"] == "0"
    assert json.loads((tmp_path / "one/preflight.json").read_text())["suspicions"]
    with pytest.raises(FileExistsError):
        analyze_compact(ROOT, tmp_path / "one", CONFIG, as_of=NOW)
    second = analyze_compact(ROOT, tmp_path / "two", CONFIG, as_of=NOW)
    assert report == second


def test_invalid_quotes_and_legal_confirmation():
    packet = prepare(ROOT, as_of=NOW)
    provider = OpenRouterProvider(CONFIG, CompactFakeTransport(), offline=True)
    raw = json.loads(provider.complete(SETTINGS, messages_for_compact(packet)).content)
    raw["observations"][0]["quote"] = "Fabricated quote not present"
    with pytest.raises(ResultError):
        validate_compact(json.dumps(raw), ROOT, packet)
    raw["observations"][0]["status"] = "confirmed"
    with pytest.raises(ResultError):
        validate_compact(json.dumps(raw), ROOT, packet)


def test_paid_preflight_refusal(tmp_path):
    with pytest.raises(PermissionError, match="sensitive"):
        analyze_compact(
            ROOT,
            tmp_path / "blocked",
            CONFIG,
            as_of=NOW,
            mode="openrouter",
            allow_external_transfer=True,
            ledger=tmp_path / "ledger.json",
        )
    assert not (tmp_path / "ledger.json").exists()


def test_durable_repeat_and_exhaustion(tmp_path):
    path = tmp_path / "budget.json"
    messages = [{"role": "user", "content": "fixture"}]
    first = SiteBudget(path, "fixture.test", Decimal("0.002"))
    token = first.reserve(SETTINGS, messages)
    with pytest.raises(BudgetExceeded):
        SiteBudget(path, "fixture.test").reserve(SETTINGS, messages)
    first.settle(token, Usage(cost_usd="0.001"))
    with pytest.raises(BudgetExceeded):
        SiteBudget(path, "fixture.test").reserve(SETTINGS, messages)
    assert json.loads(path.read_text())["fixture.test"]["limit"] == "0.002"


def test_unknown_crash_and_overrun(tmp_path):
    messages = [{"content": "fixture"}]
    ledger = SiteBudget(tmp_path / "ledger.json", "fixture.test")
    token = ledger.reserve(SETTINGS, messages)
    ledger.settle(token, Usage())
    with pytest.raises(BudgetExceeded):
        ledger.reserve(SETTINGS, messages)
    overrun = SiteBudget(tmp_path / "overrun.json", "fixture.test")
    token = overrun.reserve(SETTINGS, messages)
    overrun.settle(token, Usage(cost_usd="0.51"))
    with pytest.raises(BudgetExceeded):
        overrun.reserve(SETTINGS, messages)
    with pytest.raises(ValueError):
        SiteBudget(tmp_path / "bad.json", "fixture.test", "0.501")


def test_parallel_reservation(tmp_path):
    def reserve(_):
        try:
            return SiteBudget(tmp_path / "ledger.json", "fixture.test").reserve(
                SETTINGS, [{"content": "fixture"}]
            )
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert sum(value is not None for value in results) == 1


def test_invalid_response_pipeline(tmp_path, monkeypatch):
    original = CompactFakeTransport.post

    def forged(self, *args):
        from lexradar.auditors.provider import HTTPReply

        response = original(self, *args)
        envelope = json.loads(response.body)
        result = json.loads(envelope["choices"][0]["message"]["content"])
        result["observations"][0]["quote"] = "Invented quote"
        envelope["choices"][0]["message"]["content"] = json.dumps(result)
        return HTTPReply(200, json.dumps(envelope).encode())

    monkeypatch.setattr(CompactFakeTransport, "post", forged)
    report = analyze_compact(ROOT, tmp_path / "invalid", CONFIG, as_of=NOW)
    assert report["status"] == "invalid_compact_response_or_quote"
    assert report["result"] is None
    assert report["production_outcome"] == "HOLD"


def test_corrupt_ledger_and_crash(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = SiteBudget(path, "fixture.test")
    ledger.reserve(SETTINGS, [{"content": "fixture"}])
    # A new process after a crash sees the on-disk pending reservation.
    with pytest.raises(BudgetExceeded):
        SiteBudget(path, "fixture.test").reserve(SETTINGS, [{"content": "fixture"}])
    data = json.loads(path.read_text())
    data["fixture.test"]["entries"][0]["charge"] = "-1"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        ledger.reserve(SETTINGS, [{"content": "fixture"}])


def test_offline_requires_fake_transport():
    from lexradar.auditors.provider import OpenRouterTransport

    with pytest.raises(PermissionError):
        OpenRouterProvider(CONFIG, OpenRouterTransport(), offline=True)


def test_compact_cli(tmp_path, monkeypatch, capsys):
    from lexradar.cli import main

    config = ROOT.parents[1] / "compact-offline-config.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "compact-analyze",
            str(ROOT),
            "--output",
            str(tmp_path / "cli"),
            "--config",
            str(config),
            "--as-of",
            NOW.isoformat(),
        ],
    )
    main()
    assert "success; HOLD" in capsys.readouterr().out
    report = json.loads((tmp_path / "cli/compact-analysis.json").read_text())
    assert report["simulated"] and not report["request_sent"]
