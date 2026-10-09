"""Quality gates inspect validated results, not desired model status."""

from ..verifier.rules import WEAK_CLAIMS, _same_claim
from .models import Check, Prediction


def observed(check, data):
    evidence = next((e for e in data.evidence if e.id == check.evidence_id), None)
    page = next((p for p in data.pages if p.evidence_id == check.evidence_id), None)
    doc = next((d for d in data.documents if d.evidence_id == check.evidence_id), None)
    if check.kind == "text_contains":
        return bool(page and check.value in (page.text or ""))
    if check.kind == "unavailable":
        return bool(evidence and evidence.status == "unavailable" and not evidence.available)
    if check.kind == "partial_html":
        return bool(
            evidence
            and evidence.status == "partial"
            and evidence.available
            and {"html", "text"} <= {a.kind for a in evidence.artifacts}
        )
    if check.kind == "forms_no_checkbox":
        return bool(
            page
            and page.forms
            and all(f.type != "checkbox" for form in page.forms for f in form.fields)
        )
    if check.kind == "entities_count":
        return len(data.entities) == int(check.value) and not any(
            e.identity_verified for e in data.entities
        )
    if check.kind == "pdf_scan":
        return bool(doc and not doc.text and doc.extraction_status == "visual_review_required")
    if check.kind == "pdf_mixed":
        return bool(doc and doc.text and doc.extraction_status == "visual_review_required")
    if check.kind == "document_link":
        return bool(page and page.document_links)
    if check.kind == "script_disabled":
        return bool(page and "SCRIPT EXECUTED" not in (page.text or ""))
    return False


def grounded(candidate, materials):
    by_id = {m.evidence_id: m for m in materials}
    if candidate.fact_assertion != "present" or candidate.examination_scope != "text_excerpt":
        return False
    for eid in candidate.evidence_ids:
        material = by_id.get(eid)
        quotes = [q for q in candidate.fragments if q.evidence_id == eid]
        if not material or not quotes or material.examination == "not_examined":
            return False
        for q in quotes:
            text = material.supplied_text if q.page is None else material.page_texts.get(q.page, "")
            if q.text not in text or (
                q.page is not None and q.page not in material.examined_page_numbers
            ):
                return False
            if material.type == "pdf" and (q.page is None or candidate.fact != q.text):
                return False
    return True


def predictions(report):
    result = []
    for finding in report.independent_findings:
        c = finding.candidate
        result.append(
            Prediction(
                id="V:" + c.id,
                topic=c.topic,
                claim_code=c.claim_code,
                evidence_ids=c.evidence_ids,
                asserted=finding.status in {"potential_issue", "verified_issue"},
                status=finding.status,
                fact_grounded=grounded(c, report.materials),
                norm_verified=bool(finding.norms)
                and all(n.status == "current_confirmed" for n in finding.norms),
                applicability=finding.applicability,
                operator_ref=c.operator_ref,
                reasons=finding.reasons,
            )
        )
    for a in report.auditor_assessments:
        f = a.original_finding
        if a.duplicate_of:
            continue
        # Collapse only an exact claim with the same position and final status.
        if any(
            _same_claim(f, own.candidate)
            and a.status == own.status
            and f.fact_assertion == own.candidate.fact_assertion
            and f.legal_position
            == ("no_issue" if own.status == "no_issue_observed" else "possible_issue")
            for own in report.independent_findings
        ):
            continue
        result.append(
            Prediction(
                id=f"{a.proposal.auditor}:{f.id}",
                topic=f.topic,
                claim_code=f.claim_code,
                evidence_ids=f.evidence_ids,
                asserted=a.status in {"potential_issue", "verified_issue"},
                status=a.status,
                fact_grounded=False,
                norm_verified=bool(a.normative_assessments)
                and all(n.status == "current_confirmed" for n in a.normative_assessments),
                applicability=a.applicability,
                reasons=a.reasons,
            )
        )
    return result


def gates(reference, collection, analysis, report):
    all_items = [(f.candidate, f.status, f.norms) for f in report.independent_findings]
    all_items += [
        (a.original_finding, a.status, a.normative_assessments) for a in report.auditor_assessments
    ]
    valid = {e.id for e in collection.evidence}
    false_codes = {u.claim_code for u in reference.units if not u.expected_problem}
    checks = [
        Check(
            name="false_accusations_not_confirmed",
            passed=all(
                status != "verified_issue"
                for c, status, _ in all_items
                if c.claim_code in false_codes or c.claim_code in WEAK_CLAIMS
            ),
            detail="Labeled negative claims and weak technical signals cannot be confirmed.",
        ),
        Check(
            name="unverified_norm_not_confirmed",
            passed=all(
                norms and all(n.status == "current_confirmed" for n in norms)
                for _, status, norms in all_items
                if status == "verified_issue"
            ),
            detail="Confirmation needs a trusted current legal basis; JSON metadata is untrusted.",
        ),
        Check(
            name="evidence_ids_valid",
            passed=all(set(c.evidence_ids) <= valid for c, _, _ in all_items),
            detail="Exact dossier references.",
        ),
        Check(
            name="unread_material_not_fully_reviewed",
            passed=all(
                m.examination != "text_only" or m.examined_pages == m.page_count
                for m in report.materials
            )
            and all(m.visual_review_required for m in report.materials if m.type == "pdf"),
            detail="Acknowledged text pages do not certify images or whole document review.",
        ),
        Check(
            name="no_go_or_client_send",
            passed=not (
                report.automatic_go_allowed
                or report.automatic_send_allowed
                or analysis.automatic_go_allowed
                or analysis.automatic_send_allowed
            ),
            detail="Quality results have no legal Gateway or sending interface.",
        ),
        Check(
            name="providers_completed",
            passed=analysis.completed and report.completed,
            detail="Provider/schema/budget failures cannot be a successful audit.",
        ),
    ]
    for norm_id, expected in reference.normative_references.items():
        actual = next((n.status for n in report.normative_sources if n.norm_id == norm_id), None)
        checks.append(
            Check(
                name="norm_" + norm_id,
                passed=actual == expected,
                detail="Reference norm verification status: " + expected,
            )
        )
    for index, limitation in enumerate(reference.expected_limitations):
        checks.append(
            Check(
                name=f"limitation_{index}",
                passed=any(limitation in text for text in report.limitations),
                detail=limitation,
            )
        )
    for index, observation in enumerate(reference.observed_facts):
        checks.append(
            Check(
                name=f"fact_{index}_{observation.kind}",
                passed=observed(observation, collection),
                detail=observation.kind,
            )
        )
    for code, allowed in reference.admissible_statuses.items():
        statuses = [status for c, status, _ in all_items if c.claim_code == code]
        checks.append(
            Check(
                name="status_" + code,
                passed=all(s in allowed for s in statuses),
                detail="Allowed final statuses: "
                + ", ".join(allowed)
                + ("; no finding emitted" if not statuses else ""),
            )
        )
    return checks
