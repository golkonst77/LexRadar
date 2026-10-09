"""JSON contracts. Facts, interpretations and assumptions are separate fields."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

Text = str


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FindingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Signal(StrEnum):
    SUBSTANTIVE = "substantive"
    DOCUMENT_UNAVAILABLE = "document_unavailable"
    PDF_SCAN = "pdf_scan"
    NO_CHECKBOX = "no_checkbox"
    ANALYTICS_SCRIPT = "analytics_script"


class Outcome(StrEnum):
    GO = "GO"
    NURTURE = "NURTURE"
    HOLD = "HOLD"
    STOP = "STOP"


class LegalEntity(Model):
    name: str = Field(min_length=1)
    registration_id: str | None = None
    identity_verified: bool = False


class AuditTarget(Model):
    id: str = Field(min_length=1)
    url: HttpUrl
    legal_entity: LegalEntity
    critical_obstacles: list[str] = Field(default_factory=list)


class Evidence(Model):
    id: str = Field(min_length=1)
    source: HttpUrl
    captured_at: AwareDatetime
    observed_fact: str = Field(min_length=1)
    available: bool = True
    artifact_path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    signal: Signal = Signal.SUBSTANTIVE


class LegalFinding(Model):
    id: str = Field(min_length=1)
    source: HttpUrl
    evidence_ids: list[str] = Field(default_factory=list)
    fact_description: str = Field(min_length=1)
    legal_basis: str = Field(min_length=1)
    legal_interpretation: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: FindingStatus = FindingStatus.PENDING
    material: bool = False
    signal: Signal = Signal.SUBSTANTIVE


class AuditorResult(Model):
    auditor: Literal["A", "B"]
    completed: bool = False
    findings: list[LegalFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_findings(self):
        ids = [f.id for f in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate finding IDs")
        return self


class FindingVerification(Model):
    finding_id: str = Field(min_length=1)
    status: FindingStatus = FindingStatus.PENDING
    evidence_checked: bool = False
    legal_basis_checked: bool = False
    reviewer_kind: Literal["human", "llm"]
    reviewer_id: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    rationale: str = Field(min_length=1)


class VerificationResult(Model):
    completed: bool = False
    findings: list[FindingVerification] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    critical_obstacles: list[str] = Field(default_factory=list)


class GatewayDecision(Model):
    outcome: Outcome
    reasons: list[str] = Field(min_length=1)
    eligible_finding_ids: list[str] = Field(default_factory=list)
    rules_version: Literal["0.1"] = "0.1"
    requires_human_approval: Literal[True] = True


class AuditInput(Model):
    target: AuditTarget
    evidence: list[Evidence] = Field(default_factory=list)
    auditors: list[AuditorResult] = Field(default_factory=list)
    verification: VerificationResult

    @model_validator(mode="after")
    def references(self):
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate evidence IDs")
        labels = [a.auditor for a in self.auditors]
        if len(labels) != len(set(labels)):
            raise ValueError("Duplicate auditor labels")
        finding_ids = {f.id for a in self.auditors for f in a.findings}
        for auditor in self.auditors:
            for finding in auditor.findings:
                if not set(finding.evidence_ids) <= set(ids):
                    raise ValueError("Unknown evidence reference")
                if len(finding.evidence_ids) != len(set(finding.evidence_ids)):
                    raise ValueError("Duplicate evidence reference")
        verified = [f.finding_id for f in self.verification.findings]
        if len(verified) != len(set(verified)) or not set(verified) <= finding_ids:
            raise ValueError("Invalid verification references")
        return self


class HumanApproval(Model):
    approved: bool = False
    reviewer_id: str | None = None
    approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def approval_identity(self):
        if self.approved and (not self.reviewer_id or self.approved_at is None):
            raise ValueError("Approval requires reviewer and timestamp")
        return self


class AuditReport(Model):
    schema_version: Literal["0.1"] = "0.1"
    dossier: AuditInput
    decision: GatewayDecision
    client_message_draft: str | None = None
    human_approval: HumanApproval = Field(default_factory=HumanApproval)
    automatic_send_allowed: Literal[False] = False
