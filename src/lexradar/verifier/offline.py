"""Two-phase deterministic reinspection. No network, LLM or legal/client release."""

import hashlib
import json
import re
from pathlib import Path

from ..auditors.evidence import load_packet
from ..primary.markup import compact
from ..primary.mechanisms import MechanismMarkup
from ..primary.models import Citation, PrimaryAudit
from ..primary.qualification import digest, qualify_primary
from ..primary.qualification_models import InternalAssessment
from ..primary.service import audit_saved_dossier
from .legal_cards import read_local
from .offline_models import (
    DiscoveredFinding,
    DiscoveryCheck,
    EvidenceCheck,
    FindingCheck,
    IndependentVerification,
    MaterialCheck,
    WorkCheck,
)

DOMAINS = {
    "A": {"personal_data"},
    "B": {"personal_data"},
    "C": {"personal_data"},
    "D": {"personal_data", "consumer"},
    "E": {"personal_data", "medical"},
    "F": {"personal_data"},
    "G": {"medical", "paid_medical", "consumer"},
    "H": {"personal_data", "medical"},
}


def source_range(text, span):
    """Exact coordinates in decoded original, without accepting out-of-range slices."""
    # HTMLParser increments line numbers only at LF. Bare CR, form feed and
    # Unicode line separators are columns, not new source lines.
    lines = text.split("\n")
    offsets = []
    for position in (span.start, span.end_exclusive):
        if position.line > len(lines):
            raise ValueError("Citation line outside original")
        line = lines[position.line - 1]
        if position.column > len(line):
            raise ValueError("Citation column outside original")
        offsets.append(sum(len(line) + 1 for line in lines[: position.line - 1]) + position.column)
    if offsets[0] >= offsets[1]:
        raise ValueError("Empty or reversed citation")
    return text[offsets[0] : offsets[1]]


def fact_signature(fact):
    """IDs and asserted trust are not evidence; compare the reproduced factual payload."""
    data = fact.model_dump(mode="json")
    data.pop("id")
    data.pop("traceability")
    return digest(data)


class LocalResearch:
    """Stage I receives no primary conclusions or commercial candidates."""

    def __init__(self, root, *, registry, sector, as_of, max_input_bytes):
        self.root = root.resolve()
        self.packet = load_packet(self.root, max_input_bytes)
        self.registry_error = False
        try:
            self.primary = audit_saved_dossier(
                self.root,
                registry_path=registry,
                sector=sector,
                as_of=as_of,
                max_input_bytes=max_input_bytes,
            )
        except (OSError, ValueError):
            if registry is None:
                raise
            self.registry_error = True
            self.primary = audit_saved_dossier(
                self.root,
                sector=sector,
                as_of=as_of,
                max_input_bytes=max_input_bytes,
            )
        self.assessment = qualify_primary(self.primary)
        self.evidence = {e.id: e for e in self.packet.data.evidence}
        self.pages = {p.evidence_id: p for p in self.packet.data.pages}
        self.documents = {d.evidence_id: d for d in self.packet.data.documents}
        self.markup = {}
        self.originals = {}
        for eid, evidence in self.evidence.items():
            artifact = next((a for a in evidence.artifacts if a.kind in {"html", "pdf"}), None)
            if artifact:
                raw = read_local(self.root / artifact.path, 20_000_000)
                if hashlib.sha256(raw).hexdigest() != artifact.sha256:
                    raise ValueError("Original changed during Stage I")
                self.originals[eid] = (artifact, raw)
                if artifact.kind == "html":
                    parser = MechanismMarkup(str(evidence.source), recover=True)
                    parser.decoding_loss = False
                    try:
                        raw.decode("utf-8")
                    except UnicodeError:
                        parser.decoding_loss = True
                    parser.feed(raw.decode("utf-8", errors="replace"))
                    parser.close()
                    self.markup[eid] = parser

    def materials(self):
        rows = []
        for material in self.primary.materials:
            eid, reading = material.evidence_id, material.reading
            if eid in self.markup:
                parser = self.markup[eid]
                spans = parser.inspection().text_spans
                skipped = []
                raw_text = self.originals[eid][1].decode("utf-8", errors="replace")
                end = parser.position(parser.token_end((1, 0), raw_text))
                boundary = spans[-1].end_exclusive if spans else parser.position((1, 0))
                if (boundary.line, boundary.column) < (end.line, end.column):
                    skipped.append(
                        {
                            "start": boundary.model_dump(),
                            "end_exclusive": end.model_dump(),
                            "reason": "Uninspected suffix after static parsing boundary",
                        }
                    )
                for left, right in zip(spans, spans[1:], strict=False):
                    if left.end_exclusive != right.start:
                        skipped.append(
                            {
                                "start": left.end_exclusive.model_dump(),
                                "end_exclusive": right.start.model_dump(),
                                "reason": "Recovery boundary; no reliable textual observation",
                            }
                        )
                rows.append(
                    MaterialCheck(
                        evidence_id=eid,
                        kind="html",
                        available_parts=len(spans),
                        known_total_parts=None if parser.limitations else len(spans),
                        read_parts=list(range(1, len(spans) + 1)),
                        inspected_ranges=[s.model_dump(mode="json") for s in spans],
                        skipped_ranges=skipped,
                        text_coverage_complete=not parser.limitations and not parser.decoding_loss,
                        limitations=[
                            *reading.limitations,
                            *parser.limitations,
                            "Static text/attributes only; no rendering or JavaScript",
                        ],
                    )
                )
            else:
                doc = self.documents.get(eid)
                read = sorted(doc.page_texts) if doc and reading.provenance == "reproduced" else []
                total = reading.page_count
                rows.append(
                    MaterialCheck(
                        evidence_id=eid,
                        kind="pdf" if eid in self.originals else "unsupported_or_missing",
                        available_parts=len(read),
                        known_total_parts=total,
                        read_parts=read,
                        skipped_parts=[n for n in range(1, (total or 0) + 1) if n not in read],
                        text_coverage_complete=reading.text_coverage_complete,
                        visual_review_required=True,
                        limitations=[
                            *reading.limitations,
                            "Only reproduced text examined; images/layout not read; no OCR",
                            *[
                                f"Page {p.page}: {p.extraction_state}; {p.limitations}"
                                for p in reading.pages
                                if p.extraction_state != "complete"
                            ],
                        ],
                    )
                )
        return rows

    def organization_contexts(self, finding):
        """Nearby context for the exact independently extracted name, not whole-page words."""
        organizations = {o.id: o for o in self.primary.organizations}
        rows = []
        for obj in finding.objects:
            if obj.type != "organization" or obj.source_id not in organizations:
                continue
            org = organizations[obj.source_id]
            for citation in org.identification_sources:
                if citation.evidence_id in self.markup:
                    parser = self.markup[citation.evidence_id]
                    texts = [
                        compact(r.parts)
                        for i, r in enumerate(parser.regions)
                        if r.end
                        and (
                            citation.static_span is None
                            or parser.region_span(i) == citation.static_span
                        )
                    ]
                    quote = " ".join(texts)
                else:
                    document = self.documents.get(citation.evidence_id)
                    quote = document.page_texts.get(citation.page, "") if document else ""
                for match in re.finditer(re.escape(org.name), quote):
                    nearby = quote[max(0, match.start() - 100) : match.end() + 100]
                    biography = bool(
                        re.search(
                            r"образован|обучен|диплом|квалификац|биограф|партн",
                            quote[max(0, match.start() - 100) : match.start()],
                            re.I,
                        )
                    )
                    row = {
                        "organization_id": org.id,
                        "evidence_id": citation.evidence_id,
                        "exact_mention": org.name,
                        "nearby_text": nearby,
                        "context": "biography_education_partner_candidate"
                        if biography
                        else "public_mention_role_unestablished",
                        "operator_confirmed": False,
                    }
                    if row not in rows:
                        rows.append(row)
        return rows

    def check(self, citation: Citation):
        reasons = []
        actual = None
        status = "insufficient_evidence"
        completeness = "unknown"
        try:
            evidence = self.evidence.get(citation.evidence_id)
            if not evidence or citation.url != evidence.source:
                raise ValueError("Citation source does not match saved evidence")
            if citation.evidence_id not in self.originals:
                raise ValueError("No original artifact to check")
            artifact, raw = self.originals[citation.evidence_id]
            actual = hashlib.sha256(raw).hexdigest()
            if (citation.artifact_path, citation.artifact_sha256) != (artifact.path, actual):
                status = "contradicted"
                raise ValueError("Citation artifact path/hash differs from actual original")
            if artifact.kind == "html":
                text = raw.decode("utf-8")
                parser = self.markup[citation.evidence_id]
                # Match only independently reproduced source ranges, never arbitrary markup.
                if citation.static_span:
                    region = source_range(text, citation.static_span)
                    trusted_spans = parser.inspection().text_spans
                    # Structural declarations (form/script) have their own exact ranges.
                    reproduced_cites = [
                        c
                        for f in self.assessment.legal_findings
                        for fact in f.facts
                        for c in fact.citations
                    ]
                    ranges = [s.model_dump() for s in trusted_spans]
                    ranges += [
                        c.static_span.model_dump()
                        for c in reproduced_cites
                        if c.evidence_id == citation.evidence_id and c.static_span
                    ]
                    if citation.static_span.model_dump() not in ranges:
                        raise ValueError("Source range not independently reproduced")
                    completeness = citation.static_span.status
                    if citation.quote:
                        # Entire trusted region is parsed before selecting its text. This
                        # preserves exclusion context (script/template/head) at its boundary.
                        texts = [
                            compact(r.parts)
                            for i, r in enumerate(parser.regions)
                            if r.end and parser.region_span(i) == citation.static_span
                        ]
                        if not any(citation.quote in t for t in texts):
                            status = "contradicted"
                            raise ValueError("Quoted text not present in cited readable region")
                    if not region:
                        raise ValueError("Empty original range")
                else:
                    page = self.pages[citation.evidence_id]
                    if not citation.quote or page.reading.provenance != "reproduced":
                        raise ValueError("Unbounded HTML assertion without reproducible quote")
                    if citation.quote not in (page.text or ""):
                        status = "contradicted"
                        raise ValueError("Quoted HTML text not reproduced")
                    completeness = "complete" if page.reading.text_coverage_complete else "partial"
            else:
                doc = self.documents[citation.evidence_id]
                if not citation.page or not citation.quote:
                    raise ValueError("PDF assertion requires explicit page and text quote")
                if citation.quote not in doc.page_texts.get(citation.page, ""):
                    raise ValueError("PDF page/quote not available in reproduced text")
                completeness = "complete" if doc.reading.text_coverage_complete else "partial"
            status = "verified_fact" if completeness == "complete" else "partially_verified"
            reasons.append("Original bytes/hash and cited available region independently checked")
            reasons.append(
                "Does not authenticate acquisition, operator, full document or lawfulness"
            )
        except (OSError, UnicodeError, ValueError) as exc:
            reasons.append(str(exc))
        return EvidenceCheck(
            citation=citation,
            status=status,
            actual_sha256=actual,
            completeness=completeness,
            reasons=reasons,
        )


def verify_saved_assessment(
    root: Path,
    primary: PrimaryAudit,
    assessment: InternalAssessment,
    *,
    registry_path=None,
    max_input_bytes=2_000_000,
):
    """Complete Stage I before inspecting imported conclusions in Stage II."""
    source_hash = digest(assessment.model_dump(mode="json"))
    gaps = []
    research = None
    try:
        research = LocalResearch(
            root,
            registry=registry_path,
            sector=primary.sector_hint,
            as_of=primary.assessed_at,
            max_input_bytes=max_input_bytes,
        )
    except (OSError, ValueError, RecursionError):
        gaps.append(
            "Independent source inspection failed: unsafe/missing/changed dossier or registry"
        )
    if research and research.registry_error:
        gaps.append(
            "Norm registry could not be safely verified; available evidence still inspected"
        )
    binding_ok = bool(
        research
        and primary.dossier_sha256 == research.primary.dossier_sha256
        and assessment.source_dossier_sha256 == research.primary.dossier_sha256
        and assessment.source_primary_sha256 == digest(primary.model_dump(mode="json"))
    )
    if not binding_ok:
        gaps.append("Imported report/dossier bindings not independently established")
    # Replay uses shared conservative parsers, not an independent legal opinion.
    signatures = (
        {
            fact_signature(f): f
            for finding in research.assessment.legal_findings
            for f in finding.facts
        }
        if research and binding_ok
        else {}
    )
    finding_rows = []
    reproduced_ids = set()
    for finding in assessment.legal_findings:
        checks = [research.check(c) for f in finding.facts for c in f.citations] if research else []
        reproduced = [f.id for f in finding.facts if fact_signature(f) in signatures]
        verified_ids = [
            f.id
            for f in finding.facts
            if f.id in reproduced
            and research
            and all(
                research.check(c).status in {"verified_fact", "partially_verified"}
                for c in f.citations
            )
        ]
        reproduced_ids.update(verified_ids)
        contradictions = [r for c in checks if c.status == "contradicted" for r in c.reasons]
        if len(reproduced) != len(finding.facts):
            gaps.append(
                f"{finding.id}: factual payload not fully reproduced; imported prose is untrusted"
            )
        if contradictions:
            status = "contradicted"
        elif not verified_ids:
            status = "insufficient_evidence"
        elif len(verified_ids) < len(finding.facts) or any(
            c.status != "verified_fact" for c in checks
        ):
            status = "partially_verified"
        else:
            status = "verified_fact"
        norms = (
            [
                n.model_dump(mode="json")
                for n in research.primary.normative_references
                if n.assessment.domain in DOMAINS[finding.category]
            ]
            if research
            else []
        )
        limitations = [
            "Fact status does not confirm legal violation or obligation",
            "Shared static extractor can repeat its own omissions; no independent legal opinion",
        ]
        contexts = research.organization_contexts(finding) if research else []
        for obj in finding.objects:
            if obj.type == "organization":
                limitations.append(
                    "Name mention is not verified operator identity; roles kept separate"
                )
                if any(
                    c["organization_id"] == obj.source_id
                    and c["context"] == "biography_education_partner_candidate"
                    for c in contexts
                ):
                    limitations.append(
                        "Biography/education/partner context candidate; "
                        "operator attribution unsupported"
                    )
        actions = sorted(
            set(
                [
                    *finding.additional_checks,
                    "Independently establish current normative text and applicability",
                    "Use available documents before requesting unavailable internal records",
                ]
            )
        )
        gaps.extend(f"{finding.id}: {a}" for a in actions)
        finding_rows.append(
            FindingCheck(
                source_finding=finding,
                technical_status=status,
                evidence_checks=checks,
                independently_reproduced_fact_ids=verified_ids,
                contradictions=sorted(set(contradictions)),
                norm_candidates=norms,
                remaining_actions=actions,
                limitations=limitations,
                organization_contexts=contexts,
            )
        )
    by_id = {f.source_finding.id: f for f in finding_rows}
    independent_work = (
        {w.work_type: w for w in research.assessment.commercial_work} if research else {}
    )
    work_rows = []
    for work in assessment.commercial_work:
        linked = [by_id[i] for i in work.legal_finding_ids]
        basis = "insufficient_evidence"
        if any(f.technical_status == "contradicted" for f in linked):
            basis = "contradicted"
        elif (
            work.work_type in independent_work
            and set(work.support_fact_ids) == set(independent_work[work.work_type].support_fact_ids)
            and set(work.legal_finding_ids)
            == set(independent_work[work.work_type].legal_finding_ids)
            and all(i in reproduced_ids for i in work.support_fact_ids)
        ):
            basis = (
                "verified_fact"
                if all(f.technical_status == "verified_fact" for f in linked)
                else "partially_verified"
            )
        actions = [
            "Read available current document in full before assessing replacement",
            "Confirm operator/process, current applicable obligation and actual unmet need",
            "Determine whether existing document suffices; no absence inferred from excerpt",
        ]
        gaps.extend(f"{work.id}: {a}" for a in actions)
        work_rows.append(
            WorkCheck(
                source_candidate=work,
                factual_basis=basis,
                preparation_recommendation="conditional_internal_review_only"
                if basis in {"verified_fact", "partially_verified"}
                else "blocked",
                remaining_actions=actions,
                reasons=[
                    "Paid necessity not inherited from primary candidate",
                    "No authenticated applicable norm, complete current document review "
                    "or legal approval",
                ],
            )
        )
    imported_signatures = {
        fact_signature(f) for finding in assessment.legal_findings for f in finding.facts
    }
    new_findings = (
        [
            f
            for f in research.assessment.legal_findings
            if any(fact_signature(fact) not in imported_signatures for fact in f.facts)
        ]
        if research
        else []
    )
    imported_observations = {digest(o.model_dump(mode="json")) for o in primary.observations}
    new_observations = []
    new_finding_checks = []
    if research:
        for finding in new_findings:
            checks = [research.check(c) for fact in finding.facts for c in fact.citations]
            status = "insufficient_evidence"
            if checks and all(c.status == "verified_fact" for c in checks):
                status = "verified_fact"
            elif any(c.status in {"verified_fact", "partially_verified"} for c in checks):
                status = "partially_verified"
            new_finding_checks.append(
                DiscoveredFinding(
                    finding=finding,
                    evidence_checks=checks,
                    technical_status=status,
                )
            )
        for observation in research.primary.observations:
            if digest(observation.model_dump(mode="json")) in imported_observations:
                continue
            checks = [research.check(c) for c in observation.citations]
            status = "insufficient_evidence"
            if checks and all(c.status == "verified_fact" for c in checks):
                status = "verified_fact"
            elif any(c.status in {"verified_fact", "partially_verified"} for c in checks):
                status = "partially_verified"
            new_observations.append(
                DiscoveryCheck(
                    observation=observation,
                    evidence_checks=checks,
                    technical_status=status,
                )
            )
    if research:
        # Detect changes during comparison; do not keep earlier positive technical results.
        end_packet = load_packet(root.resolve(), max_input_bytes)
        if end_packet.manifest_sha256 != research.packet.manifest_sha256:
            raise ValueError("Dossier changed during independent comparison")
    gaps = sorted(set(gaps))
    return IndependentVerification(
        source_assessment_sha256=source_hash,
        independent_research_sha256=digest(research.assessment.model_dump(mode="json"))
        if research
        else None,
        materials=research.materials() if research else [],
        findings=finding_rows,
        new_findings=new_findings,
        new_finding_checks=new_finding_checks,
        new_observations=new_observations,
        commercial_work=work_rows,
        gaps=gaps,
        technical_fact_count=len(reproduced_ids),
        contradiction_count=len(
            {
                digest(c.citation.model_dump(mode="json"))
                for f in finding_rows
                for c in f.evidence_checks
                if c.status == "contradicted"
            }
        ),
        gap_count=len(gaps),
    )


def export_verification(result, output: Path, source: Path):
    output = output.absolute()
    if output.resolve().is_relative_to(source.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("Output must be new and outside source dossier")
    output.mkdir(parents=True, exist_ok=False)
    payloads = {"independent-verification": result.model_dump(mode="json")}
    for name, rows in (
        ("verified-legal-findings", result.findings),
        ("verified-commercial-work", result.commercial_work),
    ):
        payloads[name] = {
            "rows": [r.model_dump(mode="json") for r in rows],
            "source_assessment_sha256": result.source_assessment_sha256,
        }
    payloads["verification-gaps"] = {
        "gaps": result.gaps,
        "new_findings": [f.model_dump(mode="json") for f in result.new_findings],
        "new_observations": [o.model_dump(mode="json") for o in result.new_observations],
        "new_finding_checks": [f.model_dump(mode="json") for f in result.new_finding_checks],
    }
    for name, payload in payloads.items():
        payload.update(
            audience="internal_only",
            notice=result.notice,
            production_outcome="HOLD",
            legal_status="not_confirmed",
            client_release_allowed=False,
            automatic_send_allowed=False,
        )
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
