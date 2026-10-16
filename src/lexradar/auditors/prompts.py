"""Immutable versioned prompt registry; each request starts with exactly two fresh messages."""

import hashlib
import json
from importlib.resources import files

from .evidence import EvidencePacket
from .models import AIAuditorResult

VERSIONS = {"A": "a-v1", "B": "b-v1"}


def messages_for(auditor: str, packet: EvidencePacket) -> tuple[list[dict[str, str]], str, str]:
    version = VERSIONS[auditor]
    system = (
        files("lexradar.auditors").joinpath(f"prompts/{version}.txt").read_text(encoding="utf-8")
    )
    system += (
        "\nAuditor label: "
        + auditor
        + "\nOutput JSON schema:\n"
        + json.dumps(AIAuditorResult.model_json_schema(), ensure_ascii=False)
    )
    return (
        [{"role": "system", "content": system}, {"role": "user", "content": packet.payload}],
        version,
        hashlib.sha256(system.encode()).hexdigest(),
    )
