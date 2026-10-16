"""Historical hash fallback must never accept altered prompts."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from lexradar.audit_review import export
from lexradar.auditors.evidence import load_packet
from lexradar.single_auditor import messages_for


def test_historical_schema_exact_hash():
    root = Path("runs-064/doktstom-deepseek-chat-001")
    report = json.loads((root / "analysis.json").read_text())
    packet = load_packet(Path("runs-054/doktstom-improved/dossier"), 200_000)
    messages, version, digest = messages_for("A", packet, historical_schema=True)
    assert digest == hashlib.sha256(messages[0]["content"].encode()).hexdigest()
    assert all(log["prompt_sha256"] == digest for log in report["logs"])
    assert version == "single-a-v1"
    assert messages_for("A", packet)[2] != digest


@pytest.mark.parametrize(
    "field,value", [("prompt_sha256", "0" * 64), ("prompt_version", "unknown-version")]
)
def test_historical_fallback_rejects_tampering(tmp_path, field, value):
    source = Path("runs-064/doktstom-deepseek-chat-001")
    audit = tmp_path / "audit"
    shutil.copytree(source, audit)
    report = json.loads((audit / "analysis.json").read_text())
    for log in report["logs"]:
        log[field] = value
    (audit / "analysis.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Reconstructed prompt hash mismatch"):
        export(audit, tmp_path / "export", dossier=Path("runs-054/doktstom-improved/dossier"))
