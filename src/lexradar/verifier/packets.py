"""Stage I API has no auditor-input parameter; Stage II reads A/B only after persistence."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..auditors.evidence import EvidencePacket, load_packet
from ..auditors.models import AnalysisReport
from .materials import inventory
from .models import Material, SourceRegistry, VerifierConfig
from .registry import assess_registry, digest, load_registry


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def entity_refs(packet: EvidencePacket) -> dict[str, dict]:
    return {
        f"entity-{index:04d}": entity.model_dump(mode="json")
        for index, entity in enumerate(packet.data.entities, 1)
    }


def wrap(base: EvidencePacket, value: dict, maximum: int) -> EvidencePacket:
    payload = canonical(value)
    if len(payload.encode()) > maximum:
        raise ValueError("Verifier packet exceeds input limit; no silent truncation")
    return EvidencePacket(
        base.data, payload, digest(payload), base.manifest_sha256, base.limitations
    )


@dataclass(frozen=True)
class Prepared:
    packet: EvidencePacket
    registry: SourceRegistry
    registry_sha256: str
    materials: list[Material]


def prepare_independent(root: Path, registry_path: Path, config: VerifierConfig) -> Prepared:
    base = load_packet(root, config.max_input_bytes)
    registry, registry_hash = load_registry(registry_path)
    materials = inventory(base, root)
    # Recheck integrity after the worker has read PDF files. The outgoing snapshot is immutable.
    if load_packet(root, config.max_input_bytes).manifest_sha256 != base.manifest_sha256:
        raise ValueError("Collector changed during verifier preparation")
    collector = json.loads(base.payload)
    # Only re-extracted, page-addressable PDF text is supplied for verifier examination.
    for document in collector["untrusted_collector_data"]["documents"]:
        document["text"] = None
    packet = wrap(
        base,
        {
            "stage": "independent",
            "untrusted_collector": collector,
            "materials": [m.model_dump(mode="json") for m in materials],
            "operator_observations": entity_refs(base),
            "legal_source_registry": registry.model_dump(mode="json"),
            "registry_sha256": registry_hash,
            "normative_assessments": [
                n.model_dump(mode="json")
                for n in assess_registry(
                    registry,
                    base.data.started_at.date(),
                    datetime.now(UTC),
                    config.legal_recheck_days,
                ).values()
            ],
            "limitations": [
                *base.limitations,
                "Norm records require local provenance checks",
                "Entity observations are not confirmed operator identities",
            ],
        },
        config.max_input_bytes,
    )
    return Prepared(packet, registry, registry_hash, materials)


def load_analysis(path: Path, packet: EvidencePacket) -> AnalysisReport:
    if path.is_symlink() or path.stat().st_size > 6_000_000:
        raise ValueError("Unsafe/oversized auditor report")
    analysis = AnalysisReport.model_validate_json(path.read_text(encoding="utf-8"))
    if analysis.collector_manifest_sha256 != packet.manifest_sha256:
        raise ValueError("Auditor report belongs to another dossier")
    if not analysis.completed:
        raise ValueError("Both auditors must have a complete validated result for comparison")
    evidence = {e.id: e for e in packet.data.evidence}
    ids = set(evidence)
    for run in analysis.runs:
        for finding in run.result.findings:
            if (
                len(finding.evidence_ids) != len(set(finding.evidence_ids))
                or not set(finding.evidence_ids) <= ids
            ):
                raise ValueError("Auditor evidence references unknown materials")
            if finding.source not in {evidence[eid].source for eid in finding.evidence_ids}:
                raise ValueError("Unsupported auditor source URL")
    return analysis


def prepare_comparison(
    prepared: Prepared, stage_one: dict, analysis: AnalysisReport, maximum: int
) -> EvidencePacket:
    return wrap(
        prepared.packet,
        {
            "stage": "comparison",
            "independent_result": stage_one,
            "untrusted_auditor_results": [r.result.model_dump(mode="json") for r in analysis.runs],
            "materials": [m.model_dump(mode="json") for m in prepared.materials],
            "operator_observations": entity_refs(prepared.packet),
            "legal_source_registry": prepared.registry.model_dump(mode="json"),
            "normative_assessments": json.loads(prepared.packet.payload)["normative_assessments"],
            "limitations": list(prepared.packet.limitations),
        },
        maximum,
    )
