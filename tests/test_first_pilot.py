"""Saved first-pilot fixture: real preparation, synthetic approval, no network."""

import json
from datetime import UTC, datetime
from pathlib import Path

from test_compact_paid import CONFIG
from test_compact_pipeline import block_network  # noqa: F401
from test_primary import NOW

from lexradar.auditors.compact_pipeline import analyze_compact
from lexradar.collector.artifacts import verify_integrity
from lexradar.collector.models import CollectionResult
from lexradar.primary.compact import prepare, unpack, verify_quotes

ROOT = Path(__file__).resolve().parents[1] / "examples/compact-first-pilot/dossier"


def test_first_pilot_ready_and_exhausted_budget(tmp_path, monkeypatch):
    class VerifiedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, tzinfo=UTC)

    monkeypatch.setattr("lexradar.auditors.compact_profile.datetime", VerifiedDate)
    data = CollectionResult.model_validate_json((ROOT / "collection.json").read_text())
    assert verify_integrity(data, ROOT) == []
    assert len(data.pages) == 1 and len(data.documents) == 3
    packet = prepare(ROOT, as_of=NOW)
    verify_quotes(ROOT, packet)
    assert any(e["quote"] for e in unpack(packet)["excerpts"].values())
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="synthetic-test-only",
                approved_at=NOW.isoformat(),
            )
        )
    )
    ledger = tmp_path / "ledger.json"
    kwargs = dict(
        as_of=NOW,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        ledger=ledger,
        dry_run=True,
    )
    report = analyze_compact(ROOT, tmp_path / "ready", CONFIG, **kwargs)
    assert report["status"] == "dry_run_ready"
    assert report["approval"]["packet_sha256"] == packet.sha256
    assert report["preview"]["sensitive_categories"] == []
    assert report["preview"]["packet_bytes"] <= 40000
    assert not report["request_sent"] and report["budget_charge_usd"] == "0"
    assert not ledger.exists() and not ledger.with_suffix(".json.lock").exists()
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
    blocked = analyze_compact(ROOT, tmp_path / "blocked", CONFIG, **kwargs)
    assert blocked["status"] == "dry_run_blocked"
    kwargs["dry_run"] = False
    import pytest

    from lexradar.auditors.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        analyze_compact(ROOT, tmp_path / "paid-refused", CONFIG, **kwargs)
    assert ledger.read_bytes() == before
