"""Fail closed on schema/references; AI never verifies law or confirms a violation."""

import json

from pydantic import ValidationError

from .evidence import EvidencePacket
from .models import AIAuditorResult


class ResultError(ValueError):
    pass


def validate_result(
    content: str, auditor: str, packet: EvidencePacket
) -> tuple[AIAuditorResult, list[str]]:
    notes = []
    try:
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ResultError("invalid_response_schema")
        for finding in raw.get("findings", []):
            if finding.get("status") == "confirmed":
                finding["status"] = "potential"
                notes.append("ai_confirmed_downgraded")
            for basis in finding.get("normative_basis", []):
                if basis.get("actuality_status", "unverified") != "unverified" or basis.get(
                    "verified_source"
                ):
                    notes.append("ai_legal_verification_removed")
                basis["actuality_status"] = "unverified"
                basis["verified_source"] = None
        result = AIAuditorResult.model_validate(raw)
    except (ValueError, TypeError, AttributeError, ValidationError) as exc:
        raise ResultError("invalid_response_schema") from exc
    if result.auditor != auditor:
        raise ResultError("wrong_auditor_label")
    evidence = {e.id: e for e in packet.data.evidence}
    docs = {d.evidence_id: d for d in packet.data.documents}
    pages = {p.evidence_id: p for p in packet.data.pages}
    for finding in result.findings:
        finding.fact_supported = False
        if (
            len(finding.evidence_ids) != len(set(finding.evidence_ids))
            or not set(finding.evidence_ids) <= evidence.keys()
        ):
            raise ResultError("unknown_or_duplicate_evidence_id")
        supporting = [evidence[eid] for eid in finding.evidence_ids]
        if any(q.evidence_id not in finding.evidence_ids for q in finding.text_grounding):
            raise ResultError("quotation_refers_to_uncited_evidence")
        if finding.source not in {e.source for e in supporting}:
            raise ResultError("unsupported_source_url")
        incomplete = any(e.status != "complete" for e in supporting)
        missing_text = any(
            e.id in docs
            and (docs[e.id].extraction_status == "failed" or not (docs[e.id].text or "").strip())
            for e in supporting
        )
        if any(not e.available for e in supporting) or missing_text:
            finding.status = "unverifiable"
            finding.fact_assertion = "unknown"
            finding.evidence_quality = (
                "unavailable" if any(not e.available for e in supporting) else "text_not_provided"
            )
            finding.limitations.append("Referenced materials unavailable or not textually supplied")
            finding.additional_checks.append(
                "Obtain missing materials or perform visual PDF review"
            )
        elif incomplete:
            finding.evidence_quality = "partial"
            finding.limitations.append("Partial collection; incomplete examination")
        else:
            finding.evidence_quality = "complete"
        if any(
            e.id in docs and docs[e.id].extraction_status == "visual_review_required"
            for e in supporting
        ):
            if finding.evidence_quality == "complete":
                finding.evidence_quality = "partial"
            finding.limitations.append("PDF contains unexamined pages; visual review required")
            finding.additional_checks.append("Visually examine unread PDF pages independently")
            mixed_ids = {
                e.id
                for e in supporting
                if e.id in docs and docs[e.id].extraction_status == "visual_review_required"
            }
            grounded = finding.examination_scope == "text_excerpt" and all(
                any(
                    g.evidence_id == eid
                    and g.exact_quote.strip()
                    and finding.fact == g.exact_quote
                    and g.page is not None
                    and g.exact_quote in docs[eid].page_texts.get(g.page, "")
                    for g in finding.text_grounding
                )
                for eid in mixed_ids
            )
            # A quotation supports only an observed excerpt, never absence across unread pages.
            if not grounded or finding.fact_assertion != "present":
                finding.status = "unverifiable"
                finding.fact_assertion = "unknown"
                finding.limitations.append(
                    f"Unverified requested scope: {finding.examination_scope}"
                )
                finding.examination_scope = "unknown"
                notes.append("mixed_pdf_claim_not_grounded")
                finding.limitations.append(
                    "Claim not grounded in available text; whole-document examination prohibited"
                )
            else:
                finding.limitations.append(
                    "Only quotation occurrence checked; interpretation needs human review; "
                    "unread pages excluded"
                )
        grounded = (
            finding.fact_assertion == "present" and finding.examination_scope == "text_excerpt"
        ) and all(
            any(
                q.evidence_id == eid
                and q.exact_quote.strip()
                and (finding.fact == q.exact_quote if finding.material or eid in docs else True)
                and (
                    q.page is not None and q.exact_quote in docs[eid].page_texts.get(q.page, "")
                    if eid in docs
                    else q.page in {None, 1} and q.exact_quote in (pages[eid].text or "")
                )
                for q in finding.text_grounding
            )
            for eid in finding.evidence_ids
        )
        finding.fact_supported = bool(grounded)
        if finding.fact_assertion == "absent":
            finding.search_limitations.extend(
                [
                    "Negative assertion is unconfirmed; search scope is self-declared",
                    "Collected subset cannot establish absence across a website",
                ]
            )
            finding.limitations.append("Absence is not established by missing or incomplete text")
        if finding.material and finding.fact_assertion == "present" and not grounded:
            finding.status = "unverifiable"
            finding.limitations.append("Material assertion lacks a source/page-bound exact excerpt")
        for eid in finding.evidence_ids:
            reading = docs[eid].reading if eid in docs else pages[eid].reading
            if not reading.text_coverage_complete:
                if finding.evidence_quality == "complete":
                    finding.evidence_quality = "partial"
                finding.limitations.append(f"{eid}: extraction/reading incomplete or unknown")
            if reading.provenance != "reproduced":
                finding.fact_supported = False
                if finding.material and finding.fact_assertion == "present":
                    finding.status = "unverifiable"
                finding.limitations.append(f"{eid}: derivative provenance not reproducible")
        finding.limitations.append("All normative references are independently unverified")
        finding.additional_checks.append("Independently verify current law and its applicability")
        if finding.status != "rejected":
            finding.additional_checks.append("Human verification of factual proposition required")
    result.limitations.extend(packet.limitations)
    try:
        result = AIAuditorResult.model_validate(result.model_dump())
    except ValidationError as exc:
        raise ResultError("normalized_response_exceeds_limits") from exc
    return result, notes
