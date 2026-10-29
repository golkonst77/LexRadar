"""Repeat task 34 dry-run with network prohibited and no human approval."""

import json
import socket
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from lexradar.auditors.compact_pipeline import analyze_compact, messages_for_compact
from lexradar.auditors.compact_profile import PROVIDER, validate_profile
from lexradar.auditors.models import AnalysisConfig
from lexradar.auditors.provider import OpenRouterTransport
from lexradar.auditors.site_budget import SiteBudget
from lexradar.collector.artifacts import verify_integrity
from lexradar.collector.models import CollectionResult
from lexradar.primary.compact import prepare, verify_quotes

BASE = Path(__file__).resolve().parent


def forbidden(*args, **kwargs):
    raise AssertionError("Network/POST forbidden in task 34 dry-run")


def main():
    root = BASE / "dossier"
    output = BASE / "dry-run-034"
    ledger = Path("/tmp/lexradar-034-pilot-ledger.json")
    if output.exists() or ledger.exists() or ledger.with_suffix(".json.lock").exists():
        raise FileExistsError("Preserve previous output/ledger; use a new isolated run path")
    config = AnalysisConfig.model_validate_json(
        (BASE.parent / "compact-openrouter-config.json").read_text()
    )
    as_of = datetime.fromisoformat("2026-10-10T00:00:00+00:00")
    with (
        patch.object(socket.socket, "connect", forbidden),
        patch.object(socket, "create_connection", forbidden),
        patch.object(OpenRouterTransport, "post", forbidden),
    ):
        data = CollectionResult.model_validate_json((root / "collection.json").read_text())
        assert verify_integrity(data, root) == []
        packet = prepare(root, as_of=as_of)
        verify_quotes(root, packet)
        assert packet.payload.encode() == (BASE / "review/packet.json").read_bytes()
        # Independent read-only checks, without bypassing the approval gate.
        validate_profile(config)
        reserve = SiteBudget(ledger, "clinic.example", config.max_budget_usd).preview(
            config.auditor_a, messages_for_compact(packet)
        )
        report = analyze_compact(
            root,
            output,
            config,
            as_of=as_of,
            mode="openrouter",
            allow_external_transfer=True,
            packet_approval=None,
            ledger=ledger,
            dry_run=True,
        )
        assert report["status"] == "dry_run_blocked"
        assert report["blockers"] == ["Human approval of exact packet SHA-256 required"]
        assert report["approval"] is None and report["usage"] is None
        assert report["request_sent"] is False and report["budget_charge_usd"] == "0"
        assert report["preview"]["sensitive_categories"] == []
        assert not ledger.exists() and not ledger.with_suffix(".json.lock").exists()
        checks = dict(
            integrity="passed",
            quotes="passed",
            saved_packet_bytes="identical",
            profile="passed",
            isolated_site_budget="passed",
            provider=PROVIDER,
            reserve_usd=str(reserve),
            network="prohibited by guards; no calls",
            approval_created=False,
            ledger_created=False,
        )
        (output / "local-checks.json").write_text(json.dumps(checks, indent=2) + "\n")
        print(json.dumps(checks, indent=2))
        print(report["status"], report["blockers"])


if __name__ == "__main__":
    main()
