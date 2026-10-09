"""Verifier proposals and locally established confirmations are distinct contracts."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from ..auditors.models import AIFinding, AnalysisConfig, ModelSettings, Topic, Usage
from ..models import Model, Signal

Status = Literal[
    "verified_issue", "potential_issue", "rejected", "insufficient_evidence", "no_issue_observed"
]


class VerifierConfig(AnalysisConfig):
    # Transport compatibility only; no A/B requests are made by this module.
    auditor_a: ModelSettings = Field(
        default_factory=lambda: ModelSettings(
            model="unused/a", prompt_price_cap=1, completion_price_cap=1
        )
    )
    auditor_b: ModelSettings = Field(
        default_factory=lambda: ModelSettings(
            model="unused/b", prompt_price_cap=1, completion_price_cap=1
        )
    )
    verifier: ModelSettings
    legal_recheck_days: int = Field(default=30, ge=1, le=90)


class LegalSource(Model):
    """Untrusted registry claims, including review/status fields retained for compatibility."""

    id: str = Field(min_length=1, max_length=100)
    domain: Literal["personal_data", "medical", "consumer", "paid_medical", "other"]
    act_name: str = Field(min_length=1, max_length=500)
    act_number: str = Field(min_length=1, max_length=100)
    provision: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=300)
    effective_from: date | None = None
    effective_until: date | None = None
    checked_at: AwareDatetime | None = None
    source_url: HttpUrl
    norm_text: str = Field(default="", max_length=50000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["verified_current", "unverified", "repealed", "unavailable"] = "unverified"
    verification_method: Literal["human_official_review", "unverified", "llm"] = "unverified"
    reviewer: str | None = Field(default=None, max_length=200)
    revision_check_basis: str | None = Field(default=None, max_length=3000)
    provenance: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def dates(self):
        if (
            self.effective_from
            and self.effective_until
            and self.effective_until < self.effective_from
        ):
            raise ValueError("Invalid revision interval")
        expected = {
            "152-ФЗ": "personal_data",
            "323-ФЗ": "medical",
            "2300-1": "consumer",
            "736": "paid_medical",
        }
        if self.act_number in expected and self.domain != expected[self.act_number]:
            raise ValueError("Medical and personal-data obligations must remain separate")
        return self


class SourceRegistry(Model):
    schema_version: Literal["0.4"] = "0.4"
    sources: list[LegalSource] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate legal source IDs")
        return self


class NormAssessment(Model):
    norm_id: str
    revision: str
    status: Literal["current_confirmed", "unverified", "repealed", "unavailable"]
    reasons: list[str]
    source_url: HttpUrl
    text_sha256: str
    domain: str


class Material(Model):
    evidence_id: str
    url: HttpUrl
    type: Literal["html_page", "pdf", "other_document"]
    page_count: int | None = Field(default=None, ge=1)
    text_page_count: int = Field(default=0, ge=0)
    examined_pages: int = Field(default=0, ge=0)
    examined_page_numbers: list[int] = Field(default_factory=list)
    text_available: bool = False
    visual_review_required: bool = False
    supplied_text: str = ""
    page_texts: dict[int, str] = Field(default_factory=dict)
    collection_status: str
    examination: Literal["not_examined", "text_only", "partial_text"] = "not_examined"
    reasons: list[str] = Field(default_factory=list)


class Quote(Model):
    evidence_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=5000)
    page: int | None = Field(default=None, ge=1)


class Candidate(Model):
    id: str = Field(min_length=1, max_length=100)
    origin: Literal["independent_verifier"] = "independent_verifier"
    topic: Topic
    claim_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    subject: str = Field(min_length=1, max_length=300)
    source: HttpUrl
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    fact: str = Field(min_length=1, max_length=5000)
    fact_assertion: Literal["present", "absent", "unknown"]
    examination_scope: Literal["text_excerpt", "whole_document", "unknown"] = "unknown"
    fragments: list[Quote] = Field(default_factory=list, max_length=100)
    norm_ids: list[str] = Field(min_length=1, max_length=20)
    operator_ref: str | None = Field(default=None, max_length=100)
    legal_interpretation: str = Field(min_length=1, max_length=5000)
    applicability_claim: Literal["applicable", "not_applicable", "unknown"] = "unknown"
    alternative_explanations: list[str] = Field(default_factory=list, max_length=50)
    material: bool = False
    additional_checks: list[str] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    attention_reason: str = Field(min_length=1, max_length=3000)
    signal: Signal = Signal.SUBSTANTIVE
    status: Status = "potential_issue"


class MaterialReview(Model):
    evidence_id: str
    # Claim to examine only actually supplied text; no images/HTML browsing is possible.
    text_examined: bool = False
    pages_examined: list[int] = Field(default_factory=list, max_length=500)
    limitation: str = Field(min_length=1, max_length=2000)


class DirectionReview(Model):
    topic: Topic
    status: Literal["text_reviewed", "insufficient_evidence"]
    limitation: str = Field(min_length=1, max_length=2000)


class IndependentResult(Model):
    findings: list[Candidate] = Field(default_factory=list, max_length=100)
    materials: list[MaterialReview] = Field(default_factory=list, max_length=200)
    directions: list[DirectionReview] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=100)


class HumanFindingReview(Model):
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    fact_verified: bool = Field(strict=True)
    interpretation_verified: bool = Field(strict=True)
    applicability: Literal["applicable", "not_applicable", "unknown"]
    operator_ref: str
    operator_identity_verified: bool = Field(strict=True)
    identity_sources: list[HttpUrl] = Field(min_length=1, max_length=20)
    activity: str = Field(min_length=1, max_length=500)
    period_from: date
    period_until: date
    rationale: str = Field(min_length=1, max_length=3000)


class Finding(Model):
    candidate: Candidate
    candidate_sha256: str
    status: Status
    fact_verified: bool = False
    applicability: Literal["confirmed", "not_applicable", "unestablished"] = "unestablished"
    norms: list[NormAssessment]
    reasons: list[str]
    additional_checks: list[str]
    human_review: HumanFindingReview | None = None

    @model_validator(mode="after")
    def confirmation(self):
        if self.status == "verified_issue" and (
            not self.fact_verified
            or self.applicability != "confirmed"
            or not self.norms
            or any(n.status != "current_confirmed" for n in self.norms)
            or self.human_review is None
        ):
            raise ValueError(
                "Verified issue requires independently confirmed fact, norm and applicability"
            )
        return self


class AssessmentProposal(Model):
    auditor: Literal["A", "B"]
    finding_id: str
    status: Status
    matched_independent_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=3000)


class ComparisonResult(Model):
    assessments: list[AssessmentProposal] = Field(default_factory=list, max_length=200)
    limitations: list[str] = Field(default_factory=list, max_length=100)


class AuditAssessment(Model):
    original_finding: AIFinding
    proposal: AssessmentProposal
    applicability: Literal["confirmed", "not_applicable", "unestablished"] = "unestablished"
    additional_checks: list[str] = Field(default_factory=list)
    status: Status
    reasons: list[str]
    normative_assessments: list[NormAssessment]
    duplicate_of: str | None = None
    verified_independent_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def confirmed_basis(self):
        if self.status == "verified_issue" and (
            not self.verified_independent_ids
            or not self.normative_assessments
            or any(n.status != "current_confirmed" for n in self.normative_assessments)
        ):
            raise ValueError(
                "Auditor confirmation requires independently confirmed claim and norms"
            )
        return self


class VerifierRequestLog(Model):
    stage: Literal["independent", "comparison"]
    model: str
    started_at: AwareDatetime
    duration_seconds: float
    prompt_version: str
    prompt_sha256: str
    packet_sha256: str
    attempt: int
    request_sent: bool
    status: str
    usage: Usage = Field(default_factory=Usage)
    budget_charge_usd: Decimal


class Completeness(Model):
    method: str = "Materials with acknowledged supplied-text review / all collected materials"
    examined_text_materials: int
    total_materials: int
    text_review_fraction: float | None
    known_pdf_pages: int
    examined_pdf_text_pages: int
    unknown_page_count_documents: int
    complete_website_audit: Literal[False] = False


class VerificationReport(Model):
    schema_version: Literal["0.4"] = "0.4"
    mode: Literal["offline", "openrouter"]
    started_at: AwareDatetime
    stage_one_packet_sha256: str
    stage_two_packet_sha256: str | None = None
    stage_one_result_sha256: str
    collector_manifest_sha256: str
    registry_sha256: str
    completed: bool = False
    run_status: str
    independent_directions: list[DirectionReview]
    independent_findings: list[Finding]
    auditor_assessments: list[AuditAssessment]
    normative_sources: list[NormAssessment]
    materials: list[Material]
    unexamined_material_ids: list[str]
    completeness: Completeness
    confirmed_problem_ids: list[str]
    potential_problem_ids: list[str]
    rejected_hypothesis_ids: list[str]
    recommendations: list[str]
    limitations: list[str]
    logs: list[VerifierRequestLog]
    budget_charged_usd: Decimal
    budget_limit_usd: Decimal
    model_independence_limited: bool = True
    requires_human_approval: Literal[True] = True
    automatic_go_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False
    full_legal_compliance_established: Literal[False] = False

    @model_validator(mode="after")
    def report_integrity(self):
        if self.completed != (self.run_status == "complete"):
            raise ValueError("Completion must match persisted stage status")
        own = {f.candidate.id: f for f in self.independent_findings}
        if len(own) != len(self.independent_findings):
            raise ValueError("Independent finding IDs must be unique")
        if set(self.confirmed_problem_ids) != {
            k for k, f in own.items() if f.status == "verified_issue"
        }:
            raise ValueError("Confirmed problem index must match independently verified findings")
        for assessment in self.auditor_assessments:
            if any(
                k not in own or own[k].status != "verified_issue"
                for k in assessment.verified_independent_ids
            ):
                raise ValueError("Invalid independent confirmation reference")
        return self
