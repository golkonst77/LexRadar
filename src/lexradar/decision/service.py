"""Read-only local boundary. No demo Gateway, LLM, browser or trusted-status importer."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ..auditors.evidence import load_packet
from ..verifier.registry import assess_registry, digest, load_registry
from .models import HumanReviewSubmission, ProductionDecision, ReviewAssessment, ReviewBinding


class DecisionInputError(ValueError):
    """Safe reason code, without input contents or sensitive filesystem diagnostics."""


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_bytes(path: Path, maximum: int = 6_000_000) -> bytes:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise DecisionInputError("unsafe_or_missing_input")
    # Bound reads even if a file grows after stat; never follow a configured remote URL.
    with path.open("rb") as file:
        content = file.read(maximum + 1)
    if len(content) > maximum:
        raise DecisionInputError("input_size_limit")
    return content


def json_object(content: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DecisionInputError("duplicate_json_key")
            result[key] = value
        return result

    def constant(value):
        raise DecisionInputError("nonfinite_json_number")

    value = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise DecisionInputError("json_object_required")
    return value


def _snapshot(root, finding_path, registry_path, client_text, imported_report):
    manifest = read_bytes(root / "collection.json", 20_000_000)
    json_object(manifest)
    packet = load_packet(root, 2_000_000)
    if packet.manifest_sha256 != sha256(manifest):
        raise DecisionInputError("dossier_changed_during_read")
    finding_raw = read_bytes(finding_path)
    finding = json_object(finding_raw)
    references = finding.get("evidence_ids")
    evidence = {e.id: e for e in packet.data.evidence}
    if (
        not isinstance(references, list)
        or not references
        or any(not isinstance(eid, str) for eid in references)
        or len(references) != len(set(references))
        or not set(references) <= evidence.keys()
        or finding.get("source") not in {str(evidence[eid].source) for eid in references}
    ):
        raise DecisionInputError("unsupported_finding_references")
    registry_raw = read_bytes(registry_path)
    json_object(registry_raw)
    registry, registry_hash = load_registry(registry_path)
    if registry_hash != sha256(registry_raw):
        raise DecisionInputError("registry_changed_during_read")
    if any(digest(s.norm_text) != s.text_sha256 for s in registry.sources):
        raise DecisionInputError("norm_text_hash_mismatch")
    client_raw = None if client_text is None else read_bytes(client_text, 100_000)
    if client_raw is not None:
        client_raw.decode("utf-8")  # Hash exact bytes, preserving newlines/Unicode/empty text.
    report_raw = None if imported_report is None else read_bytes(imported_report)
    if report_raw is not None:
        json_object(report_raw)  # Even a schema-valid verified report is only an imported claim.
    inventory = [
        {"evidence_id": e.id, **a.model_dump(mode="json")}
        for e in packet.data.evidence
        for a in e.artifacts
    ]
    inventory_raw = json.dumps(inventory, sort_keys=True, ensure_ascii=False).encode("utf-8")
    binding = ReviewBinding(
        dossier_sha256=sha256(manifest),
        artifact_inventory_sha256=sha256(inventory_raw),
        finding_sha256=sha256(finding_raw),
        normative_basis_sha256=sha256(registry_raw),
        client_text_sha256=sha256(client_raw) if client_raw is not None else None,
        imported_report_sha256=sha256(report_raw) if report_raw is not None else None,
    )
    assessments = assess_registry(registry, packet.data.started_at.date(), datetime.now(UTC), 30)
    # Recheck the actual files after preparing the binding, not just caller-supplied hashes.
    if load_packet(root, 2_000_000).manifest_sha256 != binding.dossier_sha256:
        raise DecisionInputError("dossier_changed_during_read")
    reasons = ["trusted_review_origin_unavailable", "trusted_legal_basis_unavailable"]
    if not assessments or any(n.status != "current_confirmed" for n in assessments.values()):
        reasons.append("normative_revision_not_confirmed")
    if any(not evidence[eid].available for eid in references):
        reasons.append("referenced_evidence_unavailable")
    if imported_report is not None:
        reasons.append("imported_report_status_is_untrusted")
    return binding, reasons


def decide_production(
    root: Path,
    finding_path: Path,
    registry_path: Path,
    *,
    review_path: Path | None = None,
    client_text: Path | None = None,
    imported_report: Path | None = None,
) -> ProductionDecision:
    """Single production API: valid local processing can finish, legal confirmation cannot.

    There is deliberately no trust provider, promotion flag, or JSON-derived GO branch.
    A matching human submission stays untrusted. Future trust support needs new code/review.
    """
    binding = None
    try:
        binding, reasons = _snapshot(
            root, finding_path, registry_path, client_text, imported_report
        )
        review = ReviewAssessment()
        if review_path is None:
            reasons.append("human_review_missing")
        else:
            raw = read_bytes(review_path, 20_000)
            json_object(raw)
            submission = HumanReviewSubmission.model_validate_json(raw)
            matches = submission.binding == binding
            valid_claim = (
                matches
                and bool(submission.reviewer.strip())
                and bool(submission.rationale.strip())
                and submission.reviewed_at <= datetime.now(UTC)
            )
            review = ReviewAssessment(
                state="untrusted" if valid_claim else "invalidated",
                binding_matches=matches,
                submission_sha256=sha256(raw),
            )
            reasons.append(
                "matching_review_is_unauthenticated" if valid_claim else "review_invalidated"
            )
        return ProductionDecision(
            binding=binding,
            review=review,
            reasons=tuple(reasons),
            technical_processing_completed=True,
        )
    except (OSError, ValueError, TypeError, RecursionError):
        # Invalid schema, false trust fields, unavailable/mutated artifacts never call demo code.
        return ProductionDecision(
            binding=binding,
            review=ReviewAssessment(state="invalidated" if review_path is not None else "missing"),
            reasons=("inputs_or_review_not_verifiable", "trusted_basis_unavailable"),
        )


def prepare_review(
    root: Path,
    finding_path: Path,
    registry_path: Path,
    output: Path,
    *,
    client_text: Path | None = None,
    imported_report: Path | None = None,
) -> ReviewBinding:
    """Prepare an unapproved local template; never authenticate or confirm anything."""
    binding, _ = _snapshot(root, finding_path, registry_path, client_text, imported_report)
    output.mkdir(parents=True, exist_ok=False)
    (output / "binding.json").write_text(binding.model_dump_json(indent=2) + "\n", encoding="utf-8")
    template = {
        "schema_version": "decision-review-0.1",
        "binding": binding.model_dump(mode="json"),
        "reviewer": "",
        "reviewed_at": None,
        "fact_checked": False,
        "interpretation_checked": False,
        "legal_basis_checked": False,
        "operator_identity_checked": False,
        "client_text_approved": False,
        "claimed_legal_research_completed": False,
        "rationale": "",
        "trust_status": "untrusted",
        "authenticated": False,
        "origin_authentication": "unsupported",
    }
    (output / "review.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return binding
