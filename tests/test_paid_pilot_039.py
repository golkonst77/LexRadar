"""Offline safety tests: real paid transport is never invoked."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pilot039", Path(__file__).resolve().parents[1] / "scripts/run_paid_pilot_039.py"
)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def payload():
    return json.dumps({
        "model": pilot.MODEL, "max_tokens": 2000,
        "provider": {"only": [pilot.PROVIDER], "allow_fallbacks": False,
                     "require_parameters": True,
                     "max_price": {"prompt": 0.259, "completion": 0.8}},
        "reasoning": {"enabled": False}, "response_format": {"type": "json_object"}
    }).encode()


def test_timeout_and_restart_never_resend(tmp_path):
    marker = tmp_path / "attempt.json"
    def timeout(*args):
        assert marker.exists()  # Durable claim precedes even an uncertain send.
        raise TimeoutError()
    with (patch.object(pilot, "ATTEMPT", marker),
          patch.object(pilot.OpenRouterTransport, "post", side_effect=timeout) as post):
        with pytest.raises(TimeoutError):
            pilot.OnceTransport().post(payload(), "fake", 1, 100)
        with pytest.raises(FileExistsError):
            pilot.OnceTransport().post(payload(), "fake", 1, 100)
        assert post.call_count == 1


def test_route_change_cannot_claim_or_send(tmp_path):
    marker = tmp_path / "attempt.json"
    changed = json.loads(payload())
    changed["provider"]["only"] = ["other"]
    with (patch.object(pilot, "ATTEMPT", marker),
          patch.object(pilot.OpenRouterTransport, "post") as post):
        with pytest.raises(PermissionError):
            pilot.OnceTransport().post(json.dumps(changed), "fake", 1, 100)
        post.assert_not_called()
        assert not marker.exists()


def test_existing_ledger_attempt_blocks_even_known_zero(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"clinic.example": {"entries": [
        {"state": "known", "charge": "0"}]}}))
    config = pilot.AnalysisConfig.model_validate_json(
        (pilot.REPO / "examples/compact-openrouter-config.json").read_text())
    with (patch.object(pilot, "LEDGER", ledger),
          patch.object(pilot, "analyze_compact") as analyze):
        with pytest.raises(PermissionError, match="Previous site attempt"):
            pilot.local_checks(tmp_path / "dry", config)
        analyze.assert_not_called()


def test_nonfinite_financial_metadata_rejected():
    for value in ("NaN", "Infinity", "-1"):
        with pytest.raises(PermissionError):
            pilot.number(value)


def test_fresh_metadata_and_account_checks():
    config = pilot.AnalysisConfig.model_validate_json(
        (pilot.REPO / "examples/compact-openrouter-config.json").read_text())
    metadata = {"id": pilot.MODEL, "endpoints": [{
        "tag": pilot.PROVIDER, "model_id": pilot.MODEL, "status": 0,
        "supported_parameters": ["reasoning", "response_format", "max_tokens", "temperature"],
        "max_completion_tokens": 147456, "context_length": 163840,
        "pricing": {"prompt": "0.000000259", "completion": "0.0000008"}}]}
    account = {"is_free_tier": False, "limit": 1, "limit_remaining": "0.05"}
    credits = {"total_credits": "1", "total_usage": "0.5"}
    with patch.object(pilot, "get_json", side_effect=[metadata, account, credits]) as get:
        pilot.check_remote(config, pilot.Decimal("0.009756687"), "fake")
        assert get.call_count == 3
    account["limit_remaining"] = "0.001"
    with patch.object(pilot, "get_json", side_effect=[metadata, account]) as get:
        with pytest.raises(PermissionError, match="Key limit insufficient"):
            pilot.check_remote(config, pilot.Decimal("0.009756687"), "fake")
        assert get.call_count == 2
    metadata["endpoints"][0]["status"] = -2
    with patch.object(pilot, "get_json", return_value=metadata):
        with pytest.raises(PermissionError, match="Endpoint inactive"):
            pilot.check_remote(config, pilot.Decimal("0.009756687"), "fake")
