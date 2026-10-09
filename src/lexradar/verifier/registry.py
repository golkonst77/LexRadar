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
    # Registry fields are self-assertions. MVP has no authenticated trust provider.
    # URL allowlisting and a local hash verify syntax/integrity, not official provenance.
    if source.status == "unavailable":
        status = "unavailable"
        reasons.append("Declared source unavailable; no legal confirmation")
    else:
        reasons.append("No authenticated source attestation; registry JSON cannot establish trust")
    if source.source_url.scheme != "https" or source.source_url.host not in OFFICIAL_HOSTS:
        reasons.append("Declared URL is not an approved official HTTPS source")
    if digest(source.norm_text) != source.text_sha256:
        reasons.append("Local text hash mismatch")
    if not source.norm_text.strip():
        reasons.append("Norm text not supplied")
    if source.checked_at is None or source.checked_at > now:
        reasons.append("Claimed review date missing or in future")
    elif now - source.checked_at > timedelta(days=recheck_days):
        reasons.append("Claimed review is stale; repeat review required")
    if source.effective_from is None:
        reasons.append("Claimed revision effective date unknown")
    elif period < source.effective_from:
        reasons.append("Claimed revision not effective in examined period")
    if source.effective_until and period > source.effective_until:
        reasons.append("Claimed revision interval expired; repeal not independently confirmed")
    if source.status == "repealed":
        reasons.append("Claim of repeal is not independently authenticated")
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
        if (
            source.effective_from
            and period >= source.effective_from
            and (source.effective_until is None or period <= source.effective_until)
        ):
            groups.setdefault((source.act_number, source.provision), []).append(source)
    for group in groups.values():
        if len({(s.revision, s.text_sha256) for s in group}) > 1:
            for source in group:
                result[source.id].reasons.append(
                    "Conflicting claimed effective revisions; resolve interval/provenance"
                )
    return result
