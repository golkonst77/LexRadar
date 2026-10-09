"""Local conservative screening and approval of an immutable outgoing packet."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from pydantic import AwareDatetime, Field

from ..models import Model
from .evidence import EvidencePacket

LIMITATION = (
    "Heuristic screening cannot guarantee absence of personal or sensitive data; "
    "human review required"
)


class PacketApproval(Model):
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool = Field(strict=True)
    contents_reviewed: bool = Field(strict=True)
    reviewer: str = Field(min_length=1, max_length=200)
    approved_at: AwareDatetime


PATTERNS = {
    "email": r"[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}",
    "phone_or_identifier": r"(?<!\d)(?:\+?\d[\s().-]*){10,}(?!\d)",
    "identity_or_health_context": (
        r"паспорт|снилс|дата\s+рождения|фио|фамилия|patient|diagnos|passport|"
        r"date.of.birth|medical.record|health|disease|pregnan|biometric|"
        r"диагноз|пациент|анамнез|результат.{0,20}анализ|медицинск.{0,20}карт|"
        r"hiv|вич|беремен|инвалид|биометр|полис\s+омс"
    ),
    "person_name": r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b",
    "birth_date": r"\b\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}\b",
}


def screen(packet: EvidencePacket) -> list[dict[str, str]]:
    """Return locations/categories only, never the sensitive matched contents."""
    issues = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, str):
            normalized = unicodedata.normalize("NFKC", unquote(unquote(value)))
            normalized = "".join(c for c in normalized if unicodedata.category(c) != "Cf")
            for category, pattern in PATTERNS.items():
                flags = 0 if category == "person_name" else re.IGNORECASE
                if re.search(pattern, normalized, flags):
                    issues.append({"path": path, "category": category})

    visit(json.loads(packet.payload), "$")
    return issues


def authorize(packet: EvidencePacket, approval_path: Path | None) -> PacketApproval:
    if hashlib.sha256(packet.payload.encode()).hexdigest() != packet.sha256:
        raise PermissionError("Packet snapshot hash mismatch")
    if screen(packet):
        raise PermissionError(
            "Potential sensitive data: transfer blocked; review/redact and repeat"
        )
    if approval_path is None:
        raise PermissionError("Human approval of exact packet SHA-256 required")
    if approval_path.stat().st_size > 10_000:
        raise PermissionError("Oversized approval")
    approval = PacketApproval.model_validate_json(approval_path.read_text(encoding="utf-8"))
    if (
        not approval.approved
        or not approval.contents_reviewed
        or not approval.reviewer.strip()
        or approval.packet_sha256 != packet.sha256
        or approval.approved_at > datetime.now(UTC)
    ):
        raise PermissionError("Missing, stale or invalid packet-specific approval")
    return approval


def write_preflight(packet: EvidencePacket, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "packet.json").write_text(packet.payload, encoding="utf-8")
    (output / "preflight.json").write_text(
        json.dumps(
            {
                "packet_sha256": packet.sha256,
                "suspicions": screen(packet),
                "limitation": LIMITATION,
                "transfer_allowed": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output / "approval.json").write_text(
        json.dumps(
            {
                "packet_sha256": packet.sha256,
                "approved": False,
                "contents_reviewed": False,
                "reviewer": "",
                "approved_at": None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
