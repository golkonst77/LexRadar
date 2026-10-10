"""Legacy/demo rules only. A demo GO never permits production legal/client release."""

from .models import AuditInput, FindingStatus, GatewayDecision, Outcome, Signal


def decide_demo(data: AuditInput) -> GatewayDecision:
    def result(outcome, reason, ids=()):
        return GatewayDecision(outcome=outcome, reasons=[reason], eligible_finding_ids=list(ids))

    verification = data.verification
    if data.target.critical_obstacles or verification.critical_obstacles:
        return result(Outcome.STOP, "critical_obstacle")
    if (
        {a.auditor for a in data.auditors} != {"A", "B"}
        or not all(a.completed for a in data.auditors)
        or not verification.completed
    ):
        return result(Outcome.HOLD, "review_incomplete")
    if verification.contradictions or verification.uncertainties:
        return result(Outcome.HOLD, "contradiction_or_material_uncertainty")
    if not data.target.legal_entity.identity_verified:
        return result(Outcome.HOLD, "legal_entity_unverified")

    left, right = ({f.id: f for f in a.findings} for a in data.auditors)
    if left.keys() != right.keys():
        return result(Outcome.HOLD, "auditor_coverage_disagreement")
    for key in left:
        # Confidence is informational; every other assertion must agree exactly.
        if left[key].model_dump(exclude={"confidence"}) != right[key].model_dump(
            exclude={"confidence"}
        ):
            return result(Outcome.HOLD, "auditor_assertion_disagreement")

    checks = {v.finding_id: v for v in verification.findings}
    evidence = {e.id: e for e in data.evidence}
    eligible = []
    potential = False
    for key, finding in left.items():
        check = checks.get(key)
        if check is None or check.status == FindingStatus.PENDING:
            return result(Outcome.HOLD, "finding_verification_incomplete")
        if (
            finding.status == FindingStatus.REJECTED and check.status == FindingStatus.CONFIRMED
        ) or (finding.status == FindingStatus.CONFIRMED and check.status == FindingStatus.REJECTED):
            return result(Outcome.HOLD, "verifier_auditor_disagreement")
        if check.status == FindingStatus.REJECTED:
            continue
        potential = True
        supporting = [evidence[eid] for eid in finding.evidence_ids]
        if (
            finding.status == FindingStatus.CONFIRMED
            and finding.material
            and finding.signal == Signal.SUBSTANTIVE
            and not finding.assumptions
            and supporting
            and all(e.available for e in supporting)
            and any(e.signal == Signal.SUBSTANTIVE for e in supporting)
            and check.evidence_checked
            and check.legal_basis_checked
            and check.reviewer_kind == "human"
        ):
            eligible.append(key)
    if eligible:
        return result(
            Outcome.GO, "material_finding_independently_and_human_verified", sorted(eligible)
        )
    if potential:
        return result(Outcome.NURTURE, "insufficient_support_for_categorical_claim")
    return result(Outcome.NURTURE, "no_confirmed_violation_no_outreach_basis")


# Compatibility for v0.1 callers; returned scope and denied release cannot be changed.
decide = decide_demo
