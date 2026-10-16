"""Reconciliation and second-pilot safety, entirely offline."""
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from lexradar.auditors.reconciliation import apply_reviewed, propose
from lexradar.auditors.site_budget import SiteBudget

SPEC = importlib.util.spec_from_file_location(
    "pilot042", Path(__file__).resolve().parents[1] / "scripts/run_paid_pilot_042.py")
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def evidence(cost="0.002864425", **overrides):
    return json.dumps({"data": dict(id=pilot.GENERATION, request_id=pilot.REQUEST,
        model="deepseek/deepseek-v3.2", provider_name="SiliconFlow", total_cost=cost,
        finish_reason=None, **overrides)}).encode()


@pytest.fixture
def budget(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"clinic.example": {"limit": "0.05", "entries": [
        {"id": pilot.TOKEN, "state": "unknown", "charge": "0.009756687"}]}}))
    return SiteBudget(path, "clinic.example")


def proposal(budget, raw):
    return propose(budget, pilot.TOKEN, raw, generation_id=pilot.GENERATION,
                   request_id=pilot.REQUEST, reviewer="test reviewer")


def test_proposal_readonly_and_atomic_audit_and_replay(budget):
    before = budget.path.read_bytes()
    plan = proposal(budget, evidence())
    assert budget.path.read_bytes() == before
    assert plan["result_usable"] is False
    apply_reviewed(budget, plan)
    entry = json.loads(budget.path.read_bytes())[budget.site]["entries"][0]
    assert entry["charge"] == "0.002864425" and entry["state"] == "known"
    assert entry["reconciliation"]["before"]["charge"] == "0.009756687"
    with pytest.raises(ValueError):
        apply_reviewed(budget, plan)


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "-0.1"])
def test_invalid_cost_does_not_write(budget, cost):
    before = budget.path.read_bytes()
    with pytest.raises(ValueError):
        proposal(budget, evidence(cost))
    assert budget.path.read_bytes() == before


def test_wrong_identity_and_stale_plan(budget):
    raw = json.loads(evidence())
    raw["data"]["request_id"] = "wrong"
    with pytest.raises(ValueError):
        proposal(budget, json.dumps(raw).encode())
    plan = proposal(budget, evidence())
    budget.path.write_text(budget.path.read_text() + "\n")
    with pytest.raises(ValueError):
        apply_reviewed(budget, plan)


def test_overrun_stays_blocked(budget):
    plan = proposal(budget, evidence("0.02"))
    apply_reviewed(budget, plan)
    assert json.loads(budget.path.read_bytes())[budget.site]["entries"][0]["state"] == "overrun"


def test_second_marker_survives_timeout_and_restart(tmp_path):
    body = json.dumps({"model": pilot.first.MODEL, "max_tokens": 2000, "stream": False,
        "provider": {"only": [pilot.first.PROVIDER], "allow_fallbacks": False,
                     "require_parameters": True,
                     "max_price": {"prompt": 0.259, "completion": 0.8}},
        "reasoning": {"enabled": False}, "response_format": {"type": "json_object"}}).encode()
    with (patch.object(pilot, "WORK", tmp_path),
          patch.object(pilot.first.OpenRouterTransport, "post", side_effect=TimeoutError) as post):
        with pytest.raises(TimeoutError):
            pilot.SecondOnceTransport().post(body, "fake", 30, 10000)
        with pytest.raises(FileExistsError):
            pilot.SecondOnceTransport().post(body, "fake", 30, 10000)
        assert post.call_count == 1
        assert (tmp_path / "attempt-042.json").exists()
        bad = json.loads(body)
        bad["stream"] = True
        with pytest.raises(PermissionError):
            pilot.SecondOnceTransport().post(json.dumps(bad).encode(), "fake", 30, 10000)
    assert Decimal("0.002864425") + Decimal("0.009756687") == Decimal("0.012621112")


def test_execute_requires_separate_explicit_authorization(tmp_path):
    with patch.object(pilot.first.OpenRouterTransport, "post") as post:
        with pytest.raises(PermissionError):
            pilot.execute(None)
        with pytest.raises(PermissionError):
            pilot.execute(pilot.first.APPROVAL)
        approval = tmp_path / "approval.json"
        approval.write_text(json.dumps({"approved": True, "packet_sha256": pilot.first.SHA}))
        with pytest.raises(PermissionError):
            pilot.execute(approval)
        post.assert_not_called()


@pytest.fixture
def metadata_paths(tmp_path, monkeypatch):
    work = tmp_path / 'second'
    key = tmp_path / 'key'
    key.write_text('fake-secret-do-not-print')
    monkeypatch.setattr(pilot, 'WORK', work)
    monkeypatch.setattr(pilot, 'LEGACY_WORK', tmp_path / 'legacy')
    monkeypatch.setattr(pilot.first, 'KEY', key)
    return work, key


def test_metadata_preflight_offline(metadata_paths, monkeypatch, capsys):
    work, _ = metadata_paths
    monkeypatch.setattr(pilot, 'prepare', lambda: {'request_sent': False})
    monkeypatch.setattr('sys.argv', ['pilot', '--check-metadata'])
    with patch.object(pilot.first, 'get_json') as get:
        pilot.main()
        get.assert_not_called()
    assert list(work.iterdir()) == []
    assert 'fake-secret' not in capsys.readouterr().out
    assert work.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('name', ['metadata-042-attempt.json', 'metadata-042.json',
                                 'attempt-042.json'])
def test_existing_evidence_blocks_before_key(metadata_paths, name):
    work, key = metadata_paths
    work.mkdir()
    marker = work / name
    marker.write_text('keep')
    key.unlink()
    with pytest.raises(pilot.LocalFailure, match='existing-evidence'):
        pilot.metadata_preflight()
    assert marker.read_text() == 'keep'


def test_legacy_and_symlink_refused(metadata_paths):
    pilot.LEGACY_WORK.mkdir()
    with pytest.raises(pilot.LocalFailure, match='metadata-paths'):
        pilot.metadata_preflight()
    assert not pilot.WORK.exists()
    pilot.LEGACY_WORK.rmdir()
    pilot.WORK.symlink_to(pilot.LEGACY_WORK)
    with pytest.raises(pilot.LocalFailure, match='metadata-paths'):
        pilot.metadata_preflight()


def test_permission_diagnostic_redacts_exception(metadata_paths):
    with patch.object(Path, 'mkdir', side_effect=PermissionError(13, 'fake-secret')):
        with pytest.raises(pilot.LocalFailure) as error:
            pilot.metadata_preflight()
    assert 'errno=13' in str(error.value)
    assert str(pilot.WORK) in str(error.value)
    assert 'fake-secret' not in str(error.value)


def test_key_failure_does_not_claim(metadata_paths, monkeypatch):
    work, key = metadata_paths
    key.unlink()
    monkeypatch.setattr(pilot, 'prepare', lambda: {})
    monkeypatch.setattr('sys.argv', ['pilot', '--metadata'])
    with patch.object(pilot.first, 'get_json') as get:
        with pytest.raises(pilot.LocalFailure, match='metadata-key-read'):
            pilot.main()
        get.assert_not_called()
    assert not (work / 'metadata-042-attempt.json').exists()


def test_metadata_failure_retains_claim_and_blocks_retry(metadata_paths, monkeypatch):
    work, _ = metadata_paths
    monkeypatch.setattr(pilot, 'prepare', lambda: {})
    monkeypatch.setattr('sys.argv', ['pilot', '--metadata'])
    with patch.object(pilot.first, 'get_json', side_effect=OSError(13, 'fake-secret')) as get:
        pilot.main()
        with pytest.raises(pilot.LocalFailure, match='existing-evidence'):
            pilot.main()
        assert get.call_count == 1
    assert (work / 'metadata-042-attempt.json').exists()
    evidence = (work / 'metadata-042.json').read_text()
    assert 'fake-secret' not in evidence
    assert json.loads(evidence)['errno'] == 13
