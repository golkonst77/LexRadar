"""Finite local source registry. Loading a record is not automatic legal verification."""

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import LegalSource, NormAssessment, SourceRegistry

OFFICIAL_HOSTS = frozenset(
    {
        "pravo.gov.ru",
        "www.pravo.gov.ru",
        "publication.pravo.gov.ru",
        "www.publication.pravo.gov.ru",
        "actual.pravo.gov.ru",
    }
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_registry(path: Path) -> tuple[SourceRegistry, str]:
    if path.is_symlink() or path.stat().st_size > 6_000_000:
        raise ValueError("Unsafe/oversized registry")
    raw = path.read_text(encoding="utf-8")
    registry = SourceRegistry.model_validate_json(raw)
    for source in registry.sources:
        if digest(source.norm_text) != source.text_sha256:
            raise ValueError("Norm text hash mismatch")
    return registry, digest(raw)


def assess(source: LegalSource, period: date, now: datetime, recheck_days: int) -> NormAssessment:
    reasons = []
    status = "unverified"
    trusted = (
        source.verification_method == "human_official_review"
        and source.source_url.scheme == "https"
        and source.source_url.host in OFFICIAL_HOSTS
        and source.source_url.port == 443
        and digest(source.norm_text) == source.text_sha256
        and source.source_url.username is None
        and source.source_url.password is None
        and bool(source.reviewer and source.reviewer.strip())
        and bool(source.revision_check_basis and source.revision_check_basis.strip())
        and bool(source.norm_text.strip())
        and source.checked_at is not None
        and source.checked_at <= now
        and now - source.checked_at <= timedelta(days=recheck_days)
    )
    if source.status == "unavailable":
        status = "unavailable"
        reasons.append("Source unavailable; no legal confirmation")
    elif not trusted:
        reasons.append("No fresh human review of official text and revision provenance")
    elif source.effective_from is None:
        reasons.append("Revision effective date unknown")
    elif period < source.effective_from:
        reasons.append("Revision not effective in examined period")
    elif source.effective_until and period > source.effective_until:
        status = "repealed"
        reasons.append("Revision no longer effective in examined period")
    elif source.status == "repealed" and not source.effective_until:
        status = "repealed"
        reasons.append("Human-reviewed source marked repealed; select correct historical revision")
    elif source.status in {"verified_current", "repealed"}:
        status = "current_confirmed"
        reasons.append(
            "Local human attestation of official revision; applicability assessed separately"
        )
    else:
        reasons.append("Source found but revision not confirmed")
    return NormAssessment(
        norm_id=source.id,
        revision=source.revision,
        status=status,
        reasons=reasons,
        source_url=source.source_url,
        text_sha256=source.text_sha256,
        domain=source.domain,
    )


def assess_registry(
    registry: SourceRegistry, period: date, now: datetime, recheck_days: int
) -> dict[str, NormAssessment]:
    result = {s.id: assess(s, period, now, recheck_days) for s in registry.sources}
    groups = {}
    for source in registry.sources:
        if result[source.id].status == "current_confirmed":
            groups.setdefault((source.act_number, source.provision), []).append(source)
    for group in groups.values():
        if len({(s.revision, s.text_sha256) for s in group}) > 1:
            for source in group:
                result[source.id].status = "unverified"
                result[source.id].reasons.append(
                    "Conflicting effective revisions; resolve interval/provenance"
                )
    return result
