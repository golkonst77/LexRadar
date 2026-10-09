"""Reference labels and measured outputs never share a provider interface."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..models import Model


class Unit(Model):
    id: str
    topic: str
    claim_code: str = Field(pattern=r"^[a-z0-9_]+$")
    evidence_ids: list[str] = Field(min_length=1)
    expected_problem: bool
    rationale: str
    norm_ids: list[str] = Field(default_factory=list)
    expected_operator_ref: str | None = None
    expected_applicability: Literal["confirmed", "not_applicable", "unestablished"] | None = None


class ObservationCheck(Model):
    kind: Literal[
        "text_contains",
        "unavailable",
        "partial_html",
        "forms_no_checkbox",
        "entities_count",
        "pdf_scan",
        "pdf_mixed",
        "document_link",
        "script_disabled",
    ]
    evidence_id: str = "page-0001"
    value: str = ""


class Reference(Model):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    situation: str
    observed_facts: list[ObservationCheck]
    units: list[Unit]
    permitted_hypotheses: list[str]
    forbidden_conclusions: list[str]
    expected_evidence_ids: list[str]
    normative_references: dict[str, Literal["unverified"]]
    expected_limitations: list[str]
    admissible_statuses: dict[str, list[str]]
    material_pages: dict[str, int]  # Reference universe includes unread/unavailable pages.
    disagreement_expected: bool = False

    @model_validator(mode="after")
    def unique(self):
        ids = [u.id for u in self.units]
        if len(ids) != len(set(ids)) or any(v < 1 for v in self.material_pages.values()):
            raise ValueError("Unique reference units and positive page counts required")
        if any(not set(u.evidence_ids) <= set(self.expected_evidence_ids) for u in self.units):
            raise ValueError("Reference unit evidence outside labeled universe")
        return self


class Prediction(Model):
    id: str
    topic: str
    claim_code: str
    evidence_ids: list[str]
    asserted: bool
    status: str
    fact_grounded: bool
    norm_verified: bool
    applicability: str
    operator_ref: str | None = None
    reasons: list[str]

    @model_validator(mode="after")
    def confirmation(self):
        if self.status == "verified_issue" and not (
            self.norm_verified and self.fact_grounded and self.applicability == "confirmed"
        ):
            raise ValueError("Quality output cannot confirm an unsupported legal claim")
        return self


class Metric(Model):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None
    status: Literal["measured", "not_applicable"]

    @model_validator(mode="after")
    def arithmetic(self):
        if self.numerator > self.denominator:
            raise ValueError("Metric numerator exceeds denominator")
        expected = self.numerator / self.denominator if self.denominator else None
        if self.value != expected or self.status != (
            "measured" if self.denominator else "not_applicable"
        ):
            raise ValueError("Invalid metric arithmetic")
        return self


class Check(Model):
    name: str
    passed: bool
    detail: str


class RecordingMetadata(Model):
    """Optional unauthenticated provenance of saved model responses, separate from replay costs."""

    models: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)
    original_api_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class CaseReport(Model):
    id: str
    situation: str
    mode: Literal["synthetic", "replay"]
    dossier_sha256: str
    reference_sha256: str
    response_sha256: str
    checks: list[Check] = Field(min_length=1)
    predictions: list[Prediction]
    found: list[str]
    missed: list[str]
    false_positives: list[str]
    manual_review: list[str]
    counts: dict[str, int]
    metrics: dict[str, Metric]
    evidence_links: dict[str, list[str]]
    models: list[str]
    prompts: dict[str, str]
    api_cost_usd: str | None
    recording_metadata: RecordingMetadata = Field(default_factory=RecordingMetadata)
    recording_metadata_sha256: str | None = None
    limitations: list[str]


class TestReport(Model):
    schema_version: Literal["0.5"] = "0.5"
    created_at: AwareDatetime
    cases: list[CaseReport]
    metrics: dict[str, Metric]
    layers: dict[str, str]
    overall_status: Literal["passed", "failed", "manual_review_required"]
    limitations: list[str]
    automatic_go_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False
    full_legal_compliance_established: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self):
        from .evaluator import aggregate

        expected = (
            "failed"
            if any(not c.passed for case in self.cases for c in case.checks)
            else (
                "manual_review_required" if any(c.manual_review for c in self.cases) else "passed"
            )
        )
        if (
            self.overall_status != expected
            or self.metrics != aggregate(self.cases)
            or len({c.id for c in self.cases}) != len(self.cases)
            or not self.cases
        ):
            raise ValueError("Quality report totals/status do not match scenario results")
        return self


class ExpertLabel(Model):
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dossier_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_id: str
    finding_id: str
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    status: Literal[
        "expert_confirmed",
        "erroneous_finding",
        "additional_review_required",
        "unrelated_operator",
        "inapplicable_norm",
        "insufficient_evidence",
    ]
    rationale: str = Field(min_length=1, max_length=3000)
    authenticated: Literal[False] = False
