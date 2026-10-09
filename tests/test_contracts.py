import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lexradar.approval import send_client_message
from lexradar.models import AuditInput, AuditReport, HumanApproval
from lexradar.report import build_report


def test_no_automatic_send(data):
    report = build_report(data)
    assert not report.automatic_send_allowed
    with pytest.raises(PermissionError):
        send_client_message(report)
    report.human_approval = HumanApproval(
        approved=True, reviewer_id="demo", approved_at="2026-01-01T02:00:00Z"
    )
    with pytest.raises(NotImplementedError):
        send_client_message(report)
    with pytest.raises(ValidationError):
        report.automatic_send_allowed = True


def test_approval_requires_identity():
    with pytest.raises(ValidationError):
        HumanApproval(approved=True)


def test_roundtrip(data):
    report = build_report(data)
    assert AuditReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("change", ["reference", "duplicate", "unknown", "confidence", "timestamp"])
def test_invalid_contract(data, change):
    raw = data.model_dump(mode="json")
    if change == "reference":
        raw["auditors"][0]["findings"][0]["evidence_ids"] = ["missing"]
    elif change == "duplicate":
        raw["evidence"].append(raw["evidence"][0])
    elif change == "unknown":
        raw["send_now"] = True
    elif change == "confidence":
        raw["auditors"][0]["findings"][0]["confidence"] = 1.1
    else:
        raw["evidence"][0]["captured_at"] = "2026-01-01T00:00:00"
    with pytest.raises(ValidationError):
        AuditInput.model_validate(raw)


def test_examples():
    raw = AuditInput.model_validate_json(Path("examples/input.json").read_text())
    assert json.loads(build_report(raw).model_dump_json()) == json.loads(
        Path("examples/result.json").read_text()
    )


def test_no_draft_without_go(data):
    data.evidence[0].available = False
    assert build_report(data).client_message_draft is None


def test_cli(tmp_path):
    import subprocess
    import sys

    output = tmp_path / "report.json"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from lexradar.cli import main; main()",
            "examples/input.json",
            "--output",
            str(output),
        ],
        check=True,
    )
    assert output.read_bytes() == Path("examples/result.json").read_bytes()
