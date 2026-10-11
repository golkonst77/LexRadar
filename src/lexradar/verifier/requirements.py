"""Finite offline normative checks. Source integrity is not legal trust or applicability."""

from datetime import timedelta
from pathlib import Path

from ..primary.qualification import digest
from .legal_cards import card_hash
from .offline import LocalResearch, fact_signature, verify_saved_assessment
from .registry import assess_registry, load_registry
from .requirement_models import (
    AbsenceCheck,
    ConditionCheck,
    NormativeVerification,
    NormativeWorkCheck,
    NormRequirement,
    NormRequirementSpec,
    RequirementApplicability,
    RequirementCondition,
)
from .research_context import research_context
from .saved_policy import policy_observations

DOMAIN_CATEGORIES = {
    "personal_data": list("ABCDEFH"),
    "medical": ["G", "H"],
    "paid_medical": ["G"],
    "consumer": ["G", "H"],
    "other": list("ABCDEFGH"),
}
# These are search directions explicitly requested by the task, not verified provisions.
SEARCH_DIRECTIONS = (
    ("152-ФЗ", "Федеральный закон о персональных данных", "ABCDEFH"),
    ("149-ФЗ", "Федеральный закон об информации", "ABEF"),
    ("38-ФЗ", "Федеральный закон о рекламе", "D"),
    ("323-ФЗ", "Федеральный закон об основах охраны здоровья", "EGH"),
    ("2300-1", "Закон РФ о защите прав потребителей", "GH"),
    (None, "Действующие правила предоставления платных медицинских услуг", "G"),
    (None, "Применимые подзаконные требования к политике и обработке ПДн", "BC"),
    (None, "Применимые требования к медицинскому раскрытию сведений", "G"),
    (None, "КоАП РФ: состав, субъект, редакция и условия ответственности", "ABCDEFGH"),
)


def search_tasks(findings):
    return [
        {
            "task_id": f"source-search-{index + 1:02d}",
            "act_number_candidate": number,
            "search_direction": title,
            "finding_ids": [f.id for f in findings if f.category in categories],
            "categories": list(categories),
            "provision": None,
            "quote": None,
            "state": "requires_human_source_identification",
            "source_verified": False,
            "required_actions": [
                "Identify exact provision and current official source",
                "Save exact source bytes, edition, period and provenance",
                "Independently review exceptions and applicability",
            ],
            "limitation": "Search task only; no normative text, force or sanctions asserted",
        }
        for index, (number, title, categories) in enumerate(SEARCH_DIRECTIONS)
        if any(f.category in categories for f in findings)
    ]


def implicit_spec(source):
    """Old cards remain usable as candidates; domain selection is never an applicability rule."""
    return NormRequirementSpec(
        requirement_id=f"candidate-{source.id}",
        source_card_id=source.id,
        categories=DOMAIN_CATEGORIES[source.domain],
        conditions=[
            RequirementCondition(
                condition_id="legal-context",
                predicate="operator_identity",
                rationale="No interpreted requirement supplied; establish actual obligated person",
            )
        ],
        necessary_evidence=[
            "Independently interpret exact provision and define its factual conditions"
        ],
        exceptions_and_alternatives=[
            "Identify exceptions and alternative legal bases from the actual provision"
        ],
        activity_scope="unknown",
        limitations=["Legacy/domain-derived candidate only; no inferred obligation"],
    )


def build_requirement(spec, source, record, registry, event, now):
    if source is None:
        return NormRequirement(
            requirement_id=spec.requirement_id,
            source_card_id=spec.source_card_id,
            event_date=event,
            text_check="missing",
            source_state="missing",
            conditions=spec.conditions,
            necessary_evidence=spec.necessary_evidence,
            exceptions_and_alternatives=spec.exceptions_and_alternatives,
            categories=spec.categories,
            activity_scope=spec.activity_scope,
            limitations=[*spec.limitations, "Referenced source card missing; no norm invented"],
        )
    assessment = record.card_assessment
    text_check, state = "missing", "missing"
    if assessment:
        if assessment.technical_integrity == "consistent":
            text_check, state = "matched_saved_text", "available_local_unverified"
        elif assessment.technical_integrity == "invalid":
            text_check, state = "contradictory", "contradictory"
        else:
            text_check, state = "missing", "missing"
    if not source.norm_text.strip() or not source.provision.strip():
        text_check, state = "incomplete", "incomplete"
    if state == "available_local_unverified" and (
        source.checked_at is None or now - source.checked_at > timedelta(days=30)
    ):
        state = "stale"
    if (
        assessment
        and assessment.temporal_claim == "expired"
        and state == "available_local_unverified"
    ):
        state = "stale"
    if source.checked_at and source.checked_at > now and state == "available_local_unverified":
        state = "incomplete"
    reasons = [
        *spec.limitations,
        *record.reasons,
        "Requirement annotations are untrusted; exact quote does not establish interpretation",
    ]
    intake_status = "not_supplied"
    intake = next((i for i in registry.source_intakes if i.source_card_id == source.id), None)
    if intake:
        intake_status = "untrusted"
        if (
            assessment is None
            or assessment.technical_integrity != "consistent"
            or intake.original_sha256 != assessment.saved_original_sha256
            or intake.card_sha256 != card_hash(source)
            or intake.acquired_at > intake.reviewed_at
            or intake.reviewed_at > now
        ):
            intake_status = "invalidated"
        reasons.append("Manual intake is a declaration, not authenticated reviewer/source evidence")
    return NormRequirement(
        requirement_id=spec.requirement_id,
        source_card_id=source.id,
        act_name=source.act_name,
        act_number=source.act_number,
        source_kind=getattr(source, "source_kind", "legacy_unclassified"),
        provision=source.provision,
        effective_from=source.effective_from,
        effective_until=source.effective_until,
        event_date=event,
        source_url=str(source.source_url) if source.source_url else None,
        original_path=getattr(source, "original_path", None),
        expected_sha256=source.text_sha256,
        actual_sha256=assessment.saved_original_sha256 if assessment else None,
        exact_quote=source.norm_text,
        text_check=text_check,
        source_state=state,
        revision=source.revision,
        declared_temporal_state=assessment.temporal_claim if assessment else "ambiguous",
        conditions=spec.conditions,
        necessary_evidence=spec.necessary_evidence,
        exceptions_and_alternatives=spec.exceptions_and_alternatives,
        categories=spec.categories,
        activity_scope=spec.activity_scope,
        limitations=sorted(set(reasons)),
        intake_status=intake_status,
        research_context=research_context(source, registry, event, assessment),
    )


def absence_check(independent, research):
    materials = independent.materials
    unread = None
    if research:
        from ..primary.markup import link_kind

        links = [
            link.url
            for parser in research.markup.values()
            for link in parser.links
            if link_kind(link.url, link.text) or link.url.split("?")[0].endswith(".pdf")
        ]
        complete_urls = {
            str(research.evidence[m.evidence_id].source)
            for m in materials
            if m.text_coverage_complete
        }
        unread = (
            any(url not in complete_urls for url in links)
            if links
            else (None if any(parser.limitations for parser in research.markup.values()) else False)
        )
    return AbsenceCheck(
        inspected_material_ids=[m.evidence_id for m in materials],
        all_saved_text_complete=bool(materials)
        and all(m.text_coverage_complete for m in materials),
        # The saved crawl cannot establish the denominator of all relevant site sections.
        linked_documents_not_read=unread,
        reasons=[
            "Relevant-site scope/denominator not independently established",
            "Links and excerpts do not prove full document examination",
            "Actual provider/operator identity is unestablished",
            "Single-page/partial collection cannot prove information absent elsewhere",
        ],
    )


def check_condition(condition, finding, independent_row, research, policies=()):
    ids, eids = [], []
    if condition.predicate in {"policy_text_available", "saved_policy_text_present"} and policies:
        # Document presence belongs to Stage I material inventory, not a finding's quotes.
        return ConditionCheck(
            condition_id=condition.condition_id,
            predicate=condition.predicate,
            status="supported_static_fact",
            fact_ids=[p.id for p in policies],
            evidence_ids=sorted({p.evidence_id for p in policies}),
            reason="Saved policy text independently reproduced; reading scope is in observations; "
            "live accessibility, completeness of the site and legal sufficiency unknown",
        )
    if (
        research
        and independent_row
        and independent_row.technical_status in {"verified_fact", "partially_verified"}
    ):
        fresh = next(
            (
                f
                for f in research.assessment.legal_findings
                if f.id == finding.id and f.category == finding.category
            ),
            None,
        )
        if fresh:
            signatures = {fact_signature(f) for f in fresh.facts}
            for fact in finding.facts:
                if (
                    fact.id not in independent_row.independently_reproduced_fact_ids
                    or fact_signature(fact) not in signatures
                ):
                    continue
                predicate = condition.predicate
                objects = fact.objects
                quote = " ".join(c.quote or "" for c in fact.citations).casefold()
                supported = False
                if predicate == "static_form_declared":
                    supported = any(o.type == "form" for o in objects)
                elif predicate == "organization_mention":
                    supported = any(o.type == "organization" for o in objects)
                elif predicate == "multiple_organization_mentions":
                    supported = (
                        len({o.source_id for o in fresh.objects if o.type == "organization"}) > 1
                    )
                elif predicate in {"policy_text_available", "saved_policy_text_present"}:
                    # Preserve the PR15 path for independently reproduced saved PDF text.
                    # A page/organization quote cannot masquerade as a document.
                    supported = (
                        len(quote) >= 80
                        and "персональн" in quote
                        and "политик" in quote
                        and any(o.type == "document" for o in objects)
                    )
                elif predicate == "consent_marker_declared":
                    supported = "соглас" in quote or "consent" in quote
                elif predicate == "advertising_marker_declared":
                    supported = "реклам" in quote or "advertis" in quote
                elif predicate in {"booking_reference_declared", "analytics_reference_declared"}:
                    kinds = {g.id: g for g in research.primary.integration_groups}
                    expected = "E" if predicate == "booking_reference_declared" else "F"
                    supported = finding.category == expected and any(
                        o.type == "integration" and o.source_id in kinds for o in objects
                    )
                if supported:
                    ids.append(fact.id)
                    eids.extend(c.evidence_id for c in fact.citations)
    return ConditionCheck(
        condition_id=condition.condition_id,
        predicate=condition.predicate,
        status="supported_static_fact" if ids else "unknown",
        fact_ids=sorted(set(ids)),
        evidence_ids=sorted(set(eids)),
        reason=(
            "Bounded static observation reproduced; "
            "no real processing/identity/obligation established"
        )
        if ids
        else (
            "Insufficient independent evidence; "
            "declaration, sector hint or absent marker is not proof"
        ),
    )


def verify_normative(
    root, primary, assessment, registry_path, *, event_date, evaluated_at, max_input_bytes=2_000_000
):
    """No imported VerificationReport or editable review flags act as a trust provider."""
    if evaluated_at.utcoffset() is None:
        raise ValueError("Aware evaluation timestamp required")
    independent = verify_saved_assessment(
        root, primary, assessment, registry_path=registry_path, max_input_bytes=max_input_bytes
    )
    gaps = []
    research = None
    if independent.independent_research_sha256:
        research = LocalResearch(
            root,
            registry=None,
            sector=primary.sector_hint,
            as_of=primary.assessed_at,
            max_input_bytes=max_input_bytes,
        )
    registry, registry_hash, records = None, None, {}
    try:
        registry, registry_hash = load_registry(registry_path)
        records = assess_registry(registry, event_date, evaluated_at, 30)
    except (OSError, ValueError):
        gaps.append(
            "Registry missing/unsafe/invalid; identify actual sources before normative assessment"
        )
    requirements = []
    if registry:
        sources = {s.id: s for s in [*registry.cards, *registry.sources]}
        specs = list(registry.requirements)
        referenced = {s.source_card_id for s in specs}
        specs.extend(implicit_spec(s) for s in sources.values() if s.id not in referenced)
        if len({s.requirement_id for s in specs}) != len(specs):
            raise ValueError("Ambiguous explicit/implicit requirement IDs")
        for spec in sorted(specs, key=lambda s: s.requirement_id):
            source = sources.get(spec.source_card_id)
            requirements.append(
                build_requirement(
                    spec,
                    source,
                    records.get(spec.source_card_id),
                    registry,
                    event_date,
                    evaluated_at,
                )
            )
        gaps.extend(
            f"{r.requirement_id}: source={r.source_state}, text={r.text_check}; "
            "official origin and revision unverified"
            for r in requirements
        )
    rows, summaries = [], []
    policies = policy_observations(research)
    absence = absence_check(independent, research)
    independent_findings = {f.source_finding.id: f for f in independent.findings}
    tasks = search_tasks(assessment.legal_findings)
    for finding in assessment.legal_findings:
        applicable = [r for r in requirements if finding.category in r.categories]
        for requirement in applicable:
            conditions = [
                check_condition(
                    c, finding, independent_findings.get(finding.id), research, policies
                )
                for c in requirement.conditions
            ]
            actions = [
                "Authenticate exact source and current revision for event date",
                "Independently interpret exact provision, exclusions and alternative bases",
                "Establish actual obligated person, activity and processing",
                *requirement.necessary_evidence,
                *requirement.exceptions_and_alternatives,
            ]
            rows.append(
                RequirementApplicability(
                    finding_id=finding.id,
                    category=finding.category,
                    requirement_id=requirement.requirement_id,
                    status="source_unverified",
                    source_text_check=requirement.text_check,
                    condition_checks=conditions,
                    conditional_factual_support="potentially_applicable"
                    if any(c.fact_ids for c in conditions)
                    else "applicability_unknown",
                    absence_check=absence,
                    human_checks=sorted(set(actions)),
                )
            )
            gaps.extend(f"{finding.id}/{requirement.requirement_id}: {a}" for a in actions)
        summaries.append(
            {
                "finding_id": finding.id,
                "category": finding.category,
                "requirement_ids": [r.requirement_id for r in applicable],
                "source_search_task_ids": [
                    t["task_id"] for t in tasks if finding.id in t["finding_ids"]
                ],
                "status": "source_unverified",
                "revision": "revision_unverified",
                "applicability": "applicability_unknown",
                "evidence_sufficient_for_legal_conclusion": False,
                "technical_status": independent_findings[finding.id].technical_status,
                "absence_check": absence.model_dump(mode="json"),
                "human_checks": [
                    "Find exact relevant provision in saved official source",
                    "Confirm current revision and legal applicability; "
                    "examine available documents first",
                ],
            }
        )
        if not applicable:
            gaps.append(
                f"{finding.id}: no actual source requirement linked; search tasks are not norms"
            )
    work_rows = []
    for candidate in assessment.commercial_work:
        linked = [r for r in rows if r.finding_id in candidate.legal_finding_ids]
        conditional = any(
            r.conditional_factual_support == "potentially_applicable"
            and r.source_text_check == "matched_saved_text"
            for r in linked
        )
        independently_checked_work = next(
            (w for w in independent.commercial_work if w.source_candidate.id == candidate.id), None
        )
        conditional = (
            conditional
            and independently_checked_work is not None
            and (
                independently_checked_work.factual_basis in {"verified_fact", "partially_verified"}
            )
        )
        reading_partial = any(not m.text_coverage_complete for m in independent.materials)
        work_rows.append(
            NormativeWorkCheck(
                candidate_id=candidate.id,
                work_type=candidate.work_type,
                related_applicability=[f"{r.finding_id}/{r.requirement_id}" for r in linked],
                existing_document_reading="partial" if reading_partial else "unknown",
                internal_recommendation="conditional_scope_review"
                if conditional
                else "insufficient_normative_basis",
                remaining_checks=[
                    "Confirm applicable obligation and actual unmet need",
                    "Read available current document completely; assess alternatives",
                    "Distinguish new document, correction and purely technical changes",
                    "Consent is not automatically the necessary processing basis",
                ],
            )
        )
    if registry and load_registry(registry_path)[1] != registry_hash:
        raise ValueError("Registry changed during normative verification")
    if registry:
        end_records = assess_registry(registry, event_date, evaluated_at, 30)
        if digest({k: v.model_dump(mode="json") for k, v in records.items()}) != digest(
            {k: v.model_dump(mode="json") for k, v in end_records.items()}
        ):
            raise ValueError("Saved normative source changed during verification")
        cards = {c.id: c for c in registry.cards}
        for requirement in requirements:
            if requirement.research_context and requirement.source_card_id in cards:
                repeated = research_context(
                    cards[requirement.source_card_id],
                    registry,
                    event_date,
                    end_records[requirement.source_card_id].card_assessment,
                )
                if repeated != requirement.research_context:
                    raise ValueError("Research evidence changed during normative verification")
    # Recheck bytes after the additional normative stage, without creating client permission.
    if research:
        from ..auditors.evidence import load_packet

        if (
            load_packet(root.resolve(), max_input_bytes).manifest_sha256
            != research.packet.manifest_sha256
        ):
            raise ValueError("Dossier changed during normative verification")
    return NormativeVerification(
        event_date=event_date,
        evaluated_at=evaluated_at,
        registry_sha256=registry_hash,
        source_assessment_sha256=digest(assessment.model_dump(mode="json")),
        source_research_sha256=independent.independent_research_sha256,
        requirements=requirements,
        applicability=rows,
        commercial_work=work_rows,
        question_summaries=summaries,
        source_search_tasks=tasks,
        gaps=sorted(set(gaps)),
        saved_policy_observations=policies,
    )


def export_normative(result, output: Path, source: Path):
    import json

    output = output.absolute()
    if output.resolve().is_relative_to(source.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("New output outside dossier required")
    output.mkdir(parents=True, exist_ok=False)
    common = result.model_dump(mode="json")
    for name, fields in (
        ("norm-requirements", ("requirements",)),
        ("requirement-applicability", ("applicability", "question_summaries", "commercial_work")),
        ("normative-verification-gaps", ("gaps", "source_search_tasks")),
    ):
        payload = {
            k: v
            for k, v in common.items()
            if k
            not in {
                "requirements",
                "applicability",
                "question_summaries",
                "commercial_work",
                "gaps",
                "source_search_tasks",
            }
            or k in fields
        }
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
