"""AI observations are deliberately separate from the v0.1 verified legal contracts."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from ..models import Model


class Topic(StrEnum):
    OPERATOR = "operator_identity"
    POLICY = "privacy_policy"
    FORMS = "data_collection_forms"
    CONSENT = "consent"
    SPECIAL = "special_categories"
    THIRD_PARTIES = "third_party_transfers"
    COOKIES = "cookies_analytics"
    DOCUMENTS = "mandatory_documents"
    MEDICAL = "medical_vs_152fz"
    OTHER = "other"


REQUIRED_TOPICS = frozenset(Topic) - {Topic.OTHER}


class ModelSettings(Model):
    model: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_./:-]+$")
    # USD per million tokens; passed as OpenRouter provider.max_price caps.
    prompt_price_cap: Decimal = Field(gt=0, le=1000, allow_inf_nan=False)
    completion_price_cap: Decimal = Field(gt=0, le=1000, allow_inf_nan=False)
    max_output_tokens: int = Field(default=2000, ge=128, le=16000)


class AnalysisConfig(Model):
    auditor_a: ModelSettings
    auditor_b: ModelSettings
    max_budget_usd: Decimal = Field(gt=0, le=100, allow_inf_nan=False)
    timeout_seconds: float = Field(default=30, ge=0.1, le=120, allow_inf_nan=False)
    retries: int = Field(default=1, ge=0, le=3)
    max_backoff_seconds: float = Field(default=2, ge=0, le=10, allow_inf_nan=False)
    max_input_bytes: int = Field(default=200_000, ge=1024, le=2_000_000)
    max_response_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000)


class NormativeBasis(Model):
    act_name: str = Field(min_length=1, max_length=500)
    act_id: str = Field(min_length=1, max_length=100)
    provision: str = Field(min_length=1, max_length=200)
    requirement: str = Field(min_length=1, max_length=3000)
    actuality_status: Literal["unverified"] = "unverified"
    claimed_source: str | None = Field(default=None, max_length=2000)
    verified_source: None = None


class TextGrounding(Model):
    page: int | None = Field(default=None, ge=1)
    evidence_id: str = Field(min_length=1, max_length=200)
    exact_quote: str = Field(min_length=1, max_length=10000)


class AIFinding(Model):
    fact_supported: bool = False
    search_scope: Literal["unknown", "text_excerpt", "whole_document", "site_subset"] = "unknown"
    search_limitations: list[str] = Field(default_factory=list, max_length=100)
    examination_scope: Literal["unknown", "text_excerpt", "whole_document"] = "unknown"
    text_grounding: list[TextGrounding] = Field(default_factory=list, max_length=100)
    id: str = Field(min_length=1, max_length=100)
    topic: Topic
    claim_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    subject: str = Field(min_length=1, max_length=300)
    source: HttpUrl
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    fact: str = Field(min_length=1, max_length=5000)
    fact_assertion: Literal["present", "absent", "unknown"]
    evidence_quality: Literal["complete", "partial", "unavailable", "text_not_provided"] = (
        "unavailable"
    )
    legal_interpretation: str = Field(min_length=1, max_length=5000)
    legal_position: Literal["possible_issue", "no_issue", "undetermined"] = "undetermined"
    normative_basis: list[NormativeBasis] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    material: bool
    limitations: list[str] = Field(max_length=100)
    alternative_explanations: list[str] = Field(max_length=100)
    additional_checks: list[str] = Field(min_length=1, max_length=100)
    status: Literal["confirmed", "potential", "rejected", "unverifiable"]


class Coverage(Model):
    topic: Topic
    status: Literal["reviewed", "insufficient_evidence"]
    limitations: list[str] = Field(max_length=100)


class AIAuditorResult(Model):
    auditor: Literal["A", "B"]
    findings: list[AIFinding] = Field(max_length=100)
    coverage: list[Coverage] = Field(min_length=9, max_length=10)
    limitations: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def identities_and_coverage(self):
        ids = [f.id for f in self.findings]
        topics = [c.topic for c in self.coverage]
        keys = [
            (f.topic, f.claim_code, f.subject, tuple(sorted(f.evidence_ids))) for f in self.findings
        ]
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("Duplicate finding identity")
        if len(topics) != len(set(topics)) or not REQUIRED_TOPICS <= set(topics):
            raise ValueError("All nine review topics are required exactly once")
        return self


class Usage(Model):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class RequestLog(Model):
    auditor: Literal["A", "B"]
    model: str
    started_at: AwareDatetime
    duration_seconds: float = Field(ge=0)
    prompt_version: str
    prompt_sha256: str
    evidence_packet_sha256: str
    attempt: int
    request_sent: bool
    status: str
    usage: Usage = Field(default_factory=Usage)
    budget_charge_usd: Decimal = Field(ge=0)


class AuditorRun(Model):
    auditor: Literal["A", "B"]
    model: str
    prompt_version: str
    status: Literal["success", "provider_error", "invalid_response", "budget_exceeded"]
    result: AIAuditorResult | None = None
    validation_notes: list[str] = Field(default_factory=list)


class Disagreement(Model):
    kind: Literal[
        "full_agreement",
        "partial_agreement",
        "factual_contradiction",
        "legal_qualification_disagreement",
        "single_auditor",
        "insufficient_evidence",
    ]
    finding_ids_a: list[str]
    finding_ids_b: list[str]
    reason: str
    requires_review: Literal[True] = True


class AnalysisReport(Model):
    schema_version: Literal["0.3"] = "0.3"
    mode: Literal["offline", "openrouter"]
    collector_manifest_sha256: str
    evidence_packet_sha256: str
    started_at: AwareDatetime
    completed: bool
    runs: list[AuditorRun]
    disagreements: list[Disagreement]
    logs: list[RequestLog]
    budget_charged_usd: Decimal
    budget_limit_usd: Decimal
    same_model_independence_limited: bool
    limitations: list[str]
    requires_independent_legal_review: Literal[True] = True
    automatic_go_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False

    @model_validator(mode="after")
    def no_ai_confirmation_or_false_completion(self):
        if {run.auditor for run in self.runs} != {"A", "B"} or len(self.runs) != 2:
            raise ValueError("Analysis requires separate A and B run records")
        if self.completed != all(run.status == "success" for run in self.runs):
            raise ValueError("Completion must match both run statuses")
        for run in self.runs:
            if run.status == "success" and run.result is None:
                raise ValueError("Successful run requires a validated result")
            if run.result and any(f.status == "confirmed" for f in run.result.findings):
                raise ValueError("AI analysis cannot confirm legal violations")
        if self.same_model_independence_limited != (self.runs[0].model == self.runs[1].model):
            raise ValueError("Model independence limitation must match actual configuration")
        return self
