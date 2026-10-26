"""Production qualification requires a local independent legal review, bound to the result."""

import hashlib
from pathlib import Path

from pydantic import AwareDatetime, Field

from .batch import safe_path
from .models import Model
from .single_auditor import commercial


class LegalFindingReview(Model):
    finding_id: str = Field(min_length=1)
    factual_defect_verified: bool = Field(strict=True)
    legal_applicability_verified: bool = Field(strict=True)
    serviceable: bool = Field(strict=True)
    reason: str = Field(min_length=20)
    verified_legal_sources: list[str] = Field(min_length=1)


class LegalReview(Model):
    packet_sha256: str
    analysis_sha256: str
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    findings: list[LegalFindingReview]


def analysis_digest(report):
    return hashlib.sha256(report.model_dump_json().encode()).hexdigest()


def qualify(report, packet, *, production=False, legal_review=None):
    allowed, reasons = commercial(report, packet)
    if production and allowed and not reasons:
        from datetime import UTC, datetime

        if not legal_review:
            return [], ["independent_legal_review_required"]
        path = safe_path(Path(legal_review))
        if path.stat().st_size > 100_000:
            raise ValueError("Oversized legal review")
        review = LegalReview.model_validate_json(path.read_bytes())
        if (
            review.packet_sha256 != packet.sha256
            or review.analysis_sha256 != analysis_digest(report)
            or not review.reviewer.strip()
            or review.reviewed_at > datetime.now(UTC)
            or len({f.finding_id for f in review.findings}) != len(review.findings)
            or any(f.finding_id not in allowed for f in review.findings)
        ):
            raise ValueError("Independent legal review binding/identity mismatch")
        allowed = [
            f.finding_id
            for f in review.findings
            if (
                f.factual_defect_verified
                and f.legal_applicability_verified
                and f.serviceable
                and f.reason.strip()
                and all(source.strip() for source in f.verified_legal_sources)
            )
        ]
        if not allowed:
            reasons = ["no_independently_verified_serviceable_defect"]
    return sorted(allowed), reasons
