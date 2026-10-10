"""Finite local source registry. Loading a record is not automatic legal verification."""

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

from .legal_cards import assess_card, read_local
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
    raw = read_local(path).decode("utf-8")
    registry = SourceRegistry.model_validate_json(raw)
    registry._source_root = path.absolute().parent
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
    for card in registry.cards:
        assessment = assess_card(card, registry._source_root, period, now, recheck_days)
        result[card.id] = NormAssessment(
            norm_id=card.id,
            revision=card.revision,
            status="unverified",
            reasons=assessment.reasons.copy(),
            source_url=card.source_url,
            text_sha256=card.text_sha256,
            domain=card.domain,
            card_assessment=assessment,
        )
    # Check all declared intervals, including overlaps outside the current event.
    for index, card in enumerate(registry.cards):
        for other in registry.cards[index + 1 :]:
            if (card.act_id, card.provision) != (other.act_id, other.provision):
                continue
            if (card.source_kind, card.act_number, card.document_date) != (
                other.source_kind,
                other.act_number,
                other.document_date,
            ):
                reason = "Conflicting identity metadata for declared act_id"
            elif card.effective_from is None or other.effective_from is None:
                reason = "Ambiguous revision selection: interval start unknown"
            elif max(card.effective_from, other.effective_from) <= min(
                card.effective_until or date.max, other.effective_until or date.max
            ):
                reason = "Conflicting overlapping declared revisions; no latest-revision fallback"
            else:
                continue
            for current in (card, other):
                record = result[current.id]
                record.reasons.append(reason)
                record.card_assessment.reasons.append(reason)
                record.card_assessment.temporal_claim = "ambiguous"
                record.card_assessment.technical_integrity = "invalid"
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
