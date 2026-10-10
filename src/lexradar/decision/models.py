"""Integrity-bound review submissions are not authenticated human decisions."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field

from ..models import Model

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DecisionModel(Model):
    model_config = ConfigDict(frozen=True)


class ReviewBinding(DecisionModel):
    dossier_sha256: Hash
    artifact_inventory_sha256: Hash
    finding_sha256: Hash
    normative_basis_sha256: Hash
    client_text_sha256: Hash | None = None
    imported_report_sha256: Hash | None = None


class HumanReviewSubmission(DecisionModel):
    schema_version: Literal["decision-review-0.1"] = "decision-review-0.1"
    binding: ReviewBinding
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    fact_checked: bool = Field(default=False, strict=True)
    interpretation_checked: bool = Field(default=False, strict=True)
    legal_basis_checked: bool = Field(default=False, strict=True)
    operator_identity_checked: bool = Field(default=False, strict=True)
    client_text_approved: bool = Field(default=False, strict=True)
    claimed_legal_research_completed: bool = Field(default=False, strict=True)
    rationale: str = Field(min_length=1, max_length=3000)
    trust_status: Literal["untrusted"] = "untrusted"
    authenticated: Literal[False] = False
    origin_authentication: Literal["unsupported"] = "unsupported"


class ReviewAssessment(DecisionModel):
    state: Literal["missing", "untrusted", "invalidated"] = "missing"
    binding_matches: bool | None = None
    submission_sha256: Hash | None = None
    trust_status: Literal["untrusted"] = "untrusted"
    origin_authentication: Literal["unsupported"] = "unsupported"


class ProductionDecision(DecisionModel):
    schema_version: Literal["trusted-decision-0.1"] = "trusted-decision-0.1"
    decision_scope: Literal["production"] = "production"
    outcome: Literal["HOLD"] = "HOLD"
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    reasons: tuple[str, ...] = Field(min_length=1)
    binding: ReviewBinding | None = None
    review: ReviewAssessment = Field(default_factory=ReviewAssessment)
    technical_processing_completed: bool = Field(default=False, strict=True)
    legal_research_completed: Literal[False] = False
    trusted_basis_available: Literal[False] = False
    production_go_allowed: Literal[False] = False
    client_release_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False
    limitations: tuple[str, ...] = (
        "Hashes establish local integrity and binding, not source or reviewer authenticity",
        "No authenticated review origin or trusted current normative source is available",
        "Imported statuses, human flags, model agreement and confidence are untrusted claims",
        "Technical completion is not completion of legal research or client approval",
    )
