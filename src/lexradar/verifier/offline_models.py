"""Independent local checks are technical observations, never legal authority."""

from typing import Literal

from pydantic import Field

from ..models import Model
from ..primary.models import Citation, Observation
from ..primary.qualification_models import CommercialWorkCandidate, LegalFinding

Status = Literal[
    "verified_fact",
    "partially_verified",
    "contradicted",
    "insufficient_evidence",
    "requires_external_check",
]


class EvidenceCheck(Model):
    citation: Citation
    status: Status
    actual_sha256: str | None = None
    reasons: list[str]
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    authenticity_confirmed: Literal[False] = False


class MaterialCheck(Model):
    evidence_id: str
    kind: str
    available_parts: int | None = None
    read_parts: list[int] = Field(default_factory=list)
    skipped_parts: list[int] = Field(default_factory=list)
    known_total_parts: int | None = None
    text_coverage_complete: bool = False
    inspected_ranges: list[dict] = Field(default_factory=list)
    skipped_ranges: list[dict] = Field(default_factory=list)
    limitations: list[str]
    visual_review_required: bool = False
    visual_review_performed: Literal[False] = False
    scope: Literal["bounded_static_text_not_whole_site_or_legal_review"] = (
        "bounded_static_text_not_whole_site_or_legal_review"
    )


class FindingCheck(Model):
    source_finding: LegalFinding
    technical_status: Status
    evidence_checks: list[EvidenceCheck]
    independently_reproduced_fact_ids: list[str]
    contradictions: list[str]
    normative_status: Literal["normative_review_required"] = "normative_review_required"
    legal_applicability: Literal["unestablished"] = "unestablished"
    norm_candidates: list[dict] = Field(default_factory=list)
    remaining_actions: list[str]
    limitations: list[str]
    organization_contexts: list[dict] = Field(default_factory=list)
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    source_conclusion_verified: Literal[False] = False


class DiscoveredFinding(Model):
    finding: LegalFinding
    evidence_checks: list[EvidenceCheck]
    technical_status: Status
    legal_status: Literal["not_confirmed"] = "not_confirmed"


class DiscoveryCheck(Model):
    observation: Observation
    evidence_checks: list[EvidenceCheck]
    technical_status: Status
    legal_status: Literal["not_confirmed"] = "not_confirmed"


class WorkCheck(Model):
    source_candidate: CommercialWorkCandidate
    factual_basis: Status
    applicable_obligation: Literal["unverified"] = "unverified"
    full_current_document_read: Literal["not_established"] = "not_established"
    new_document_needed: Literal["unknown"] = "unknown"
    preparation_recommendation: Literal["conditional_internal_review_only", "blocked"]
    written_specification_needed: Literal["conditional_on_verified_legal_work"] = (
        "conditional_on_verified_legal_work"
    )
    necessity_status: Literal["not_confirmed"] = "not_confirmed"
    remaining_actions: list[str]
    reasons: list[str]


class IndependentVerification(Model):
    schema_version: Literal["independent-local-verification-0.1"] = (
        "independent-local-verification-0.1"
    )
    audience: Literal["internal_only"] = "internal_only"
    notice: str = "ВНУТРЕННЕЕ — НЕ ПЕРЕСЫЛАТЬ КЛИЕНТУ"
    source_assessment_sha256: str
    independent_research_sha256: str | None = None
    stage_i_input: Literal["saved_dossier_and_registry_only"] = "saved_dossier_and_registry_only"
    materials: list[MaterialCheck]
    findings: list[FindingCheck]
    new_findings: list[LegalFinding]
    new_finding_checks: list[DiscoveredFinding] = Field(default_factory=list)
    new_observations: list[DiscoveryCheck] = Field(default_factory=list)
    commercial_work: list[WorkCheck]
    gaps: list[str]
    technical_fact_count: int
    contradiction_count: int
    gap_count: int
    technical_processing_completed: Literal[True] = True
    legal_research_completed: Literal[False] = False
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    production_outcome: Literal["HOLD"] = "HOLD"
    production_go_allowed: Literal[False] = False
    client_release_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False
    review_trust: Literal["untrusted_no_authenticated_legal_review"] = (
        "untrusted_no_authenticated_legal_review"
    )
