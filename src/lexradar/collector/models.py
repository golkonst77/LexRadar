"""Collector contracts contain observations only, never legal decisions."""

from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from ..models import Evidence, Model


class Limits(Model):
    max_pages: int = Field(default=10, ge=1, le=100)
    max_documents: int = Field(default=10, ge=0, le=100)
    max_file_bytes: int = Field(default=5_000_000, ge=1024, le=20_000_000)
    max_total_bytes: int = Field(default=50_000_000, ge=1024, le=200_000_000)
    timeout_seconds: float = Field(default=10, ge=0.1, le=60)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_links_per_page: int = Field(default=200, ge=1, le=1000)
    max_pdf_pages: int = Field(default=100, ge=1, le=500)


class Artifact(Model):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    kind: Literal["html", "screenshot", "form_screenshot", "pdf", "text"]


class CollectedEvidence(Evidence):
    unavailable_reason: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def availability_reason(self):
        if not self.available and not self.unavailable_reason:
            raise ValueError("Unavailable evidence requires a reason")
        return self


class InputField(Model):
    tag: str
    type: str
    name: str | None = None
    label: str | None = None
    required: bool = False
    checked: bool | None = None


class FormObservation(Model):
    page_url: HttpUrl
    purpose: str | None = None
    fields: list[InputField]
    policy_links: list[str]
    consent_links: list[str]
    submission_context: str
    screenshot: Artifact | None = None
    unavailable_reason: str | None = None


class EntityObservation(Model):
    source: HttpUrl
    raw_text: str
    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    address: str | None = None
    identity_verified: Literal[False] = False


class PageObservation(Model):
    requested_url: HttpUrl
    final_url: HttpUrl | None = None
    captured_at: AwareDatetime
    http_status: int | None = None
    title: str | None = None
    text: str | None = None
    categories: list[str] = Field(default_factory=list)
    document_links: list[str] = Field(default_factory=list)
    forms: list[FormObservation] = Field(default_factory=list)
    evidence_id: str


class DocumentObservation(Model):
    requested_url: HttpUrl
    final_url: HttpUrl | None = None
    http_status: int | None = None
    evidence_id: str
    text: str | None = None
    extraction_status: Literal["text", "visual_review_required", "failed"] = "failed"
    extraction_reason: str | None = None


class CollectionResult(Model):
    schema_version: Literal["0.2"] = "0.2"
    target_url: HttpUrl
    started_at: AwareDatetime
    limits: Limits
    pages: list[PageObservation] = Field(default_factory=list)
    documents: list[DocumentObservation] = Field(default_factory=list)
    entities: list[EntityObservation] = Field(default_factory=list)
    evidence: list[CollectedEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requires_independent_review: Literal[True] = True
