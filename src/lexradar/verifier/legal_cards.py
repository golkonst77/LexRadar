"""Local source cards: reproducible integrity checks, no authenticated legal trust."""

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from ..models import Model


class LegalSourceCard(Model):
    schema_version: Literal["legal-source-card-0.1"] = "legal-source-card-0.1"
    id: str = Field(min_length=1, max_length=100)
    act_id: str = Field(min_length=1, max_length=200)
    source_kind: Literal[
        "normative_act", "official_explanation", "judicial_act", "secondary_material", "ai_claim"
    ]
    domain: Literal["personal_data", "medical", "consumer", "paid_medical", "other"]
    act_name: str = Field(min_length=1, max_length=500)
    act_number: str = Field(min_length=1, max_length=100)
    document_date: date | None
    provision: str = Field(min_length=1, max_length=200)
    norm_text: str = Field(min_length=1, max_length=50000)
    source_url: HttpUrl | None
    publication_id: str | None = Field(max_length=500)
    publication_date: date | None
    revision: str = Field(min_length=1, max_length=300)
    effective_from: date | None
    effective_until: date | None
    transition_from: date | None = None
    transition_until: date | None = None
    transition_note: str | None = Field(default=None, max_length=3000)
    claimed_lifecycle: Literal["active", "repealed", "not_yet_effective", "unknown"] = "unknown"
    checked_at: AwareDatetime | None
    verification_method: Literal["local_integrity", "human_review", "llm", "unverified"]
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_path: str = Field(min_length=1, max_length=500)
    revision_check_basis: str = Field(min_length=1, max_length=3000)
    claimed_official: bool = False
    claimed_status: str = Field(default="unverified", max_length=100)
    claimed_reviewer: str | None = Field(default=None, max_length=200)
    intended_use: Literal["binding_norm", "context"] = "context"
    source_availability: Literal["saved_locally", "unavailable", "unknown"] = "unknown"
    limitations: list[str] = Field(max_length=100)
    uncertainties: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def locator(self):
        if self.source_url is None and not (self.publication_id or "").strip():
            raise ValueError("Publication URL or identifier required; neither proves provenance")
        return self


class SourceCardAssessment(Model):
    card_id: str
    event_date: date
    technical_integrity: Literal["consistent", "invalid", "unavailable"]
    saved_original_sha256: str | None = None
    hash_matches: bool | None = None
    exact_quote_present: bool | None = None
    authenticity: Literal["unverified"] = "unverified"
    revision_confirmation: Literal["unverified"] = "unverified"
    temporal_claim: Literal["within_declared_interval", "not_yet_effective", "expired", "ambiguous"]
    legal_force: Literal["unverified"] = "unverified"
    applicability: Literal["unestablished"] = "unestablished"
    factual_violation: Literal["unestablished"] = "unestablished"
    status: Literal["unverified"] = "unverified"
    trusted: Literal[False] = False
    production_go_allowed: Literal[False] = False
    reasons: list[str]
    binding_sha256: str


def hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def card_hash(card: LegalSourceCard) -> str:
    return hash_bytes(
        json.dumps(card.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def read_local(path: Path, maximum: int = 6_000_000) -> bytes:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Unsafe or missing local source")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Local source size limit")
    return raw


def original_bytes(card: LegalSourceCard, root: Path | None) -> bytes:
    relative = Path(card.original_path)
    if root is None or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Missing root or unsafe source path")
    return read_local(root / relative)


def assess_card(
    card: LegalSourceCard,
    root: Path | None,
    event: date,
    now: datetime,
    recheck_days: int = 30,
) -> SourceCardAssessment:
    reasons = [
        "Source origin and reviewer authentication unsupported; all declarations remain untrusted",
        "Integrity, publication officiality, legal force, revision and applicability are distinct",
        *card.limitations,
        *card.uncertainties,
    ]
    integrity = "consistent"
    saved_hash = None
    hash_matches = None
    quote_present = None
    if not card.norm_text.strip():
        integrity = "invalid"
        reasons.append("Provision quote is empty/whitespace")
    try:
        raw = original_bytes(card, root)
        saved_hash = hash_bytes(raw)
        hash_matches = saved_hash == card.text_sha256
        if not hash_matches:
            integrity = "invalid"
            reasons.append("Saved original SHA-256 mismatch")
        text = raw.decode("utf-8")
        quote_present = bool(card.norm_text.strip()) and card.norm_text in text
        if not quote_present:
            integrity = "invalid"
            reasons.append("Exact provision quote not present in saved UTF-8 text")
    except (OSError, ValueError):
        integrity = "unavailable"
        reasons.append("Saved original missing, unsafe, oversized or not UTF-8")
    temporal = "ambiguous"
    intervals = [(card.effective_from, card.effective_until)]
    if card.transition_note or card.transition_from or card.transition_until:
        intervals.append((card.transition_from, card.transition_until))
        reasons.append(
            "Transition rule declared; its interpretation and scope require legal review"
        )
    invalid_dates = any(start and end and start > end for start, end in intervals)
    if invalid_dates:
        integrity = "invalid"
        reasons.append("Contradictory declared effective/transition interval")
    elif any(start is None for start, _ in intervals):
        reasons.append("Ambiguous start of effective/transition period")
    elif any(event < start for start, _ in intervals):
        temporal = "not_yet_effective"
    elif any(end and event > end for _, end in intervals):
        temporal = "expired"
    else:
        temporal = "within_declared_interval"
    if card.claimed_lifecycle == "repealed" and card.effective_until is None:
        temporal = "ambiguous"
        reasons.append("Repeal claimed without an end date")
    if card.claimed_lifecycle == "not_yet_effective" and temporal == "within_declared_interval":
        temporal = "ambiguous"
        integrity = "invalid"
        reasons.append("Lifecycle claim contradicts declared event interval")
    if card.document_date and card.publication_date and card.publication_date < card.document_date:
        integrity = "invalid"
        reasons.append("Publication predates document date")
    if card.document_date is None or card.publication_date is None:
        reasons.append("Document/publication date unknown; metadata completeness not established")
    if card.publication_date and card.publication_date > now.date():
        reasons.append("Claimed publication date is in future; independent checking required")
    if card.checked_at is None or card.checked_at > now:
        reasons.append("Check timestamp missing or in future")
    elif now - card.checked_at > timedelta(days=recheck_days):
        reasons.append("Declared review stale; recheck required")
    if card.checked_at and card.publication_date and card.checked_at.date() < card.publication_date:
        integrity = "invalid"
        reasons.append("Claimed check predates claimed publication")
    if card.source_availability == "unavailable":
        reasons.append("Official source declared unavailable; no origin confirmation")
    reasons.append("Source category and declared lifecycle are not independently authenticated")
    if card.source_kind != "normative_act":
        reasons.append(
            "Source is not an NPA; no normative obligation established by source category"
        )
        if card.intended_use == "binding_norm":
            integrity = "invalid"
            reasons.append("Non-NPA source incorrectly requested as binding normative act")
    if card.source_kind in {"secondary_material", "ai_claim"} and card.claimed_official:
        integrity = "invalid"
        reasons.append("Secondary/AI material falsely declared official")
    if card.source_url is not None:
        from .registry import OFFICIAL_HOSTS

        if (
            card.source_url.scheme != "https"
            or card.source_url.host not in OFFICIAL_HOSTS
            or card.source_url.port != 443
            or card.source_url.username is not None
            or card.source_url.password is not None
        ):
            reasons.append("URL is not an approved publication HTTPS locator; no origin proof")
        else:
            reasons.append("Official-domain locator is a declaration, not verified content")
    reasons.append(f"Temporal result is conditional on unverified dates: {temporal}")
    return SourceCardAssessment(
        card_id=card.id,
        event_date=event,
        technical_integrity=integrity,
        saved_original_sha256=saved_hash,
        hash_matches=hash_matches,
        exact_quote_present=quote_present,
        temporal_claim=temporal,
        reasons=reasons,
        binding_sha256=card_hash(card),
    )


class SourceLegalReview(Model):
    schema_version: Literal["source-legal-review-0.1"] = "source-legal-review-0.1"
    card_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_date: date
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    independent_of_author: bool = Field(strict=True)
    origin_checked: bool = Field(strict=True)
    revision_checked: bool = Field(strict=True)
    legal_force_checked: bool = Field(strict=True)
    operator_reference: str = Field(min_length=1, max_length=300)
    activity: str = Field(min_length=1, max_length=500)
    applicability_rationale: str = Field(min_length=1, max_length=3000)
    confirmation_references: list[str] = Field(min_length=1, max_length=20)
    trust_status: Literal["untrusted"] = "untrusted"
    authenticated: Literal[False] = False


def check_review(
    review: SourceLegalReview,
    card: LegalSourceCard,
    assessment: SourceCardAssessment,
    now: datetime,
) -> Literal["untrusted", "invalidated"]:
    if (
        review.card_sha256 != card_hash(card)
        or review.original_sha256 != card.text_sha256
        or review.event_date != assessment.event_date
        or review.reviewed_at > now
        or assessment.technical_integrity != "consistent"
        or not review.reviewer.strip()
        or not review.applicability_rationale.strip()
        or not review.independent_of_author
    ):
        return "invalidated"
    return "untrusted"
