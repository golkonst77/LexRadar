"""Offline documentary review, separate from strict legal GO qualification."""

from .auditors.grounding import exact_locations


def review_candidate(packet, *, identifier, source, evidence_ids, quote, defect, paid_work):
    """An explicit documentary defect and deliverable need a unique reproduced quote."""
    spans = exact_locations(packet, evidence_ids, source, quote)
    if not spans or not defect.strip() or not paid_work.strip():
        return None
    return dict(
        id=identifier,
        outcome="REVIEW_CANDIDATE",
        text_observation_confirmed=True,
        legal_violation_proven=False,
        source=str(source),
        grounding=spans[0],
        documentary_defect=defect,
        proposed_paid_work=paid_work,
        automatic_send_allowed=False,
        client_release_allowed=False,
    )
