"""Internal questions and conditional work; no trusted legal confirmation or client release.

LegalFinding here is the PR13 contract, separate from the legacy/demo finding.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..collector.provenance import MaterialOrigin
from ..models import Model
from .models import Citation, MaterialReference

Category = Literal["A", "B", "C", "D", "E", "F", "G", "H"]
Completeness = Literal["complete", "partial", "unknown"]


class FindingKind(StrEnum):
    OBSERVED_FACT = "observed_fact"
    REVIEW_QUESTION = "review_question"
    POTENTIAL_RISK = "potential_risk"
    CONFIRMED_VIOLATION = "confirmed_violation"


class ReviewObject(Model):
    type: Literal["organization", "form", "document", "integration", "page"]
    source_id: str
    identity_or_operator_confirmed: Literal[False] = False


class FindingFact(Model):
    id: str
    kind: Literal["observed_fact"] = "observed_fact"
    description: str
    objects: list[ReviewObject] = Field(min_length=1)
    observation_ids: list[str] = Field(default_factory=list)
    declaration_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=1)
    traceability: Literal["traceable_saved_material", "incomplete"]
    source_authenticity_confirmed: Literal[False] = False
    legal_violation_established: Literal[False] = False


class LegalFinding(Model):
    id: str
    category: Category
    kind: FindingKind = FindingKind.REVIEW_QUESTION
    objects: list[ReviewObject] = Field(min_length=1)
    factual_description: str
    facts: list[FindingFact] = Field(min_length=1)
    established_fact_ids: list[str] = Field(default_factory=list)
    evidence_completeness: Completeness
    evidence_scope: Literal["supplied_saved_regions_only"] = "supplied_saved_regions_only"
    chain_status: Literal["traceable_saved_material", "incomplete"]
    unconfirmed_assumptions: list[str] = Field(min_length=1)
    additional_checks: list[str] = Field(min_length=1)
    possible_legal_consequences: list[str] = Field(default_factory=list, max_length=0)
    consequences_status: Literal["blocked_no_trusted_applicable_norm"] = (
        "blocked_no_trusted_applicable_norm"
    )
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    manual_review_priority: Literal["P1", "P2"]
    priority_rationale: str
    limitations: list[str] = Field(min_length=1)
    entire_site_examined: Literal[False] = False

    @model_validator(mode="after")
    def no_automatic_confirmation(self):
        if self.kind == FindingKind.CONFIRMED_VIOLATION:
            raise ValueError("PR13 has no trusted legal confirmation mechanism")
        if self.chain_status == "incomplete" and self.kind != FindingKind.REVIEW_QUESTION:
            raise ValueError("Incomplete evidence can only create a review question")
        if self.kind == FindingKind.POTENTIAL_RISK:
            raise ValueError("PR13 does not establish applicable legal consequences or risks")
        supported = {f.id for f in self.facts if f.traceability == "traceable_saved_material"}
        if not set(self.established_fact_ids) <= supported:
            raise ValueError("Unsupported fact cannot become established")
        return self


class CommercialWorkCandidate(Model):
    id: str
    work_type: Literal[
        "policy_document",
        "processing_consent",
        "advertising_consent",
        "medical_disclosure_documents",
        "legal_webmaster_specification",
    ]
    legal_finding_ids: list[str] = Field(min_length=1)
    support_fact_ids: list[str] = Field(min_length=1)
    discovered_scope: str
    client_clarifications: list[str] = Field(min_length=1)
    expected_paid_result: str
    proposal_justification: Literal["conditional_scope_candidate"] = "conditional_scope_candidate"
    necessity_conditions: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    scope_policy: Literal["separate_organization_and_form_scopes_until_verified"] = (
        "separate_organization_and_form_scopes_until_verified"
    )
    document_need_confirmed: Literal[False] = False
    violation_proof: Literal[False] = False
    offer_generated: Literal[False] = False
    customer_webmaster_implements: Literal[True] = True
    direct_site_fix_included: Literal[False] = False


class CommercialModel(Model):
    preliminary_diagnosis: Literal["free"] = "free"
    main_paid_product: Literal["legal_documents_and_written_webmaster_specification"] = (
        "legal_documents_and_written_webmaster_specification"
    )
    seller_and_payment_recipient: Literal["ИП Исполнитель"] = "ИП Исполнитель"
    sales_site: Literal["seller.example.invalid"] = "seller.example.invalid"
    implementation: Literal["customer_webmaster_normally_exception_separately_agreed"] = (
        "customer_webmaster_normally_exception_separately_agreed"
    )
    payment_or_message_action_available: Literal[False] = False


class InternalAssessment(Model):
    schema_version: Literal["internal-assessment-0.1"] = "internal-assessment-0.1"
    audience: Literal["internal_only"] = "internal_only"
    notice: Literal["ВНУТРЕННЕЕ — НЕ ПЕРЕСЫЛАТЬ КЛИЕНТУ"] = "ВНУТРЕННЕЕ — НЕ ПЕРЕСЫЛАТЬ КЛИЕНТУ"
    source_primary_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_primary_report: Literal["audit.json"] = "audit.json"
    source_primary_hash_scope: Literal["canonical_validated_json_utf8_sorted_compact"] = (
        "canonical_validated_json_utf8_sorted_compact"
    )
    review_trust: Literal["untrusted_no_authenticated_legal_review"] = (
        "untrusted_no_authenticated_legal_review"
    )
    source_dossier_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_origin: MaterialOrigin
    materials: list[MaterialReference]
    legal_findings: list[LegalFinding]
    commercial_work: list[CommercialWorkCandidate]
    additional_checks: list[str]
    limitations: list[str] = Field(min_length=1)
    assessed_stage_ids: list[str]
    commercial_model: CommercialModel = Field(default_factory=CommercialModel)
    production_outcome: Literal["HOLD"] = "HOLD"
    legal_status: Literal["not_confirmed"] = "not_confirmed"
    technical_processing_completed: Literal[True] = True
    legal_research_completed: Literal[False] = False
    production_go_allowed: Literal[False] = False
    client_release_allowed: Literal[False] = False
    automatic_send_allowed: Literal[False] = False

    @model_validator(mode="after")
    def references(self):
        findings = {f.id: f for f in self.legal_findings}
        if len(findings) != len(self.legal_findings):
            raise ValueError("Duplicate legal finding")
        facts = {}
        materials = {m.evidence_id: m for m in self.materials}
        for finding in self.legal_findings:
            for fact in finding.facts:
                if fact.id in facts and facts[fact.id] != fact:
                    raise ValueError("One fact ID cannot bind different observations")
                facts[fact.id] = fact
                if fact.traceability != "traceable_saved_material":
                    continue
                for citation in fact.citations:
                    material = materials.get(citation.evidence_id)
                    if (
                        not material
                        or str(citation.url) != str(material.url)
                        or not any(
                            a.path == citation.artifact_path
                            and a.sha256 == citation.artifact_sha256
                            and a.kind in {"html", "pdf"}
                            for a in material.artifacts
                        )
                        or not (citation.static_span or citation.basis == "reproduced_text")
                    ):
                        raise ValueError("Traceable fact requires an original artifact reference")
            incomplete = any(f.traceability == "incomplete" for f in finding.facts)
            if incomplete != (finding.chain_status == "incomplete"):
                raise ValueError("Inconsistent finding evidence chain")
        work_types = set()
        for work in self.commercial_work:
            if work.work_type in work_types:
                raise ValueError("Duplicate paid work type")
            work_types.add(work.work_type)
            if not set(work.legal_finding_ids) <= findings.keys():
                raise ValueError("Unknown commercial finding")
            selected = {
                fact.id: fact for fid in work.legal_finding_ids for fact in findings[fid].facts
            }
            if not set(work.support_fact_ids) <= selected.keys() or any(
                facts[i].traceability != "traceable_saved_material" for i in work.support_fact_ids
            ):
                raise ValueError("Commercial work requires linked traceable facts")
        return self


SUMMARY_REASONS = {
    "A": "Без подтверждения роли лица нельзя определить адресата юридических документов",
    "B": "Без сопоставления текстов и процессов нельзя оценить пригодность документов",
    "C": "Для оценки оснований нужны конкретная форма и фактический порядок обработки",
    "D": "Сначала нужно установить характер сообщений и применимость отдельного согласия",
    "E": "Ссылка не объясняет фактическую передачу данных и роли участников записи",
    "F": "Объявление ресурса не объясняет исполнение, cookies или получателей данных",
    "G": "Маркеры разделов не подтверждают лицензию, полноту и применимость сведений",
    "H": "Разные упоминания требуют раздельной проверки контекста и фактических ролей",
}


class SummaryDirection(Model):
    category: Category
    uncertainty: Literal["requires_independent_review"] = "requires_independent_review"
    attention_reason: str
    violation_confirmed: Literal[False] = False

    @model_validator(mode="after")
    def controlled_outline(self):
        if self.attention_reason != SUMMARY_REASONS[self.category]:
            raise ValueError("Preliminary outline permits only controlled high-level reasons")
        return self


class PreliminaryClientSummary(Model):
    """Restricted internal outline, no release field, quotes, repair steps or client letter."""

    schema_version: Literal["preliminary-client-outline-0.1"] = "preliminary-client-outline-0.1"
    audience: Literal["internal_only"] = "internal_only"
    draft_only: Literal[True] = True
    dispatch_permitted: Literal[False] = False
    source_assessment_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    directions: list[SummaryDirection]
    uncertainty: Literal["saved_materials_only_legal_research_incomplete"] = (
        "saved_materials_only_legal_research_incomplete"
    )
    next_step_intent: Literal["professional_review_then_documents_only_if_necessary"] = (
        "professional_review_then_documents_only_if_necessary"
    )
    not_legal_conclusion: Literal[True] = True
    automatic_send_allowed: Literal[False] = False
