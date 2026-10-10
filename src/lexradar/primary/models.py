"""Facts, hypotheses, missing evidence, legal references and potential work are separate."""

from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from ..collector.models import Artifact, InputField
from ..collector.reading import ReadingState
from ..models import Model
from ..verifier.models import NormAssessment
from .stages import STAGES, STATUS_SCOPE

StageStatus = Literal[
    "observed", "potential_issue", "insufficient_evidence", "not_checked", "no_issue_observed"
]
WorkKind = Literal[
    "policy_preparation",
    "separate_consents",
    "multiple_operator_documents",
    "legal_webmaster_specification",
    "medical_disclosure_review",
    "additional_legal_expertise",
]


class MaterialReference(Model):
    evidence_id: str
    url: HttpUrl
    collection_status: str
    reading: ReadingState
    artifacts: list[Artifact]
    checked_scope: Literal["local_inventory_not_full_document_read"] = (
        "local_inventory_not_full_document_read"
    )
    live_access_performed: Literal[False] = False


class Citation(Model):
    evidence_id: str
    url: HttpUrl
    artifact_path: str | None = None
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    basis: Literal["reproduced_text", "saved_html", "collector_manifest"]
    quote: str | None = None
    page: int | None = None
    element: str | None = None


class Observation(Model):
    id: str
    stage_id: str
    kind: Literal["text_quote", "static_markup", "collection_metadata", "scoped_search"]
    description: str
    citations: list[Citation] = Field(min_length=1)
    scope: str
    legal_violation_established: Literal[False] = False


class Hypothesis(Model):
    id: str
    stage_id: str
    question: str
    observation_ids: list[str] = Field(min_length=1)
    normative_reference_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(min_length=1)
    required_checks: list[str] = Field(min_length=1)
    status: Literal["unconfirmed"] = "unconfirmed"


class CheckTask(Model):
    id: str
    stage_id: str
    type: str
    question: str
    organization_ids: list[str] = Field(default_factory=list)
    form_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    external_access_required: bool = False
    state: Literal["not_performed"] = "not_performed"
    legal_confirmation_blocked: Literal[True] = True


class NormReference(Model):
    id: str
    act_name: str
    act_number: str
    provision: str
    quote: str
    source_kind: str
    assessment: NormAssessment
    use: Literal["candidate_for_independent_review"] = "candidate_for_independent_review"
    applicability: Literal["unestablished"] = "unestablished"


class OrganizationRow(Model):
    id: str
    name: str
    inn: str | None = None
    ogrn: str | None = None
    identifiers_scope: Literal["public_text_mentions_not_registry_verified"] = (
        "public_text_mentions_not_registry_verified"
    )
    role_hint: str = "Упоминание в публичном тексте; фактическая роль не установлена"
    form_ids: list[str] = Field(default_factory=list)
    flow_ids: list[str] = Field(default_factory=list)
    identification_sources: list[Citation] = Field(min_length=1)
    mention_confidence: Literal["text_supported"] = "text_supported"
    uncertainties: list[str] = Field(min_length=1)
    identity_verified: Literal[False] = False
    operator_confirmed: Literal[False] = False


class FormRow(Model):
    id: str
    evidence_id: str
    url: HttpUrl
    element: str
    purpose_hint: str | None
    fields: list[InputField]
    policy_links: list[str]
    consent_links: list[str]
    advertising_links: list[str]
    interface_elements: list[str]
    submission_context: str
    action_reference: str | None
    organization_candidates: list[str] = Field(default_factory=list)
    recipient_hint: str = "Не установлен; HTML action и упоминания не доказывают получателя"
    unresolved_processing: list[str] = Field(min_length=1)
    citation: Citation
    screenshot: Artifact | None = None
    screenshot_scope: str | None = None
    examination: Literal["saved_static_markup"] = "saved_static_markup"
    functional_test_performed: Literal[False] = False
    submitted: Literal[False] = False
    lawfulness: Literal["unestablished"] = "unestablished"


class FlowRow(Model):
    id: str
    form_id: str
    action_reference: str | None
    organization_candidates: list[str]
    observed_scope: Literal["declared_static_reference"] = "declared_static_reference"
    actual_recipient: Literal["unknown"] = "unknown"
    network_route: Literal["not_checked"] = "not_checked"
    cross_border_transfer: Literal["unconfirmed"] = "unconfirmed"
    legal_basis: Literal["unestablished"] = "unestablished"


class ServiceReference(Model):
    id: str
    url_reference: str
    reference_kind: str
    service_hint: str | None = None
    citation: Citation
    execution_confirmed: Literal[False] = False
    data_transfer_confirmed: Literal[False] = False
    cookies_examined: Literal[False] = False


class MedicalCheck(Model):
    check_id: str
    status: StageStatus
    observation_ids: list[str]
    limitation: str
    applicable_obligation_confirmed: Literal[False] = False


class CommercialOpportunity(Model):
    id: str
    kind: WorkKind
    stage_ids: list[str] = Field(min_length=1)
    observation_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prerequisites: list[str] = Field(min_length=1)
    status: Literal["potential_scope_requires_review"] = "potential_scope_requires_review"
    violation_proof: Literal[False] = False
    proposal_generated: Literal[False] = False
    direct_site_implementation_offered: Literal[False] = False


class StageResult(Model):
    stage_id: str
    title: str
    status: StageStatus = "not_checked"
    status_scope: str = STATUS_SCOPE
    checked_materials: list[str] = Field(default_factory=list)
    observed_fact_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    normative_reference_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    additional_check_ids: list[str] = Field(default_factory=list)
    possible_paid_legal_work: bool = False
    paid_work_rationale: str = "Недостаточно предметных оснований для определения объёма работ"


class AuditCoverage(Model):
    page_attempts: int
    document_attempts: int
    reproduced_text_materials: int
    incomplete_text_materials: int
    known_pdf_pages: int
    supplied_pdf_text_pages: int
    unknown_pdf_page_count_documents: int
    stages_with_observations: int
    total_stages: Literal[13] = 13
    site_coverage_denominator: None = None
    entire_site_examined: Literal[False] = False
    visual_examination_performed: Literal[False] = False
    legal_research_completed: Literal[False] = False


class PrimaryAudit(Model):
    schema_version: Literal["primary-audit-0.1"] = "primary-audit-0.1"
    audience: Literal["internal_only"] = "internal_only"
    notice: str = "ВНУТРЕННЕЕ — НЕ ПЕРЕСЫЛАТЬ КЛИЕНТУ"
    methodology: Literal["pr10_working_structure"] = "pr10_working_structure"
    verbatim_v25_correspondence: Literal[False] = False
    target_url: HttpUrl
    dossier_sha256: str
    artifact_inventory_sha256: str
    source_registry_sha256: str | None
    source_started_at: AwareDatetime
    assessed_at: AwareDatetime
    sector_hint: Literal["medical", "unknown"]
    organizations: list[OrganizationRow]
    operator_situation: Literal[
        "operator_unestablished", "one_organization_mentioned", "multiple_roles_unestablished"
    ]
    materials: list[MaterialReference]
    forms: list[FormRow]
    flows: list[FlowRow]
    services: list[ServiceReference]
    medical_checks: list[MedicalCheck]
    stages: list[StageResult]
    observations: list[Observation]
    hypotheses: list[Hypothesis]
    normative_references: list[NormReference]
    missing_checks: list[CheckTask]
    commercial_opportunity: list[CommercialOpportunity]
    coverage: AuditCoverage
    limitations: list[str]
    technical_processing_completed: Literal[True] = True
    audit_complete: Literal[False] = False
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    production_outcome: Literal["HOLD"] = "HOLD"
    production_go_allowed: Literal[False] = False
    client_release_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False

    @model_validator(mode="after")
    def references(self):
        if [s.stage_id for s in self.stages] != [s.stage_id for s in STAGES]:
            raise ValueError("Exactly thirteen ordered working stages required")
        collections = {
            "material": self.materials,
            "organization": self.organizations,
            "form": self.forms,
            "flow": self.flows,
            "observation": self.observations,
            "hypothesis": self.hypotheses,
            "norm": self.normative_references,
            "check": self.missing_checks,
            "service": self.services,
            "work": self.commercial_opportunity,
        }
        keys = {}
        for kind, values in collections.items():
            ids = [v.evidence_id if kind == "material" else v.id for v in values]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate primary IDs")
            keys[kind] = set(ids)
        for stage in self.stages:
            for kind, refs in (
                ("material", stage.checked_materials),
                ("observation", stage.observed_fact_ids),
                ("hypothesis", stage.hypothesis_ids),
                ("norm", stage.normative_reference_ids),
                ("check", stage.additional_check_ids),
            ):
                if not set(refs) <= keys[kind]:
                    raise ValueError("Dangling primary stage references")
        for hypothesis in self.hypotheses:
            if (
                not set(hypothesis.observation_ids) <= keys["observation"]
                or not set(hypothesis.required_checks) <= keys["check"]
                or not set(hypothesis.normative_reference_ids) <= keys["norm"]
            ):
                raise ValueError("Ungrounded hypothesis")
        for work in self.commercial_opportunity:
            if not set(work.observation_ids) <= keys["observation"]:
                raise ValueError("Ungrounded potential legal work")
        for observation in self.observations:
            if any(c.evidence_id not in keys["material"] for c in observation.citations):
                raise ValueError("Unknown cited primary material")
        stage_ids = {s.stage_id for s in self.stages}
        for item in [*self.observations, *self.hypotheses, *self.missing_checks]:
            if item.stage_id not in stage_ids:
                raise ValueError("Unknown primary stage")
        for work in self.commercial_opportunity:
            if not set(work.stage_ids) <= stage_ids:
                raise ValueError("Unknown work stage")
        for org in self.organizations:
            if not set(org.form_ids) <= keys["form"] or not set(org.flow_ids) <= keys["flow"]:
                raise ValueError("Unknown organization form/flow")
        for form in self.forms:
            if (
                form.evidence_id not in keys["material"]
                or not set(form.organization_candidates) <= keys["organization"]
            ):
                raise ValueError("Unknown form material/organization")
        for flow in self.flows:
            if (
                flow.form_id not in keys["form"]
                or not set(flow.organization_candidates) <= keys["organization"]
            ):
                raise ValueError("Unknown flow form/organization")
        for task in self.missing_checks:
            if (
                not set(task.organization_ids) <= keys["organization"]
                or not set(task.form_ids) <= keys["form"]
                or not set(task.evidence_ids) <= keys["material"]
            ):
                raise ValueError("Unknown check subject")
        for check in self.medical_checks:
            if not set(check.observation_ids) <= keys["observation"]:
                raise ValueError("Unknown medical observation")
        return self
