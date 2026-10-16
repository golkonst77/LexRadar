"""Offline checks only; no transfer approvals are created."""

import hashlib
import json
import shutil
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from lexradar.auditors.evidence import load_packet
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.preflight import MVP_NO_SCREENING_NOTICE, authorize, screen
from lexradar.single_auditor import messages_for, profile

ROOT = Path(__file__).resolve().parents[1] / "examples/compact-first-pilot/dossier"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def packet():
    packet = load_packet(ROOT, profile().max_input_bytes)
    payload = json.dumps({"text": "Synthetic patient private@example.org 01.01.2000"})
    return replace(packet, payload=payload, sha256=hashlib.sha256(payload.encode()).hexdigest())


def test_default_blocks_and_enabled_still_requires_approval(packet):
    assert screen(packet)
    with pytest.raises(PermissionError, match="sensitive"):
        authorize(packet, None)
    with pytest.raises(PermissionError, match="sensitive"):
        authorize(packet, None, mvp_no_screening=False)
    with pytest.raises(PermissionError, match="approval"):
        authorize(packet, None, mvp_no_screening=True)
    with pytest.raises(PermissionError, match="hash mismatch"):
        authorize(replace(packet, sha256="0" * 64), None, mvp_no_screening=True)


@pytest.mark.parametrize("enabled", [False, True])
def test_report_and_no_automatic_send(tmp_path, enabled):
    report = analyze(
        ROOT,
        tmp_path / "analysis",
        profile(),
        message_builder=messages_for,
        mvp_no_screening=enabled,
    )
    assert (MVP_NO_SCREENING_NOTICE in report.limitations) is enabled
    assert report.automatic_send_allowed is False
    run = json.loads((tmp_path / "analysis/run.json").read_text())
    assert run["MVP_NO_SCREENING"] is enabled
    assert run["packet_approval"] is None
    assert run["external_transfer_explicitly_permitted"] is False
    assert all(not log.request_sent for log in report.logs)


def test_external_permission_and_single_profile_still_required(tmp_path):
    with pytest.raises(PermissionError, match="Explicit permission"):
        analyze(
            ROOT,
            tmp_path / "out",
            profile(),
            mode="openrouter",
            message_builder=messages_for,
            mvp_no_screening=True,
        )
    with pytest.raises(ValueError, match="Single DeepSeek"):
        analyze(ROOT, tmp_path / "out", profile(), mvp_no_screening=True)
    with pytest.raises(PermissionError, match="approval"):
        analyze(
            ROOT,
            tmp_path / "out",
            profile(),
            mode="openrouter",
            allow_external_transfer=True,
            message_builder=messages_for,
            mvp_no_screening=True,
        )
    assert not (tmp_path / "out").exists()


def test_size_and_artifact_integrity_still_checked(tmp_path):
    from lexradar.auditors.evidence import DossierError

    root = tmp_path / "dossier"
    shutil.copytree(ROOT, root)
    packet = load_packet(root, profile().max_input_bytes)
    artifact = packet.data.evidence[0].artifacts[0]
    (root / artifact.path).write_bytes(b"tampered")
    with pytest.raises(DossierError, match="integrity"):
        analyze(
            root, tmp_path / "out", profile(), message_builder=messages_for, mvp_no_screening=True
        )
    with pytest.raises(DossierError):
        load_packet(ROOT, 1)
