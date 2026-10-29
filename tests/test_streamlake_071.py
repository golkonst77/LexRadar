"""Request construction only: synthetic content, credentials, and transport."""

import json

import pytest

from lexradar.auditors.provider import OpenRouterProvider, ProviderError
from lexradar.experiment_069 import experiment_profile


@pytest.mark.parametrize("enabled", [False, True])
def test_streamlake_request(monkeypatch, enabled):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key-071")
    config = experiment_profile()

    class Capture:
        def post(self, payload, key, timeout, max_bytes):
            assert key == "synthetic-key-071"
            assert key.encode() not in payload
            request = json.loads(payload)
            assert request["model"] == "deepseek/deepseek-chat"
            assert request["provider"].get("order") == (["streamlake"] if enabled else None)
            assert request["provider"]["allow_fallbacks"] is False
            assert request["provider"]["require_parameters"] is True
            assert request["provider"]["max_price"] == {"prompt": 1.0, "completion": 2.0}
            assert request["n"] == 1 and request["stream"] is False
            assert timeout == 120
            raise ProviderError("offline_capture")

    provider = OpenRouterProvider(config, transport=Capture(), streamlake=enabled)
    assert config.retries == 0 and str(config.max_budget_usd) == "0.10"
    with pytest.raises(ProviderError, match="offline_capture"):
        provider.complete(config.auditor_a, [{"role": "user", "content": "synthetic"}])


def test_wrong_model_rejected_before_transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key-071")
    config = experiment_profile()
    provider = OpenRouterProvider(config, streamlake=True)
    wrong = config.auditor_a.model_copy(update={"model": "synthetic/wrong"})
    with pytest.raises(ValueError, match="requires deepseek/deepseek-chat"):
        provider.complete(wrong, [])
