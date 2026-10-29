"""Requirement annotations reference existing source cards; they do not establish trust."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..models import Model

Category = Literal["A", "B", "C", "D", "E", "F", "G", "H"]
Predicate = Literal[
    "organization_mention",
    "multiple_organization_mentions",
    "static_form_declared",
    "policy_text_available",
    "saved_policy_text_present",
    "live_policy_accessibility",
    "consent_marker_declared",
    "advertising_marker_declared",
    "booking_reference_declared",
    "analytics_reference_declared",
    "operator_identity",
    "medical_provider_identity",
    "medical_activity",
    "actual_personal_data_processing",
    "actual_advertising_communication",
    "actual_third_party_transfer",
    "cookies_observed",
    "whole_current_document_read",
    "scope_coverage",
    "document_deficiency",
    "exceptions_checked",
    "alternative_legal_basis_checked",
]


class RequirementCondition(Model):
    condition_id: str = Field(min_length=1, max_length=100)
    predicate: Predicate
    rationale: str = Field(min_length=1, max_length=3000)


class NormRequirementSpec(Model):
    requirement_id: str = Field(min_length=1, max_length=100)
    source_card_id: str = Field(min_length=1, max_length=100)
    categories: list[Category] = Field(min_length=1, max_length=8)
    conditions: list[RequirementCondition] = Field(min_length=1, max_length=30)
    necessary_evidence: list[str] = Field(min_length=1, max_length=30)
    exceptions_and_alternatives: list[str] = Field(min_length=1, max_length=30)
    activity_scope: Literal["personal_data", "advertising", "medical", "unknown"] = "unknown"
    limitations: list[str] = Field(min_length=1, max_length=30)
    annotation_trust: Literal["untrusted_requires_legal_review"] = "untrusted_requires_legal_review"


class SourceIntake(Model):
    source_card_id: str
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    card_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    acquired_at: AwareDatetime
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    trust_status: Literal["untrusted"] = "untrusted"
    authenticated: Literal[False] = False


class ResearchMaterial(Model):
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role: str = Field(min_length=1, max_length=500)


class ImportedNormResearch(Model):
    """Research claims are preserved, never promoted to authenticated evidence."""

    source_card_id: str
    card_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provision: str = Field(min_length=1, max_length=200)
    event_date: date
    provenance_method: Literal["user_attested", "unknown"] = "unknown"
    edition_basis: Literal["secondary_SPS_consolidation", "unknown"] = "unknown"
    declared_revision: str = Field(min_length=1, max_length=300)
    review_scope: list[str] = Field(min_length=1, max_length=30)
    claimed_result: Literal["current_in_scope", "unverified", "blocked"] = "unverified"
    materials: list[ResearchMaterial] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(min_length=1, max_length=100)


class NormResearchContext(Model):
    imported_claim: ImportedNormResearch
    binding_status: Literal["consistent", "invalidated"]
    mechanism_operations: list[str]
    limitations: list[str]
    claim_authenticated: Literal[False] = False
    independent_official_portal_verification: Literal[False] = False
    production_trust: Literal["untrusted"] = "untrusted"


class PolicyPosition(Model):
    line: int = Field(ge=1)
    column: int = Field(ge=0)


class PolicyBlock(Model):
    start: PolicyPosition
    end_exclusive: PolicyPosition
    status: Literal["complete", "partial", "unknown"]
    limitations: list[str]
    coordinate_scope: Literal["decoded_utf8_line_1_based_column_0_based"] = (
        "decoded_utf8_line_1_based_column_0_based"
    )


class SavedPolicyObservation(Model):
    id: str
    evidence_id: str
    source_url: str
    artifact_path: str
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    block: PolicyBlock
    text: str = Field(min_length=80, max_length=50000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reading_completeness: Literal["complete", "partial", "unknown"]
    page_completeness: Literal["complete", "partial", "unknown"]
    saved_text_present: Literal[True] = True
    live_interface_accessibility: Literal["unknown"] = "unknown"
    policy_sufficiency: Literal["unknown"] = "unknown"
    limitations: list[str]


class NormRequirement(Model):
    requirement_id: str
    source_card_id: str
    act_name: str | None = None
    act_number: str | None = None
    source_kind: str | None = None
    provision: str | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    event_date: date
    source_url: str | None = None
    original_path: str | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    exact_quote: str | None = None
    text_check: Literal["matched_saved_text", "missing", "incomplete", "contradictory"]
    source_state: Literal[
        "available_local_unverified", "missing", "incomplete", "stale", "contradictory"
    ]
    official_origin: Literal["unverified"] = "unverified"
    revision_check: Literal["revision_unverified"] = "revision_unverified"
    declared_temporal_state: Literal[
        "within_declared_interval", "not_yet_effective", "expired", "ambiguous"
    ] = "ambiguous"
    revision: str | None = None
    conditions: list[RequirementCondition]
    necessary_evidence: list[str]
    exceptions_and_alternatives: list[str]
    categories: list[Category]
    activity_scope: str
    limitations: list[str]
    intake_status: Literal["not_supplied", "untrusted", "invalidated"] = "not_supplied"
    legal_requirement_confirmed: Literal[False] = False
    research_context: NormResearchContext | None = None


class ConditionCheck(Model):
    condition_id: str
    predicate: str
    status: Literal["supported_static_fact", "unknown", "contradicted"]
    fact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str


class AbsenceCheck(Model):
    scope: Literal["supplied_saved_materials_only"] = "supplied_saved_materials_only"
    inspected_material_ids: list[str]
    all_saved_text_complete: bool
    website_denominator: None = None
    linked_documents_not_read: bool | None
    actual_executor_established: Literal[False] = False
    sufficient_coverage_for_absence: Literal[False] = False
    absence_proven: Literal[False] = False
    reasons: list[str]


class RequirementApplicability(Model):
    finding_id: str
    category: Category
    requirement_id: str
    status: Literal[
        "source_unverified",
        "revision_unverified",
        "applicability_unknown",
        "potentially_applicable",
        "conditions_supported",
        "not_applicable_with_evidence",
        "requires_human_legal_review",
    ]
    norm_in_force: Literal["unverified"] = "unverified"
    source_text_check: str
    revision_checked: Literal[False] = False
    activity_supported: Literal["unknown"] = "unknown"
    obligated_person_established: Literal[False] = False
    factual_action_established: Literal[False] = False
    exceptions_checked: Literal[False] = False
    sufficient_for_legal_conclusion: Literal[False] = False
    condition_checks: list[ConditionCheck]
    conditional_factual_support: Literal["potentially_applicable", "applicability_unknown"]
    absence_check: AbsenceCheck
    human_checks: list[str]
    violation_status: Literal["not_confirmed"] = "not_confirmed"

    @model_validator(mode="after")
    def reserved_confirmations(self):
        if self.status in {"conditions_supported", "not_applicable_with_evidence"}:
            raise ValueError("MVP cannot authenticate legal conditions or legal exclusion")
        return self


class NormativeWorkCheck(Model):
    candidate_id: str
    work_type: str
    related_applicability: list[str]
    normative_requirement_confirmed: Literal[False] = False
    requirement_applicable_confirmed: Literal[False] = False
    existing_document_reading: Literal["partial", "unknown"] = "unknown"
    existing_document_insufficient: Literal["unknown"] = "unknown"
    new_document_needed: Literal["unknown"] = "unknown"
    correction_needed: Literal["unknown"] = "unknown"
    technical_change_only: Literal["unknown"] = "unknown"
    internal_recommendation: Literal["conditional_scope_review", "insufficient_normative_basis"]
    necessity_confirmed: Literal[False] = False
    remaining_checks: list[str]


class NormativeVerification(Model):
    schema_version: Literal["normative-verification-0.1"] = "normative-verification-0.1"
    audience: Literal["internal_only"] = "internal_only"
    notice: str = "ВНУТРЕННЕЕ — НЕ ПЕРЕСЫЛАТЬ КЛИЕНТУ"
    event_date: date
    evaluated_at: AwareDatetime
    registry_sha256: str | None
    source_assessment_sha256: str
    source_research_sha256: str | None
    requirements: list[NormRequirement]
    applicability: list[RequirementApplicability]
    commercial_work: list[NormativeWorkCheck]
    question_summaries: list[dict]
    source_search_tasks: list[dict]
    gaps: list[str]
    saved_policy_observations: list[SavedPolicyObservation] = Field(default_factory=list)
    technical_processing_completed: Literal[True] = True
    legal_research_completed: Literal[False] = False
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    production_outcome: Literal["HOLD"] = "HOLD"
    production_go_allowed: Literal[False] = False
    client_release_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False
    sanctions: list[str] = Field(default_factory=list, max_length=0)
