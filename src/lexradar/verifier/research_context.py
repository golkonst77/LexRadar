"""Preserve research declarations and report only operations actually performed offline."""

import hashlib
from pathlib import Path

from .legal_cards import card_hash, read_local
from .requirement_models import NormResearchContext


def research_context(source, registry, event, source_assessment):
    claim = next((c for c in registry.research_contexts if c.source_card_id == source.id), None)
    if claim is None:
        return None
    operations, failures = [], []
    if source_assessment is None or source_assessment.technical_integrity != "consistent":
        failures.append("Normative source bytes/quote unavailable, inconsistent or ambiguous")
    else:
        operations.append("Compared saved normative source hash and exact quote")
    if claim.card_sha256 != card_hash(source):
        failures.append("Source card binding changed")
    else:
        operations.append("Compared exact source card hash")
    if claim.provision != source.provision or claim.event_date != event:
        failures.append("Provision or control date binding changed")
    else:
        operations.append("Compared exact provision and control date")
    if claim.declared_revision != source.revision:
        failures.append("Declared edition binding changed")
    for material in claim.materials:
        relative = Path(material.path)
        try:
            if relative.is_absolute() or ".." in relative.parts or registry._source_root is None:
                raise ValueError("Unsafe research path")
            raw = read_local(registry._source_root / relative)
            if hashlib.sha256(raw).hexdigest() != material.sha256:
                raise ValueError("Research material changed")
            operations.append(f"Read saved bytes and compared SHA-256: {material.path}")
        except (OSError, ValueError):
            failures.append(f"Research material missing/unsafe/changed: {material.path}")
    return NormResearchContext(
        imported_claim=claim,
        binding_status="invalidated" if failures else "consistent",
        mechanism_operations=operations,
        limitations=[
            *claim.limitations,
            *failures,
            "Imported editorial conclusion and user attestation are not authenticated",
            "Byte/metadata checks do not establish source origin, current law or applicability",
            "No independent official portal or amendment-history verification performed",
        ],
    )
