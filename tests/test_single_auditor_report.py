"""Regression tests for optional single-auditor reports."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from lexradar.auditors.models import AnalysisReport, AuditorRun


def report(runs, same_model=False):
    return AnalysisReport(
        mode="offline",
        collector_manifest_sha256="manifest",
        evidence_packet_sha256="packet",
        started_at=datetime.now(UTC),
        completed=False,
        runs=runs,
        disagreements=[],
        logs=[],
        budget_charged_usd=Decimal("0"),
        budget_limit_usd=Decimal("1"),
        same_model_independence_limited=same_model,
        limitations=[],
    )


def failed_run(label, model="example/model"):
    return AuditorRun(auditor=label, model=model, prompt_version="v1", status="provider_error")


def test_single_auditor_report_is_valid():
    result = report([failed_run("A")])
    assert len(result.runs) == 1
    assert result.same_model_independence_limited is False


def test_two_auditor_report_remains_valid():
    result = report([failed_run("A"), failed_run("B")], same_model=True)
    assert len(result.runs) == 2


@pytest.mark.parametrize("runs", [[], [failed_run("B")], [failed_run("A"), failed_run("A")]])
def test_invalid_auditor_sets_rejected(runs):
    with pytest.raises(ValidationError):
        report(runs)


def test_single_auditor_cannot_claim_same_model_independence_limitation():
    with pytest.raises(ValidationError):
        report([failed_run("A")], same_model=True)
