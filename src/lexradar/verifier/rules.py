"""Deterministic normalization; a model's assertion never verifies law or applicability."""

from datetime import datetime

from ..auditors.models import REQUIRED_TOPICS, AnalysisReport, Topic
from ..models import Signal
from .models import (
    AuditAssessment,
    Candidate,
    ComparisonResult,
    Finding,
    HumanFindingReview,
    IndependentResult,
    VerifierConfig,
)
from .packets import Prepared, canonical, entity_refs
from .registry import assess_registry, digest

WEAK_CLAIMS = {
    "pdf_scan_illegal",
    "missing_checkbox_illegal",
    "no_checkbox",
    "analytics_illegal",
    "cookies_illegal",
    "google_analytics_illegal",
    "document_unavailable_illegal",
    "operator_name_confirms_identity",
}


def candidate_hash(candidate: Candidate) -> str:
    return digest(canonical(candidate.model_dump(mode="json")))


def normalize_independent(
    result: IndependentResult,
    prepared: Prepared,
    config: VerifierConfig,
    now: datetime,
    human_reviews: list[HumanFindingReview] | None = None,
) -> list[Finding]:
    ids = [f.id for f in result.findings]
    topics = [r.topic for r in result.directions]
    if (
        len(ids) != len(set(ids))
        or len(topics) != len(set(topics))
        or not REQUIRED_TOPICS <= set(topics)
    ):
        raise ValueError("Unique findings and all nine independent review directions required")
    materials = {m.evidence_id: m for m in prepared.materials}
    sources = {s.id: s for s in [*prepared.registry.sources, *prepared.registry.cards]}
    reviews = human_reviews or []
    hashes = [r.candidate_sha256 for r in reviews]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Duplicate human finding approval")
    candidates = {candidate_hash(f) for f in result.findings}
    if any(
        r.candidate_sha256 not in candidates or r.packet_sha256 != prepared.packet.sha256
        for r in reviews
    ):
        raise ValueError("Human finding review is stale or belongs to another packet")
    normalized = []
    keys = set()
    period = prepared.packet.data.started_at.date()
    normative = assess_registry(prepared.registry, period, now, config.legal_recheck_days)
    for candidate in result.findings:
        if (
            len(candidate.evidence_ids) != len(set(candidate.evidence_ids))
            or not set(candidate.evidence_ids) <= materials.keys()
        ):
            raise ValueError("Unknown/duplicate evidence in independent finding")
        if candidate.source not in {materials[eid].url for eid in candidate.evidence_ids}:
            raise ValueError("Unsupported independent source")
        if (
            len(candidate.norm_ids) != len(set(candidate.norm_ids))
            or not set(candidate.norm_ids) <= sources.keys()
        ):
            raise ValueError("Unknown/duplicate norm; LLM cannot extend trusted registry")
        reasons = [*candidate.limitations, "Model confidence is not legal confirmation"]
        status = "potential_issue"
        checks = [
            *candidate.additional_checks,
            "Independently review interpretation and applicability",
        ]
        facts = (
            candidate.fact_assertion == "present" and candidate.examination_scope == "text_excerpt"
        )
        for eid in candidate.evidence_ids:
            material = materials[eid]
            quotes = [q for q in candidate.fragments if q.evidence_id == eid]
            facts &= (
                bool(quotes)
                and material.text_available
                and material.examination != "not_examined"
                and material.reading.provenance == "reproduced"
            )
            for quote in quotes:
                text = (
                    material.supplied_text
                    if quote.page is None
                    else material.page_texts.get(quote.page, "")
                )
                facts &= bool(quote.text.strip()) and quote.text in text
                if quote.page is not None:
                    facts &= quote.page in material.examined_page_numbers
                if material.type == "pdf":
                    # Mixed PDFs must be grounded in a particular supplied text page.
                    facts &= quote.page is not None and candidate.fact == quote.text
            if material.examination != "text_only" or material.visual_review_required:
                reasons.append(f"{eid}: text excerpt only; unread material is excluded")
        if any(q.evidence_id not in candidate.evidence_ids for q in candidate.fragments):
            raise ValueError("Quotation refers to uncited evidence")
        if not facts:
            status = "insufficient_evidence"
            reasons.append(
                "Claim not bound to available text; absence/whole-document claims unsupported"
            )
            checks.append(
                "Review publicly obtainable missing text or unread pages; scans are not violations"
            )
        if candidate.fact_assertion == "absent":
            reasons.extend(
                [
                    f"Self-declared search scope: {candidate.search_scope}",
                    *candidate.search_limitations,
                    "Negative assertion cannot establish absence beyond supplied/reviewed text",
                ]
            )
        assessments = [normative[nid] for nid in candidate.norm_ids]
        reasons.extend(reason for assessment in assessments for reason in assessment.reasons)
        if not assessments or any(n.status != "current_confirmed" for n in assessments):
            reasons.append("Current applicable legal basis not fully confirmed")
            checks.append("Verify specific official revision and effective period")
        human = next((r for r in reviews if r.candidate_sha256 == candidate_hash(candidate)), None)
        applicability = "unestablished"
        human_valid = False
        if human:
            if (
                not human.reviewer.strip()
                or human.reviewed_at > now
                or human.period_from > period
                or human.period_until < period
                or human.period_until < human.period_from
                or human.reviewed_at < prepared.packet.data.started_at
                or human.operator_ref != candidate.operator_ref
                or human.operator_ref not in entity_refs(prepared.packet)
            ):
                raise ValueError("Human verification period/operator does not match dossier")
            human_valid = (
                human.fact_verified
                and human.interpretation_verified
                and human.operator_identity_verified
            )
            if human.operator_identity_verified:
                applicability = {
                    "applicable": "confirmed",
                    "not_applicable": "not_applicable",
                    "unknown": "unestablished",
                }[human.applicability]
            if applicability == "not_applicable":
                status = "rejected"
                reasons.append(
                    "Human review establishes norm is inapplicable to this operator/activity/period"
                )
        if candidate.signal != Signal.SUBSTANTIVE or candidate.claim_code in WEAK_CLAIMS:
            status = "rejected"
            reasons.append("Technical signal alone is not proof of unlawful processing")
        elif facts and assessments and all(n.status == "repealed" for n in assessments):
            status = "rejected"
            reasons.append("Cited revisions are no longer in force for the examined period")
        elif (
            facts
            and human_valid
            and applicability == "confirmed"
            and assessments
            and all(n.status == "current_confirmed" for n in assessments)
            and candidate.status not in {"rejected", "no_issue_observed"}
        ):
            status = "verified_issue"
        elif candidate.status == "no_issue_observed" and facts:
            status = "no_issue_observed"
            reasons.append("No issue observed only in quoted scope; not full legal compliance")
        elif candidate.status == "rejected" and status != "rejected":
            reasons.append("LLM-only rejection is not an established rebuttal")
        key = (
            candidate.topic,
            candidate.claim_code,
            candidate.subject,
            tuple(sorted(candidate.evidence_ids)),
        )
        if key in keys:
            status = "rejected"
            reasons.append("Duplicate independent hypothesis; does not create another problem")
        keys.add(key)
        if applicability == "unestablished":
            checks.append(
                "Establish actual operator identity, activity and applicability for examined period"
            )
        normalized.append(
            Finding(
                candidate=candidate,
                candidate_sha256=candidate_hash(candidate),
                status=status,
                fact_verified=bool(facts and human_valid),
                applicability=applicability,
                norms=assessments,
                reasons=reasons,
                additional_checks=checks,
                human_review=human,
            )
        )
    return normalized


def _same_claim(finding, candidate: Candidate) -> bool:
    return (
        finding.topic == candidate.topic
        and finding.claim_code == candidate.claim_code
        and finding.subject == candidate.subject
        and set(finding.evidence_ids) == set(candidate.evidence_ids)
        and finding.source == candidate.source
        and finding.examination_scope == "text_excerpt"
        and finding.fact == candidate.fact
        and finding.legal_interpretation == candidate.legal_interpretation
    )


def compare_results(
    proposals: ComparisonResult,
    analysis: AnalysisReport,
    independent: list[Finding],
    prepared: Prepared,
    config: VerifierConfig,
    now: datetime,
) -> list[AuditAssessment]:
    all_findings = {(r.auditor, f.id): f for r in analysis.runs for f in r.result.findings}
    keys = [(p.auditor, p.finding_id) for p in proposals.assessments]
    if len(keys) != len(set(keys)) or set(keys) != set(all_findings):
        raise ValueError("Comparison must assess every A/B finding exactly once")
    own = {f.candidate.id: f for f in independent}
    materials = {m.evidence_id: m for m in prepared.materials}
    assessed = []
    seen = {}
    period = prepared.packet.data.started_at.date()
    normative = assess_registry(prepared.registry, period, now, config.legal_recheck_days)
    assertions = {}
    for original in all_findings.values():
        key = (
            original.topic,
            original.claim_code,
            original.subject,
            tuple(sorted(original.evidence_ids)),
        )
        assertions.setdefault(key, set()).add(original.fact_assertion)
    for proposal in proposals.assessments:
        original = all_findings[(proposal.auditor, proposal.finding_id)]
        if not set(proposal.matched_independent_ids) <= own.keys():
            raise ValueError("Unknown independent match")
        matches = [
            own[fid]
            for fid in proposal.matched_independent_ids
            if _same_claim(original, own[fid].candidate)
        ]
        reasons = [proposal.reason, "A/B/Verifier agreement alone cannot confirm a violation"]
        assertion_key = (
            original.topic,
            original.claim_code,
            original.subject,
            tuple(sorted(original.evidence_ids)),
        )
        if {"present", "absent"} <= assertions[assertion_key]:
            reasons.append("A/B factual contradiction requires independent review; not a duplicate")
        norms = [
            normative[s.id]
            for basis in original.normative_basis
            for s in [*prepared.registry.sources, *prepared.registry.cards]
            if s.act_number == basis.act_id and s.provision == basis.provision
        ]
        status = "potential_issue"
        reasons.extend(reason for norm in norms for reason in norm.reasons)
        invalid_material = any(
            not materials[eid].text_available or materials[eid].reading.provenance != "reproduced"
            for eid in original.evidence_ids
        )
        grounded = all(
            any(
                q.evidence_id == eid
                and q.exact_quote.strip()
                and original.fact == q.exact_quote
                and (
                    q.page is not None
                    and q.exact_quote in materials[eid].page_texts.get(q.page, "")
                    if materials[eid].type == "pdf"
                    else q.page in {None, 1} and q.exact_quote in materials[eid].supplied_text
                )
                for q in original.text_grounding
            )
            for eid in original.evidence_ids
        )
        if original.material and not grounded:
            invalid_material = True
            reasons.append("Material A/B claim lacks independently checked source/page quote")
        if original.fact_assertion != "present" or invalid_material:
            status = "insufficient_evidence"
            reasons.append("Unsupported absence or unavailable content is not proof of a violation")
        if original.claim_code in WEAK_CLAIMS:
            status = "rejected"
            reasons.append(
                "Categorical conclusion from a technical signal or extracted name is invalid"
            )
        elif original.topic == Topic.CONSENT and original.fact_assertion == "absent":
            status = "insufficient_evidence"
            reasons.append(
                "No separate checkbox/consent assertion does not establish unlawful processing"
            )
        if any(
            materials[eid].type == "pdf" and materials[eid].visual_review_required
            for eid in original.evidence_ids
        ):
            status = "insufficient_evidence" if status != "rejected" else status
            reasons.append("PDF images/unread pages cannot support whole-document conclusions")
        if norms and all(n.status == "repealed" for n in norms):
            status = "rejected"
            reasons.append("Referenced normative revisions are not in force for examined period")
        if any(f.applicability == "not_applicable" for f in matches):
            status = "rejected"
            reasons.append("Independent review found norm inapplicable")
        if (
            status == "potential_issue"
            and norms
            and all(n.status == "current_confirmed" for n in norms)
            and all(
                any(
                    s.act_number == basis.act_id and s.provision == basis.provision
                    for s in [*prepared.registry.sources, *prepared.registry.cards]
                )
                for basis in original.normative_basis
            )
            and any(
                f.status == "verified_issue"
                and {n.norm_id for n in f.norms} == {n.norm_id for n in norms}
                for f in matches
            )
        ):
            status = "verified_issue"
            reasons.append(
                "Exact claim matched independently human-confirmed fact, norm and applicability"
            )
        if (
            proposal.status == "no_issue_observed"
            and original.legal_position == "no_issue"
            and status == "potential_issue"
            and any(f.status == "no_issue_observed" for f in matches)
        ):
            status = "no_issue_observed"
            reasons.append("Limited examined scope only")
        if proposal.status == "rejected" and status != "rejected":
            reasons.append("Model-only rebuttal retained as hypothesis pending factual review")
        if not norms or any(n.status == "unverified" for n in norms):
            reasons.append(
                "Original legal references lack confirmed revision; registry invents no sources"
            )
        key = (
            original.topic,
            original.claim_code,
            original.subject,
            tuple(sorted(original.evidence_ids)),
            original.fact_assertion,
            original.legal_position,
            original.fact,
            original.legal_interpretation,
            tuple(sorted((b.act_id, b.provision, b.requirement) for b in original.normative_basis)),
        )
        duplicate = seen.get(key)
        reference = f"{proposal.auditor}:{proposal.finding_id}"
        if duplicate:
            reasons.append("Duplicate hypothesis; do not count as another confirmed problem")
        else:
            seen[key] = reference
        assessed.append(
            AuditAssessment(
                original_finding=original.model_copy(deep=True),
                proposal=proposal,
                status=status,
                applicability="confirmed"
                if status == "verified_issue"
                else "not_applicable"
                if any(f.applicability == "not_applicable" for f in matches)
                else "unestablished",
                additional_checks=[
                    *original.additional_checks,
                    "Review contradictions, law revision and operator applicability",
                ],
                reasons=reasons,
                normative_assessments=norms,
                duplicate_of=duplicate,
                verified_independent_ids=[
                    f.candidate.id for f in matches if f.status == "verified_issue"
                ]
                if status == "verified_issue"
                else [],
            )
        )
    return assessed
