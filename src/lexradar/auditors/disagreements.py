"""Structured conservative matching, without demanding equal natural-language wording."""

import re
import unicodedata
from collections import defaultdict

from .models import AIAuditorResult, Disagreement


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def compare(a: AIAuditorResult | None, b: AIAuditorResult | None) -> list[Disagreement]:
    def item(kind, left, right, reason):
        return Disagreement(
            kind=kind,
            finding_ids_a=[f.id for f in left],
            finding_ids_b=[f.id for f in right],
            reason=reason,
        )

    if a is None or b is None:
        return [item("insufficient_evidence", [], [], "One or both auditors did not complete")]

    def key(f):
        # claim_code describes the checked proposition, not its legal judgment or prose.
        return f.topic, normalized(f.subject), f.claim_code

    left, right = defaultdict(list), defaultdict(list)
    for finding in a.findings:
        left[key(finding)].append(finding)
    for finding in b.findings:
        right[key(finding)].append(finding)
    results = []
    for identity in sorted(left.keys() | right.keys()):
        la, lb = left.get(identity, []), right.get(identity, [])
        if not la or not lb:
            results.append(
                item(
                    "single_auditor",
                    la,
                    lb,
                    "Unmatched proposition; semantic matching requires review",
                )
            )
            continue
        if len(la) != 1 or len(lb) != 1:
            results.append(
                item(
                    "partial_agreement",
                    la,
                    lb,
                    "Ambiguous grouping; all findings retained for semantic review",
                )
            )
            continue
        fa, fb = la[0], lb[0]
        if fa.source != fb.source or not set(fa.evidence_ids) & set(fb.evidence_ids):
            kind, reason = "partial_agreement", "Related proposition, different sources/evidence"
        elif (
            fa.evidence_quality != "complete"
            or fb.evidence_quality != "complete"
            or fa.status == "unverifiable"
            or fb.status == "unverifiable"
            or "unknown" in (fa.fact_assertion, fb.fact_assertion)
        ):
            kind, reason = "insufficient_evidence", "Missing support; a comparison is not proof"
        elif fa.fact_assertion != fb.fact_assertion:
            kind, reason = "factual_contradiction", "Opposite assertions for the same proposition"
        else:
            basis_a = {(normalized(n.act_id), normalized(n.provision)) for n in fa.normative_basis}
            basis_b = {(normalized(n.act_id), normalized(n.provision)) for n in fb.normative_basis}
            if (
                basis_a != basis_b
                or fa.legal_position != fb.legal_position
                or ((fa.status == "rejected") != (fb.status == "rejected"))
            ):
                kind, reason = (
                    "legal_qualification_disagreement",
                    "Different law/provision or qualification",
                )
            elif fa.legal_position == "undetermined":
                kind, reason = "insufficient_evidence", "Legal qualification remains undetermined"
            elif set(fa.evidence_ids) != set(fb.evidence_ids) or fa.material != fb.material:
                kind, reason = "partial_agreement", "Different support or materiality"
            else:
                kind, reason = (
                    "full_agreement",
                    "Structured agreement only; law and facts still unverified",
                )
        results.append(item(kind, [fa], [fb], reason))
    if not results:
        results.append(
            item("insufficient_evidence", [], [], "No findings is not proof of compliance")
        )
    return results
